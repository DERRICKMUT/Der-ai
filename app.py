import os, json, requests, traceback, base64
from datetime import datetime, timezone
import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np

# ── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Der-AI | Institutional Trading", page_icon="🎯", layout="wide")

# ── API Key Handling ──────────────────────────────────────────────────────────
API_KEY = st.secrets.get("OPENAI_API_KEY", "")
if not API_KEY:
    API_KEY = st.sidebar.text_input("🔑 OpenAI API Key (sk-...)", type="password")

if not API_KEY or not API_KEY.startswith("sk-"):
    st.warning("⚠️ Please enter a valid OpenAI API Key in the sidebar or Streamlit Secrets to continue.")
    st.stop()

# ── Symbol → yfinance ticker map ──────────────────────────────────────────────
YFINANCE_MAP = {
    'XAUUSD': 'GC=F',  'GOLD': 'GC=F',
    'XAGUSD': 'SI=F',  'SILVER': 'SI=F',
    'XTIUSD': 'CL=F',  'USOIL': 'CL=F',  'OIL': 'CL=F',  'WTIUSD': 'CL=F',
    'XBRUSD': 'BZ=F',  'BRENT': 'BZ=F',
    'BTCUSD': 'BTC-USD', 'ETHUSD': 'ETH-USD', 'BNBUSD': 'BNB-USD',
    'US30':   '^DJI',   'DOW': '^DJI',
    'SPX500': '^GSPC',  'SP500': '^GSPC',
    'NAS100': '^NDX',   'NASDAQ': '^NDX',
    'GER40':  '^GDAXI', 'DAX': '^GDAXI',
    'UK100':  '^FTSE',  'FTSE': '^FTSE',
    'JPN225': '^N225',
    'USDJPY': 'JPY=X',
}

def symbol_to_ticker(symbol: str) -> str:
    s = symbol.upper().strip()
    clean = s.rstrip('M+.').replace('PRO','').replace('ECN','').replace('RAW','')
    if clean in YFINANCE_MAP: return YFINANCE_MAP[clean]
    if s in YFINANCE_MAP: return YFINANCE_MAP[s]
    if len(clean) == 6 and clean.isalpha(): return clean + '=X'
    return s

# ── Confluence scoring rubric ─────────────────────────────────────────────────
SCORING_RUBRIC = """
## Confluence Scoring Rubric — apply to EVERY setup
| # | Factor | Points |
|---|--------|--------|
| 1 | HTF trend alignment (EMA stack + swing sequence matches entry direction) | 20 |
| 2 | Price is at a defined structural zone (order block, supply/demand, FVG, key S/R) | 20 |
| 3 | Confirmed entry trigger at zone (wick ≥ 2× body, engulfing, pin bar) | 15 |
| 4 | Liquidity sweep / stop hunt confirms intent | 10 |
| 5 | FVG or breaker block present in entry zone | 10 |
| 6 | R:R ≥ 2.5:1 to TP1 | 10 |
| 7 | Tight structural SL invalidation | 10 |
| 8 | Clear path to TP1 — no major opposing structural level blocking | 5 |
**Maximum = 100 pts. Score honestly. Report conviction: HIGH ≥ 90 | MEDIUM 75-89 | LOW < 75.**
You MUST fill a `checklist` array. ALWAYS select the highest-scoring setup.
"""

