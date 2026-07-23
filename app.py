import os, json, requests, traceback, base64, time
from datetime import datetime, timezone, timedelta
import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np

# Safely import MetaTrader5 (It will fail on Streamlit Cloud because it's Linux)
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

# ── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Der-AI | Professional Trading System",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Initialize Session State ──────────────────────────────────────────────────
if 'bot_running' not in st.session_state:
    st.session_state.bot_running = False
if 'last_analysis_time' not in st.session_state:
    st.session_state.last_analysis_time = None
if 'signal_history' not in st.session_state:
    st.session_state.signal_history = []
if 'next_check_time' not in st.session_state:
    st.session_state.next_check_time = None
if 'active_signals' not in st.session_state:
    st.session_state.active_signals = {}
if 'app_notifications' not in st.session_state:
    st.session_state.app_notifications = []
if 'rate_limit_hit' not in st.session_state:
    st.session_state.rate_limit_hit = False

# ── API Keys & Config ─────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")

# MT5 Config
MT5_ENABLED = st.sidebar.checkbox("Enable MT5 Auto-Execution", value=False)
if MT5_ENABLED:
    MT5_ACCOUNT = st.sidebar.text_input("MT5 Account Number", "")
    MT5_PASSWORD = st.sidebar.text_input("MT5 Password", type="password")
    MT5_SERVER = st.sidebar.text_input("MT5 Server", "")
    MT5_LOT_SIZE = st.sidebar.number_input("Lot Size per Trade", value=0.01, min_value=0.01, max_value=100.0, step=0.01)
    MT5_NUM_TRADES = st.sidebar.number_input("Number of Trades to Execute", value=1, min_value=1, max_value=10, step=1)

# Symbols to monitor (Reduced to save tokens)
SYMBOLS = ['XAUUSD', 'USOIL']

# ── YFinance Symbol Map ───────────────────────────────────────────────────────
YFINANCE_MAP = {
    'XAUUSD': 'GC=F', 'USOIL': 'CL=F',
}

# ── Helper: Add Notification ──────────────────────────────────────────────────
def add_notification(note_type: str, message: str):
    st.session_state.app_notifications.append({
        'time': datetime.now().strftime('%H:%M:%S'),
        'type': note_type,
        'message': message
    })
    if len(st.session_state.app_notifications) > 100:
        st.session_state.app_notifications = st.session_state.app_notifications[-100:]

# ── Telegram Functions ────────────────────────────────────────────────────────
def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=10)
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

# ── News API Integration ──────────────────────────────────────────────────────
def get_high_impact_news():
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        params = {"apifooter": "false"}
        res = requests.get(url, params=params, timeout=10)
        data = res.json()
        today = datetime.now().strftime('%Y-%m-%d')
        high_impact = []
        for event in data:
            if event.get('date') == today and event.get('impact') == '3':
                high_impact.append({
                    'time': event.get('time', ''),
                    'currency': event.get('country', ''),
                    'event': event.get('event', ''),
                    'impact': 'HIGH',
                    'forecast': event.get('forecast', ''),
                    'previous': event.get('previous', '')
                })
        return high_impact
    except Exception as e:
        print(f"News fetch error: {e}")
        return []

# ── Multi-Timeframe Data Fetching ─────────────────────────────────────────────
def fetch_mtf_data(symbol):
    ticker = YFINANCE_MAP.get(symbol, symbol)
    data = {}
    try:
        t = yf.Ticker(ticker)
        data['M10'] = t.history(period="1d", interval="10m")
        data['M15'] = t.history(period="2d", interval="15m")
        data['M30'] = t.history(period="5d", interval="30m")
        data['H1'] = t.history(period="30d", interval="1h")
        data['H4'] = t.history(period="90d", interval="1d")
        return data
    except Exception as e:
        print(f"Data fetch error for {symbol}: {e}")
        return None

# ── Advanced Intra-Candle Analysis ────────────────────────────────────────────
def analyze_candle_structure(df):
    if len(df) < 3:
        return []
    
    analysis = []
    for i in range(max(0, len(df)-10), len(df)):
        candle = df.iloc[i]
        body = abs(candle['Close'] - candle['Open'])
        total_range = candle['High'] - candle['Low']
        
        if total_range == 0:
            continue
            
        upper_wick = candle['High'] - max(candle['Open'], candle['Close'])
        lower_wick = min(candle['Open'], candle['Close']) - candle['Low']
        upper_wick_ratio = upper_wick / total_range
        lower_wick_ratio = lower_wick / total_range
        body_ratio = body / total_range
        
        candle_type = "BULLISH" if candle['Close'] > candle['Open'] else "BEARISH"
        pattern = "NORMAL"
        if body_ratio > 0.7:
            pattern = "STRONG_" + candle_type
        elif body_ratio < 0.3:
            pattern = "DOJI"
        elif upper_wick_ratio > 0.6:
            pattern = "REJECTION_HIGH"
        elif lower_wick_ratio > 0.6:
            pattern = "REJECTION_LOW"
        elif upper_wick_ratio > 0.4 and body_ratio < 0.4:
            pattern = "SHOOTING_STAR" if candle_type == "BEARISH" else "HANGING_MAN"
        elif lower_wick_ratio > 0.4 and body_ratio < 0.4:
            pattern = "HAMMER" if candle_type == "BULLISH" else "INVERTED_HAMMER"
        
        analysis.append({
            'time': df.index[i],
            'candle_type': candle_type,
            'pattern': pattern,
            'body_ratio': body_ratio,
            'upper_wick_ratio': upper_wick_ratio,
            'lower_wick_ratio': lower_wick_ratio,
            'price': candle['Close'],
            'volume': candle['Volume']
        })
    
    return analysis[-5:]

