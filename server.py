"""
CER Bond Analyzer v7 — Production Server (Railway-ready)
=========================================================
Sirve el dashboard HTML + precios de Google Sheets + proxy BCRA.
Auth via PIN (env var AUTH_PIN). Google creds via env var GOOGLE_CREDS_JSON.

Local:   python server.py
Deploy:  Railway / Render (auto-detecta PORT env var)
"""

import os
import sys
import json
import re
import base64
import datetime as _dt
import threading
# Fix Unicode output on Windows (cp1252 can't handle emoji)
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import time
import hashlib
import secrets
import urllib.request
import urllib.error
import urllib.parse
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from http.cookies import SimpleCookie

import storage
import fx_module

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════
PORT = int(os.getenv("PORT", 8888))
AUTH_PIN = os.getenv("AUTH_PIN", "")  # Set in Railway env vars. Empty = no auth
SESSION_TTL = 86400 * 7  # 7 days

# Google Sheets
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1upAbMPW3SqPh8mF5GUCf2h0yy7ZOL52bT9-2dP5AnM0")
SHEET_RANGE = os.getenv("SHEET_RANGE", "DATA!A1:K1500")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# For local dev: path to JSON key file
KEY_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "pythonon-450915-c108a4e2426e.json"),
    os.getenv("GOOGLE_SHEETS_KEY_PATH", ""),
]

# History DB (snapshots diarios para z-score, pairs, backtest)
SCRIPT_DIR_HIST = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(SCRIPT_DIR_HIST, "data", "history.json")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO  = os.getenv("GITHUB_REPO", "maximodubois/cer-bond-analyzer")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_HISTORY_PATH = "data/history.json"
_HISTORY_LOCK = threading.Lock()

# Primary API (Matba/Rofex) — depth-5 market data
PRIMARY_USERNAME = os.getenv("PRIMARY_USERNAME", "")
PRIMARY_PASSWORD = os.getenv("PRIMARY_PASSWORD", "")
PRIMARY_BASE_URL = os.getenv("PRIMARY_BASE_URL", "https://api.remarkets.primary.com.ar").rstrip("/")
PRIMARY_MARKET_ID = os.getenv("PRIMARY_MARKET_ID", "ROFX")
# Symbol prefix used by MERVAL bonds via Primary. Default works for the
# remarkets/cocos/eco/etc. brokers that route MERVAL through ROFX.
PRIMARY_MERV_PREFIX = os.getenv("PRIMARY_MERV_PREFIX", "MERV - XMEV")

# ══════════════════════════════════════════════════════════════════
# AUTH — Simple PIN-based sessions
# ══════════════════════════════════════════════════════════════════
_sessions = {}  # token -> expiry timestamp

def auth_required():
    """Check if auth is enabled."""
    return bool(AUTH_PIN)

def create_session():
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token

def validate_session(token):
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp:
        return False
    if time.time() > exp:
        del _sessions[token]
        return False
    return True

def get_session_from_cookie(cookie_header):
    if not cookie_header:
        return None
    c = SimpleCookie()
    c.load(cookie_header)
    morsel = c.get("cer_session")
    return morsel.value if morsel else None

# ══════════════════════════════════════════════════════════════════
# GOOGLE SHEETS
# ══════════════════════════════════════════════════════════════════
_sheet_service = None
_cached_prices = {"ts": 0, "data": [], "error": None}
_cache_ttl = 3

