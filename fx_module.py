"""
fx_module.py — Panel FX (TwelveData + DolarAPI + Yahoo fallback) + persistencia SQLite
========================================================================================

Arquitectura:
  - DolarAPI:   5 cotizaciones ARS, sin créditos.
  - TwelveData: 6 símbolos FX/Index en 1 call multi-símbolo cada ~3min.
  - Yahoo:      fallback automático si TwelveData falla / sin quota.

Persistencia (vía storage.py):
  - Cada refresh exitoso → fx_tick_insert por moneda → DB SQLite local.
  - storage.commit_db_to_github() se llama desde server.py en scheduler horario.

ENV vars relevantes:
  TWELVEDATA_API_KEY        (obligatoria para activar TwelveData)
  FX_TWELVEDATA_CADENCE_SEC (default 190)
  FX_TWELVEDATA_DAILY_CAP   (default 790)
  FX_WINDOW_START_HOUR      (default 10, ART)
  FX_WINDOW_END_HOUR        (default 17, ART)
"""

import os
import json
import time
import threading
import urllib.request
import urllib.error
import urllib.parse
import datetime as _dt

import storage

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
TWELVEDATA_BASE = "https://api.twelvedata.com"

TD_CADENCE_SEC = int(os.getenv("FX_TWELVEDATA_CADENCE_SEC", "300"))
TD_DAILY_CAP = int(os.getenv("FX_TWELVEDATA_DAILY_CAP", "790"))
TD_WINDOW_START_HOUR_ART = int(os.getenv("FX_WINDOW_START_HOUR", "10"))
TD_WINDOW_END_HOUR_ART = int(os.getenv("FX_WINDOW_END_HOUR", "17"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDITS_FILE = os.path.join(SCRIPT_DIR, "data", "fx_credits.json")


# ══════════════════════════════════════════════════════════════════
# UNIVERSO
# ══════════════════════════════════════════════════════════════════
CURRENCIES = [
    {"code": "ARS_OF",   "label": "USD/ARS Oficial",   "source": "dolarapi",
     "dolarapi_casa": "oficial",         "yh_symbol": "ARS=X",
     "quote_convention": "x_per_usd", "group": "ARS"},
    {"code": "ARS_MAY",  "label": "USD/ARS Mayorista", "source": "dolarapi",
     "dolarapi_casa": "mayorista",       "yh_symbol": "ARS=X",
     "quote_convention": "x_per_usd", "group": "ARS"},
    {"code": "ARS_MEP",  "label": "USD/ARS MEP",       "source": "dolarapi",
     "dolarapi_casa": "bolsa",           "quote_convention": "x_per_usd", "group": "ARS"},
    {"code": "ARS_CCL",  "label": "USD/ARS CCL",       "source": "dolarapi",
     "dolarapi_casa": "contadoconliqui", "quote_convention": "x_per_usd", "group": "ARS"},
    {"code": "ARS_BLUE", "label": "USD/ARS Blue",      "source": "dolarapi",
     "dolarapi_casa": "blue",            "quote_convention": "x_per_usd", "group": "ARS"},

    {"code": "BRL",    "label": "USD/BRL", "source": "twelvedata",
     "td_symbol": "USD/BRL", "yh_symbol": "USDBRL=X", "quote_convention": "x_per_usd", "group": "LATAM"},
    {"code": "MXN",    "label": "USD/MXN", "source": "twelvedata",
     "td_symbol": "USD/MXN", "yh_symbol": "USDMXN=X", "quote_convention": "x_per_usd", "group": "LATAM"},
    {"code": "CLP",    "label": "USD/CLP", "source": "twelvedata",
     "td_symbol": "USD/CLP", "yh_symbol": "USDCLP=X", "quote_convention": "x_per_usd", "group": "LATAM"},
    {"code": "JPY",    "label": "USD/JPY", "source": "twelvedata",
     "td_symbol": "USD/JPY", "yh_symbol": "JPY=X",    "quote_convention": "x_per_usd", "group": "MAJORS"},
    {"code": "EURUSD", "label": "EUR/USD", "source": "twelvedata",
     "td_symbol": "EUR/USD", "yh_symbol": "EURUSD=X", "quote_convention": "usd_per_x", "group": "MAJORS"},
    {"code": "DXY",    "label": "DXY",     "source": "twelvedata",
     "td_symbol": "DXY",     "yh_symbol": "DX-Y.NYB", "quote_convention": "index",     "group": "MAJORS"},
]

CURRENCY_BY_CODE = {c["code"]: c for c in CURRENCIES}
TD_CODES = [c["code"] for c in CURRENCIES if c["source"] == "twelvedata"]
TD_SYMBOLS = [c["td_symbol"] for c in CURRENCIES if c["source"] == "twelvedata"]


# ══════════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════════
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (CERBondAnalyzer FX/3.0)",
    "Accept": "application/json,text/plain,*/*",
}


def _http_get_json(url, timeout=12):
    req = urllib.request.Request(url, headers=_DEFAULT_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ══════════════════════════════════════════════════════════════════
# CREDIT COUNTER persistente (archivo JSON liviano)
# ══════════════════════════════════════════════════════════════════
_credit_lock = threading.Lock()


def _today_utc_str():
    return _dt.datetime.utcnow().strftime("%Y-%m-%d")


def _credits_read():
    try:
        with open(CREDITS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") != _today_utc_str():
            return {"date": _today_utc_str(), "used": 0, "last_call_ts": 0,
                    "minute_used": 0, "minute_bucket": 0}
        d.setdefault("minute_used", 0)
        d.setdefault("minute_bucket", 0)
        return d
    except (FileNotFoundError, json.JSONDecodeError):
        return {"date": _today_utc_str(), "used": 0, "last_call_ts": 0,
                "minute_used": 0, "minute_bucket": 0}


def _credits_write(state):
    os.makedirs(os.path.dirname(CREDITS_FILE), exist_ok=True)
    tmp = CREDITS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, CREDITS_FILE)


def _credits_charge(amount):
    with _credit_lock:
        state = _credits_read()
        now = time.time()
        cur_minute = int(now // 60)
        if cur_minute != state.get("minute_bucket", 0):
            state["minute_bucket"] = cur_minute
            state["minute_used"] = 0
        if state["used"] + amount > TD_DAILY_CAP:
            return False, state
        if state["minute_used"] + amount > 8:
            return False, state
        state["used"] += amount
        state["minute_used"] += amount
        state["last_call_ts"] = int(now * 1000)
        _credits_write(state)
        return True, state


def get_credit_state():
    return _credits_read()


# ══════════════════════════════════════════════════════════════════
# VENTANA OPERATIVA
# ══════════════════════════════════════════════════════════════════
def _is_within_window():
    art = _dt.datetime.utcnow() - _dt.timedelta(hours=3)
    return TD_WINDOW_START_HOUR_ART <= art.hour < TD_WINDOW_END_HOUR_ART


# ══════════════════════════════════════════════════════════════════
# TWELVEDATA
# ══════════════════════════════════════════════════════════════════
_td_snapshot = {"ts": 0, "by_code": {}, "last_error": None, "last_success_ts": 0}
_td_lock = threading.Lock()


def _td_fetch_all():
    if not TWELVEDATA_API_KEY:
        raise RuntimeError("TWELVEDATA_API_KEY no configurada")
    symbols_str = ",".join(TD_SYMBOLS)
    qs = urllib.parse.urlencode({"symbol": symbols_str, "apikey": TWELVEDATA_API_KEY})
    url = f"{TWELVEDATA_BASE}/quote?{qs}"
    raw = _http_get_json(url, timeout=15)
    if isinstance(raw, dict) and raw.get("status") == "error":
        raise RuntimeError(f"TwelveData error {raw.get('code')}: {raw.get('message')}")

    sym_to_code = {c["td_symbol"]: c["code"] for c in CURRENCIES if c["source"] == "twelvedata"}
    if "symbol" in raw and isinstance(raw.get("symbol"), str):
        items = [(raw["symbol"], raw)]
    else:
        items = list(raw.items())

    result = {}
    for sym, data in items:
        code = sym_to_code.get(sym)
        if not code or not isinstance(data, dict):
            continue
        if data.get("status") == "error":
            result[code] = {"error": data.get("message", "error")}
            continue
        def _f(k):
            try:
                v = data.get(k)
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        last = _f("close")
        prev = _f("previous_close")
        pct = _f("percent_change")
        if pct is None and last is not None and prev:
            pct = (last / prev - 1.0) * 100.0
        result[code] = {
            "last": last, "prev_close": prev, "pct_day": pct,
            "datetime": data.get("datetime"),
            "is_market_open": data.get("is_market_open"),
        }
    return result


def _td_do_refresh():
    """Hace el fetch real a TwelveData + persistencia. NO chequea cadencia ni
    ventana — eso es responsabilidad del caller. Single source of truth para
    todo lo que llame a TD.

    FIX perf: NO mantener _td_lock durante el HTTP request (15s timeout).
    El HTTP corre sin lock; al volver, tomamos el lock SOLO para mutar el
    cache global. Esto permite que get_snapshot() siga leyendo el cache
    viejo sin bloquearse durante el fetch.
    """
    if not TWELVEDATA_API_KEY:
        return False

    # Chequeo de créditos + reserva (rápido, requiere lock interno propio)
    allowed, state = _credits_charge(len(TD_SYMBOLS))
    if not allowed:
        with _td_lock:
            _td_snapshot["last_error"] = (
                f"quota agotada: used={state['used']}/{TD_DAILY_CAP} "
                f"min={state['minute_used']}/8")
        return False

    # HTTP fetch SIN lock (15s timeout). Otras lecturas siguen funcionando.
    try:
        data = _td_fetch_all()
    except Exception as e:
        with _td_lock:
            _td_snapshot["last_error"] = str(e)
        return False

    # Update atómico del cache (lock corto)
    now = time.time()
    with _td_lock:
        _td_snapshot["by_code"] = data
        _td_snapshot["last_success_ts"] = now
        _td_snapshot["ts"] = int(now * 1000)
        _td_snapshot["last_error"] = None

    # Persistencia SQLite (sin lock TD — storage tiene su propio lock)
    ts_ms = int(now * 1000)
    for code, q in data.items():
        if q.get("error") or q.get("last") is None:
            continue
        try:
            storage.fx_tick_insert(
                code=code, price=q["last"],
                prev_close=q.get("prev_close"),
                pct_day=q.get("pct_day"),
                source="twelvedata", ts_ms=ts_ms,
            )
        except Exception as e:
            print(f"[fx] tick persist error {code}: {e}")
    return True


def _td_refresh_if_due():
    """Legacy alias: usado por get_snapshot() en versiones viejas. Ahora no-op
    para evitar disparar TD desde el path del request del cliente. La
    actualización de TD vive en el thread dedicado start_td_scheduler()."""
    return False


def start_td_scheduler():
    """Thread daemon ÚNICO que actualiza TwelveData según TD_CADENCE_SEC.
    Todos los clients leen del cache (_td_snapshot). Llamar una vez al boot.
    """
    def loop():
        # Pequeña espera inicial para que init termine
        time.sleep(15)
        while True:
            try:
                if _is_within_window():
                    ok = _td_do_refresh()
                    if ok:
                        print(f"[td-scheduler] refresh ok @{_dt.datetime.utcnow().isoformat()} "
                              f"used={_credits_read().get('used',0)}/{TD_DAILY_CAP}")
                    else:
                        # Si la quota está agotada o falló, esperamos igual la cadencia
                        pass
            except Exception as e:
                print(f"[td-scheduler] error: {e}")
            time.sleep(max(TD_CADENCE_SEC, 60))
    t = threading.Thread(target=loop, name="td-scheduler", daemon=True)
    t.start()
    return t


# ══════════════════════════════════════════════════════════════════
# YAHOO (fallback + histórico)
# ══════════════════════════════════════════════════════════════════
def _yahoo_chart(symbol, range_str="1d", interval="5m"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}"
           f"?range={range_str}&interval={interval}&includePrePost=false")
    raw = _http_get_json(url, timeout=12)
    chart = raw.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"yahoo error {symbol}: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"yahoo: sin resultados para {symbol}")
    r = results[0]
    meta = r.get("meta") or {}
    last = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    timestamps = r.get("timestamp") or []
    quote_list = (r.get("indicators") or {}).get("quote") or [{}]
    closes = quote_list[0].get("close") or [] if quote_list else []
    clean = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
    timestamps = [t for t, _ in clean]
    closes = [c for _, c in clean]
    if last is None and closes:
        last = closes[-1]
    pct = (last / prev - 1.0) * 100.0 if (last is not None and prev) else None
    return {"last": last, "prev_close": prev, "pct_day": pct,
            "timestamps": timestamps, "closes": closes, "meta": meta}


# ══════════════════════════════════════════════════════════════════
# DOLARAPI
# ══════════════════════════════════════════════════════════════════
_dolarapi_cache = {"ts": 0, "data": None}
_DOLARAPI_TTL = 30


def _dolarapi_all():
    now = time.time()
    if _dolarapi_cache["data"] and now - _dolarapi_cache["ts"] < _DOLARAPI_TTL:
        return _dolarapi_cache["data"]
    data = _http_get_json("https://dolarapi.com/v1/dolares", timeout=10)
    _dolarapi_cache["ts"] = now
    _dolarapi_cache["data"] = data
    return data


def _dolarapi_quote(casa):
    data = _dolarapi_all()
    for item in data:
        if item.get("casa") == casa:
            c = item.get("compra"); v = item.get("venta")
            mid = None
            if c is not None and v is not None:
                mid = (float(c) + float(v)) / 2.0
            elif v is not None:
                mid = float(v)
            elif c is not None:
                mid = float(c)
            return {"last": mid, "compra": c, "venta": v,
                    "fecha": item.get("fechaActualizacion")}
    raise RuntimeError(f"dolarapi: casa '{casa}' no encontrada")


_ars_prev_close = {}


def _ars_pct_day(casa, current_mid):
    if current_mid is None:
        return None
    today = (_dt.datetime.utcnow() - _dt.timedelta(hours=3)).strftime("%Y-%m-%d")
    prev = _ars_prev_close.get(casa)
    if not prev or prev["date"] != today:
        _ars_prev_close[casa] = {"date": today, "open_mid": current_mid}
        return 0.0
    om = prev["open_mid"]
    return (current_mid / om - 1.0) * 100.0 if om else None


# ══════════════════════════════════════════════════════════════════
# DOLARAPI persistencia: tick por casa cada refresh
# ══════════════════════════════════════════════════════════════════
def _persist_dolarapi(cfg, q):
    try:
        storage.fx_tick_insert(
            code=cfg["code"], price=q["last"],
            prev_close=None,
            pct_day=_ars_pct_day(cfg["dolarapi_casa"], q["last"]),
            source="dolarapi",
        )
    except Exception as e:
        print(f"[fx] dolarapi persist {cfg['code']}: {e}")


# ══════════════════════════════════════════════════════════════════
# SNAPSHOT
# ══════════════════════════════════════════════════════════════════
def get_snapshot():
    # NOTA: el refresh de TwelveData vive en start_td_scheduler() (server.py),
    # NO se dispara desde acá. Cada client lee el cache compartido sin gastar
    # créditos. Solo persistimos DolarAPI (que tiene su propio TTL local).
    items = []
    td_cache = _td_snapshot["by_code"]
    td_age_sec = int(time.time() - _td_snapshot["last_success_ts"]) if _td_snapshot["last_success_ts"] else None

    for cfg in CURRENCIES:
        item = {
            "code": cfg["code"], "label": cfg["label"], "group": cfg["group"],
            "source": cfg["source"], "quote_convention": cfg["quote_convention"],
            "last": None, "prev_close": None, "pct_day": None,
            "error": None, "data_source": None, "age_sec": None,
        }
        try:
            if cfg["source"] == "dolarapi":
                q = _dolarapi_quote(cfg["dolarapi_casa"])
                item["last"] = q["last"]; item["compra"] = q.get("compra")
                item["venta"] = q.get("venta"); item["fecha"] = q.get("fecha")
                item["pct_day"] = _ars_pct_day(cfg["dolarapi_casa"], q["last"])
                item["data_source"] = "dolarapi"
                _persist_dolarapi(cfg, q)
            elif cfg["source"] == "twelvedata":
                td = td_cache.get(cfg["code"])
                if td and not td.get("error") and td.get("last") is not None:
                    item["last"] = td["last"]; item["prev_close"] = td["prev_close"]
                    item["pct_day"] = td["pct_day"]; item["data_source"] = "twelvedata"
                    item["age_sec"] = td_age_sec
                else:
                    # FIX DoS: NO disparar Yahoo sync inline. Cada call serial de
                    # 12s × 6 monedas = 72s respuesta → UI freezada. En cambio
                    # leer último tick guardado (fx_ticks) o devolver stale flag.
                    item["last"] = None; item["prev_close"] = None; item["pct_day"] = None
                    item["data_source"] = "no_data"
                    item["error"] = "TD cache vacío — schedule fetcher corriendo"
                    # Intentar el último tick persistido si existe
                    try:
                        last = storage.fx_latest(cfg["code"])
                        if last and last.get("price") is not None:
                            item["last"] = last["price"]
                            item["prev_close"] = last.get("prev_close")
                            item["pct_day"] = last.get("pct_day")
                            item["data_source"] = "sqlite_last_tick"
                            item["age_sec"] = int(time.time() - last["ts"]/1000) if last.get("ts") else None
                            item["error"] = None
                    except Exception:
                        pass
        except Exception as e:
            item["error"] = str(e)
        items.append(item)

    return {
        "ts": int(time.time() * 1000),
        "items": items,
        "twelvedata": {
            "configured": bool(TWELVEDATA_API_KEY),
            "in_window": _is_within_window(),
            "cadence_sec": TD_CADENCE_SEC,
            "credits": get_credit_state(),
            "last_error": _td_snapshot["last_error"],
            "last_success_ts": _td_snapshot["last_success_ts"],
            "data_age_sec": td_age_sec,
        },
    }


# ══════════════════════════════════════════════════════════════════
# SERIES — combina histórico SQLite (intradía propio) + Yahoo (multi-tf)
# ══════════════════════════════════════════════════════════════════
TF_MAP = {
    "1D":  ("1d",  "5m"),
    "1W":  ("5d",  "30m"),
    "1M":  ("1mo", "1d"),
    "YTD": ("ytd", "1d"),
}

_series_cache = {}
_SERIES_TTL_INTRADAY = 60
_SERIES_TTL_DAILY = 300


def get_series(code, tf="1D"):
    cfg = CURRENCY_BY_CODE.get(code)
    if not cfg:
        raise ValueError(f"código desconocido: {code}")
    tf = (tf or "1D").upper()
    if tf not in TF_MAP:
        raise ValueError(f"timeframe inválido: {tf}")

    cache_key = (code, tf)
    ttl = _SERIES_TTL_INTRADAY if tf == "1D" else _SERIES_TTL_DAILY
    cached = _series_cache.get(cache_key)
    if cached and time.time() - cached["ts"] < ttl:
        return cached["data"]

    out = {"code": code, "label": cfg["label"], "tf": tf, "points": []}

    try:
        if cfg["source"] == "dolarapi":
            # Para 1D: SQLite (intradía denso de DolarAPI + cálculos).
            # Para 1W/1M/YTD: si hay yh_symbol, usar Yahoo (histórico denso desde
            # la apertura del par); sino caer a SQLite (lo que tengamos).
            if tf == "1D":
                since = int((time.time() - 86400) * 1000)
                ticks = storage.fx_ticks_range(code, since_ts_ms=since)
                out["points"] = [{"t": t["ts"], "v": t["price"]} for t in ticks]
                out["data_source"] = "sqlite_history"
            else:
                yh_sym = cfg.get("yh_symbol")
                if yh_sym:
                    range_str, interval = TF_MAP[tf]
                    try:
                        data = _yahoo_chart(yh_sym, range_str=range_str, interval=interval)
                        out["points"] = [{"t": int(t) * 1000, "v": float(c)}
                                         for t, c in zip(data["timestamps"], data["closes"])]
                        out["data_source"] = "yahoo"
                    except Exception as e:
                        # Fallback a SQLite si Yahoo falla
                        days = {"1W": 7, "1M": 30, "YTD": 365}.get(tf, 30)
                        since = int((time.time() - days * 86400) * 1000)
                        ticks = storage.fx_ticks_range(code, since_ts_ms=since)
                        out["points"] = [{"t": t["ts"], "v": t["price"]} for t in ticks]
                        out["data_source"] = "sqlite_history_fallback"
                        out["note"] = f"yahoo error: {e}"
                else:
                    days = {"1W": 7, "1M": 30, "YTD": 365}.get(tf, 30)
                    since = int((time.time() - days * 86400) * 1000)
                    ticks = storage.fx_ticks_range(code, since_ts_ms=since)
                    out["points"] = [{"t": t["ts"], "v": t["price"]} for t in ticks]
                    out["data_source"] = "sqlite_history"
            q = _dolarapi_quote(cfg["dolarapi_casa"])
            out["last"] = q["last"]
            out["prev_close"] = None
            out["pct_day"] = _ars_pct_day(cfg["dolarapi_casa"], q["last"])
            if not out["points"]:
                out["points"] = [{"t": int(time.time() * 1000), "v": q["last"]}]
                out["note"] = "histórico ARS aún se está construyendo"
        else:
            range_str, interval = TF_MAP[tf]
            data = _yahoo_chart(cfg["yh_symbol"], range_str=range_str, interval=interval)
            out["points"] = [{"t": int(t) * 1000, "v": float(c)}
                             for t, c in zip(data["timestamps"], data["closes"])]
            td = _td_snapshot["by_code"].get(code)
            if tf == "1D" and td and not td.get("error") and td.get("last") is not None:
                out["last"] = td["last"]; out["prev_close"] = td["prev_close"]
                out["pct_day"] = td["pct_day"]
                out["data_source"] = "yahoo+twelvedata_last"
            else:
                out["last"] = data["last"]; out["prev_close"] = data["prev_close"]
                out["pct_day"] = data["pct_day"]; out["data_source"] = "yahoo"
            # Overlay opcional: ticks propios de las últimas 24h (más densos que el 5m de Yahoo)
            if tf == "1D":
                since = int((time.time() - 86400) * 1000)
                own = storage.fx_ticks_range(code, since_ts_ms=since)
                if len(own) > 5:
                    out["own_ticks"] = [{"t": t["ts"], "v": t["price"]} for t in own]
    except Exception as e:
        out["error"] = str(e)

    _series_cache[cache_key] = {"ts": time.time(), "data": out}
    return out


# ══════════════════════════════════════════════════════════════════
# MATRIZ CRUZADA
# ══════════════════════════════════════════════════════════════════
def _normalize_to_usd_per_unit(item):
    last = item.get("last")
    if last is None or last == 0:
        return None
    qc = item.get("quote_convention")
    if qc == "x_per_usd":
        return 1.0 / float(last)
    if qc == "usd_per_x":
        return float(last)
    return None


def get_cross_matrix():
    snap = get_snapshot()
    items = snap["items"]
    quotables = [it for it in items if it.get("quote_convention") in ("x_per_usd", "usd_per_x")]
    codes = ["USD"] + [it["code"] for it in quotables]
    labels = {"USD": "USD"}
    pct_day_map = {"USD": 0.0}
    usd_per_unit = {"USD": 1.0}
    for it in quotables:
        labels[it["code"]] = it["label"]
        pct_day_map[it["code"]] = it.get("pct_day")
        usd_per_unit[it["code"]] = _normalize_to_usd_per_unit(it)

    n = len(codes)
    matrix = [[None] * n for _ in range(n)]
    pct_matrix = [[None] * n for _ in range(n)]

    def appr(code, pct):
        if pct is None:
            return None
        if code == "USD":
            return 0.0
        cfg = CURRENCY_BY_CODE.get(code, {})
        return pct if cfg.get("quote_convention") == "usd_per_x" else -pct

    for i, ci in enumerate(codes):
        for j, cj in enumerate(codes):
            if i == j:
                matrix[i][j] = 1.0; pct_matrix[i][j] = 0.0; continue
            ui = usd_per_unit.get(ci); uj = usd_per_unit.get(cj)
            if ui and uj:
                matrix[i][j] = uj / ui
            ai = appr(ci, pct_day_map.get(ci)); aj = appr(cj, pct_day_map.get(cj))
            if ai is not None and aj is not None:
                pct_matrix[i][j] = aj - ai

    return {
        "ts": snap["ts"], "codes": codes, "labels": labels,
        "matrix": matrix, "pct_matrix": pct_matrix,
        "pct_day_vs_usd": pct_day_map,
    }


# ══════════════════════════════════════════════════════════════════
# HISTORY accesor para frontend (raw ticks o agregado diario)
# ══════════════════════════════════════════════════════════════════
def get_history(code, days=30, mode="auto"):
    """mode: 'ticks' | 'daily' | 'auto' (auto: ticks si <=7 días, daily si más).

    Acepta códigos calc-only (PS/PSP/IMP/BRECHA/CANJE) que el cliente pushea
    vía /api/fx/calc_ticks — éstos no están en CURRENCY_BY_CODE pero sí en
    fx_ticks. Si el código es desconocido, usar el code como label.
    """
    cfg = CURRENCY_BY_CODE.get(code)
    label = cfg["label"] if cfg else code
    if mode == "auto":
        mode = "ticks" if days <= 7 else "daily"
    if mode == "daily":
        rows = storage.fx_daily_series(code, days=days)
        return {"code": code, "label": label, "mode": "daily", "rows": rows}
    since = int((time.time() - days * 86400) * 1000)
    ticks = storage.fx_ticks_range(code, since_ts_ms=since, limit=20000)
    return {"code": code, "label": label, "mode": "ticks",
            "points": [{"t": t["ts"], "v": t["price"]} for t in ticks]}


# ══════════════════════════════════════════════════════════════════
# DIAGNOSTICO
# ══════════════════════════════════════════════════════════════════
def get_status():
    state = get_credit_state()
    art = _dt.datetime.utcnow() - _dt.timedelta(hours=3)
    return {
        "twelvedata_configured": bool(TWELVEDATA_API_KEY),
        "in_window": _is_within_window(),
        "window_art": f"{TD_WINDOW_START_HOUR_ART:02d}:00-{TD_WINDOW_END_HOUR_ART:02d}:00",
        "now_art": art.strftime("%Y-%m-%d %H:%M:%S"),
        "cadence_sec": TD_CADENCE_SEC,
        "daily_cap": TD_DAILY_CAP,
        "credits_today": {
            "date": state["date"], "used": state["used"],
            "remaining": TD_DAILY_CAP - state["used"],
            "minute_used": state.get("minute_used", 0),
        },
        "td_symbols": TD_SYMBOLS,
        "td_last_success_ts": _td_snapshot["last_success_ts"],
        "td_last_error": _td_snapshot["last_error"],
        "td_data_age_sec": (int(time.time() - _td_snapshot["last_success_ts"])
                            if _td_snapshot["last_success_ts"] else None),
        "storage": storage.stats(),
    }


# ══════════════════════════════════════════════════════════════════
# SCHEDULER hook: snapshot horario + commit DB
# ══════════════════════════════════════════════════════════════════
def hourly_tick():
    """Llamada cada ~1h desde server.py. Fuerza snapshot, persiste, commitea DB."""
    try:
        snap = get_snapshot()
        # commit con interval mínimo 50 min para no spamear GitHub
        ok, msg = storage.commit_db_to_github(min_interval_sec=50 * 60)
        return {"snapshot_items": len(snap["items"]), "commit_ok": ok, "commit_msg": msg}
    except Exception as e:
        return {"error": str(e)}
