import os, json, requests, traceback
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify
import yfinance as yf
import pandas as pd
import numpy as np

app = Flask(__name__)
API_KEY = os.environ.get('OPENAI_API_KEY', '')

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
    # Strip broker suffixes (m, +, ., pro, etc.)
    clean = s.rstrip('M+.').replace('PRO','').replace('ECN','').replace('RAW','')
    if clean in YFINANCE_MAP:
        return YFINANCE_MAP[clean]
    if s in YFINANCE_MAP:
        return YFINANCE_MAP[s]
    # Forex 6-char pairs: EURUSD → EURUSD=X
    if len(clean) == 6 and clean.isalpha():
        return clean + '=X'
    return s

# ── Confluence scoring rubric ─────────────────────────────────────────────────
SCORING_RUBRIC = """
## Confluence Scoring Rubric — apply to EVERY setup

| # | Factor | Points |
|---|--------|--------|
| 1 | HTF trend alignment (EMA stack + swing sequence matches entry direction) | 20 |
| 2 | Price is at a defined structural zone (order block, supply/demand, FVG, key S/R) | 20 |
| 3 | Confirmed entry trigger at zone (wick ≥ 2× body, engulfing, pin bar — not just "near") | 15 |
| 4 | Liquidity sweep / stop hunt confirms intent (price hunted equal highs/lows before move) | 10 |
| 5 | FVG or breaker block present in entry zone | 10 |
| 6 | R:R ≥ 2.5:1 to TP1 | 10 |
| 7 | Tight structural SL invalidation (beyond the swept level or order block, not arbitrary) | 10 |
| 8 | Clear path to TP1 — no major opposing structural level blocking the route | 5 |

**Maximum = 100 pts. Score honestly. Report conviction: HIGH ≥ 90 | MEDIUM 75-89 | LOW < 75.**
You MUST fill a `checklist` array for the selected setup. ALWAYS select the highest-scoring setup — never refuse to signal.
"""

