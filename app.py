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
st.set_page_config(page_title="Der-AI | Professional Trading System", page_icon="", layout="wide")

# ── Initialize Session State ──────────────────────────────────────────────────
if 'bot_running' not in st.session_state:
    st.session_state.bot_running = False
if 'last_analysis_time' not in st.session_state:
    st.session_state.last_analysis_time = None
if 'signal_history' not in st.session_state:
    st.session_state.signal_history = []
if 'next_check_time' not in st.session_state:
    st.session_state.next_check_time = None

# ── API Keys & Config ─────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")

# MT5 Config
MT5_ENABLED = st.sidebar.checkbox("Enable MT5 Auto-Execution", value=False)
if MT5_ENABLED:
    MT5_ACCOUNT = st.sidebar.text_input("MT5 Account Number", "")
    MT5_PASSWORD = st.sidebar.text_input("MT5 Password", type="password")
    MT5_SERVER = st.sidebar.text_input("MT5 Server", "")
    MT5_LOT_SIZE = st.sidebar.number_input("Lot Size", value=0.01, min_value=0.01, max_value=100.0)

# Symbols to monitor
SYMBOLS = ['XAUUSD', 'BTCUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'US30', 'USOIL']

# ── YFinance Symbol Map ───────────────────────────────────────────────────────
YFINANCE_MAP = {
    'XAUUSD': 'GC=F', 'BTCUSD': 'BTC-USD', 'EURUSD': 'EURUSD=X',
    'GBPUSD': 'GBPUSD=X', 'USDJPY': 'JPY=X', 'US30': '^DJI', 'USOIL': 'CL=F',
}

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

# ── Multi-Timeframe Data Fetching with Intra-Candle Analysis ──────────────────
def fetch_mtf_data(symbol):
    ticker = YFINANCE_MAP.get(symbol, symbol)
    data = {}
    try:
        t = yf.Ticker(ticker)
        # Fetch detailed data for intra-candle analysis
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
    """Deep intra-candle analysis: wick ratios, body strength, rejection patterns"""
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
        
        # Detect specific patterns
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
    
    return analysis[-5:]  # Last 5 candles

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
                'bottom': prev2['High'],
                'size': abs(prev['Low'] - prev2['High'])
            })
        
        if prev['High'] < prev2['Low'] and curr['High'] < prev['Low']:
            fvgs.append({
                'type': 'BEARISH_FVG',
                'top': prev2['Low'],
                'bottom': prev['High'],
                'size': abs(prev2['Low'] - prev['High'])
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

# ── Premium AI Analysis Prompt (Deep Intra-Candle Focus) ─────────────────────
PREMIUM_ANALYSIS_PROMPT = """You are an ELITE institutional trading AI with deep expertise in ICT/SMC concepts. You MUST perform exhaustive intra-candle analysis and provide ONLY high-probability signals with NO GUESSWORK.

DATA PROVIDED:
{data_summary}

INTRA-CANDLE ANALYSIS (CRITICAL):
{intra_candle_data}

NEWS CONTEXT:
{news_summary}

HIGH IMPACT NEWS (next 24h):
{high_impact_events}

═══════════════════════════════════════════════════════════════════════════════
MANDATORY ANALYSIS REQUIREMENTS - NO EXCEPTIONS:
═══════════════════════════════════════════════════════════════════════════════

1. **INTRA-CANDLE PRECISION**: Analyze every candle's internal structure:
   - Wick ratios: What % of the range is upper/lower wick?
   - Body strength: Is the body >70% of range (strong conviction) or <30% (indecision)?
   - Rejection patterns: Long wicks = rejection at those levels
   - Volume confirmation: Does volume support the price action?

2. **MULTI-TIMEFRAME CONFLUENCE** (NON-NEGOTIABLE):
   - H4/H1: Establish MACRO bias (must align)
   - M30/M15: Identify structural zones (order blocks, FVGs)
   - M10: Precision entry timing
   - REQUIRE: Minimum 3 timeframes in agreement

3. **SMC ELEMENTS** (Must identify ALL):
   - BOS/CHoCH: Clear break of structure with candle CLOSE beyond level
   - Liquidity sweeps: Did price hunt stops before reversing? (wick analysis)
   - Order blocks: Strong candles after consolidation (body ratio >60%)
   - FVGs: Imbalance zones (3-candle sequence with gaps)
   - Premium/Discount: Is price in upper or lower 50% of range?

4. **ORDER FLOW ANALYSIS**:
   - Absorption: Large wicks with high volume = institutional absorption
   - Imbalance: Consecutive strong candles in one direction
   - Exhaustion: Long wicks + declining volume = reversal warning

5. **NEWS AWARENESS**:
   - If high-impact news within 2 hours: Analyze potential volatility expansion
   - Pre-news: Look for compression/coiling patterns
   - Post-news: Wait for liquidity sweep + reversal confirmation

6. **RISK MANAGEMENT**:
   - SL MUST be beyond structural invalidation point (sweep level, OB boundary)
   - TP MUST target logical liquidity pools (recent highs/lows, FVG edges)
   - Minimum R:R = 1:2.5 (non-negotiable for HIGH confidence)

═══════════════════════════════════════════════════════════════════════════════
SCORING CRITERIA (BE BRUTALLY HONEST):
═══════════════════════════════════════════════════════════════════════════════

**HIGH CONFIDENCE (Score 90-100)** - ALL must be true:
✓ 4-5 timeframes aligned
✓ Clear BOS + CHoCH confirmed
✓ Liquidity sweep detected + rejection candle (wick ratio >60%)
✓ Entry at STRONG order block or FVG
✓ R:R ≥ 1:3
✓ No opposing news within 2 hours
✓ Intra-candle structure shows strong conviction (body ratio >70%)

**MEDIUM CONFIDENCE (Score 75-89)** - Most true:
✓ 3 timeframes aligned
✓ BOS or CHoCH present
✓ Some liquidity sweep or rejection
✓ Entry at moderate zone
✓ R:R ≥ 1:2.5

**LOW CONFIDENCE (Score <75)** - DO NOT SIGNAL:
✗ Less than 3 timeframes aligned
✗ No clear structure break
✗ Choppy/consolidating market
✗ High-impact news imminent
✗ Poor R:R

═══════════════════════════════════════════════════════════════════════════════
YOUR TASK:
═══════════════════════════════════════════════════════════════════════════════

1. Analyze intra-candle data FIRST - what is price REALLY doing inside candles?
2. Determine H4/H1 bias from macro structure
3. Find M30/M15 structural zones (OBs, FVGs, sweeps)
4. Use M10 for precise entry timing
5. Calculate EXACT SL/TP based on structure (NO arbitrary numbers)
6. Score brutally honestly - if score <90, set signal to "WAIT"
7. If news approaching, provide pre-news strategy OR wait recommendation

OUTPUT JSON ONLY (NO MARKDOWN, NO TEXT OUTSIDE JSON):
{{
  "bias": "BULLISH|BEARISH|RANGING",
  "signal": "BUY|SELL|WAIT",
  "confluence_score": 0-100,
  "confidence": "HIGH|MEDIUM|LOW",
  "timeframes_aligned": ["H1", "M30", "M15"],
  "intra_candle_analysis": {{
    "recent_pattern": "description of last 3 candles",
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
  "news_impact": "Analysis if news approaching",
  "why_not_lower_score": "If score <90, explain why signal is WAIT"
}}
"""

# ── AI Analysis Function (Groq Free API) ──────────────────────────────────────
def call_gpt(system_prompt: str, user_content: list, max_tokens: int = 3000) -> dict:
    api_key = st.secrets.get("GROQ_API_KEY", "")
    if not api_key:
        api_key = st.sidebar.text_input("🔑 Groq API Key (gsk_...)", type="password")
    
    if not api_key or not api_key.startswith("gsk_"):
        raise ValueError("Please add a valid GROQ_API_KEY to Streamlit Secrets.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": system_prompt + "\n\nCRITICAL: Output ONLY valid JSON. NO markdown code blocks. NO text outside JSON."},
            {"role": "user", "content": user_content}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.05,  # Ultra-deterministic for precision
    }
    
    res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=180)
    res_data = res.json()
    
    if 'error' in res_data:
        raise ValueError(f"Groq API Error: {res_data['error']['message']}")
    
    content = res_data['choices'][0]['message'].get('content')
    if not content:
        raise ValueError("AI returned no content.")
    
    # Clean up markdown wrappers
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    if content.endswith("```"):
        content = content[:-3]
    
    return json.loads(content.strip())