# ── Advanced SMC Detection ────────────────────────────────────────────────────
def detect_bos_choch(df):
    if len(df) < 10:
        return None, None
    
    highs = df['High'].rolling(window=5).max()
    lows = df['Low'].rolling(window=5).min()
    
    recent_high = df['High'].iloc[-1]
    prev_high = highs.iloc[-6] if len(highs) > 5 else df['High'].iloc[-6]
    recent_low = df['Low'].iloc[-1]
    prev_low = lows.iloc[-6] if len(lows) > 5 else df['Low'].iloc[-6]
    
    bos, choch = None, None
    
    if recent_high > prev_high * 1.001:
        bos = "BULLISH_BOS"
    elif recent_low < prev_low * 0.999:
        bos = "BEARISH_BOS"
    
    if bos == "BULLISH_BOS" and recent_low > prev_low:
        choch = "BULLISH_CHOCH"
    elif bos == "BEARISH_BOS" and recent_high < prev_high:
        choch = "BEARISH_CHOCH"
    
    return bos, choch

def find_swings(df, window=5):
    highs = df['High'].rolling(window * 2 + 1, center=True).max()
    lows = df['Low'].rolling(window * 2 + 1, center=True).min()
    swing_highs = df['High'][df['High'] == highs].tail(4).tolist()
    swing_lows = df['Low'][df['Low'] == lows].tail(4).tolist()
    return {
        "recent_swing_highs": [round(p, 5) for p in swing_highs],
        "recent_swing_lows": [round(p, 5) for p in swing_lows]
    }

def detect_order_blocks(df):
    if len(df) < 5:
        return []
    
    order_blocks = []
    for i in range(len(df)-3, len(df)):
        if i < 2:
            continue
        candle = df.iloc[i]
        prev_candle = df.iloc[i-1]
        
        if (candle['Close'] > candle['Open'] and 
            (candle['Close'] - candle['Open']) > (candle['High'] - candle['Low']) * 0.6 and
            prev_candle['Close'] < prev_candle['Open']):
            order_blocks.append({
                'type': 'BULLISH_OB',
                'price': candle['Low'],
                'time': df.index[i],
                'strength': 'STRONG' if (candle['Close'] - candle['Open']) > (candle['High'] - candle['Low']) * 0.8 else 'MODERATE'
            })
        
        if (candle['Close'] < candle['Open'] and
            (candle['Open'] - candle['Close']) > (candle['High'] - candle['Low']) * 0.6 and
            prev_candle['Close'] > prev_candle['Open']):
            order_blocks.append({
                'type': 'BEARISH_OB',
                'price': candle['High'],
                'time': df.index[i],
                'strength': 'STRONG' if (candle['Open'] - candle['Close']) > (candle['High'] - candle['Low']) * 0.8 else 'MODERATE'
            })
    
    return order_blocks[-3:]

def detect_fvg(df):
    if len(df) < 3:
        return []
    
    fvgs = []
    for i in range(len(df)-2, len(df)):
        if i < 2:
            continue
        
        curr = df.iloc[i]
        prev = df.iloc[i-1]
        prev2 = df.iloc[i-2]
        
        if prev['Low'] > prev2['High'] and curr['Low'] > prev['High']:
            fvgs.append({
                'type': 'BULLISH_FVG',
                'top': prev['Low'],
                'bottom': prev2['High']
            })
        
        if prev['High'] < prev2['Low'] and curr['High'] < prev['Low']:
            fvgs.append({
                'type': 'BEARISH_FVG',
                'top': prev2['Low'],
                'bottom': prev['High']
            })
    
    return fvgs[-2:]

def detect_liquidity_sweeps(df):
    if len(df) < 10:
        return []
    
    sweeps = []
    recent = df.tail(10)
    
    for i in range(1, len(recent)):
        candle = recent.iloc[i]
        prev = recent.iloc[i-1]
        
        if (candle['Low'] < prev['Low'] * 0.999 and 
            candle['Close'] > candle['Open'] and
            (candle['Close'] - candle['Low']) > (candle['High'] - candle['Low']) * 0.6):
            sweeps.append({
                'type': 'BULLISH_SWEEP',
                'price': candle['Low'],
                'time': recent.index[i],
                'strength': 'STRONG' if (candle['Close'] - candle['Low']) > (candle['High'] - candle['Low']) * 0.8 else 'MODERATE'
            })
        
        if (candle['High'] > prev['High'] * 1.001 and
            candle['Close'] < candle['Open'] and
            (candle['High'] - candle['Close']) > (candle['High'] - candle['Low']) * 0.6):
            sweeps.append({
                'type': 'BEARISH_SWEEP',
                'price': candle['High'],
                'time': recent.index[i],
                'strength': 'STRONG' if (candle['High'] - candle['Close']) > (candle['High'] - candle['Low']) * 0.8 else 'MODERATE'
            })
    
    return sweeps[-2:]