CHART_ANALYSIS_PROMPT = """You are a senior institutional price action trader (ICT / Smart Money Concepts). You have been given BOTH a chart screenshot AND live multi-timeframe market data.
Your primary job: determine whether price will REVERSE or BREAK THROUGH the current zone, then commit to a BUY or SELL signal. NEVER refuse, NEVER say "no signal."

## Data hierarchy
1. Live MTF data (EMAs, RSI, ATR, swing levels, sweeps) — macro bias
2. Chart screenshot — structural precision (zones, order blocks, FVGs, exact candle patterns)

## Step 1 — Multi-timeframe structural read
From live data: EMA stack, Swing sequence (HH/HL or LH/LL), RSI momentum, ATR phase.
From chart image: Read Y-axis prices precisely. Map ALL structural zones.

## Step 2 — Sweep & trap analysis
Check SWEEPS DETECTED in live data. Cross-reference with chart: is the sweep visible as a wick? Does price confirm with a rejection candle?

## Step 3 — Expansion vs Reversal verdict (MANDATORY commitment)
REVERSAL: Recent liquidity sweep + Rejection candle + RSI divergence/oversold + ATR compression.
BREAKOUT: EMA stack fully aligned + RSI momentum + ATR expansion + Clean structural break.

## Step 4 — Generate setups (minimum 2)
Always generate at least: one reversal setup AND one breakout setup. Score each honestly.

## Step 5 — Score and always select
""" + SCORING_RUBRIC + """
Select the highest-scoring setup. Use it as the signal regardless of score.

## Response — return ONLY valid JSON (no markdown, no text outside):
{
  "structure": "bullish | bearish | ranging",
  "structure_notes": "<swing sequence with precise price levels>",
  "verdict": "reversal | breakout | continuation",
  "verdict_reason": "<1-2 sentences: cite specific sweep, RSI, ATR, zone>",
  "conviction": "HIGH | MEDIUM | LOW",
  "phase": "expansion | compression | neutral",
  "sweeps_detected": ["<describe sweep>"],
  "exhaustion_signals": ["<exhaustion pattern>"],
  "signal_available": true,
  "all_setups": [{
    "name": "<setup name>", "bias": "buy | sell", "entry_type": "MARKET | BUY_LIMIT | SELL_LIMIT | BUY_STOP | SELL_STOP",
    "entry": <number>, "stop_loss": <number>, "tp1": <number>, "tp2": <number>, "score": <0-100>,
    "checklist": [{"factor": "<factor>", "points_awarded": <n>, "reason": "<one sentence>"}],
    "factors": ["<key reason 1>", "<key reason 2>"]
  }],
  "selected_index": <index of highest-scoring setup>,
  "entry_reason": "<detailed justification>",
  "sl_reason": "<exact structural level that invalidates>",
  "tp_reason": "<structural targets>",
  "key_levels": [{"price": <number>, "type": "support | resistance | order_block | supply_zone | demand_zone | fvg | liquidity"}]
}
"""

SIGNAL_OF_DAY_PROMPT = """You are a senior institutional gold trader (ICT / SMC) specialising in XAUUSD. You have live MTF OHLCV data AND pre-computed technical indicators AND detected market dynamics.
Follow the exact same 5-step logic as the chart analysis prompt. Always commit to a verdict. Always select the highest-scoring setup.
Return ONLY valid JSON matching the exact same schema as the chart analysis prompt, but add: "current_price", "daily_structure", "four_h_trend", "ema_stack", "rsi_context", "atr_context", "one_h_entry_context", "session".
"""

# ── Technical indicators ──────────────────────────────────────────────────────
def compute_ema(series: pd.Series, period: int) -> float:
    return round(float(series.ewm(span=period, adjust=False).mean().iloc[-1]), 5)

