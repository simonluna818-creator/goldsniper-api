# ================================================================
#  GOLD SNIPER AI v5.0 — API SERVER
#  Expone las señales del bot MT5 como JSON para el dashboard
#  Deploy en Railway.app (gratis)
#
#  USO LOCAL:
#    pip install fastapi uvicorn MetaTrader5 pandas ta
#    python api_server.py
#
#  Endpoints:
#    GET /signal       → señal actual completa
#    GET /candles      → últimas 60 velas XAUUSD M5
#    GET /health       → status del servidor
# ================================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import ta, warnings, uvicorn
warnings.filterwarnings('ignore')

app = FastAPI(title="GoldSniper AI API", version="5.0")

# ── CORS: permite que Vercel consuma esta API ──────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # En producción: ["https://goldsniper-ai.vercel.app"]
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── CONFIG ─────────────────────────────────────────────────────
SYMBOL       = "XAUUSD"
SCORE_MIN    = 5
ATR_SL_MULT  = 1.2
ATR_TP1_MULT = 1.0
ATR_TP2_MULT = 2.0
ATR_TP3_MULT = 3.0
PIPS_MAX     = 15
VELAS_C_MAX  = 3
VENTANA_MOM  = 6
VP_BINS      = 30

# ── INICIALIZAR MT5 ────────────────────────────────────────────
def init_mt5():
    if not mt5.initialize():
        return False
    disponibles = [s.name for s in mt5.symbols_get()]
    global SYMBOL
    for c in ["XAUUSD", "XAUUSDm", "XAUUSD.", "GOLD"]:
        if c in disponibles:
            SYMBOL = c
            mt5.symbol_select(SYMBOL, True)
            break
    return True

MT5_OK = init_mt5()
print(f"{'✅' if MT5_OK else '❌'} MT5 {'conectado' if MT5_OK else 'NO disponible'} — {SYMBOL}")

# ── OBTENER DATOS ──────────────────────────────────────────────
def get_data():
    r5  = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5,  0, 200)
    r15 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 60)
    if r5 is None:
        return None, None
    df5 = pd.DataFrame(r5)
    df5['time'] = pd.to_datetime(df5['time'], unit='s')
    df5 = df5.reset_index(drop=True)
    df15 = None
    if r15 is not None:
        df15 = pd.DataFrame(r15)
        df15['time'] = pd.to_datetime(df15['time'], unit='s')
        df15 = df15.reset_index(drop=True)
    return df5, df15

# ── INDICADORES ─────────────────────────────────────────────────
def calc_indicators(df):
    df = df.copy()
    df['ema9']  = ta.trend.EMAIndicator(df['close'],  9).ema_indicator()
    df['ema21'] = ta.trend.EMAIndicator(df['close'], 21).ema_indicator()
    df['ema50'] = ta.trend.EMAIndicator(df['close'], 50).ema_indicator()
    df['rsi']   = ta.momentum.RSIIndicator(df['close'], 7).rsi()
    df['atr']   = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], 14).average_true_range()
    bb = ta.volatility.BollingerBands(df['close'], 20, 2)
    df['bb_upper']  = bb.bollinger_hband()
    df['bb_middle'] = bb.bollinger_mavg()
    df['bb_lower']  = bb.bollinger_lband()
    vol = df['tick_volume'].astype(float)
    df['mfi'] = ta.volume.MFIIndicator(df['high'], df['low'], df['close'], vol, 14).money_flow_index()
    adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], 14)
    df['adx'] = adx.adx()
    return df

def trend_m15(df15):
    if df15 is None: return 'neutral'
    df = df15.copy()
    df['ema21'] = ta.trend.EMAIndicator(df['close'], 21).ema_indicator()
    df['ema50'] = ta.trend.EMAIndicator(df['close'], 50).ema_indicator()
    adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], 14)
    df['adx'] = adx.adx()
    u = df.iloc[-1]
    if u['close'] > u['ema21'] > u['ema50'] and u['adx'] > 20: return 'alcista'
    if u['close'] < u['ema21'] < u['ema50'] and u['adx'] > 20: return 'bajista'
    return 'neutral'