# ── Premium AI Analysis Prompt (FIXED MATH & VOLUME LOGIC) ────────────────────
PREMIUM_ANALYSIS_PROMPT = """You are an ELITE institutional trading AI. You MUST perform exhaustive, data-driven analysis. NO GUESSWORK. NO HALLUCINATIONS.

DATA PROVIDED:
{data_summary}
INTRA-CANDLE ANALYSIS:
{intra_candle_data}
NEWS CONTEXT:
{news_summary}
HIGH IMPACT NEWS (next 24h):
{high_impact_events}

═══════════════════════════════════════════════════════════════════════════════
MANDATORY ANALYSIS REQUIREMENTS:
═══════════════════════════════════════════════════════════════════════════════
1. **INTRA-CANDLE PRECISION (STRICT WICK & VOLUME LOGIC)**:
   - Upper wick on a BEARISH candle = Bearish rejection (VALID for SELL).
   - Lower wick on a BEARISH candle = Bullish absorption (INVALID for SELL).
   - Reverse logic for BULLISH candles.
   - VOLUME DIVERGENCE TRAP: A strong candle body (>70%) combined with DECREASING volume is an exhaustion trap. YOU MUST LOWER THE SCORE SIGNIFICANTLY AND REJECT THE SIGNAL.

2. **MULTI-TIMEFRAME CONFLUENCE**: H4/H1 for macro bias, M15 for structural zones. REQUIRE minimum 3 timeframes aligned.

3. **SMC ELEMENTS**: Identify BOS/CHoCH, liquidity sweeps, Order Blocks, and FVGs.

4. **RISK MANAGEMENT & TP CALCULATION (STRICT MATHEMATICAL RULES)**:
   - For a BUY signal: Entry MUST be strictly LESS than TP1 and TP2. Stop Loss (SL) MUST be strictly LESS than Entry.
   - For a SELL signal: Entry MUST be strictly GREATER than TP1 and TP2. Stop Loss (SL) MUST be strictly GREATER than Entry.
   - TP1 MUST target the nearest 'Recent Swing Highs' (for BUY) or 'Recent Swing Lows' (for SELL) explicitly provided in the data.
   - SL MUST be placed below the recent Swing Low (for BUY) or above the recent Swing High (for SELL).
   - DO NOT hallucinate price levels. Use the exact 'Swing Highs/Lows' provided.

═══════════════════════════════════════════════════════════════════════════════
SCORING CRITERIA (BE BRUTALLY HONEST):
═══════════════════════════════════════════════════════════════════════════════
**HIGH CONFIDENCE (Score 85-100)**: 4-5 timeframes aligned, Clear BOS + CHoCH, Valid wick rejection, Entry at STRONG OB/FVG, R:R ≥ 1:2, NO volume divergence traps.
**MEDIUM CONFIDENCE (Score 70-84)**: 3 timeframes aligned, BOS or CHoCH present, Moderate zone, R:R ≥ 1:2.
**LOW CONFIDENCE (Score <70)**: Choppy market, strong body with decreasing volume (exhaustion trap), contradictory wick/volume signals, poor R:R, or high-impact news imminent.

═══════════════════════════════════════════════════════════════════════════════
YOUR TASK:
═══════════════════════════════════════════════════════════════════════════════
1. Analyze intra-candle data FIRST. Reject immediately if volume divergence or contradictory wicks are present.
2. Determine H4/H1 bias. Find M15 zones. 
3. Calculate EXACT Entry, SL, TP1, TP2 based on the provided Swing Highs/Lows, ensuring strict mathematical validity (BUY: TP > Entry > SL).
4. Score brutally honestly. If score < 85 OR confidence is not HIGH, set signal to "WAIT" and explicitly state the missing factors in 'rejection_reason'.

OUTPUT JSON ONLY (NO MARKDOWN, NO TEXT OUTSIDE JSON):
{{
  "bias": "BULLISH|BEARISH|RANGING",
  "signal": "BUY|SELL|WAIT",
  "confluence_score": 0-100,
  "confidence": "HIGH|MEDIUM|LOW",
  "timeframes_aligned": ["H1", "M15"],
  "intra_candle_analysis": {{
    "recent_pattern": "description of last 2-3 candles",
    "wick_rejection": "upper/lower/none with ratios",
    "body_strength": "strong/moderate/weak with percentage",
    "volume_trend": "increasing/decreasing/neutral"
  }},
  "order_blocks": ["detailed description with price levels"],
  "fvg_zones": ["detailed description with price levels"],
  "liquidity_sweeps": ["detailed description with price levels"],
  "bos_choch": ["detailed description with price levels"],
  "entry_type": "MARKET|BUY_LIMIT|SELL_LIMIT|BUY_STOP|SELL_STOP",
  "entry": 0.00,
  "stop_loss": 0.00,
  "take_profit": [0.00, 0.00],
  "rr_ratio": 0.00,
  "reasoning": "Detailed explanation citing SPECIFIC intra-candle patterns, multi-TF confluence, and structural levels",
  "rejection_reason": "If signal is WAIT or score < 85, explicitly list the missing confluence factors, contradictory wick/volume logic, or invalid math",
  "news_impact": "Analysis if news approaching"
}}
"""