# ── Chart Analysis prompt (image + live data, always signals) ─────────────────
CHART_ANALYSIS_PROMPT = """You are a senior institutional price action trader (ICT / Smart Money Concepts). You have been given BOTH a chart screenshot AND live multi-timeframe market data for the same instrument, fetched moments ago.

Your primary job: determine whether price will REVERSE or BREAK THROUGH the current zone, then commit to a BUY or SELL signal. You must ALWAYS provide a directional verdict — never refuse, never say "no signal."

## Data hierarchy
1. Live MTF data (EMAs, RSI, ATR, swing levels, sweeps, exhaustion, market phase) — your macro bias
2. Chart screenshot — your structural precision (zones, order blocks, FVGs, exact candle patterns)
Combine both: the live data tells you WHERE the market is macro-structurally; the chart tells you the exact entry zone.

## Step 1 — Multi-timeframe structural read
From live data:
- EMA stack: price vs EMA20/50/200 → macro bias (price > all three = strong bullish; price < all three = strong bearish)
- Swing sequence: HH/HL = bullish, LH/LL = bearish, mix = ranging
- RSI momentum (Daily + 4H): above 55 = bullish momentum; below 45 = bearish; divergence = exhaustion warning
- ATR phase: expansion (ATR > 1.3× avg) = trending; compression (ATR < 0.75× avg) = coiling, expect breakout

From chart image:
- Read Y-axis prices precisely — do NOT round to hundreds
- Map ALL structural zones: order blocks, supply/demand, FVGs, equal highs/lows, breaker blocks

## Step 2 — Sweep & trap analysis (CRITICAL)
Look at the SWEEPS DETECTED section in the live data:
- BULL SWEEP (price hunted swing low, closed back above) → liquidity collected, bias is now BULLISH — expect reversal up
- BEAR SWEEP (price hunted swing high, closed back below) → liquidity collected, bias is now BEARISH — expect reversal down
- No sweep + RSI momentum + EMA aligned → BREAKOUT / CONTINUATION bias

Cross-reference sweeps with the chart: is the sweep visible as a wick? Does price confirm with a rejection candle?

## Step 3 — Expansion vs Reversal verdict (MANDATORY commitment)

REVERSAL signals (fade the move):
- Recent liquidity sweep at the current zone ✓
- Rejection candle (long wick ≥ 2× body) at zone ✓
- RSI divergence or overbought/oversold ✓
- ATR compression (equilibrium, not trending) ✓
- HTF opposing zone overhead/below ✓
→ Set verdict = "reversal"

BREAKOUT/CONTINUATION signals (follow the move):
- EMA stack fully aligned (price > EMA20 > EMA50 > EMA200 for bull) ✓
- RSI above 60 (bull) or below 40 (bear) with momentum — no divergence ✓
- ATR in expansion (current ATR > 1.3× average) ✓
- Clean structural break with candle close beyond zone ✓
- No major opposing level within 1.5× ATR of current price ✓
→ Set verdict = "breakout"

## Step 4 — Generate setups (minimum 2)
Always generate at least: one reversal setup AND one breakout setup. Score each honestly.

## Step 5 — Score and always select
""" + SCORING_RUBRIC + """

Select the highest-scoring setup. Use it as the signal regardless of score. Set conviction = HIGH/MEDIUM/LOW accordingly.

## Response — return ONLY valid JSON (no markdown, no text outside):
{
  "structure": "bullish | bearish | ranging",
  "structure_notes": "<swing sequence with precise price levels from chart + live data>",
  "verdict": "reversal | breakout | continuation",
  "verdict_reason": "<1-2 sentences: cite specific sweep, RSI, ATR, zone — data-backed commitment>",
  "conviction": "HIGH | MEDIUM | LOW",
  "phase": "expansion | compression | neutral",
  "sweeps_detected": ["<describe any sweep or stop-hunt you identify>"],
  "exhaustion_signals": ["<any exhaustion pattern, wick, divergence>"],
  "signal_available": true,
  "all_setups": [
    {
      "name": "<setup name>",
      "bias": "buy | sell",
      "entry_type": "MARKET | BUY_LIMIT | SELL_LIMIT | BUY_STOP | SELL_STOP",
      "entry": <number from chart axis>,
      "stop_loss": <number>,
      "tp1": <number>,
      "tp2": <number>,
      "score": <0-100>,
      "checklist": [
        {"factor": "<factor name>", "points_awarded": <n>, "reason": "<one sentence>"}
      ],
      "factors": ["<key reason 1>", "<key reason 2>"]
    }
  ],
  "selected_index": <index of highest-scoring setup>,
  "entry_reason": "<detailed justification combining live data + chart evidence>",
  "sl_reason": "<exact structural level that invalidates the trade>",
  "tp_reason": "<structural targets for TP1 and TP2>",
  "key_levels": [
    {"price": <number>, "type": "support | resistance | order_block | supply_zone | demand_zone | fvg | liquidity"}
  ]
}
"""