# ── MOMENTUM ───────────────────────────────────────────────────
def analyze_momentum(df):
    if len(df) < VENTANA_MOM + 2:
        return False, False, 0, 0, 0, ''
    rec    = df.tail(VENTANA_MOM)
    cambio = (rec['close'].iloc[-1] - rec['close'].iloc[0]) / 0.1
    vR = vV = 0
    for i in range(len(df)-1, max(0, len(df)-8), -1):
        v = df.iloc[i]
        if v['close'] < v['open']:
            if vV == 0: vR += 1
            else: break
        else:
            if vR == 0: vV += 1
            else: break
    mom_baj = cambio < -PIPS_MAX or vR >= VELAS_C_MAX
    mom_alc = cambio >  PIPS_MAX or vV >= VELAS_C_MAX
    if   cambio < -PIPS_MAX: razon = f"Bajó {abs(cambio):.0f}p en {VENTANA_MOM} velas"
    elif vR >= VELAS_C_MAX:  razon = f"{vR} velas rojas consecutivas"
    elif cambio >  PIPS_MAX: razon = f"Subió {cambio:.0f}p en {VENTANA_MOM} velas"
    elif vV >= VELAS_C_MAX:  razon = f"{vV} velas verdes consecutivas"
    else: razon = ''
    return mom_baj, mom_alc, cambio, vR, vV, razon

# ── FVG ────────────────────────────────────────────────────────
def detect_fvg(df):
    fvgs = []
    for i in range(2, len(df)):
        if df['low'].iloc[i] > df['high'].iloc[i-2]:
            fvgs.append({'tipo':'bull','idx':i,'low':float(df['high'].iloc[i-2]),'high':float(df['low'].iloc[i])})
        if df['high'].iloc[i] < df['low'].iloc[i-2]:
            fvgs.append({'tipo':'bear','idx':i,'low':float(df['high'].iloc[i]),'high':float(df['low'].iloc[i-2])})
    return fvgs[-5:]

# ── SWEEP ──────────────────────────────────────────────────────
def detect_sweeps(df, ventana=15):
    sweeps = []
    for i in range(ventana, len(df)-1):
        rng  = df.iloc[i-ventana:i]
        vela = df.iloc[i]
        if vela['low']  < rng['low'].min()  and vela['close'] > vela['open']:
            sweeps.append({'tipo':'bull','idx':i,'precio':float(vela['low'])})
        if vela['high'] > rng['high'].max() and vela['close'] < vela['open']:
            sweeps.append({'tipo':'bear','idx':i,'precio':float(vela['high'])})
    return sweeps[-5:]

# ── VOLUME PROFILE ─────────────────────────────────────────────
def calc_vp(df, bins=VP_BINS):
    pmin = float(df['low'].min())
    pmax = float(df['high'].max())
    step = (pmax - pmin) / bins
    niveles = [pmin + (i+0.5)*step for i in range(bins)]
    vols = [0.0]*bins
    for _, row in df.iterrows():
        for b in range(bins):
            bL = pmin + b*step
            bH = bL + step
            ov = max(0, min(row['high'], bH) - max(row['low'], bL))
            rng = row['high'] - row['low']
            if rng > 0:
                vols[b] += float(row['tick_volume']) * (ov/rng)
    poc_idx = int(np.argmax(vols))
    poc     = niveles[poc_idx]
    total   = sum(vols)
    target  = total * 0.70
    acum    = vols[poc_idx]
    lo = hi = poc_idx
    while acum < target and (lo > 0 or hi < bins-1):
        eL = vols[lo-1] if lo > 0 else 0
        eH = vols[hi+1] if hi < bins-1 else 0
        if eH >= eL and hi < bins-1: hi += 1; acum += vols[hi]
        elif lo > 0: lo -= 1; acum += vols[lo]
        else: hi += 1; acum += vols[hi]
    return {
        'poc': round(poc, 2),
        'vah': round(niveles[hi], 2),
        'val': round(niveles[lo], 2),
        'niveles': [round(n,2) for n in niveles],
        'vols': [round(v,1) for v in vols],
    }