# ── AI Analysis Function (Groq Free API - Optimized) ──────────────────────────
def call_gpt(system_prompt: str, user_content: list, max_tokens: int = 2000) -> dict:
    api_key = st.secrets.get("GROQ_API_KEY", "")
    if not api_key:
        api_key = st.sidebar.text_input("🔑 Groq API Key (gsk_...)", type="password")
    
    if not api_key or not api_key.startswith("gsk_"):
        raise ValueError("Please add a valid GROQ_API_KEY to Streamlit Secrets.")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    
    payload = {
        "model": "llama-3.1-8b-instant",  
        "messages": [
            {"role": "system", "content": system_prompt + "\n\nCRITICAL: Output ONLY valid JSON. NO markdown code blocks. NO text outside JSON."},
            {"role": "user", "content": user_content}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    
    res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=60)
    res_data = res.json()
    
    if 'error' in res_data:
        err_msg = res_data['error'].get('message', '')
        if 'Rate limit reached' in err_msg or '429' in str(res_data.get('error', {}).get('code', '')):
            raise ValueError("RATE_LIMIT")
        raise ValueError(f"Groq API Error: {err_msg}")
    
    content = res_data['choices'][0]['message'].get('content')
    if not content:
        raise ValueError("AI returned no content.")
    
    content = content.strip()
    if content.startswith("```json"): content = content[7:]
    if content.endswith("```"): content = content[:-3]
    
    return json.loads(content.strip())

# ── Mathematical Validation Guardrail ─────────────────────────────────────────
def validate_signal_math(analysis):
    """Ensures the AI's TP and SL mathematically make sense for the signal direction."""
    signal = analysis.get('signal')
    entry = analysis.get('entry', 0)
    sl = analysis.get('stop_loss', 0)
    tp_list = analysis.get('take_profit', [])
    tp1 = tp_list[0] if len(tp_list) > 0 else 0
    
    if not entry or not sl or not tp1:
        return False, "Missing entry, SL, or TP values."
    
    if signal == "BUY":
        if tp1 <= entry:
            return False, f"Invalid Math: For BUY, TP1 ({tp1}) MUST be > Entry ({entry})."
        if sl >= entry:
            return False, f"Invalid Math: For BUY, SL ({sl}) MUST be < Entry ({entry})."
    elif signal == "SELL":
        if tp1 >= entry:
            return False, f"Invalid Math: For SELL, TP1 ({tp1}) MUST be < Entry ({entry})."
        if sl <= entry:
            return False, f"Invalid Math: For SELL, SL ({sl}) MUST be > Entry ({entry})."
            
    return True, "Valid"

# ── MT5 Execution Functions ───────────────────────────────────────────────────
def execute_mt5_trade(symbol, direction, entry, sl, tp, lot_size, num_trades=1):
    if not MT5_ENABLED:
        return {"error": "MT5 not enabled"}
    if not MT5_AVAILABLE:
        return {"error": "MT5 requires Windows. Use Windows VPS for auto-execution."}
    try:
        if not mt5.initialize():
            return {"error": "MT5 init failed"}
        if not mt5.login(login=int(MT5_ACCOUNT), password=MT5_PASSWORD, server=MT5_SERVER):
            return {"error": "MT5 login failed"}
        
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return {"error": f"Symbol {symbol} not found"}
        
        trade_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(symbol)
        
        success_count = 0
        errors = []
        
        for i in range(int(num_trades)):
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(lot_size),
                "type": trade_type,
                "price": tick.ask if direction == "BUY" else tick.bid,
                "sl": sl,
                "tp": tp,
                "deviation": 10,
                "magic": 234000,
                "comment": f"Der-AI {i+1}/{num_trades}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                success_count += 1
            else:
                errors.append(f"Trade {i+1} failed: {result.comment}")
        
        if success_count > 0:
            return {"success": True, "executed": success_count, "total": int(num_trades), "errors": errors}
        else:
            return {"error": "All trades failed. " + " | ".join(errors)}
            
    except Exception as e:
        return {"error": str(e)}

# ── Premium Signal Generation Engine ──────────────────────────────────────────
def analyze_symbol_premium(symbol):
    try:
        mtf_data = fetch_mtf_data(symbol)
        if not mtf_data:
            return None
        
        news = get_high_impact_news()
        analysis_summary, intra_candle_data = [], []
        
        for tf, df in mtf_data.items():
            if df.empty:
                continue
            
            is_key_tf = tf in ['H1', 'M15']
            
            bos, choch = detect_bos_choch(df)
            obs = detect_order_blocks(df) if is_key_tf else []
            fvgs = detect_fvg(df) if is_key_tf else []
            sweeps = detect_liquidity_sweeps(df) if is_key_tf else []
            swings = find_swings(df)
            
            if len(df) > 50:
                ema20 = df['Close'].ewm(span=20).mean().iloc[-1]
                rsi = 100 - (100 / (1 + df['Close'].diff().clip(lower=0).rolling(14).mean() / 
                      df['Close'].diff().clip(upper=0).abs().rolling(14).mean())).iloc[-1]
            else:
                ema20, rsi = 0, 50
            
            current_price = df['Close'].iloc[-1]
            
            if is_key_tf:
                ob_str = ', '.join([f"{ob['type']}@{ob['price']:.2f}" for ob in obs]) if obs else 'None'
                fvg_str = ', '.join([f"{fvg['type']} {fvg['bottom']:.2f}-{fvg['top']:.2f}" for fvg in fvgs]) if fvgs else 'None'
                sweep_str = ', '.join([f"{sweep['type']}@{sweep['price']:.2f}" for sweep in sweeps]) if sweeps else 'None'
                swing_highs_str = ', '.join([f"{h:.5f}" for h in swings['recent_swing_highs']]) if swings['recent_swing_highs'] else 'None'
                swing_lows_str = ', '.join([f"{l:.5f}" for l in swings['recent_swing_lows']]) if swings['recent_swing_lows'] else 'None'
                
                analysis_summary.append(f"{tf} (KEY): Price: {current_price:.5f} | EMA20: {ema20:.5f} | RSI: {rsi:.1f} | BOS: {bos} | OBs: {ob_str} | FVGs: {fvg_str} | Sweeps: {sweep_str} | Swing Highs: {swing_highs_str} | Swing Lows: {swing_lows_str}")
                
                candle_analysis = analyze_candle_structure(df)
                if candle_analysis:
                    recent = candle_analysis[-2:]
                    c_str = [f"{c['candle_type']} {c['pattern']} (Body:{c['body_ratio']*100:.0f}%)" for c in recent]
                    intra_candle_data.append(f"{tf} Candles: {' | '.join(c_str)}")
            else:
                analysis_summary.append(f"{tf}: Price: {current_price:.5f} | EMA20: {ema20:.5f} | RSI: {rsi:.1f} | BOS: {bos}")
        
        news_text = "\n".join([f"- {n['time']} {n['currency']}: {n['event']} (Impact: {n['impact']})" for n in news[:5]]) if news else "No high-impact news today"
        
        user_content = [{"type": "text", "text": PREMIUM_ANALYSIS_PROMPT.format(
            data_summary="\n".join(analysis_summary),
            intra_candle_data="\n".join(intra_candle_data),
            news_summary=news_text,
            high_impact_events=news_text
        )}]
        system_prompt = "You are an ELITE institutional trader. Output ONLY valid JSON with ZERO guesswork."
        
        analysis = call_gpt(system_prompt, user_content, max_tokens=2000)
        analysis['symbol'] = symbol
        analysis['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        analysis['analyzed_at'] = datetime.now()
        return analysis
    except Exception as e:
        print(f"Analysis error for {symbol}: {e}")
        return {"error": str(e)}

# ── Signal Formatter for Telegram ────────────────────────────────────────────
def format_signal_for_telegram(analysis):
    if 'error' in analysis:
        return f"❌ Error: {analysis['error']}"
    
    emoji = "🟢" if analysis.get('signal') == "BUY" else "🔴" if analysis.get('signal') == "SELL" else ""
    
    message = f"""
{emoji} <b>DER-AI PREMIUM SIGNAL</b> {emoji}

📊 <b>{analysis['symbol']}</b> - {analysis.get('signal', 'WAIT')}
⏰ {analysis.get('timestamp', 'N/A')}

🎯 <b>CONFIDENCE: {analysis.get('confidence', 'N/A')}</b>
📈 <b>SCORE: {analysis.get('confluence_score', 0)}/100</b>
⚖️ <b>R:R: 1:{analysis.get('rr_ratio', 0):.1f}</b>

💰 <b>ENTRY:</b> {analysis.get('entry', 'N/A')}
🛑 <b>STOP LOSS:</b> {analysis.get('stop_loss', 'N/A')}
🎯 <b>TP1:</b> {analysis.get('take_profit', ['N/A'])[0] if analysis.get('take_profit') else 'N/A'}
🎯 <b>TP2:</b> {analysis.get('take_profit', ['N/A', 'N/A'])[1] if len(analysis.get('take_profit', [])) > 1 else 'N/A'}

🔍 <b>CONFLUENCE:</b>
• Timeframes: {', '.join(analysis.get('timeframes_aligned', []))}
• Order Blocks: {len(analysis.get('order_blocks', []))} | FVGs: {len(analysis.get('fvg_zones', []))} | Sweeps: {len(analysis.get('liquidity_sweeps', []))}

🧠 <b>ANALYSIS:</b>
{analysis.get('reasoning', 'N/A')}

{f"📰 <b>NEWS:</b>\n{analysis.get('news_impact', 'N/A')}" if analysis.get('news_impact') else ""}
<i>Der-AI Professional Trading System</i>
    """
    return message.strip()

# ── Main App UI ───────────────────────────────────────────────────────────────
st.title("🎯 Der-AI | Professional Multi-Timeframe Trading System")
st.markdown("**Elite ICT/SMC Analysis with Intra-Candle Precision | Telegram Alerts | MT5 Execution**")

# Sidebar Configuration
st.sidebar.header("⚙️ System Configuration")
selected_symbols = st.sidebar.multiselect("Monitor Symbols", SYMBOLS, default=['XAUUSD', 'USOIL'])
check_interval = st.sidebar.slider("Analysis Interval (minutes)", min_value=5, max_value=60, value=30)

# NEW: Sensitivity Slider
st.sidebar.markdown("---")
st.sidebar.subheader("🎯 Signal Sensitivity")
sensitivity = st.sidebar.select_slider(
    "Minimum Confluence Score",
    options=[80, 85, 90],
    value=85,
    help="Higher scores mean fewer, but higher-quality signals."
)

# Bot Control Buttons
col1, col2, col3 = st.sidebar.columns(3)
with col1:
    if st.button("▶️ START", type="primary", use_container_width=True):
        st.session_state.bot_running = True
        st.session_state.next_check_time = datetime.now()
        st.session_state.rate_limit_hit = False
        add_notification('info', "✅ Bot started. Monitoring markets.")
        st.rerun()
with col2:
    if st.button("⏹️ STOP", type="secondary", use_container_width=True):
        st.session_state.bot_running = False
        add_notification('warning', "⏸️ Bot stopped by user.")
        st.rerun()
with col3:
    if st.button("🗑️ CLEAR", use_container_width=True):
        st.session_state.active_signals = {}
        st.session_state.signal_history = []
        st.session_state.app_notifications = []
        st.session_state.bot_running = False
        st.session_state.last_analysis_time = None
        st.session_state.next_check_time = None
        st.session_state.rate_limit_hit = False
        add_notification('info', "🗑️ System memory cleared.")
        st.rerun()

# Bot Status Display
if st.session_state.bot_running:
    st.sidebar.success("✅ **BOT RUNNING**")
    if st.session_state.next_check_time:
        time_left = (st.session_state.next_check_time - datetime.now()).total_seconds()
        if time_left > 0:
            st.sidebar.info(f"⏱️ Next check in: **{int(time_left // 60)}m {int(time_left % 60)}s**")
        else:
            st.sidebar.info("⏱️ Checking now...")
else:
    st.sidebar.warning("⏸️ **BOT STOPPED**")

st.sidebar.markdown("---")
st.sidebar.subheader("📊 Session Stats")
st.sidebar.metric("Premium Signals", len(st.session_state.signal_history))
st.sidebar.metric("Notifications", len(st.session_state.app_notifications))

# Main Tabs
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "🔴 Live Monitoring", 
    "📜 Signal History", 
    "🔔 Notifications", 
    "📰 News Calendar", 
    "⚙️ Settings"
])