# ── Signal of the Day prompt ──────────────────────────────────────────────────
SIGNAL_OF_DAY_PROMPT = """You are a senior institutional gold trader (ICT / Smart Money Concepts) specialising in XAUUSD. You have live multi-timeframe OHLCV data AND pre-computed technical indicators AND detected market dynamics (sweeps, exhaustion, phase). Use ALL of it.

## Step 1 — Multi-timeframe structural analysis
1. **Daily**: EMA stack (price > EMA20 > EMA50 > EMA200 = strong bullish). Confirm with swing sequence. Note ATR phase.
2. **4H**: Intermediate trend + key zones. RSI momentum direction + any divergence.
3. **1H**: Entry trigger (rejection candle, structure break, liquidity sweep).

## Step 2 — Sweep & trap analysis
Check sweeps data. Recent bull sweep at lows = bullish; bear sweep at highs = bearish. No sweep + trend momentum = continuation.

## Step 3 — Expansion vs Reversal verdict
Apply the same logic as the chart analysis — always commit to reversal or breakout.

## Step 4 — Enumerate setups (minimum 3)
Generate realistic setups from the data. Each needs a structural basis.

## Step 5 — Score and ALWAYS select the best
""" + SCORING_RUBRIC + """

Always select the highest-scoring setup. Never set signal_available = false.

## Response — return ONLY valid JSON:
{
  "generated_at": "<ISO datetime UTC>",
  "current_price": <number>,
  "daily_structure": "bullish | bearish | ranging",
  "four_h_trend": "bullish | bearish | ranging",
  "ema_stack": "<e.g. Price > EMA20 > EMA50 > EMA200 — strong bullish>",
  "rsi_context": "<4H RSI value and implication>",
  "atr_context": "<Daily ATR and what it means for SL sizing>",
  "one_h_entry_context": "<1H trigger description>",
  "structure_notes": "<MTF narrative with specific price levels>",
  "verdict": "reversal | breakout | continuation",
  "verdict_reason": "<1-2 sentences, data-backed>",
  "conviction": "HIGH | MEDIUM | LOW",
  "phase": "expansion | compression | neutral",
  "sweeps_detected": ["<description>"],
  "exhaustion_signals": ["<description>"],
  "signal_available": true,
  "all_setups": [
    {
      "name": "<setup name>",
      "bias": "buy | sell",
      "entry_type": "MARKET | BUY_LIMIT | SELL_LIMIT | BUY_STOP | SELL_STOP",
      "entry": <number>,
      "stop_loss": <number>,
      "tp1": <number>,
      "tp2": <number>,
      "score": <0-100>,
      "checklist": [
        {"factor": "<factor>", "points_awarded": <n>, "reason": "<why>"}
      ],
      "factors": ["<reason 1>", "<reason 2>"]
    }
  ],
  "selected_index": <index>,
  "entry_reason": "<detailed justification>",
  "sl_reason": "<structural invalidation>",
  "tp_reason": "<structural targets>",
  "session": "London | New York | Asian | London-NY Overlap",
  "key_levels": [
    {"price": <number>, "type": "support | resistance | order_block | supply_zone | demand_zone | fvg | liquidity"}
  ]
}
"""

# ── Technical indicators — scalar ─────────────────────────────────────────────

def compute_ema(series: pd.Series, period: int) -> float:
    return round(float(series.ewm(span=period, adjust=False).mean().iloc[-1]), 5)