def init_sheets():
    global _sheet_service
    creds = None

    # Method 1: GOOGLE_CREDS_JSON env var (for Railway)
    creds_json = os.getenv("GOOGLE_CREDS_JSON", "")
    if creds_json:
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            info = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            print("✓ Google creds loaded from GOOGLE_CREDS_JSON env var")
        except Exception as e:
            print(f"✗ Error parsing GOOGLE_CREDS_JSON: {e}")

    # Method 2: JSON key file (for local dev)
    if not creds:
        for path in KEY_PATHS:
            if path and os.path.isfile(path):
                try:
                    from google.oauth2 import service_account
                    from googleapiclient.discovery import build
                    creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
                    print(f"✓ Google creds loaded from file: {path}")
                    break
                except Exception as e:
                    print(f"✗ Error loading {path}: {e}")

    if not creds:
        print("\n⚠ No Google credentials found.")
        print("  Set GOOGLE_CREDS_JSON env var (paste full JSON) or place .json in server dir.")
        return False

    try:
        from googleapiclient.discovery import build
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        _sheet_service = service.spreadsheets()
        result = _sheet_service.values().get(
            spreadsheetId=SPREADSHEET_ID, range="DATA!A1:A2"
        ).execute()
        print(f"✓ Connected to Google Sheets ({SPREADSHEET_ID[:20]}...)")
        return True
    except Exception as e:
        print(f"✗ Google Sheets connection error: {e}")
        return False


def fetch_prices():
    global _cached_prices
    now = time.time()
    if now - _cached_prices["ts"] < _cache_ttl:
        return _cached_prices

    if not _sheet_service:
        _cached_prices = {"ts": now, "data": [], "error": "Google Sheets no conectado"}
        return _cached_prices

    try:
        result = _sheet_service.values().get(
            spreadsheetId=SPREADSHEET_ID, range=SHEET_RANGE
        ).execute()
        values = result.get("values", [])
        if len(values) < 2:
            _cached_prices = {"ts": now, "data": [], "error": "Sin datos en hoja DATA"}
            return _cached_prices

        rows = []
        for row in values[1:]:
            while len(row) < 11:
                row.append("")
            inst = row[0].strip() if row[0] else ""
            if not inst:
                continue
            lower_inst = inst.lower()
            is_24hs = "24hs" in lower_inst or "24 hs" in lower_inst
            is_ci = (not is_24hs) and (" ci " in (" " + lower_inst + " ") or "- ci -" in lower_inst or "- ci" == lower_inst[-4:] or " ci-" in lower_inst)
            is_dlr = "dlr" in lower_inst
            # Cauciones XMEV: "MERV - XMEV - PESOS - {N}D"
            is_caucion = ("xmev" in lower_inst and "pesos" in lower_inst
                          and bool(re.search(r'\b\d+d$', lower_inst.strip())))
            # Naked tickers (no " - " separator) — DL bonds, futures, etc.
            is_naked = " - " not in inst
            if not (is_24hs or is_ci or is_dlr or is_caucion or is_naked):
                continue
            parts = [p.strip() for p in inst.split(" - ")]
            if is_caucion:
                # Extract day number → ticker = "CAUCION-{N}D"
                m = re.search(r'(\d+)[dD]$', inst.strip())
                ticker = f"CAUCION-{m.group(1)}D" if m else ""
            else:
                ticker = parts[2] if len(parts) >= 3 else parts[0] if parts else ""
            if not ticker:
                continue
            # CI instruments: suffix ticker to differentiate from 24hs
            if is_ci and not is_dlr:
                ticker = ticker + " CI"

            def parse_num(val):
                try:
                    return float(str(val).replace(",", ".").strip()) if val else 0
                except:
                    return 0

            bid_size = parse_num(row[1])
            bid = parse_num(row[2])
            last = parse_num(row[3])
            ask = parse_num(row[4])
            ask_size = parse_num(row[5])
            close = parse_num(row[6])
            volume = parse_num(row[9])

            # DLR/SPOT special layout: last price in column I (index 8)
            if "dlr/spot" in lower_inst and len(row) > 8:
                spot_last = parse_num(row[8])
                if spot_last:
                    last = spot_last

            if bid or ask or last or close:
                mid = (bid + ask) / 2 if bid and ask else (last or bid or ask or close)
                rows.append({
                    "ticker": ticker, "bid": bid, "ask": ask, "last": last,
                    "mid": round(mid, 4), "bidSize": bid_size, "askSize": ask_size,
                    "close": close, "volume": volume,
                })

        _cached_prices = {"ts": now, "data": rows, "error": None}
    except Exception as e:
        _cached_prices = {"ts": now, "data": _cached_prices.get("data", []), "error": str(e)}

    return _cached_prices