with tab1:
    st.header("🔴 Live Multi-Timeframe Analysis")
    if st.session_state.bot_running:
        st.markdown("<div style='background-color: #28a745; color: white; padding: 10px; border-radius: 5px; text-align: center;'><h3>🟢 SYSTEM ACTIVE - Monitoring Markets</h3></div>", unsafe_allow_html=True)
    else:
        st.markdown("<div style='background-color: #dc3545; color: white; padding: 10px; border-radius: 5px; text-align: center;'><h3>⚪ SYSTEM INACTIVE - Click START to begin</h3></div>", unsafe_allow_html=True)
    
    def process_symbol_result(result, symbol, is_auto=False):
        # 1. Handle Rate Limits Gracefully with Early Exit
        if result and isinstance(result, dict) and 'error' in result and 'RATE_LIMIT' in str(result.get('error')):
            st.session_state.rate_limit_hit = True
            msg = f"⏳ **{symbol}**: Groq free tier daily token limit reached. Pausing all analysis until limit resets."
            if not is_auto: st.warning(msg)
            add_notification('warning', msg)
            return

        # 2. Handle other errors
        if result is None or (isinstance(result, dict) and 'error' in result):
            error_msg = "Data fetch failed or no data available." if result is None else result.get('error', 'Unknown error')
            st.error(f"❌ Error analyzing {symbol}: {error_msg}")
            return

        # 3. Process valid results
        min_score = sensitivity
        if result.get('confidence') == 'HIGH' and result.get('confluence_score', 0) >= min_score:
            
            # 4. PYTHON-SIDE MATHEMATICAL VALIDATION (The Ultimate Guardrail)
            is_valid_math, math_reason = validate_signal_math(result)
            if not is_valid_math:
                msg = f"⚪ **{symbol}**: Signal Rejected due to Invalid Math. AI Reason: {math_reason}"
                if not is_auto: st.info(msg)
                add_notification('warning', msg)
                return

            is_repeat = False
            current_time = datetime.now()
            
            if symbol in st.session_state.active_signals:
                last_sig = st.session_state.active_signals[symbol]
                time_diff_minutes = (current_time - last_sig['timestamp']).total_seconds() / 60
                entry_price = result.get('entry', 0)
                last_entry = last_sig['entry']
                
                if (last_sig['direction'] == result.get('signal') and 
                    entry_price > 0 and last_entry > 0 and
                    abs(last_entry - entry_price) / entry_price < 0.01 and 
                    time_diff_minutes < 15.0):
                    is_repeat = True
            
            if is_repeat:
                last_time = st.session_state.active_signals[symbol]['timestamp'].strftime('%H:%M')
                msg = f"⏸️ **{symbol}**: Setup already active since {last_time}. Waiting for execution or structural invalidation. (15-min cooldown)"
                if not is_auto: st.info(msg)
                add_notification('info', msg)
            else:
                sig_color = "🟢" if result.get('signal') == "BUY" else "🔴"
                if not is_auto: st.markdown(f"### {sig_color} **NEW SIGNAL:** {result['symbol']} - {result.get('signal')}")
                else: st.success(f"{sig_color} **NEW SIGNAL:** {result['symbol']} - {result.get('signal')} | Score: {result.get('confluence_score')}/100")
                
                if not is_auto:
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Bias", result.get('bias', 'N/A'))
                    col2.metric("Confidence", result.get('confidence', 'N/A'))
                    col3.metric("Score", f"{result.get('confluence_score', 0)}/100")
                    col4.metric("Timeframes", f"{len(result.get('timeframes_aligned', []))} Aligned")
                    st.info(f"**Entry:** {result.get('entry')} | **SL:** {result.get('stop_loss')} | **TP:** {result.get('take_profit')}")
                    st.write(f"**Reasoning:** {result.get('reasoning')}")
                
                st.session_state.active_signals[symbol] = {
                    'direction': result.get('signal'),
                    'entry': result.get('entry', 0),
                    'timestamp': current_time
                }
                
                result['analyzed_at'] = current_time
                st.session_state.signal_history.append(result)
                
                telegram_msg = format_signal_for_telegram(result)
                if send_telegram_message(telegram_msg):
                    if not is_auto: st.success("✅ Signal sent to Telegram")
                
                add_notification('success', f"✅ **{symbol}**: New {result.get('signal')} signal generated (Score: {result.get('confluence_score')}/100)")
                if not is_auto: st.markdown("---")
        else:
            ai_reason = result.get('rejection_reason', 'Insufficient confluence factors met or contradictory wick/volume logic.')
            msg = f"⚪ **{symbol}**: Signal Rejected. Score: {result.get('confluence_score', 0)}/100, Confidence: {result.get('confidence', 'N/A')}. AI Reason: {ai_reason}"
            if not is_auto: st.info(msg)
            add_notification('warning', msg)

    if st.button("🔍 Run Manual Analysis Now", type="secondary", disabled=st.session_state.bot_running):
        progress_bar = st.progress(0)
        st.session_state.rate_limit_hit = False
        
        for i, symbol in enumerate(selected_symbols):
            if st.session_state.get('rate_limit_hit', False):
                st.warning("⏳ Daily token limit reached. Stopping analysis cycle.")
                break
                
            with st.spinner(f"Analyzing {symbol}..."):
                result = analyze_symbol_premium(symbol)
                process_symbol_result(result, symbol, is_auto=False)
            progress_bar.progress((i + 1) / len(selected_symbols))
        progress_bar.empty()
        st.session_state.last_analysis_time = datetime.now()
        st.rerun()
    
    # Auto-run logic
    if st.session_state.bot_running:
        if st.session_state.next_check_time and datetime.now() >= st.session_state.next_check_time:
            st.info("🔄 Running scheduled analysis...")
            progress_bar = st.progress(0)
            
            st.session_state.rate_limit_hit = False 
            
            for i, symbol in enumerate(selected_symbols):
                if st.session_state.get('rate_limit_hit', False):
                    st.warning("⏳ Daily token limit reached. Stopping analysis cycle to save resources.")
                    break
                
                result = analyze_symbol_premium(symbol)
                process_symbol_result(result, symbol, is_auto=True)
                progress_bar.progress((i + 1) / len(selected_symbols))
            
            progress_bar.empty()
            st.session_state.last_analysis_time = datetime.now()
            st.session_state.next_check_time = datetime.now() + timedelta(minutes=check_interval)
            st.rerun()