def compute_rsi_series(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def compute_rsi(series: pd.Series, period: int = 14) -> float:
    return round(float(compute_rsi_series(series, period).iloc[-1]), 1)

def compute_atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl  = df['High'] - df['Low']
    hc  = (df['High'] - df['Close'].shift()).abs()
    lc  = (df['Low']  - df['Close'].shift()).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    return round(float(compute_atr_series(df, period).iloc[-1]), 5)

def find_swings(df: pd.DataFrame, window: int = 5) -> dict:
    highs = df['High'].rolling(window * 2 + 1, center=True).max()
    lows  = df['Low'].rolling(window * 2 + 1, center=True).min()
    swing_highs = df['High'][df['High'] == highs].tail(4).tolist()
    swing_lows  = df['Low'][df['Low']   == lows ].tail(4).tolist()
    return {
        "recent_swing_highs": [round(p, 5) for p in swing_highs],
        "recent_swing_lows":  [round(p, 5) for p in swing_lows],
    }

# ── Market dynamics detection ─────────────────────────────────────────────────

def detect_liquidity_sweeps(df: pd.DataFrame, window: int = 5) -> list:
    """Detect stop-hunts: price breaks a swing level then closes back beyond it."""
    if len(df) < window * 4 + 5:
        return []
    swings = find_swings(df, window)
    sh_prices = swings['recent_swing_highs']
    sl_prices = swings['recent_swing_lows']
    recent = df.tail(20)
    sweeps = []
    for i in range(1, len(recent)):
        row  = recent.iloc[i]
        prev = recent.iloc[i - 1]
        ts   = str(recent.index[i])[:10]
        for sh in sh_prices:
            if prev['High'] < sh * 0.9999 and row['High'] >= sh and row['Close'] < sh:
                sweeps.append(
                    f"BEAR SWEEP {ts}: hunted swing high {sh:.5g}, "
                    f"closed back below ({row['Close']:.5g}) — bearish trap, sell pressure"
                )
        for sl in sl_prices:
            if prev['Low'] > sl * 1.0001 and row['Low'] <= sl and row['Close'] > sl:
                sweeps.append(
                    f"BULL SWEEP {ts}: hunted swing low {sl:.5g}, "
                    f"closed back above ({row['Close']:.5g}) — bullish trap, buy pressure"
                )
    return sweeps[-3:]

def detect_exhaustion(df: pd.DataFrame, lookback: int = 10) -> list:
    """Detect wick exhaustion and RSI divergence."""
    signals = []
    recent = df.tail(lookback)
    for i in range(len(recent)):
        row  = recent.iloc[i]
        ts   = str(recent.index[i])[:10]
        body = abs(row['Close'] - row['Open'])
        rng  = row['High'] - row['Low']
        if rng < 1e-9:
            continue
        up_wick = row['High'] - max(row['Open'], row['Close'])
        dn_wick = min(row['Open'], row['Close']) - row['Low']
        threshold = max(body * 2, rng * 0.35)
        if up_wick > threshold:
            signals.append(
                f"Bearish exhaustion wick {ts}: upper wick {up_wick:.5g} "
                f"vs body {body:.5g} — sellers rejecting highs at {row['High']:.5g}"
            )
        if dn_wick > threshold:
            signals.append(
                f"Bullish rejection wick {ts}: lower wick {dn_wick:.5g} "
                f"vs body {body:.5g} — buyers absorbing lows at {row['Low']:.5g}"
            )
    # RSI divergence
    if len(df) >= 22:
        rsi_s   = compute_rsi_series(df['Close']).dropna()
        close_s = df['Close'].reindex(rsi_s.index)
        if len(rsi_s) >= 12:
            p_dir = close_s.iloc[-1] - close_s.iloc[-12]
            r_dir = rsi_s.iloc[-1]   - rsi_s.iloc[-12]
            if p_dir > 0 and r_dir < -6:
                signals.append(
                    f"Bearish RSI divergence: price higher but RSI falling "
                    f"({rsi_s.iloc[-1]:.1f}) — momentum weakening at highs"
                )
            elif p_dir < 0 and r_dir > 6:
                signals.append(
                    f"Bullish RSI divergence: price lower but RSI rising "
                    f"({rsi_s.iloc[-1]:.1f}) — momentum building at lows"
                )
    return signals[-3:]

def detect_market_phase(df: pd.DataFrame) -> str:
    """Compare current ATR to 20-period average to identify expansion vs compression."""
    if len(df) < 36:
        return "unknown"
    atr_s   = compute_atr_series(df).dropna()
    cur_atr = float(atr_s.iloc[-1])
    avg_atr = float(atr_s.tail(20).mean())
    if avg_atr < 1e-10:
        return "unknown"
    ratio = cur_atr / avg_atr
    if ratio > 1.30:
        return f"expansion (ATR {cur_atr:.5g} = {ratio:.1f}× 20-period avg — trending/volatile)"
    if ratio < 0.75:
        return f"compression (ATR {cur_atr:.5g} = {ratio:.1f}× avg — coiling, breakout pending)"
    return f"neutral (ATR {cur_atr:.5g} = {ratio:.1f}× avg)"

# ── OHLCV → text ─────────────────────────────────────────────────────────────

def ohlcv_to_text(df: pd.DataFrame, label: str, n: int = 60) -> str:
    df = df.tail(n).copy()
    try:
        df.index = df.index.strftime('%Y-%m-%d %H:%M')
    except Exception:
        pass
    rows = ["DateTime,Open,High,Low,Close,Volume"]
    for ts, row in df.iterrows():
        rows.append(
            f"{ts},{row['Open']:.5g},{row['High']:.5g},"
            f"{row['Low']:.5g},{row['Close']:.5g},{int(row['Volume'])}"
        )
    return f"## {label} ({len(df)} candles)\n" + "\n".join(rows)

# ── Fetch live data for chart upload analysis ─────────────────────────────────

def fetch_chart_live_data(symbol: str) -> tuple:
    """Return (context_text, current_price) for any symbol. Non-fatal on error."""
    ticker_sym = symbol_to_ticker(symbol)
    try:
        t      = yf.Ticker(ticker_sym)
        daily  = t.history(period="90d",  interval="1d")
        raw_1h = t.history(period="25d",  interval="1h")
        one_h  = t.history(period="5d",   interval="1h")
        if daily.empty or raw_1h.empty:
            return f"[Live data unavailable for {symbol} — ticker {ticker_sym} returned no data]", 0.0

        four_h = raw_1h.resample('4h').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min',
            'Close': 'last', 'Volume': 'sum'
        }).dropna()

        current_price = round(float(daily['Close'].iloc[-1]), 5)

        # Technicals
        d_t = {
            "ema20":  compute_ema(daily['Close'], 20),
            "ema50":  compute_ema(daily['Close'], 50),
            "ema200": compute_ema(daily['Close'], 200),
            "rsi14":  compute_rsi(daily['Close']),
            "atr14":  compute_atr(daily),
            **find_swings(daily),
        }
        h4_t = {
            "ema20": compute_ema(four_h['Close'], 20),
            "ema50": compute_ema(four_h['Close'], 50),
            "rsi14": compute_rsi(four_h['Close']),
            "atr14": compute_atr(four_h),
            **find_swings(four_h),
        }
        h1_t = {
            "ema20": compute_ema(one_h['Close'], 20),
            "rsi14": compute_rsi(one_h['Close']),
            "atr14": compute_atr(one_h),
        }

        # Dynamics
        sweeps_d  = detect_liquidity_sweeps(daily)
        sweeps_4h = detect_liquidity_sweeps(four_h)
        sweeps_1h = detect_liquidity_sweeps(one_h)
        exh_4h    = detect_exhaustion(four_h)
        exh_1h    = detect_exhaustion(one_h)
        phase_d   = detect_market_phase(daily)
        phase_4h  = detect_market_phase(four_h)

        now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        lines = [
            f"=== LIVE MARKET DATA: {symbol.upper()} (source: {ticker_sym}) ===",
            f"Current price : {current_price}",
            f"Analysis time : {now_utc}",
            "",
            "─── DAILY INDICATORS ───",
            f"EMA20={d_t['ema20']}  EMA50={d_t['ema50']}  EMA200={d_t['ema200']}",
            f"RSI14={d_t['rsi14']}  ATR14={d_t['atr14']}  Phase: {phase_d}",
            f"Daily swing highs: {d_t['recent_swing_highs']}",
            f"Daily swing lows : {d_t['recent_swing_lows']}",
            "",
            "─── 4H INDICATORS ───",
            f"EMA20={h4_t['ema20']}  EMA50={h4_t['ema50']}",
            f"RSI14={h4_t['rsi14']}  ATR14={h4_t['atr14']}  Phase: {phase_4h}",
            f"4H swing highs: {h4_t['recent_swing_highs']}",
            f"4H swing lows : {h4_t['recent_swing_lows']}",
            "",
            "─── 1H INDICATORS ───",
            f"EMA20={h1_t['ema20']}  RSI14={h1_t['rsi14']}  ATR14={h1_t['atr14']}",
            "",
            "─── LIQUIDITY SWEEPS DETECTED ───",
        ]
        all_sweeps = sweeps_d + sweeps_4h + sweeps_1h
        lines += (all_sweeps if all_sweeps else ["No clear liquidity sweeps detected in recent candles"])
        lines += ["", "─── EXHAUSTION SIGNALS ───"]
        all_exh = exh_4h + exh_1h
        lines += (all_exh if all_exh else ["No clear exhaustion signals detected"])
        lines += [
            "",
            "─── OHLCV DATA ───",
            ohlcv_to_text(daily,  f"Daily (last 30 candles)",  30),
            "",
            ohlcv_to_text(four_h, f"4H (last 30 candles)",     30),
            "",
            ohlcv_to_text(one_h,  f"1H (last 48 candles)",     48),
        ]
        return "\n".join(lines), current_price

    except Exception as e:
        print(f"[live data] {symbol}: {e}")
        return f"[Live data unavailable for {symbol}: {e}]", 0.0