# ══════════════════════════════════════════════════════════════════
# PRIMARY API — Token auth + depth-5 market data
# ══════════════════════════════════════════════════════════════════
_primary_token = {"value": None, "expiry": 0}
_depth_cache = {}            # symbol -> {"ts": float, "data": dict}
_DEPTH_CACHE_TTL = 3.0       # seconds — book moves fast pero 3s es buen compromiso para hover/prefetch (instantáneo) vs frescura


def primary_enabled():
    return bool(PRIMARY_USERNAME and PRIMARY_PASSWORD)


def get_primary_token(force_refresh=False):
    """Return a valid X-Auth-Token, refreshing if expired (24h TTL per docs)."""
    global _primary_token
    now = time.time()
    if (not force_refresh) and _primary_token["value"] and now < _primary_token["expiry"]:
        return _primary_token["value"]
    if not primary_enabled():
        raise RuntimeError("PRIMARY_USERNAME / PRIMARY_PASSWORD env vars not set")

    url = f"{PRIMARY_BASE_URL}/auth/getToken"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "X-Username": PRIMARY_USERNAME,
            "X-Password": PRIMARY_PASSWORD,
            "User-Agent": "CERBondAnalyzer/2.0",
        },
        data=b"",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        token = resp.headers.get("X-Auth-Token")
    if not token:
        raise RuntimeError("Primary API: no X-Auth-Token in response headers")
    # Token vive 24h, renovamos a las 23h por margen.
    _primary_token = {"value": token, "expiry": now + 23 * 3600}
    return token