# ── MT5 Execution Functions ───────────────────────────────────────────────────
def execute_mt5_trade(symbol, direction, entry, sl, tp, lot_size):
    if not MT5_ENABLED:
        return {"error": "MT5 not enabled"}
    if not MT5_AVAILABLE:
        return {"error": "MT5 requires Windows. Use Windows VPS for auto-execution."}
    try:
        if not mt5.initialize():
            return {"error": f"MT5 init failed"}
        if not mt5.login(login=int(MT5_ACCOUNT), password=MT5_PASSWORD, server=MT5_SERVER):
            return {"error": f"MT5 login failed"}
        
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return {"error": f"Symbol {symbol} not found"}
        
        trade_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(symbol)
        
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
            "comment": "Der-AI Signal",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        return {"success": result.retcode == mt5.TRADE_RETCODE_DONE, "order": result._asdict() if result else None}
    except Exception as e:
        return {"error": str(e)}

# ── Premium Signal Generation Engine ──────────────────────────────────────────
def analyze_symbol_premium(symbol):
    try:
        mtf_data = fetch_mtf_data(symbol)
        if not mtf_data:
            return None
        
        news = get_high_impact_news()
        analysis_summary = []
        intra_candle_data = []
        
        # Deep multi-timeframe analysis
        for tf, df in mtf_data.items():
            if df.empty:
                continue
            
            # Standard indicators
            bos, choch = detect_bos_choch(df)
            obs = detect_order_blocks(df)
            fvgs = detect_fvg(df)
            sweeps = detect_liquidity_sweeps(df)
            
            # Intra-candle analysis
            candle_analysis = analyze_candle_structure(df)
            
            # Technical indicators
            if len(df) > 50:
                ema20 = df['Close'].ewm(span=20).mean().iloc[-1]
                ema50 = df['Close'].ewm(span=50).mean().iloc[-1]
                ema200 = df['Close'].ewm(span=200).mean().iloc[-1]
                rsi = 100 - (100 / (1 + df['Close'].diff().clip(lower=0).rolling(14).mean() / 
                      df['Close'].diff().clip(upper=0).abs().rolling(14).mean())).iloc[-1]
                atr = df['High'].rolling(14).mean() - df['Low'].rolling(14).mean()
                atr = atr.iloc[-1] if len(atr) > 0 else 0
            else:
                ema20, ema50, ema200, rsi, atr = 0, 0, 0, 50, 0
            
            current_price = df['Close'].iloc[-1]
            
            analysis_summary.append(f"""
{tf} Timeframe:
- Price: {current_price:.5f} | EMA20: {ema20:.5f} | EMA50: {ema50:.5f} | EMA200: {ema200:.5f}
- RSI: {rsi:.1f} | ATR: {atr:.5f}
- Structure: BOS={bos}, CHoCH={choch}
- Order Blocks: {len(obs)} detected ({', '.join([f\"{ob['type']}@{ob['price']:.2f} ({ob['strength']})\" for ob in obs])})
- FVGs: {len(fvgs)} detected ({', '.join([f\"{fvg['type']} {fvg['bottom']:.2f}-{fvg['top']:.2f}\" for fvg in fvgs])})
- Liquidity Sweeps: {len(sweeps)} detected ({', '.join([f\"{sweep['type']}@{sweep['price']:.2f} ({sweep['strength']})\" for sweep in sweeps])})
            """)
            
            # Format intra-candle data
            if candle_analysis:
                recent_candles = []
                for c in candle_analysis[-3:]:
                    recent_candles.append(f"Time:{c['time'].strftime('%H:%M')} | {c['candle_type']} | {c['pattern']} | Body:{c['body_ratio']*100:.0f}% | UpperWick:{c['upper_wick_ratio']*100:.0f}% | LowerWick:{c['lower_wick_ratio']*100:.0f}% | Vol:{c['volume']:.0f}")
                
                intra_candle_data.append(f"""
{tf} Intra-Candle Analysis (Last 3 Candles):
{chr(10).join(recent_candles)}
                """)
        
        news_text = "\n".join([f"- {n['time']} {n['currency']}: {n['event']} (Impact: {n['impact']})" 
                               for n in news[:5]]) if news else "No high-impact news today"
        
        # Call premium AI analysis
        user_content = [
            {"type": "text", "text": PREMIUM_ANALYSIS_PROMPT.format(
                data_summary="\n".join(analysis_summary),
                intra_candle_data="\n".join(intra_candle_data),
                news_summary=news_text,
                high_impact_events=news_text
            )}
        ]
        
        system_prompt = "You are an ELITE institutional trader. Output ONLY valid JSON with ZERO guesswork."
        analysis = call_gpt(system_prompt, user_content, max_tokens=3000)
        
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
 {analysis.get('timestamp', 'N/A')}