# ── Fetch XAUUSD data for Signal of the Day ───────────────────────────────────

def fetch_xauusd_data() -> tuple:
    ticker = yf.Ticker("GC=F")
    daily  = ticker.history(period="120d", interval="1d")
    raw_1h = ticker.history(period="30d",  interval="1h")
    one_h  = ticker.history(period="5d",   interval="1h")

    four_h = raw_1h.resample('4h').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min',
        'Close': 'last', 'Volume': 'sum'
    }).dropna()

    current_price = round(float(daily['Close'].iloc[-1]), 2) if not daily.empty else 0

    technicals = {
        "daily": {
            "ema20":  compute_ema(daily['Close'], 20),
            "ema50":  compute_ema(daily['Close'], 50),
            "ema200": compute_ema(daily['Close'], 200),
            "rsi14":  compute_rsi(daily['Close']),
            "atr14":  compute_atr(daily),
            **find_swings(daily),
        },
        "four_h": {
            "ema20": compute_ema(four_h['Close'], 20),
            "ema50": compute_ema(four_h['Close'], 50),
            "rsi14": compute_rsi(four_h['Close']),
            "atr14": compute_atr(four_h),
            **find_swings(four_h),
        },
    }

    # Dynamics for SOTD
    sweeps = (detect_liquidity_sweeps(daily) +
              detect_liquidity_sweeps(four_h) +
              detect_liquidity_sweeps(one_h))
    exhaustion = detect_exhaustion(four_h) + detect_exhaustion(one_h)
    phase_d  = detect_market_phase(daily)
    phase_4h = detect_market_phase(four_h)

    return daily, four_h, one_h, current_price, technicals, sweeps, exhaustion, phase_d, phase_4h