# ── SL/TP ──────────────────────────────────────────────────────
def calc_sltp(df, tipo):
    precio = float(df['close'].iloc[-1])
    atr_v  = float(df['atr'].iloc[-1])
    swing  = df.tail(20)
    if tipo == 'buy':
        sl     = float(swing['low'].min()) - atr_v * ATR_SL_MULT
        riesgo = precio - sl
        return {
            'tipo':  'buy',
            'entry': round(precio, 2),
            'sl':    round(sl, 2),
            'tp1':   round(precio + riesgo * ATR_TP1_MULT, 2),
            'tp2':   round(precio + riesgo * ATR_TP2_MULT, 2),
            'tp3':   round(precio + riesgo * ATR_TP3_MULT, 2),
            'riesgo':round(riesgo, 2),
            'atr':   round(atr_v, 2),
        }
    else:
        sl     = float(swing['high'].max()) + atr_v * ATR_SL_MULT
        riesgo = sl - precio
        return {
            'tipo':  'sell',
            'entry': round(precio, 2),
            'sl':    round(sl, 2),
            'tp1':   round(precio - riesgo * ATR_TP1_MULT, 2),
            'tp2':   round(precio - riesgo * ATR_TP2_MULT, 2),
            'tp3':   round(precio - riesgo * ATR_TP3_MULT, 2),
            'riesgo':round(riesgo, 2),
            'atr':   round(atr_v, 2),
        }

# ── SCORE ──────────────────────────────────────────────────────
def calc_score(df, t15):
    if len(df) < 22: return 0, 0, [], [], None
    u  = df.iloc[-1]
    p  = df.iloc[-2]
    pr = u['close']
    rb = []; rs = []

    if p['ema9'] < p['ema21'] and u['ema9'] > u['ema21']: rb.append("EMA cruce ▲")
    if p['ema9'] > p['ema21'] and u['ema9'] < u['ema21']: rs.append("EMA cruce ▼")
    if pr > u['ema50']: rb.append("Sobre EMA50")
    else:               rs.append("Bajo EMA50")

    rsi = u['rsi']
    if 48 <= rsi <= 68: rb.append(f"RSI {rsi:.0f}")
    if 32 <= rsi <= 52: rs.append(f"RSI {rsi:.0f}")

    if pr > u['bb_middle']: rb.append("Sobre BB media")
    else:                   rs.append("Bajo BB media")

    mfi = u['mfi']
    if 45 <= mfi <= 72: rb.append(f"MFI {mfi:.0f}")
    if 28 <= mfi <= 55: rs.append(f"MFI {mfi:.0f}")

    if u['adx'] > 22:
        if u['ema9'] > u['ema21']: rb.append(f"ADX {u['adx']:.0f}")
        else:                      rs.append(f"ADX {u['adx']:.0f}")

    sw = detect_sweeps(df)
    for s in [x for x in sw if x['idx'] >= len(df)-4]:
        if s['tipo'] == 'bull': rb.append("Sweep ▲")
        else:                   rs.append("Sweep ▼")

    fvgs = detect_fvg(df)
    for f in [x for x in fvgs if x['idx'] >= len(df)-8]:
        mid = (f['low'] + f['high']) / 2
        if abs(pr - mid) < u['atr'] * 2:
            if f['tipo'] == 'bull' and pr > f['low']:    rb.append("FVG ▲")
            elif f['tipo'] == 'bear' and pr < f['high']: rs.append("FVG ▼")

    hora = datetime.now(timezone.utc).hour
    if not (7 <= hora < 20):
        return 0, 0, ["Fuera de sesión"], ["Fuera de sesión"], None

    if t15 == 'alcista':   rb.append("M15 ▲")
    elif t15 == 'bajista': rs.append("M15 ▼")

    mom_baj, mom_alc, _, _, _, razon = analyze_momentum(df)
    if mom_baj and len(rb) >= SCORE_MIN:
        rb = [f"BLOQUEADO: {razon}"]
    if mom_alc and len(rs) >= SCORE_MIN:
        rs = [f"BLOQUEADO: {razon}"]

    sb = len([r for r in rb if 'BLOQUEADO' not in r])
    ss = len([r for r in rs if 'BLOQUEADO' not in r])

    sltp = None
    if sb >= SCORE_MIN and sb > ss and not mom_baj:
        sltp = calc_sltp(df, 'buy')
    elif ss >= SCORE_MIN and ss > sb and not mom_alc:
        sltp = calc_sltp(df, 'sell')

    return sb, ss, rb, rs, sltp

# ================================================================
#  ENDPOINTS
# ================================================================

@app.get("/health")
def health():
    global MT5_OK
    if not MT5_OK:
        MT5_OK = init_mt5()
    return {
        "status": "ok" if MT5_OK else "mt5_offline",
        "mt5": MT5_OK,
        "symbol": SYMBOL,
        "time": datetime.now(timezone.utc).isoformat()
    }