def compute_rsi_series(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain, loss = delta.clip(lower=0).rolling(period).mean(), (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def compute_rsi(series: pd.Series, period: int = 14) -> float:
    return round(float(compute_rsi_series(series, period).iloc[-1]), 1)

def compute_atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl, hc, lc = df['High'] - df['Low'], (df['High'] - df['Close'].shift()).abs(), (df['Low'] - df['Close'].shift()).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(period).mean()

def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    return round(float(compute_atr_series(df, period).iloc[-1]), 5)

def find_swings(df: pd.DataFrame, window: int = 5) -> dict:
    highs, lows = df['High'].rolling(window * 2 + 1, center=True).max(), df['Low'].rolling(window * 2 + 1, center=True).min()
    return {
        "recent_swing_highs": [round(p, 5) for p in df['High'][df['High'] == highs].tail(4).tolist()],
        "recent_swing_lows":  [round(p, 5) for p in df['Low'][df['Low']   == lows ].tail(4).tolist()],
    }

def detect_liquidity_sweeps(df: pd.DataFrame, window: int = 5) -> list:
    if len(df) < window * 4 + 5: return []
    swings = find_swings(df, window)
    recent = df.tail(20)
    sweeps = []
    for i in range(1, len(recent)):
        row, prev = recent.iloc[i], recent.iloc[i - 1]
        ts = str(recent.index[i])[:10]
        for sh in swings['recent_swing_highs']:
            if prev['High'] < sh * 0.9999 and row['High'] >= sh and row['Close'] < sh:
                sweeps.append(f"BEAR SWEEP {ts}: hunted swing high {sh:.5g}, closed back below ({row['Close']:.5g})")
        for sl in swings['recent_swing_lows']:
            if prev['Low'] > sl * 1.0001 and row['Low'] <= sl and row['Close'] > sl:
                sweeps.append(f"BULL SWEEP {ts}: hunted swing low {sl:.5g}, closed back above ({row['Close']:.5g})")
    return sweeps[-3:]

def detect_exhaustion(df: pd.DataFrame, lookback: int = 10) -> list:
    signals = []
    recent = df.tail(lookback)
    for i in range(len(recent)):
        row = recent.iloc[i]
        ts = str(recent.index[i])[:10]
        body, rng = abs(row['Close'] - row['Open']), row['High'] - row['Low']
        if rng < 1e-9: continue
        up_wick, dn_wick = row['High'] - max(row['Open'], row['Close']), min(row['Open'], row['Close']) - row['Low']
        threshold = max(body * 2, rng * 0.35)
        if up_wick > threshold: signals.append(f"Bearish exhaustion wick {ts}: upper wick {up_wick:.5g} vs body {body:.5g}")
        if dn_wick > threshold: signals.append(f"Bullish rejection wick {ts}: lower wick {dn_wick:.5g} vs body {body:.5g}")
    if len(df) >= 22:
        rsi_s, close_s = compute_rsi_series(df['Close']).dropna(), df['Close']
        if len(rsi_s) >= 12:
            p_dir, r_dir = close_s.iloc[-1] - close_s.iloc[-12], rsi_s.iloc[-1] - rsi_s.iloc[-12]
            if p_dir > 0 and r_dir < -6: signals.append(f"Bearish RSI divergence: price higher, RSI falling ({rsi_s.iloc[-1]:.1f})")
            elif p_dir < 0 and r_dir > 6: signals.append(f"Bullish RSI divergence: price lower, RSI rising ({rsi_s.iloc[-1]:.1f})")
    return signals[-3:]

def detect_market_phase(df: pd.DataFrame) -> str:
    if len(df) < 36: return "unknown"
    atr_s = compute_atr_series(df).dropna()
    cur_atr, avg_atr = float(atr_s.iloc[-1]), float(atr_s.tail(20).mean())
    if avg_atr < 1e-10: return "unknown"
    ratio = cur_atr / avg_atr
    if ratio > 1.30: return f"expansion (ATR {cur_atr:.5g} = {ratio:.1f}× avg)"
    if ratio < 0.75: return f"compression (ATR {cur_atr:.5g} = {ratio:.1f}× avg)"
    return f"neutral (ATR {cur_atr:.5g} = {ratio:.1f}× avg)"

def ohlcv_to_text(df: pd.DataFrame, label: str, n: int = 60) -> str:
    df = df.tail(n).copy()
    try: df.index = df.index.strftime('%Y-%m-%d %H:%M')
    except: pass
    rows = ["DateTime,Open,High,Low,Close,Volume"]
    for ts, row in df.iterrows():
        rows.append(f"{ts},{row['Open']:.5g},{row['High']:.5g},{row['Low']:.5g},{row['Close']:.5g},{int(row['Volume'])}")
    return f"## {label} ({len(df)} candles)\n" + "\n".join(rows)

def fetch_chart_live_data(symbol: str) -> tuple:
    ticker_sym = symbol_to_ticker(symbol)
    try:
        t = yf.Ticker(ticker_sym)
        daily, raw_1h = t.history(period="90d", interval="1d"), t.history(period="25d", interval="1h")
        one_h = t.history(period="5d", interval="1h")
        if daily.empty or raw_1h.empty: return f"[Live data unavailable for {symbol}]", 0.0
        four_h = raw_1h.resample('4h').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}).dropna()
        current_price = round(float(daily['Close'].iloc[-1]), 5)
        
        d_t = {"ema20": compute_ema(daily['Close'], 20), "ema50": compute_ema(daily['Close'], 50), "ema200": compute_ema(daily['Close'], 200), "rsi14": compute_rsi(daily['Close']), "atr14": compute_atr(daily), **find_swings(daily)}
        h4_t = {"ema20": compute_ema(four_h['Close'], 20), "ema50": compute_ema(four_h['Close'], 50), "rsi14": compute_rsi(four_h['Close']), "atr14": compute_atr(four_h), **find_swings(four_h)}
        h1_t = {"ema20": compute_ema(one_h['Close'], 20), "rsi14": compute_rsi(one_h['Close']), "atr14": compute_atr(one_h)}
        
        sweeps = (detect_liquidity_sweeps(daily) + detect_liquidity_sweeps(four_h) + detect_liquidity_sweeps(one_h))[-3:]
        exh = (detect_exhaustion(four_h) + detect_exhaustion(one_h))[-3:]
        
        lines = [f"=== LIVE MARKET DATA: {symbol.upper()} ===", f"Current price : {current_price}",
                 f"Daily: EMA20={d_t['ema20']} EMA50={d_t['ema50']} EMA200={d_t['ema200']} | RSI={d_t['rsi14']} | ATR={d_t['atr14']} | Phase: {detect_market_phase(daily)}",
                 f"4H: EMA20={h4_t['ema20']} EMA50={h4_t['ema50']} | RSI={h4_t['rsi14']} | ATR={h4_t['atr14']} | Phase: {detect_market_phase(four_h)}",
                 f"1H: EMA20={h1_t['ema20']} | RSI={h1_t['rsi14']} | ATR={h1_t['atr14']}",
                 f"Swings High: {d_t['recent_swing_highs']} | Low: {d_t['recent_swing_lows']}",
                 "SWEEPS: " + (", ".join(sweeps) if sweeps else "None"),
                 "EXHAUSTION: " + (", ".join(exh) if exh else "None")]
        return "\n".join(lines), current_price
    except Exception as e:
        return f"[Live data error: {e}]", 0.0