def _primary_get(path, params):
    """GET against Primary REST. Auto-refreshes token on 401."""
    qs = urllib.parse.urlencode(params, safe=",/-: ")
    url = f"{PRIMARY_BASE_URL}{path}?{qs}"
    for attempt in (0, 1):
        token = get_primary_token(force_refresh=(attempt == 1))
        req = urllib.request.Request(url, headers={
            "X-Auth-Token": token,
            "User-Agent": "CERBondAnalyzer/2.0",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and attempt == 0:
                continue
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            raise RuntimeError(f"Primary API HTTP {e.code}: {body[:300]}")
    raise RuntimeError("Primary API: auth failed after retry")


def fetch_primary_depth(symbol, depth=5, entries=("BI", "OF", "LA", "CL", "OP")):
    """Return {bids:[{price,size}...], offers:[...], last, close, open, raw}."""
    cache_key = f"{symbol}|{depth}|{','.join(entries)}"
    now = time.time()
    cached = _depth_cache.get(cache_key)
    if cached and now - cached["ts"] < _DEPTH_CACHE_TTL:
        return cached["data"]

    data = _primary_get("/rest/marketdata/get", {
        "marketId": PRIMARY_MARKET_ID,
        "symbol": symbol,
        "entries": ",".join(entries),
        "depth": int(depth),
    })
    md = data.get("marketData") or {}

    def _to_levels(node):
        # API devuelve list para depth>1, dict {price,size} para depth=1
        if node is None:
            return []
        if isinstance(node, list):
            return [{"price": x.get("price"), "size": x.get("size")}
                    for x in node if isinstance(x, dict) and x.get("price") is not None]
        if isinstance(node, dict) and node.get("price") is not None:
            return [{"price": node.get("price"), "size": node.get("size")}]
        return []

    def _scalar(node):
        if isinstance(node, dict):
            return node.get("price")
        if isinstance(node, (int, float)):
            return node
        return None

    out = {
        "status": data.get("status"),
        "symbol": symbol,
        "marketId": PRIMARY_MARKET_ID,
        "depth": data.get("depth"),
        "bids": _to_levels(md.get("BI")),
        "offers": _to_levels(md.get("OF")),
        "last": _scalar(md.get("LA")),
        "close": _scalar(md.get("CL")),
        "open": _scalar(md.get("OP")),
        "ts": int(now * 1000),
    }
    _depth_cache[cache_key] = {"ts": now, "data": out}
    return out


def build_primary_symbol(ticker, settle):
    """Construye 'MERV - XMEV - {ticker} - {settle}'. Acepta ya-formateado."""
    if not ticker:
        return ""
    t = ticker.strip()
    # Si el caller ya mandó símbolo completo, respetar.
    if " - " in t:
        return t
    s = (settle or "24hs").strip()
    # Normalizar shortcuts
    s_upper = s.upper()
    if s_upper in ("CI", "T0", "0", "HOY"):
        s = "CI"
    elif s_upper in ("24HS", "T1", "1", "T+1", "24"):
        s = "24hs"
    return f"{PRIMARY_MERV_PREFIX} - {t} - {s}"


# ══════════════════════════════════════════════════════════════════
# HTTP SERVER
# ══════════════════════════════════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = None
for name in ["CERBondAnalyzer_v7.html", "CERBondAnalyzer_v6.html", "dashboard.html"]:
    path = os.path.join(SCRIPT_DIR, name)
    if os.path.isfile(path):
        HTML_FILE = path
        break

LOGIN_HTML = """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CER Bond Analyzer — Login</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'IBM Plex Mono',monospace;background:#080c14;color:#e2e8f0;min-height:100vh;
display:flex;align-items:center;justify-content:center}
.box{background:#0f1520;border:1px solid #1a2233;border-radius:12px;padding:40px;width:340px;text-align:center}
h1{color:#00e5a0;font-size:18px;margin-bottom:8px}
.sub{color:#64748b;font-size:11px;margin-bottom:24px}
input{background:#0c1220;border:1px solid #1e2d44;border-radius:6px;padding:12px;color:#e2e8f0;
font-family:'IBM Plex Mono',monospace;font-size:16px;width:100%;text-align:center;letter-spacing:8px;
outline:none;margin-bottom:16px}
input:focus{border-color:#00e5a0}
button{background:#00e5a0;color:#080c14;border:none;border-radius:6px;padding:12px;font-size:14px;
font-weight:700;cursor:pointer;width:100%;font-family:'IBM Plex Mono',monospace}
button:hover{opacity:0.85}
.err{color:#ff6b4a;font-size:11px;margin-top:8px;min-height:16px}
</style></head><body>
<div class="box">
<h1>CER Bond Analyzer</h1>
<div class="sub">Ingresá el PIN de acceso</div>
<form method="POST" action="/login">
<input type="password" name="pin" placeholder="••••••" autofocus maxlength="20">
<button type="submit">Entrar</button>
<div class="err" id="err">__ERR__</div>
</form>
</div></body></html>"""


# ══════════════════════════════════════════════════════════════════
# HISTORY DB — wrappers que ahora apuntan a SQLite (storage.py)
# ══════════════════════════════════════════════════════════════════
def history_read():
    """Compat: lista de snapshots desde SQLite."""
    return storage.bond_snapshots_all(limit=365)

def history_upsert(snapshot):
    """Upsert vía SQLite. Devuelve (lista_completa, err)."""
    total, err = storage.bond_snapshot_upsert(snapshot)
    if err:
        return None, err
    return storage.bond_snapshots_all(limit=365), None

def github_commit_history(history, commit_message=None):
    """Comitea data/history.json al repo via API. Requiere GITHUB_TOKEN."""
    if not GITHUB_TOKEN:
        return False, "GITHUB_TOKEN no configurado"
    api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_HISTORY_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "CERBondAnalyzer/1.0",
    }
    # Get current sha (si existe)
    sha = None
    try:
        req = urllib.request.Request(api + f"?ref={GITHUB_BRANCH}", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            meta = json.loads(r.read().decode("utf-8"))
            sha = meta.get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return False, f"github GET {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:
        return False, f"github GET: {e}"
    # PUT
    content_str = json.dumps(history, ensure_ascii=False, indent=2)
    b64 = base64.b64encode(content_str.encode("utf-8")).decode("ascii")
    msg = commit_message or f"data: snapshot {_dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    body = {"message": msg, "content": b64, "branch": GITHUB_BRANCH}
    if sha:
        body["sha"] = sha
    try:
        req = urllib.request.Request(
            api,
            data=json.dumps(body).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, f"commit ok {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"github PUT {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:
        return False, f"github PUT: {e}"


class Handler(SimpleHTTPRequestHandler):
    def _check_auth(self):
        if not auth_required():
            return True
        token = get_session_from_cookie(self.headers.get("Cookie"))
        return validate_session(token)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8") if isinstance(html, str) else html
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._check_auth() and self.path not in ("/login", "/favicon.ico"):
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return

        if self.path == "/api/prices":
            self._send_json(fetch_prices())
        elif self.path.startswith("/api/depth"):
            self._proxy_depth()
        elif self.path == "/api/pib":
            self._proxy_pib()
        elif self.path == "/api/history":
            self._send_json({"history": history_read()})
        elif self.path == "/api/fx/snapshot":
            try:
                self._send_json(fx_module.get_snapshot())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/api/fx/series"):
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code = (qs.get("code") or [""])[0].strip()
                tf = (qs.get("tf") or ["1D"])[0].strip()
                if not code:
                    self._send_json({"error": "param 'code' requerido"}, 400); return
                self._send_json(fx_module.get_series(code, tf))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/fx/cross":
            try:
                self._send_json(fx_module.get_cross_matrix())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/fx/status":
            try:
                self._send_json(fx_module.get_status())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/api/fx/history"):
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code = (qs.get("code") or [""])[0].strip()
                days = int((qs.get("days") or ["30"])[0])
                mode = (qs.get("mode") or ["auto"])[0].strip()
                if not code:
                    self._send_json({"error": "param 'code' requerido"}, 400); return
                self._send_json(fx_module.get_history(code, days=days, mode=mode))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/storage/compact":
            try:
                bd = storage.bond_compact_old_ticks(keep_raw_days=7, compact_interval_min=15)
                fxd = storage.fx_compact_old_ticks(keep_raw_days=7, compact_interval_min=15)
                bp = storage.bond_prune_ticks(keep_days=60)
                fxp = storage.fx_prune_ticks(keep_days=90)
                self._send_json({
                    "ok": True,
                    "compacted": {"bond": bd, "fx": fxd},
                    "pruned": {"bond": bp, "fx": fxp},
                    "stats": storage.stats(),
                })
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/storage/commit":
            try:
                ok, msg = storage.commit_db_to_github(min_interval_sec=60)
                self._send_json({"ok": ok, "msg": msg, "stats": storage.stats()})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/api/bond/series"):
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                ticker = (qs.get("ticker") or [""])[0].strip()
                days = int((qs.get("days") or ["1"])[0])
                if not ticker:
                    self._send_json({"error": "param 'ticker' requerido"}, 400); return
                since = int((time.time() - days * 86400) * 1000)
                rows = storage.bond_series(ticker, since_ts_ms=since, limit=20000)
                self._send_json({
                    "ticker": ticker, "days": days, "count": len(rows),
                    "points": rows,
                })
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/health":
            self._send_json({
                "status": "ok",
                "sheets": _sheet_service is not None,
                "primary": primary_enabled(),
                "primaryBase": PRIMARY_BASE_URL if primary_enabled() else None,
                "githubCommit": bool(GITHUB_TOKEN),
                "githubRepo": GITHUB_REPO if GITHUB_TOKEN else None,
                "historyEntries": len(history_read()),
                "fx": fx_module.get_status(),
                "storage": storage.stats(),
            })
        elif self.path == "/login":
            self._send_html(LOGIN_HTML.replace("__ERR__", ""))
        elif self.path in ("/", "/index.html"):
            self._send_dashboard()
        else:
            super().do_GET()

    def do_POST(self):
        if not self._check_auth() and self.path not in ("/login",):
            self._send_json({"error": "auth required"}, 401)
            return
        if self.path == "/api/history/snapshot":
            self._save_snapshot()
            return
        if self.path == "/api/bond/ticks":
            self._save_bond_ticks()
            return
        if self.path == "/login":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            pin = ""
            for part in body.split("&"):
                if part.startswith("pin="):
                    pin = part[4:].strip()
            if pin == AUTH_PIN:
                token = create_session()
                self.send_response(302)
                self.send_header("Set-Cookie", f"cer_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}")
                self.send_header("Location", "/")
                self.end_headers()
            else:
                self._send_html(LOGIN_HTML.replace("__ERR__", "PIN incorrecto"), 401)
            return
        self.send_error(404)

    def _proxy_pib(self):
        try:
            url = "https://apis.datos.gob.ar/series/api/series/?ids=8.2_P_2004_T_5&format=json&limit=200"
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "CERBondAnalyzer/2.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            self._send_json(json.loads(data))
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _save_snapshot(self):
        """POST /api/history/snapshot
        Body: {date:'YYYY-MM-DD', ts:int, bonds:{CER:[...], FX:[...], TAMAR:[...]}}
        Persiste en data/history.json + opcionalmente comitea al repo (si hay token).
        """
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            snap = json.loads(raw)
            if not snap.get("date"):
                self._send_json({"error": "date requerido (YYYY-MM-DD)"}, 400)
                return
            history, err = history_upsert(snap)
            if err:
                self._send_json({"error": err}, 400)
                return
            commit_ok, commit_msg = (False, "skipped (no token)")
            if GITHUB_TOKEN:
                # Commit del .db (no del JSON viejo): captura bonds + fx_ticks juntos
                commit_ok, commit_msg = storage.commit_db_to_github(
                    message=f"data: snapshot bonds {snap.get('date')}",
                    min_interval_sec=60,
                )
            self._send_json({
                "ok": True,
                "entries": len(history),
                "commit": {"ok": commit_ok, "msg": commit_msg},
            })
        except json.JSONDecodeError as e:
            self._send_json({"error": f"json invalido: {e}"}, 400)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _save_bond_ticks(self):
        """POST /api/bond/ticks
        Body: {ts:int (ms, opt), rows:[{ticker, bond_type, price, bid, ask, tir, tna, duration}, ...]}
        El cliente computa TIR/duration (toda la lógica financiera vive en JS)
        y nos lo manda cada ~60s. Persistimos en SQLite.
        """
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            body = json.loads(raw)
            rows = body.get("rows") or []
            ts_ms = body.get("ts")
            n = storage.bond_tick_insert_many(rows, ts_ms=ts_ms)
            self._send_json({"ok": True, "inserted": n})
        except json.JSONDecodeError as e:
            self._send_json({"error": f"json invalido: {e}"}, 400)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _proxy_depth(self):
        """GET /api/depth?ticker=X15Y6&settle=24hs|CI&depth=5&symbol=...

        Si `symbol` viene completo (e.g. 'MERV - XMEV - X15Y6 - 24hs') lo usa tal cual.
        Si no, construye el símbolo desde ticker + settle.
        """
        if not primary_enabled():
            self._send_json({
                "error": "Primary API no configurada",
                "detail": "Definí PRIMARY_USERNAME y PRIMARY_PASSWORD como env vars (ver Primary-API.pdf, página 9).",
            }, 503)
            return
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ticker = (qs.get("ticker") or [""])[0].strip()
            settle = (qs.get("settle") or ["24hs"])[0].strip()
            symbol_override = (qs.get("symbol") or [""])[0].strip()
            try:
                depth = max(1, min(5, int((qs.get("depth") or ["5"])[0])))
            except ValueError:
                depth = 5

            symbol = symbol_override or build_primary_symbol(ticker, settle)
            if not symbol:
                self._send_json({"error": "missing ticker or symbol"}, 400)
                return

            data = fetch_primary_depth(symbol, depth=depth)
            data["ticker"] = ticker
            data["settle"] = settle
            self._send_json(data)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _send_dashboard(self):
        if not HTML_FILE or not os.path.isfile(HTML_FILE):
            self.send_error(404, "Dashboard HTML not found. Place CERBondAnalyzer_v7.html in server directory.")
            return
        with open(HTML_FILE, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt, *args):
        if "/api/prices" not in str(args[0]):
            super().log_message(fmt, *args)


def _start_fx_scheduler():
    """Thread daemon: cada hora dispara hourly_tick() (snapshot FX + commit DB)."""
    def loop():
        # Pequeña espera al arranque para que init_sheets + restore terminen
        time.sleep(30)
        while True:
            try:
                result = fx_module.hourly_tick()
                print(f"[fx-scheduler] {result}")
            except Exception as e:
                print(f"[fx-scheduler] error: {e}")
            # Mantenimiento diario (~03 UTC):
            #   - Compactación tiered: ticks > 7d se downsamplean a 1 cada 15 min
            #   - Prune duro: ticks > 60d se borran (último resort)
            # Esto mantiene la .db chica para los commits a GitHub (cap 100 MB).
            try:
                if _dt.datetime.utcnow().hour == 3:
                    bd = storage.bond_compact_old_ticks(keep_raw_days=7, compact_interval_min=15)
                    fxd = storage.fx_compact_old_ticks(keep_raw_days=7, compact_interval_min=15)
                    storage.bond_prune_ticks(keep_days=60)
                    storage.fx_prune_ticks(keep_days=90)
                    print(f"[scheduler] daily compact: bond -{bd} fx -{fxd} rows")
            except Exception as e:
                print(f"[scheduler] compact error: {e}")
            time.sleep(3600)
    t = threading.Thread(target=loop, name="fx-scheduler", daemon=True)
    t.start()
    return t


def main():
    print("\n" + "=" * 60)
    print("  CER Bond Analyzer v7 — Production Server")
    print("=" * 60)

    if auth_required():
        print(f"✓ Auth enabled (PIN set via AUTH_PIN env var)")
    else:
        print("⚠ No AUTH_PIN set — running without authentication")

    # Init SQLite (restore from GitHub if missing, migrate history.json one-shot)
    try:
        storage.init_db()
        print(f"✓ Storage: {storage.stats()}")
    except Exception as e:
        print(f"⚠ Storage init failed: {e}")

    sheets_ok = init_sheets()
    if not sheets_ok:
        print("⚠ Starting without Google Sheets. Manual prices only.\n")

    if primary_enabled():
        print(f"✓ Primary API configurada → {PRIMARY_BASE_URL}")
        print(f"  /api/depth?ticker=X15Y6&settle=24hs&depth=5")
    else:
        print("⚠ Primary API NO configurada — depth-5 deshabilitado.")
        print("  Setea PRIMARY_USERNAME y PRIMARY_PASSWORD para activar /api/depth.")

    if HTML_FILE:
        print(f"✓ Dashboard: {os.path.basename(HTML_FILE)}")
    else:
        print("✗ No dashboard HTML found!")

    os.chdir(SCRIPT_DIR)
    # ThreadingHTTPServer permite atender requests concurrentes (prefetch del book,
    # /api/depth en paralelo, etc.). Con HTTPServer plain las requests se serializaban
    # y las concurrentes recibían body vacío → "Unexpected end of JSON input".
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"\n✓ Server running on port {PORT}")

    # FX scheduler: snapshot horario + commit DB a GitHub
    if fx_module.TWELVEDATA_API_KEY or True:
        _start_fx_scheduler()
        print("✓ FX scheduler iniciado (snapshot + commit cada 1h)")

    is_railway = os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("PORT")
    if not is_railway:
        print(f"  Open http://localhost:{PORT}\n")
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{PORT}")
        except:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