with tab2:
    st.header("📜 Premium Signal History")
    if len(st.session_state.signal_history) == 0:
        st.info("📭 No high-quality signals generated yet.")
    else:
        premium_signals = [s for s in st.session_state.signal_history if s.get('confidence') == 'HIGH' and s.get('confluence_score', 0) >= 80]
        st.metric("Total Premium Signals Logged", len(premium_signals))
        
        for i, signal in enumerate(reversed(premium_signals)):
            with st.expander(f"{'🟢' if signal.get('signal') == 'BUY' else '🔴'} {signal['symbol']} - {signal.get('signal')} | Score: {signal.get('confluence_score')}/100 | {signal.get('timestamp', 'N/A')}", expanded=False):
                col1, col2, col3 = st.columns(3)
                col1.metric("Entry", signal.get('entry', 'N/A'))
                col2.metric("Stop Loss", signal.get('stop_loss', 'N/A'))
                col3.metric("Take Profit", signal.get('take_profit', ['N/A'])[0] if signal.get('take_profit') else 'N/A')
                
                st.write(f"**Bias:** {signal.get('bias')} | **Confidence:** {signal.get('confidence')}")
                st.write(f"**Timeframes:** {', '.join(signal.get('timeframes_aligned', []))}")
                st.write(f"**Reasoning:** {signal.get('reasoning')}")
                
                # Add MT5 execution button if enabled
                if MT5_ENABLED:
                    if st.button(f"⚡ Execute {MT5_NUM_TRADES} trade(s) on MT5", key=f"exec_{i}", use_container_width=True):
                        exec_result = execute_mt5_trade(
                            symbol=signal['symbol'],
                            direction=signal.get('signal'),
                            entry=signal.get('entry'),
                            sl=signal.get('stop_loss'),
                            tp=signal.get('take_profit', [None])[0] if signal.get('take_profit') else None,
                            lot_size=MT5_LOT_SIZE,
                            num_trades=MT5_NUM_TRADES
                        )
                        if exec_result.get('success'):
                            st.success(f"✅ {exec_result.get('executed')}/{exec_result.get('total')} trades executed on MT5!")
                            add_notification('success', f"✅ **{signal['symbol']}**: {exec_result.get('executed')}/{exec_result.get('total')} trades executed on MT5")
                        else:
                            st.error(f"❌ Execution failed: {exec_result.get('error', 'Unknown error')}")
                            add_notification('warning', f"❌ **{signal['symbol']}**: Execution failed - {exec_result.get('error', 'Unknown error')}")
                
                if 'intra_candle_analysis' in signal:
                    st.write("**Intra-Candle Analysis:**")
                    st.json(signal['intra_candle_analysis'])
                
                st.markdown("---")