# ── GPT caller — deterministic ────────────────────────────────────────────────

def call_gpt(system_prompt: str, user_content: list, max_tokens: int = 2500) -> dict:
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ],
        "max_tokens":      max_tokens,
        "temperature":     0,
        "seed":            42,
        "response_format": {"type": "json_object"},
    }
    res      = requests.post("https://api.openai.com/v1/chat/completions",
                             headers=headers, json=payload, timeout=120)
    res_data = res.json()
    if 'error' in res_data:
        raise ValueError(res_data['error']['message'])
    choice  = res_data['choices'][0]
    content = choice['message'].get('content')
    refusal = choice['message'].get('refusal')
    if not content:
        raise ValueError(refusal or "AI returned no content. Upload a clear MT5 chart screenshot.")
    return json.loads(content)

# ── Response builder ──────────────────────────────────────────────────────────

def build_response(plan: dict, current_price: float = None) -> dict:
    setups  = plan.get('all_setups', [])
    sel_idx = int(plan.get('selected_index', 0))
    if sel_idx >= len(setups):
        sel_idx = 0
    selected = setups[sel_idx] if setups else {}

    bias  = selected.get('bias', '').lower()
    etype = selected.get('entry_type', 'MARKET').upper()
    score = selected.get('score', selected.get('probability', 0))

    # Conviction from GPT or derived from score
    conviction = plan.get('conviction') or (
        'HIGH' if score >= 90 else 'MEDIUM' if score >= 75 else 'LOW'
    )

    # Always signal — never NO SIGNAL
    if bias == 'buy':
        signal = 'BUY'
    elif bias == 'sell':
        signal = 'SELL'
    else:
        signal = 'WAIT'

    entry = float(selected.get('entry') or 0)
    sl    = float(selected.get('stop_loss') or 0)
    tp1   = float(selected.get('tp1') or 0)
    tp2   = float(selected.get('tp2') or 0)
    risk  = abs(entry - sl) if entry and sl else 0

    def rr(target):
        return round(abs(target - entry) / risk, 2) if risk else None

    return {
        "signal":           signal,
        "signal_available": True,
        "conviction":       conviction,
        "verdict":          plan.get('verdict', ''),
        "verdict_reason":   plan.get('verdict_reason', ''),
        "phase":            plan.get('phase', ''),
        "sweeps_detected":  plan.get('sweeps_detected', []),
        "exhaustion_signals": plan.get('exhaustion_signals', []),
        "entry_type":       etype,
        "entry":            round(entry, 5) if entry else None,
        "stop_loss":        round(sl,    5) if sl    else None,
        "tp1":              round(tp1,   5) if tp1   else None,
        "tp2":              round(tp2,   5) if tp2   else None,
        "rr_tp1":           rr(tp1),
        "rr_tp2":           rr(tp2),
        "score":            score,
        "checklist":        selected.get('checklist', []),
        "structure":        plan.get('structure', plan.get('daily_structure', '')),
        "structure_notes":  plan.get('structure_notes', ''),
        "entry_reason":     plan.get('entry_reason', ''),
        "sl_reason":        plan.get('sl_reason', ''),
        "tp_reason":        plan.get('tp_reason', ''),
        "key_levels":       plan.get('key_levels', []),
        "all_setups":       setups,
        "selected_index":   sel_idx,
        # SOTD extras
        "current_price":       current_price or plan.get('current_price'),
        "daily_structure":     plan.get('daily_structure'),
        "four_h_trend":        plan.get('four_h_trend'),
        "ema_stack":           plan.get('ema_stack'),
        "rsi_context":         plan.get('rsi_context'),
        "atr_context":         plan.get('atr_context'),
        "one_h_entry_context": plan.get('one_h_entry_context'),
        "session":             plan.get('session'),
        "generated_at":        plan.get('generated_at'),
    }

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        data       = request.get_json()
        image_data = data['image'].split(',')[1] if ',' in data['image'] else data['image']
        symbol     = data.get('symbol', 'Unknown').strip()

        # Fetch live multi-timeframe data for this symbol
        live_context, current_price = fetch_chart_live_data(symbol)

        user_content = [
            {"type": "text", "text": (
                f"SYMBOL: {symbol.upper()}\n\n"
                f"{live_context}\n\n"
                "─────────────────────────────────────────\n"
                "The MT5 chart screenshot is attached below.\n\n"
                "INSTRUCTIONS:\n"
                "1. Read the chart precisely: swing sequence, exact zone prices from the Y-axis, "
                "candle patterns at key levels (wicks, engulfing, pin bars).\n"
                "2. Cross-reference with the live data above: confirm EMA trend, check if a sweep "
                "is visible in the chart that matches the sweep data, confirm RSI momentum.\n"
                "3. Commit to a verdict: REVERSAL or BREAKOUT at the current zone — be specific.\n"
                "4. Score all setups. Select the highest-scoring one. Give a BUY or SELL signal."
            )},
            {"type": "image_url", "image_url": {
                "url":    f"data:image/png;base64,{image_data}",
                "detail": "high"
            }},
        ]

        plan   = call_gpt(CHART_ANALYSIS_PROMPT, user_content, max_tokens=2800)
        result = build_response(plan, current_price)
        return jsonify(result)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e), "signal": "ERROR"}), 500