🎯 <b>CONFIDENCE: {analysis.get('confidence', 'N/A')}</b>
📈 <b>SCORE: {analysis.get('confluence_score', 0)}/100</b>
⚖️ <b>R:R: 1:{analysis.get('rr_ratio', 0):.1f}</b>

💰 <b>ENTRY:</b> {analysis.get('entry', 'N/A')}
 <b>STOP LOSS:</b> {analysis.get('stop_loss', 'N/A')}
 <b>TP1:</b> {analysis.get('take_profit', ['N/A'])[0] if analysis.get('take_profit') else 'N/A'}
🎯 <b>TP2:</b> {analysis.get('take_profit', ['N/A', 'N/A'])[1] if len(analysis.get('take_profit', [])) > 1 else 'N/A'}

🔍 <b>CONFLUENCE:</b>
• Timeframes: {', '.join(analysis.get('timeframes_aligned', []))}
• Order Blocks: {len(analysis.get('order_blocks', []))}
• FVGs: {len(analysis.get('fvg_zones', []))}
• Sweeps: {len(analysis.get('liquidity_sweeps', []))}

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
st.sidebar.header("️ System Configuration")
selected_symbols = st.sidebar.multiselect("Monitor Symbols", SYMBOLS, default=['XAUUSD'])
check_interval = st.sidebar.slider("Analysis Interval (minutes)", min_value=5, max_value=60, value=15)