def fetch_xauusd_data() -> tuple:
    t = yf.Ticker("GC=F")
    daily, raw_1h = t.history(period="120d", interval="1d"), t.history(period="30d", interval="1h")
    one_h = t.history(period="5d", interval="1h")
    four_h = raw_1h.resample('4h').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}).dropna()
    current_price = round(float(daily['Close'].iloc[-1]), 2) if not daily.empty else 0
    tech = {
        "daily": {"ema20": compute_ema(daily['Close'], 20), "ema50": compute_ema(daily['Close'], 50), "ema200": compute_ema(daily['Close'], 200), "rsi14": compute_rsi(daily['Close']), "atr14": compute_atr(daily), **find_swings(daily)},
        "four_h": {"ema20": compute_ema(four_h['Close'], 20), "ema50": compute_ema(four_h['Close'], 50), "rsi14": compute_rsi(four_h['Close']), "atr14": compute_atr(four_h), **find_swings(four_h)},
    }
    sweeps = (detect_liquidity_sweeps(daily) + detect_liquidity_sweeps(four_h) + detect_liquidity_sweeps(one_h))[-3:]
    exhaustion = (detect_exhaustion(four_h) + detect_exhaustion(one_h))[-3:]
    return daily, four_h, one_h, current_price, tech, sweeps, exhaustion, detect_market_phase(daily), detect_market_phase(four_h)

def call_gpt(system_prompt: str, user_content: list, max_tokens: int = 2500) -> dict:
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "gpt-4o", "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}], "max_tokens": max_tokens, "temperature": 0, "response_format": {"type": "json_object"}}
    res = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=120)
    res_data = res.json()
    if 'error' in res_data: raise ValueError(res_data['error']['message'])
    content = res_data['choices'][0]['message'].get('content')
    if not content: raise ValueError("AI returned no content.")
    return json.loads(content)