@app.route('/signal-of-day', methods=['GET'])
def signal_of_day():
    try:
        daily, four_h, one_h, current_price, tech, sweeps, exhaustion, phase_d, phase_4h = fetch_xauusd_data()
        now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

        tech_summary = (
            f"=== PRE-COMPUTED TECHNICALS: XAUUSD ===\n"
            f"Current price : {current_price}\n"
            f"Analysis time : {now_utc}\n\n"
            f"DAILY:\n"
            f"  EMA20={tech['daily']['ema20']}  EMA50={tech['daily']['ema50']}  EMA200={tech['daily']['ema200']}\n"
            f"  RSI14={tech['daily']['rsi14']}  ATR14={tech['daily']['atr14']}  Phase: {phase_d}\n"
            f"  Swing highs: {tech['daily']['recent_swing_highs']}\n"
            f"  Swing lows : {tech['daily']['recent_swing_lows']}\n\n"
            f"4H:\n"
            f"  EMA20={tech['four_h']['ema20']}  EMA50={tech['four_h']['ema50']}\n"
            f"  RSI14={tech['four_h']['rsi14']}  ATR14={tech['four_h']['atr14']}  Phase: {phase_4h}\n"
            f"  Swing highs: {tech['four_h']['recent_swing_highs']}\n"
            f"  Swing lows : {tech['four_h']['recent_swing_lows']}\n\n"
            f"LIQUIDITY SWEEPS:\n" +
            "\n".join(sweeps if sweeps else ["No sweeps detected"]) + "\n\n"
            f"EXHAUSTION SIGNALS:\n" +
            "\n".join(exhaustion if exhaustion else ["No exhaustion signals"])
        )

        ohlcv_block = (
            ohlcv_to_text(daily,  "Daily OHLCV",  60) + "\n\n" +
            ohlcv_to_text(four_h, "4-Hour OHLCV", 60) + "\n\n" +
            ohlcv_to_text(one_h,  "1-Hour OHLCV", 48)
        )

        plan   = call_gpt(SIGNAL_OF_DAY_PROMPT,
                          [{"type": "text", "text": tech_summary + "\n\n" + ohlcv_block}],
                          max_tokens=2800)
        result = build_response(plan, current_price)
        return jsonify(result)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e), "signal": "ERROR"}), 500