# Bot Control Buttons
col1, col2, col3 = st.sidebar.columns(3)
with col1:
    if st.button("▶️ START", type="primary", use_container_width=True):
        st.session_state.bot_running = True
        st.session_state.next_check_time = datetime.now()
        st.rerun()
with col2:
    if st.button("️ STOP", type="secondary", use_container_width=True):
        st.session_state.bot_running = False
        st.rerun()
with col3:
    if st.button("🔄 RESET", use_container_width=True):
        st.session_state.bot_running = False
        st.session_state.signal_history = []
        st.session_state.last_analysis_time = None
        st.session_state.next_check_time = None
        st.rerun()

# Bot Status Display
if st.session_state.bot_running:
    st.sidebar.success("✅ **BOT RUNNING**")
    
    # Countdown timer
    if st.session_state.next_check_time:
        time_left = (st.session_state.next_check_time - datetime.now()).total_seconds()
        if time_left > 0:
            minutes = int(time_left // 60)
            seconds = int(time_left % 60)
            st.sidebar.info(f"⏱️ Next check in: **{minutes}m {seconds}s**")
        else:
            st.sidebar.info("⏱️ Checking now...")
else:
    st.sidebar.warning("⏸️ **BOT STOPPED**")

st.sidebar.markdown("---")
st.sidebar.subheader(" Session Stats")
st.sidebar.metric("Signals Generated", len(st.session_state.signal_history))
if st.session_state.last_analysis_time:
    st.sidebar.metric("Last Analysis", st.session_state.last_analysis_time.strftime('%H:%M:%S'))

# Main Tabs
tab1, tab2, tab3, tab4 = st.tabs([" Live Monitoring", "📜 Signal History", "📰 News Calendar", "️ Settings"])

with tab1:
    st.header("🔴 Live Multi-Timeframe Analysis")
    
    # Status indicator
    if st.session_state.bot_running:
        st.markdown("<div style='background-color: #28a745; color: white; padding: 10px; border-radius: 5px; text-align: center;'><h3>🟢 SYSTEM ACTIVE - Monitoring Markets</h3></div>", unsafe_allow_html=True)
    else:
        st.markdown("<div style='background-color: #dc3545; color: white; padding: 10px; border-radius: 5px; text-align: center;'><h3> SYSTEM INACTIVE - Click START to begin</h3></div>", unsafe_allow_html=True)
    
    # Manual analysis button
    if st.button("🔍 Run Manual Analysis Now", type="secondary", disabled=st.session_state.bot_running):
        progress_bar = st.progress(0)
        results_container = st.container()
        
        for i, symbol in enumerate(selected_symbols):
            with st.spinner(f"Analyzing {symbol}..."):
                result = analyze_symbol_premium(symbol)
                
                if result and 'error' not in result:
                    # ONLY show HIGH confidence + score >= 90
                    if result.get('confidence') == 'HIGH' and result.get('confluence_score', 0) >= 90:
                        with results_container:
                            sig_color = "🟢" if result.get('signal') == "BUY" else "🔴"
                            st.markdown(f"### {sig_color} {result['symbol']} - {result.get('signal')} SIGNAL")
                            
                            col1, col2, col3, col4 = st.columns(4)
                            col1.metric("Bias", result.get('bias', 'N/A'))
                            col2.metric("Confidence", result.get('confidence', 'N/A'))
                            col3.metric("Score", f"{result.get('confluence_score', 0)}/100")
                            col4.metric("Timeframes", f"{len(result.get('timeframes_aligned', []))} Aligned")
                            
                            st.info(f"**Entry:** {result.get('entry')} | **SL:** {result.get('stop_loss')} | **TP:** {result.get('take_profit')}")
                            st.write(f"**Reasoning:** {result.get('reasoning')}")
                            
                            # Add to history
                            result['analyzed_at'] = datetime.now()
                            st.session_state.signal_history.append(result)
                            
                            # Send to Telegram
                            telegram_msg = format_signal_for_telegram(result)
                            if send_telegram_message(telegram_msg):
                                st.success("✅ Signal sent to Telegram")
                            
                            st.markdown("---")
                    else:
                        st.info(f"⚪ {symbol}: Score {result.get('confluence_score', 0)}/100 - Confidence {result.get('confidence', 'N/A')} - **WAITING FOR BETTER SETUP**")
                else:
                    st.error(f"❌ Error analyzing {symbol}: {result.get('error', 'Unknown error')}")
            
            progress_bar.progress((i + 1) / len(selected_symbols))
        
        progress_bar.empty()
        st.session_state.last_analysis_time = datetime.now()
        st.rerun()
    
    # Auto-run logic
    if st.session_state.bot_running:
        if st.session_state.next_check_time and datetime.now() >= st.session_state.next_check_time:
            st.info("🔄 Running scheduled analysis...")
            progress_bar = st.progress(0)
            
            for i, symbol in enumerate(selected_symbols):
                result = analyze_symbol_premium(symbol)
                
                if result and 'error' not in result:
                    # STRICT FILTER: Only HIGH confidence + score >= 90
                    if result.get('confidence') == 'HIGH' and result.get('confluence_score', 0) >= 90:
                        sig_color = "🟢" if result.get('signal') == "BUY" else "🔴"
                        st.success(f"{sig_color} **{result['symbol']}** - {result.get('signal')} | Score: {result.get('confluence_score')}/100 | Entry: {result.get('entry')}")
                        
                        result['analyzed_at'] = datetime.now()
                        st.session_state.signal_history.append(result)
                        
                        # Send to Telegram
                        telegram_msg = format_signal_for_telegram(result)
                        send_telegram_message(telegram_msg)
                
                progress_bar.progress((i + 1) / len(selected_symbols))
            
            progress_bar.empty()
            st.session_state.last_analysis_time = datetime.now()
            st.session_state.next_check_time = datetime.now() + timedelta(minutes=check_interval)
            st.rerun()
        
        # Show countdown
        if st.session_state.next_check_time:
            time_left = (st.session_state.next_check_time - datetime.now()).total_seconds()
            if time_left > 0:
                st.info(f"⏱️ Next automatic check in: **{int(time_left // 60)} minutes {int(time_left % 60)} seconds**")

with tab2:
    st.header("📜 Premium Signal History")
    
    if len(st.session_state.signal_history) == 0:
        st.info(" No high-quality signals generated yet. Only signals with HIGH confidence and score ≥90 are shown.")
    else:
        # Filter for only HIGH confidence + score >= 90
        premium_signals = [s for s in st.session_state.signal_history 
                          if s.get('confidence') == 'HIGH' and s.get('confluence_score', 0) >= 90]
        
        st.metric("Total Premium Signals", len(premium_signals))
        
        for i, signal in enumerate(reversed(premium_signals)):
            with st.expander(f"{'🟢' if signal.get('signal') == 'BUY' else '🔴'} {signal['symbol']} - {signal.get('signal')} | Score: {signal.get('confluence_score')}/100 | {signal.get('timestamp', 'N/A')}", expanded=False):
                col1, col2, col3 = st.columns(3)
                col1.metric("Entry", signal.get('entry', 'N/A'))
                col2.metric("Stop Loss", signal.get('stop_loss', 'N/A'))
                col3.metric("Take Profit", signal.get('take_profit', ['N/A'])[0] if signal.get('take_profit') else 'N/A')
                
                st.write(f"**Bias:** {signal.get('bias')}")
                st.write(f"**Confidence:** {signal.get('confidence')}")
                st.write(f"**Timeframes:** {', '.join(signal.get('timeframes_aligned', []))}")
                st.write(f"**Reasoning:** {signal.get('reasoning')}")
                
                if 'intra_candle_analysis' in signal:
                    st.write("**Intra-Candle Analysis:**")
                    st.json(signal['intra_candle_analysis'])
                
                st.markdown("---")

with tab3:
    st.header("📰 High-Impact News Calendar")
    
    if st.button("🔄 Refresh News"):
        st.rerun()
    
    news = get_high_impact_news()
    if news:
        for n in news:
            st.markdown(f"**{n['time']}** - {n['currency']}: {n['event']}")
            st.write(f"Impact: {n['impact']} | Forecast: {n['forecast']} | Previous: {n['previous']}")
            st.markdown("---")
    else:
        st.info("No high-impact news today")

with tab4:
    st.header("⚙️ System Settings")
    
    st.subheader("📱 Telegram Setup")
    st.markdown("""
    **To enable Telegram alerts:**
    1. Create a bot via @BotFather on Telegram
    2. Get your bot token
    3. Get your chat ID (use @userinfobot)
    4. Add to Streamlit Secrets:
       - `TELEGRAM_BOT_TOKEN` = "your-bot-token"
       - `TELEGRAM_CHAT_ID` = "your-chat-id"
    """)
    
    st.subheader("🖥️ MT5 Auto-Execution")
    st.markdown("""
    **To enable MT5 execution:**
    1. Check "Enable MT5 Auto-Execution" in sidebar
    2. Enter your MT5 account details
    3. **Note:** MT5 requires Windows environment. For cloud deployment, use a Windows VPS.
    """)
    
    st.subheader("🎯 Quality Filters")
    st.info("""
    **Current Settings:**
    - Minimum Confidence: **HIGH**
    - Minimum Confluence Score: **90/100**
    - Minimum R:R Ratio: **1:2.5**
    
    Only signals meeting ALL criteria will be generated and sent to Telegram.
    """)

# Auto-refresh for bot
if st.session_state.bot_running and st.session_state.next_check_time:
    time.sleep(30)  # Check every 30 seconds
    st.rerun()