def build_response(plan: dict, current_price: float = None) -> dict:
    setups, sel_idx = plan.get('all_setups', []), int(plan.get('selected_index', 0))
    if sel_idx >= len(setups): sel_idx = 0
    selected = setups[sel_idx] if setups else {}
    bias, etype, score = selected.get('bias', '').lower(), selected.get('entry_type', 'MARKET').upper(), selected.get('score', 0)
    conviction = plan.get('conviction') or ('HIGH' if score >= 90 else 'MEDIUM' if score >= 75 else 'LOW')
    signal = 'BUY' if bias == 'buy' else 'SELL' if bias == 'sell' else 'WAIT'
    entry, sl, tp1, tp2 = float(selected.get('entry') or 0), float(selected.get('stop_loss') or 0), float(selected.get('tp1') or 0), float(selected.get('tp2') or 0)
    risk = abs(entry - sl) if entry and sl else 0
    def rr(target): return round(abs(target - entry) / risk, 2) if risk else None
    return {"signal": signal, "conviction": conviction, "verdict": plan.get('verdict', ''), "verdict_reason": plan.get('verdict_reason', ''), "phase": plan.get('phase', ''), "sweeps_detected": plan.get('sweeps_detected', []), "exhaustion_signals": plan.get('exhaustion_signals', []), "entry_type": etype, "entry": round(entry, 5) if entry else None, "stop_loss": round(sl, 5) if sl else None, "tp1": round(tp1, 5) if tp1 else None, "tp2": round(tp2, 5) if tp2 else None, "rr_tp1": rr(tp1), "rr_tp2": rr(tp2), "score": score, "checklist": selected.get('checklist', []), "structure": plan.get('structure', plan.get('daily_structure', '')), "structure_notes": plan.get('structure_notes', ''), "entry_reason": plan.get('entry_reason', ''), "sl_reason": plan.get('sl_reason', ''), "tp_reason": plan.get('tp_reason', ''), "key_levels": plan.get('key_levels', []), "current_price": current_price or plan.get('current_price')}

# ── Streamlit UI ──────────────────────────────────────────────────────────────
st.title("🎯 Der-AI | Institutional Trading Analyzer")
st.markdown("Advanced ICT / Smart Money Concepts multi-timeframe analysis with live yfinance data and Vision AI.")

tab1, tab2 = st.tabs(["📸 Chart Analysis", "📅 Signal of the Day (XAUUSD)"])

with tab1:
    st.header("Upload MT5 Chart")
    col1, col2 = st.columns([2, 1])
    with col1:
        uploaded_file = st.file_uploader("Upload Screenshot", type=["png", "jpg", "jpeg"])
    with col2:
        symbol = st.text_input("Trading Symbol", "XAUUSD").upper()
    
    if st.button("🔍 Analyze Chart", type="primary", use_container_width=True) and uploaded_file is not None:
        with st.spinner("🤖 Fetching live MTF data and analyzing chart structure..."):
            try:
                bytes_data = uploaded_file.getvalue()
                base64_image = base64.b64encode(bytes_data).decode('utf-8')
                live_context, current_price = fetch_chart_live_data(symbol)
                
                user_content = [
                    {"type": "text", "text": f"SYMBOL: {symbol}\n\n{live_context}\n\nINSTRUCTIONS: Read chart precisely, cross-reference with live data, commit to REVERSAL or BREAKOUT, score setups, and select the highest-scoring one."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}", "detail": "high"}}
                ]
                
                plan = call_gpt(CHART_ANALYSIS_PROMPT, user_content, max_tokens=2800)
                result = build_response(plan, current_price)
                
                # Display Results
                sig_color = "🟢" if result['signal'] == 'BUY' else "🔴" if result['signal'] == 'SELL' else "⚪"
                st.markdown(f"### {sig_color} {result['signal']} SIGNAL | Conviction: **{result['conviction']}** (Score: {result['score']}/100)")
                st.info(f"**Verdict:** {result['verdict'].upper()} — {result['verdict_reason']}")
                
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("Entry", result['entry'])
                c2.metric("Stop Loss", result['stop_loss'])
                c3.metric("Take Profit 1", result['tp1'])
                c4.metric("Take Profit 2", result['tp2'])
                c5.metric("R:R to TP1", f"1:{result['rr_tp1']}")
                
                with st.expander("📊 Technical Confluence & Checklist", expanded=True):
                    st.write(f"**Structure:** {result['structure']} | **Phase:** {result['phase']}")
                    st.write(f"**Entry Reason:** {result['entry_reason']}")
                    st.write(f"**SL Reason:** {result['sl_reason']}")
                    st.write(f"**TP Reason:** {result['tp_reason']}")
                    if result['sweeps_detected']: st.write("🧹 **Sweeps:**", ", ".join(result['sweeps_detected']))
                    if result['exhaustion_signals']: st.write("🕯️ **Exhaustion:**", ", ".join(result['exhaustion_signals']))
                    st.markdown("**Scoring Checklist:**")
                    for item in result['checklist']:
                        st.markdown(f"- **{item['factor']}** ({item['points_awarded']} pts): {item['reason']}")
                        
            except Exception as e:
                st.error(f"Analysis failed: {str(e)}")
                st.code(traceback.format_exc())