@app.route('/execute-trade', methods=['POST'])
def execute_trade():
    """Proxy trade execution request to the user's local MT5 bridge."""
    try:
        data       = request.get_json()
        bridge_url = (data.get('bridge_url') or '').strip().rstrip('/')
        if not bridge_url:
            return jsonify({
                "error": "Bridge URL not set. Run mt5_bridge.py on your Windows machine, "
                         "expose it via ngrok (ngrok http 5001), then paste the URL here."
            }), 400

        payload = {
            "symbol":     data.get('broker_symbol', ''),
            "action":     data.get('action', '').lower(),
            "volume":     float(data.get('lot_size', 0.01)),
            "entry":      data.get('entry'),
            "sl":         data.get('sl'),
            "tp":         data.get('tp1'),
            "count":      int(data.get('num_trades', 1)),
            "entry_type": data.get('entry_type', 'MARKET'),
        }

        res = requests.post(f"{bridge_url}/trade", json=payload, timeout=20)
        res.raise_for_status()
        return jsonify(res.json())

    except requests.Timeout:
        return jsonify({
            "error": "Bridge timeout — is mt5_bridge.py running? Check the URL and ngrok tunnel."
        }), 504
    except requests.ConnectionError:
        return jsonify({
            "error": "Cannot reach bridge — verify ngrok is running and the URL is correct."
        }), 502
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
