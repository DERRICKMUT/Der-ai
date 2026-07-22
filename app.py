import os, json, requests, traceback, base64, time, schedule
from datetime import datetime, timezone, timedelta
import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
from threading import Thread

# Safely import MetaTrader5 (It will fail on Streamlit Cloud because it's Linux)
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

# ── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Der-AI | Automated Trading System", page_icon="🤖", layout="wide")

# ── API Keys & Config ─────────────────────────────────────────────────────────
# OpenAI key removed; Groq key is now handled securely inside call_gpt()
TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")

# MT5 Config (Optional - for auto-execution)
MT5_ENABLED = st.sidebar.checkbox("Enable MT5 Auto-Execution", value=False)
if MT5_ENABLED:
    MT5_ACCOUNT = st.sidebar.text_input("MT5 Account Number", "")
    MT5_PASSWORD = st.sidebar.text_input("MT5 Password", type="password")
    MT5_SERVER = st.sidebar.text_input("MT5 Server", "")
    MT5_LOT_SIZE = st.sidebar.number_input("Lot Size", value=0.01, min_value=0.01, max_value=100.0)

# Symbols to monitor
SYMBOLS = ['XAUUSD', 'BTCUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'US30', 'USOIL']
TIMEFRAMES = {'M10': 10, 'M15': 15, 'M30': 30, 'H1': 60, 'H4': 240}

# ── YFinance Symbol Map ───────────────────────────────────────────────────────
YFINANCE_MAP = {
    'XAUUSD': 'GC=F', 'BTCUSD': 'BTC-USD', 'EURUSD': 'EURUSD=X',
    'GBPUSD': 'GBPUSD=X', 'USDJPY': 'JPY=X', 'US30': '^DJI', 'USOIL': 'CL=F',
}

# ── Telegram Functions ────────────────────────────────────────────────────────
def send_telegram_message(message):
    """Send formatted message to Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=10)
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

# ── News API Integration (ForexFactory/TradingEconomics) ─────────────────────
def get_high_impact_news():
    """Fetch high-impact news for the day"""
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
    """Fetch live data for all timeframes"""
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

# ── Advanced Technical Analysis ───────────────────────────────────────────────
def detect_bos_choch(df):
    if len(df) < 10:
        return None, None
    highs = df['High'].rolling(window=5).max()
    lows = df['Low'].rolling(window=5).min()
    recent_high, prev_high = df['High'].iloc[-1], highs.iloc[-6] if len(highs) > 5 else df['High'].iloc[-6]
    recent_low, prev_low = df['Low'].iloc[-1], lows.iloc[-6] if len(lows) > 5 else df['Low'].iloc[-6]
    bos, choch = None, None
    if recent_high > prev_high * 1.001: bos = "BULLISH_BOS"
    elif recent_low < prev_low * 0.999: bos = "BEARISH_BOS"
    if bos == "BULLISH_BOS" and recent_low > prev_low: choch = "BULLISH_CHOCH"
    elif bos == "BEARISH_BOS" and recent_high < prev_high: choch = "BEARISH_CHOCH"
    return bos, choch

def detect_order_blocks(df):
    if len(df) < 5: return []
    order_blocks = []
    for i in range(len(df)-3, len(df)):
        if i < 2: continue
        candle, prev_candle = df.iloc[i], df.iloc[i-1]
        if (candle['Close'] > candle['Open'] and (candle['Close'] - candle['Open']) > (candle['High'] - candle['Low']) * 0.6 and prev_candle['Close'] < prev_candle['Open']):
            order_blocks.append({'type': 'BULLISH_OB', 'price': candle['Low'], 'time': df.index[i]})
        if (candle['Close'] < candle['Open'] and (candle['Open'] - candle['Close']) > (candle['High'] - candle['Low']) * 0.6 and prev_candle['Close'] > prev_candle['Open']):
            order_blocks.append({'type': 'BEARISH_OB', 'price': candle['High'], 'time': df.index[i]})
    return order_blocks[-3:]

def detect_fvg(df):
    if len(df) < 3: return []
    fvgs = []
    for i in range(len(df)-2, len(df)):
        if i < 2: continue
        curr, prev, prev2 = df.iloc[i], df.iloc[i-1], df.iloc[i-2]
        if prev['Low'] > prev2['High'] and curr['Low'] > prev['High']:
            fvgs.append({'type': 'BULLISH_FVG', 'top': prev['Low'], 'bottom': prev2['High']})
        if prev['High'] < prev2['Low'] and curr['High'] < prev['Low']:
            fvgs.append({'type': 'BEARISH_FVG', 'top': prev2['Low'], 'bottom': prev['High']})
    return fvgs[-2:]

def detect_liquidity_sweeps(df):
    if len(df) < 10: return []
    sweeps = []
    recent = df.tail(10)
    for i in range(1, len(recent)):
        candle, prev = recent.iloc[i], recent.iloc[i-1]
        if (candle['Low'] < prev['Low'] * 0.999 and candle['Close'] > candle['Open'] and (candle['Close'] - candle['Low']) > (candle['High'] - candle['Low']) * 0.6):
            sweeps.append({'type': 'BULLISH_SWEEP', 'price': candle['Low'], 'time': recent.index[i]})
        if (candle['High'] > prev['High'] * 1.001 and candle['Close'] < candle['Open'] and (candle['High'] - candle['Close']) > (candle['High'] - candle['Low']) * 0.6):
            sweeps.append({'type': 'BEARISH_SWEEP', 'price': candle['High'], 'time': recent.index[i]})
    return sweeps[-2:]

# ── Advanced AI Analysis Prompt ───────────────────────────────────────────────
ADVANCED_ANALYSIS_PROMPT = """You are an institutional trading AI specializing in ICT/SMC concepts. Analyze multi-timeframe data and provide precise signals.

DATA PROVIDED:
{data_summary}

STRATEGY REQUIREMENTS:
1. **Multi-Timeframe Confluence**: Require agreement from at least 2 timeframes (H4/H1 for bias, M30/M15/M10 for entry)
2. **Order Flow**: Analyze buying vs selling pressure, absorption, imbalance
3. **SMC Elements**: BOS, CHoCH, liquidity sweeps, order blocks, FVGs
4. **ICT Concepts**: Killzones, liquidity pools, premium/discount arrays
5. **Supply/Demand**: Identify key zones
6. **Price Action**: Candle structure, engulfing, rejection wicks
7. **Indicators**: Use RSI, MAs, volume as confirmation only

NEWS AWARENESS:
{news_summary}

HIGH IMPACT NEWS (next 24h):
{high_impact_events}

If high-impact news (NFP, CPI, etc.) is approaching within 1-2 hours, analyze potential market reaction and provide pre-news positioning signals.

TASK:
1. Determine overall bias (BULLISH/BEARISH/RANGING) from H4/H1
2. Find precise entry from M30/M15/M10
3. Require confluence of at least 3 elements (e.g., BOS + Order Block + FVG)
4. Calculate SL/TP based on structure
5. If news is approaching, provide pre-news strategy

OUTPUT JSON ONLY:
{{
  "bias": "BULLISH|BEARISH|RANGING",
  "signal": "BUY|SELL|WAIT",
  "confluence_score": 0-100,
  "timeframes_aligned": ["H1", "M15"],
  "order_blocks": ["description"],
  "fvg_zones": ["description"],
  "liquidity_sweeps": ["description"],
  "bos_choch": ["description"],
  "entry_type": "MARKET|LIMIT|STOP",
  "entry": 0.00,
  "stop_loss": 0.00,
  "take_profit": [0.00, 0.00],
  "reasoning": "Concise explanation of confluence",
  "news_impact": "Analysis of upcoming news if applicable",
  "confidence": "HIGH|MEDIUM|LOW"
}}
"""

# ── AI Analysis Function (Groq Free API) ──────────────────────────────────────
def call_gpt(system_prompt: str, user_content: list, max_tokens: int = 2500) -> dict:
    # Use Groq's free API instead of OpenAI
    api_key = st.secrets.get("GROQ_API_KEY", "")
    if not api_key:
        # Fallback to sidebar input if secret is missing
        api_key = st.sidebar.text_input("🔑 Groq API Key (gsk_...)", type="password")
    
    if not api_key or not api_key.startswith("gsk_"):
        raise ValueError("Please add a valid GROQ_API_KEY to Streamlit Secrets.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json" # Fixed typo from original
    }
    
    # Groq uses the exact same OpenAI-compatible endpoint format
    payload = {
        "model": "llama-3.1-70b-versatile",  # Free, powerful, and fast
        "messages": [
            {"role": "system", "content": system_prompt + "\n\nIMPORTANT: You MUST output ONLY valid JSON. Do not wrap it in markdown code blocks."},
            {"role": "user", "content": user_content}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    
    res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=120)
    res_data = res.json()
    
    if 'error' in res_data:
        raise ValueError(f"Groq API Error: {res_data['error']['message']}")
    
    content = res_data['choices'][0]['message'].get('content')
    if not content:
        raise ValueError("AI returned no content.")
    
    # Clean up markdown wrappers if the AI accidentally adds them
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    if content.endswith("```"):
        content = content[:-3]
    
    return json.loads(content.strip())

# ── MT5 Execution Functions ───────────────────────────────────────────────────
def initialize_mt5():
    if not MT5_ENABLED:
        return False
    try:
        if not mt5.initialize():
            print(f"MT5 init failed: {mt5.last_error()}")
            return False
        if not mt5.login(login=int(MT5_ACCOUNT), password=MT5_PASSWORD, server=MT5_SERVER):
            print(f"MT5 login failed: {mt5.last_error()}")
            return False
        return True
    except Exception as e:
        print(f"MT5 error: {e}")
        return False

def execute_mt5_trade(symbol, direction, entry, sl, tp, lot_size):
    if not MT5_ENABLED:
        return {"error": "MT5 not enabled in settings"}
    if not MT5_AVAILABLE:
        return {"error": "MT5 requires Windows. Streamlit Cloud runs on Linux. Please use a local Windows PC or Windows VPS for auto-execution."}
    try:
        if not mt5.initialize():
            return {"error": f"MT5 init failed: {mt5.last_error()}"}
        if not mt5.login(login=int(MT5_ACCOUNT), password=MT5_PASSWORD, server=MT5_SERVER):
            return {"error": f"MT5 login failed: {mt5.last_error()}"}
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return {"error": f"Symbol {symbol} not found"}
        trade_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lot_size),
            "type": trade_type,
            "price": entry,
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

# ── Signal Generation Engine ──────────────────────────────────────────────────
@st.cache_data(ttl=300)
def analyze_symbol_comprehensive(symbol):
    try:
        mtf_data = fetch_mtf_data(symbol)
        if not mtf_data:
            return None
        
        news = get_high_impact_news()
        analysis_summary = []
        
        for tf, df in mtf_data.items():
            if df.empty:
                continue
            bos, choch = detect_bos_choch(df)
            obs = detect_order_blocks(df)
            fvgs = detect_fvg(df)
            sweeps = detect_liquidity_sweeps(df)
            
            if len(df) > 20:
                ema20 = df['Close'].ewm(span=20).mean().iloc[-1]
                ema50 = df['Close'].ewm(span=50).mean().iloc[-1]
                rsi = 100 - (100 / (1 + df['Close'].diff().clip(lower=0).rolling(14).mean() / 
                      df['Close'].diff().clip(upper=0).abs().rolling(14).mean())).iloc[-1]
            else:
                ema20, ema50, rsi = 0, 0, 50
            
            current_price = df['Close'].iloc[-1]
            analysis_summary.append(f"{tf} Timeframe:\n- Price: {current_price:.5f}\n- EMA20: {ema20:.5f}, EMA50: {ema50:.5f}\n- RSI: {rsi:.1f}\n- BOS/CHoCH: {bos}, {choch}\n- Order Blocks: {len(obs)} detected\n- FVGs: {len(fvgs)} detected\n- Liquidity Sweeps: {len(sweeps)} detected")
        
        news_text = "\n".join([f"- {n['time']} {n['currency']}: {n['event']} (Impact: {n['impact']})" for n in news[:5]]) if news else "No high-impact news today"
        
        # Call AI for analysis using Groq
        user_content = [
            {"type": "text", "text": ADVANCED_ANALYSIS_PROMPT.format(
                data_summary="\n".join(analysis_summary),
                news_summary=news_text,
                high_impact_events=news_text
            )}
        ]
        
        system_prompt = "You are an expert ICT/SMC trader. Output ONLY valid JSON."
        analysis = call_gpt(system_prompt, user_content, max_tokens=1500)
        
        analysis['symbol'] = symbol
        analysis['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return analysis
    
    except Exception as e:
        print(f"Analysis error for {symbol}: {e}")
        return {"error": str(e)}

# ── Signal Formatter for Telegram ─────────────────────────────────────────────
def format_signal_for_telegram(analysis):
    if 'error' in analysis:
        return f"❌ Error: {analysis['error']}"
    
    emoji = "🟢" if analysis.get('signal') == "BUY" else "🔴" if analysis.get('signal') == "SELL" else "⚪"
    message = f"""
{emoji} <b>{analysis['symbol']} - {analysis.get('signal', 'WAIT')} SIGNAL</b> {emoji}

⏰ Time: {analysis.get('timestamp', 'N/A')}
📊 Bias: {analysis.get('bias', 'N/A')}
🎯 Confidence: {analysis.get('confidence', 'N/A')}
📈 Confluence Score: {analysis.get('confluence_score', 0)}/100

💰 <b>TRADE DETAILS:</b>
Entry: {analysis.get('entry', 'N/A')}
SL: {analysis.get('stop_loss', 'N/A')}
TP1: {analysis.get('take_profit', ['N/A'])[0] if analysis.get('take_profit') else 'N/A'}
TP2: {analysis.get('take_profit', ['N/A', 'N/A'])[1] if len(analysis.get('take_profit', [])) > 1 else 'N/A'}
Type: {analysis.get('entry_type', 'MARKET')}

🔍 <b>CONFLUENCE FACTORS:</b>
• Timeframes: {', '.join(analysis.get('timeframes_aligned', []))}
• Order Blocks: {len(analysis.get('order_blocks', []))} detected
• FVGs: {len(analysis.get('fvg_zones', []))} detected
• Sweeps: {len(analysis.get('liquidity_sweeps', []))} detected

🧠 <b>REASONING:</b>
{analysis.get('reasoning', 'N/A')}

{f"📰 <b>NEWS IMPACT:</b>\n{analysis.get('news_impact', 'N/A')}" if analysis.get('news_impact') else ""}
    """
    return message.strip()

# ── Main App UI ───────────────────────────────────────────────────────────────
st.title("🤖 Der-AI | Automated Multi-Timeframe Trading System")
st.markdown("ICT/SMC Analysis with Telegram Alerts & MT5 Execution")

st.sidebar.header("⚙️ Configuration")
selected_symbols = st.sidebar.multiselect("Monitor Symbols", SYMBOLS, default=['XAUUSD'])
auto_refresh = st.sidebar.checkbox("Auto-Refresh (Every 5 min)", value=True)

tab1, tab2, tab3, tab4 = st.tabs(["📊 Live Analysis", "📜 Signals Log", "📰 News Calendar", "⚙️ Settings"])

with tab1:
    st.header("Multi-Timeframe Analysis")
    if st.button("🔍 Analyze All Symbols", type="primary"):
        progress_bar = st.progress(0)
        results_container = st.container()
        
        for i, symbol in enumerate(selected_symbols):
            with st.spinner(f"Analyzing {symbol}..."):
                result = analyze_symbol_comprehensive(symbol)
                with results_container:
                    if result and 'error' not in result:
                        sig_color = "🟢" if result.get('signal') == "BUY" else "🔴" if result.get('signal') == "SELL" else "⚪"
                        st.markdown(f"### {sig_color} {symbol} - {result.get('signal', 'WAIT')}")
                        
                        col1, col2, col3, col4 = st.columns(4)
                        col1.metric("Bias", result.get('bias', 'N/A'))
                        col2.metric("Confidence", result.get('confidence', 'N/A'))
                        col3.metric("Score", f"{result.get('confluence_score', 0)}/100")
                        col4.metric("Timeframes", len(result.get('timeframes_aligned', [])))
                        
                        if result.get('signal') != "WAIT":
                            st.info(f"**Entry:** {result.get('entry')} | **SL:** {result.get('stop_loss')} | **TP:** {result.get('take_profit')}")
                            st.write(f"**Reasoning:** {result.get('reasoning')}")
                            
                            telegram_msg = format_signal_for_telegram(result)
                            if send_telegram_message(telegram_msg):
                                st.success("✅ Signal sent to Telegram")
                            
                            if MT5_ENABLED and st.button(f"Execute {symbol} on MT5", key=f"exec_{symbol}"):
                                exec_result = execute_mt5_trade(
                                    symbol=symbol, direction=result.get('signal'), entry=result.get('entry'),
                                    sl=result.get('stop_loss'), tp=result.get('take_profit', [None])[0] if result.get('take_profit') else None,
                                    lot_size=MT5_LOT_SIZE
                                )
                                if exec_result.get('success'):
                                    st.success("✅ Trade executed on MT5!")
                                else:
                                    st.error(f"❌ Execution failed: {exec_result.get('error')}")
                        st.markdown("---")
                    else:
                        st.error(f"Error analyzing {symbol}: {result.get('error', 'Unknown error')}")
            progress_bar.progress((i + 1) / len(selected_symbols))
        progress_bar.empty()

with tab2:
    st.header("Signal History")
    st.info("Signals are logged here after generation. (Implement persistent storage for full history)")

with tab3:
    st.header("High-Impact News Calendar")
    if st.button("📅 Fetch Today's News"):
        news = get_high_impact_news()
        if news:
            for n in news:
                st.markdown(f"**{n['time']}** - {n['currency']}: {n['event']} (Impact: {n['impact']})")
                st.write(f"Forecast: {n['forecast']} | Previous: {n['previous']}")
                st.markdown("---")
        else:
            st.write("No high-impact news today")

with tab4:
    st.header("System Settings")
    st.subheader("Telegram Setup")
    st.markdown("""
    **To enable Telegram alerts:**
    1. Create a bot via @BotFather on Telegram
    2. Get your bot token
    3. Get your chat ID (use @userinfobot)
    4. Add to Streamlit Secrets:
       - `TELEGRAM_BOT_TOKEN` = "your-bot-token"
       - `TELEGRAM_CHAT_ID` = "your-chat-id"
    """)
    st.subheader("MT5 Auto-Execution")
    st.markdown("""
    **To enable MT5 execution:**
    1. Check "Enable MT5 Auto-Execution" in sidebar
    2. Enter your MT5 account details
    3. **Note:** MT5 requires Windows environment. For cloud deployment, use a VPS with MT5 installed.
    """)

if auto_refresh:
    time.sleep(300)
    st.experimental_rerun()