with tab2:
    st.header("Daily XAUUSD Institutional Signal")
    st.markdown("Auto-fetches live Daily, 4H, and 1H data to generate the highest-probability setup of the day.")
    if st.button("📅 Generate Signal of the Day", type="primary", use_container_width=True):
        with st.spinner("🤖 Analyzing multi-timeframe XAUUSD structure..."):
            try:
                daily, four_h, one_h, current_price, tech, sweeps, exhaustion, phase_d, phase_4h = fetch_xauusd_data()
                now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                
                tech_summary = (f"Current: {current_price} | Time: {now_utc}\n"
                                f"DAILY: EMA20={tech['daily']['ema20']} EMA50={tech['daily']['ema50']} EMA200={tech['daily']['ema200']} | RSI={tech['daily']['rsi14']} | ATR={tech['daily']['atr14']} | Phase: {phase_d}\n"
                                f"4H: EMA20={tech['four_h']['ema20']} EMA50={tech['four_h']['ema50']} | RSI={tech['four_h']['rsi14']} | ATR={tech['four_h']['atr14']} | Phase: {phase_4h}\n"
                                f"Swings High: {tech['daily']['recent_swing_highs']} | Low: {tech['daily']['recent_swing_lows']}\n"
                                f"SWEEPS: {', '.join(sweeps) if sweeps else 'None'}\nEXHAUSTION: {', '.join(exhaustion) if exhaustion else 'None'}")
                
                ohlcv_block = ohlcv_to_text(daily, "Daily", 30) + "\n\n" + ohlcv_to_text(four_h, "4H", 30) + "\n\n" + ohlcv_to_text(one_h, "1H", 48)
                
                plan = call_gpt(SIGNAL_OF_DAY_PROMPT, [{"type": "text", "text": tech_summary + "\n\n" + ohlcv_block}], max_tokens=2800)
                result = build_response(plan, current_price)
                
                sig_color = "🟢" if result['signal'] == 'BUY' else "🔴" if result['signal'] == 'SELL' else "⚪"
                st.markdown(f"### {sig_color} {result['signal']} SIGNAL | Conviction: **{result['conviction']}** (Score: {result['score']}/100)")
                st.info(f"**Verdict:** {result['verdict'].upper()} — {result['verdict_reason']}")
                
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("Current Price", result['current_price'])
                c2.metric("Entry", result['entry'])
                c3.metric("Stop Loss", result['stop_loss'])
                c4.metric("Take Profit 1", result['tp1'])
                c5.metric("R:R to TP1", f"1:{result['rr_tp1']}")
                
                with st.expander("📊 MTF Technical Confluence", expanded=True):
                    st.write(f"**Daily Structure:** {result.get('daily_structure', result['structure'])} | **4H Trend:** {result.get('four_h_trend', 'N/A')}")
                    st.write(f"**EMA Stack:** {result.get('ema_stack', 'N/A')}")
                    st.write(f"**RSI Context:** {result.get('rsi_context', 'N/A')} | **ATR Context:** {result.get('atr_context', 'N/A')}")
                    st.write(f"**1H Entry Context:** {result.get('one_h_entry_context', 'N/A')}")
                    st.write(f"**Entry Reason:** {result['entry_reason']}")
                    st.write(f"**SL Reason:** {result['sl_reason']}")
                    if result['sweeps_detected']: st.write("🧹 **Sweeps:**", ", ".join(result['sweeps_detected']))
                    
            except Exception as e:
                st.error(f"Analysis failed: {str(e)}")