with tab3:
    st.header("🔔 System Notifications & Rejected Signals")
    st.markdown("This tab logs all app activities, including exactly *why* signals were rejected or put on cooldown.")
    if not st.session_state.app_notifications:
        st.info("No notifications yet. Start the bot to see activity logs.")
    else:
        for note in reversed(st.session_state.app_notifications):
            if note['type'] == 'success':
                st.success(f"**[{note['time']}]** {note['message']}")
            elif note['type'] == 'warning':
                st.warning(f"**[{note['time']}]** {note['message']}")
            else:
                st.info(f"**[{note['time']}]** {note['message']}")

with tab4:
    st.header("📰 High-Impact News Calendar")
    if st.button("🔄 Refresh News"): st.rerun()
    news = get_high_impact_news()
    if news:
        for n in news:
            st.markdown(f"**{n['time']}** - {n['currency']}: {n['event']}")
            st.write(f"Impact: {n['impact']} | Forecast: {n['forecast']} | Previous: {n['previous']}")
            st.markdown("---")
    else:
        st.info("No high-impact news today")

with tab5:
    st.header("⚙️ System Settings")
    st.subheader("📱 Telegram Setup")
    st.markdown("1. Create a bot via @BotFather on Telegram\n2. Get your bot token\n3. Get your chat ID (use @userinfobot)\n4. Add to Streamlit Secrets: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`")
    st.subheader("🖥️ MT5 Auto-Execution")
    st.markdown("1. Check 'Enable MT5 Auto-Execution' in sidebar\n2. Enter your MT5 account details\n3. **Note:** MT5 requires Windows environment. For cloud deployment, use a Windows VPS.")
    st.subheader("🎯 Quality Filters")
    st.info(f"**Current Active Settings:**\n- Minimum Confidence: **HIGH**\n- Minimum Confluence Score: **{sensitivity}/100** (Adjustable via sidebar slider)\n- Minimum R:R Ratio: **1:2.5**\n- **Anti-Spam:** Blocks duplicate signals within 1% price range for 15 minutes.\n- **Math Validation:** Automatically rejects signals with illogical TP/SL placement.")

# Auto-refresh for bot
if st.session_state.bot_running and st.session_state.next_check_time:
    time.sleep(30)
    st.rerun()