@app.get("/signal")
def get_signal():
    """Señal completa con score, SL/TP, FVG, Volume Profile, momentum"""
    global MT5_OK
    if not MT5_OK:
        MT5_OK = init_mt5()
    if not MT5_OK:
        return {"error": "MT5 no disponible. Abre MetaTrader 5 primero."}

    try:
        df, df15 = get_data()
        if df is None:
            return {"error": "No se pudieron obtener datos de MT5"}

        df = calc_indicators(df)
        t15 = trend_m15(df15)
        sb, ss, rb, rs, sltp = calc_score(df, t15)
        mom_baj, mom_alc, cambio_pips, v_rojas, v_verdes, razon_mom = analyze_momentum(df)
        vp = calc_vp(df.tail(60))

        u = df.iloc[-1]
        precio = float(u['close'])

        # Determinar tipo de señal
        if   sb >= SCORE_MIN and sb > ss and not mom_baj: signal_type = "BUY"
        elif ss >= SCORE_MIN and ss > sb and not mom_alc: signal_type = "SELL"
        elif mom_baj and sb == 0:                         signal_type = "BLOCKED_BUY"
        elif mom_alc and ss == 0:                         signal_type = "BLOCKED_SELL"
        else:                                             signal_type = "WAIT"

        return {
            "symbol":     SYMBOL,
            "price":      round(precio, 2),
            "signal":     signal_type,
            "score_buy":  sb,
            "score_sell": ss,
            "score_min":  SCORE_MIN,
            "reasons_buy":  rb,
            "reasons_sell": rs,
            "blocked_reason": razon_mom if signal_type.startswith("BLOCKED") else "",
            "sltp":       sltp,
            "momentum": {
                "change_pips": round(float(cambio_pips), 1),
                "red_candles":   int(v_rojas),
                "green_candles": int(v_verdes),
                "blocked_buy":   bool(mom_baj),
                "blocked_sell":  bool(mom_alc),
            },
            "indicators": {
                "ema9":   round(float(u['ema9']),  2),
                "ema21":  round(float(u['ema21']), 2),
                "ema50":  round(float(u['ema50']), 2),
                "rsi":    round(float(u['rsi']),   1),
                "mfi":    round(float(u['mfi']),   1),
                "adx":    round(float(u['adx']),   1),
                "atr":    round(float(u['atr']),   2),
                "bb_upper":  round(float(u['bb_upper']),  2),
                "bb_middle": round(float(u['bb_middle']), 2),
                "bb_lower":  round(float(u['bb_lower']),  2),
            },
            "volume_profile": vp,
            "fvg":     detect_fvg(df),
            "sweeps":  detect_sweeps(df),
            "m15_trend": t15,
            "session_active": 7 <= datetime.now(timezone.utc).hour < 20,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/candles")
def get_candles(count: int = 60):
    """Últimas N velas M5 para la gráfica del dashboard"""
    global MT5_OK
    if not MT5_OK:
        MT5_OK = init_mt5()
    if not MT5_OK:
        return {"error": "MT5 no disponible"}
    try:
        df, _ = get_data()
        if df is None:
            return {"error": "Sin datos"}
        df = calc_indicators(df)
        vis = df.tail(count).reset_index(drop=True)
        candles = []
        for _, row in vis.iterrows():
            candles.append({
                "t":    row['time'].isoformat(),
                "o":    round(float(row['open']),  2),
                "h":    round(float(row['high']),  2),
                "l":    round(float(row['low']),   2),
                "c":    round(float(row['close']), 2),
                "v":    int(row['tick_volume']),
                "ema9": round(float(row['ema9']),  2) if not np.isnan(row['ema9'])  else None,
                "ema21":round(float(row['ema21']), 2) if not np.isnan(row['ema21']) else None,
                "ema50":round(float(row['ema50']), 2) if not np.isnan(row['ema50']) else None,
                "rsi":  round(float(row['rsi']),   1) if not np.isnan(row['rsi'])   else None,
                "mfi":  round(float(row['mfi']),   1) if not np.isnan(row['mfi'])   else None,
            })
        return {"symbol": SYMBOL, "timeframe": "M5", "candles": candles}
    except Exception as e:
        return {"error": str(e)}

# ================================================================
if __name__ == "__main__":
    print("🚀 GoldSniper AI API v5.0 iniciando...")
    print("   http://localhost:8000/signal")
    print("   http://localhost:8000/candles")
    print("   http://localhost:8000/health")
    uvicorn.run(app, host="0.0.0.0", port=8000)
