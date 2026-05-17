"""
storage.py — Persistencia SQLite + sync GitHub para CER Bond Analyzer
======================================================================

Reemplaza data/history.json por data/history.db (SQLite). Cubre:
  - bond_snapshots: snapshots diarios (CER/FX/TAMAR) — antes en history.json
  - fx_ticks:       cada refresh de TwelveData → un tick por moneda
  - fx_daily:       agregados diarios (open/high/low/close) por moneda
  - kv:             metadatos (last_github_commit_ts, etc)

Durabilidad cross-deploy (Render free tier es efímero):
  - Al arrancar, si data/history.db no existe localmente pero hay GITHUB_TOKEN,
    descarga el .db más reciente del repo (data/history.db) y lo deja en disco.
  - commit_db_to_github() empaqueta el .db (base64) y lo PUT-ea al repo.
  - Llamarlo cada N minutos (o tras cada snapshot relevante).
"""

import os
import json
import time
import sqlite3
import base64
import threading
import urllib.request
import urllib.error
import datetime as _dt

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "data", "history.db")
LEGACY_JSON_PATH = os.path.join(SCRIPT_DIR, "data", "history.json")

# GitHub sync config (compartido con server.py)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "maximodubois/cer-bond-analyzer")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_DB_PATH = "data/history.db"

_DB_LOCK = threading.RLock()
_COMMIT_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════════
# CONEXION + SCHEMA
# ══════════════════════════════════════════════════════════════════
def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bond_snapshots (
    date    TEXT PRIMARY KEY,
    ts      INTEGER NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fx_ticks (
    ts          INTEGER NOT NULL,
    code        TEXT NOT NULL,
    price       REAL,
    prev_close  REAL,
    pct_day     REAL,
    source      TEXT,
    PRIMARY KEY (code, ts)
);
CREATE INDEX IF NOT EXISTS idx_fx_ticks_code_ts ON fx_ticks(code, ts);
CREATE INDEX IF NOT EXISTS idx_fx_ticks_ts ON fx_ticks(ts);

CREATE TABLE IF NOT EXISTS fx_daily (
    date    TEXT NOT NULL,
    code    TEXT NOT NULL,
    open    REAL,
    high    REAL,
    low     REAL,
    close   REAL,
    ticks   INTEGER DEFAULT 0,
    PRIMARY KEY (date, code)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS bond_ticks (
    ts          INTEGER NOT NULL,
    ticker      TEXT NOT NULL,
    bond_type   TEXT,
    price       REAL,
    bid         REAL,
    ask         REAL,
    tir         REAL,
    tna         REAL,
    duration    REAL,
    PRIMARY KEY (ticker, ts)
);
CREATE INDEX IF NOT EXISTS idx_bond_ticks_ticker_ts ON bond_ticks(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_bond_ticks_ts ON bond_ticks(ts);
"""


def init_db():
    """Crea schema. Restaura desde GitHub si el archivo local no existe."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if not os.path.exists(DB_PATH) and GITHUB_TOKEN:
        try:
            ok, msg = restore_db_from_github()
            print(f"[storage] restore from github: {ok} — {msg}")
        except Exception as e:
            print(f"[storage] warn: restore failed: {e}")
    with _DB_LOCK, _conn() as c:
        c.executescript(SCHEMA_SQL)
    _migrate_legacy_history_json()


def _migrate_legacy_history_json():
    """Si existe data/history.json y no migramos antes, lo importamos a bond_snapshots."""
    if not os.path.exists(LEGACY_JSON_PATH):
        return
    if kv_get("migrated_history_json") == "1":
        return
    try:
        with open(LEGACY_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return
        for snap in data:
            d = snap.get("date")
            if not d:
                continue
            bond_snapshot_upsert(snap)
        kv_set("migrated_history_json", "1")
        print(f"[storage] migrated {len(data)} snapshots from history.json")
    except Exception as e:
        print(f"[storage] migration error: {e}")


# ══════════════════════════════════════════════════════════════════
# KV (metadatos)
# ══════════════════════════════════════════════════════════════════
def kv_get(key, default=None):
    with _DB_LOCK, _conn() as c:
        r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def kv_set(key, value):
    with _DB_LOCK, _conn() as c:
        c.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


# ══════════════════════════════════════════════════════════════════
# BOND SNAPSHOTS (reemplaza history.json)
# ══════════════════════════════════════════════════════════════════
def bond_snapshot_upsert(snapshot):
    """Inserta o reemplaza el snapshot del día. Retorna (count_total, None) o (None, err)."""
    date = snapshot.get("date")
    if not date:
        return None, "snapshot.date requerido"
    ts = int(snapshot.get("ts") or time.time() * 1000)
    payload = json.dumps(snapshot, ensure_ascii=False)
    with _DB_LOCK, _conn() as c:
        c.execute(
            "INSERT INTO bond_snapshots(date, ts, payload) VALUES(?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET ts=excluded.ts, payload=excluded.payload",
            (date, ts, payload),
        )
        total = c.execute("SELECT COUNT(*) AS n FROM bond_snapshots").fetchone()["n"]
    return total, None


def bond_snapshots_all(limit=365):
    """Devuelve la lista completa de snapshots (más recientes últimos), formato compatible."""
    with _DB_LOCK, _conn() as c:
        rows = c.execute(
            "SELECT payload FROM bond_snapshots ORDER BY date ASC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except json.JSONDecodeError:
            continue
    return out


# ══════════════════════════════════════════════════════════════════
# FX TICKS
# ══════════════════════════════════════════════════════════════════
def fx_tick_insert(code, price, prev_close=None, pct_day=None, source=None, ts_ms=None):
    """Inserta un tick. Si ya existe (code,ts), lo ignora silenciosamente."""
    if price is None:
        return False
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
    with _DB_LOCK, _conn() as c:
        try:
            c.execute(
                "INSERT INTO fx_ticks(ts, code, price, prev_close, pct_day, source) "
                "VALUES(?,?,?,?,?,?)",
                (ts, code, float(price),
                 float(prev_close) if prev_close is not None else None,
                 float(pct_day) if pct_day is not None else None,
                 source),
            )
            _fx_daily_touch(c, code, ts, float(price))
            return True
        except sqlite3.IntegrityError:
            return False


def _fx_daily_touch(c, code, ts_ms, price):
    """Update agregado diario (UTC date)."""
    date = _dt.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
    r = c.execute(
        "SELECT open, high, low, ticks FROM fx_daily WHERE date=? AND code=?",
        (date, code),
    ).fetchone()
    if r is None:
        c.execute(
            "INSERT INTO fx_daily(date, code, open, high, low, close, ticks) "
            "VALUES(?,?,?,?,?,?,1)",
            (date, code, price, price, price, price),
        )
    else:
        hi = max(r["high"], price) if r["high"] is not None else price
        lo = min(r["low"], price) if r["low"] is not None else price
        c.execute(
            "UPDATE fx_daily SET high=?, low=?, close=?, ticks=ticks+1 "
            "WHERE date=? AND code=?",
            (hi, lo, price, date, code),
        )


def fx_ticks_range(code, since_ts_ms=None, until_ts_ms=None, limit=5000):
    """Lista ticks [(ts, price, pct_day), ...] ordenado ascendente."""
    q = "SELECT ts, price, pct_day FROM fx_ticks WHERE code=?"
    args = [code]
    if since_ts_ms is not None:
        q += " AND ts >= ?"
        args.append(int(since_ts_ms))
    if until_ts_ms is not None:
        q += " AND ts <= ?"
        args.append(int(until_ts_ms))
    q += " ORDER BY ts ASC LIMIT ?"
    args.append(int(limit))
    with _DB_LOCK, _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def fx_latest(code):
    """Último tick conocido de una moneda."""
    with _DB_LOCK, _conn() as c:
        r = c.execute(
            "SELECT ts, price, prev_close, pct_day, source FROM fx_ticks "
            "WHERE code=? ORDER BY ts DESC LIMIT 1",
            (code,),
        ).fetchone()
        return dict(r) if r else None


def fx_daily_series(code, days=90):
    """Serie diaria (open/high/low/close) últimos N días."""
    with _DB_LOCK, _conn() as c:
        rows = c.execute(
            "SELECT date, open, high, low, close, ticks FROM fx_daily "
            "WHERE code=? ORDER BY date DESC LIMIT ?",
            (code, int(days)),
        ).fetchall()
    return list(reversed([dict(r) for r in rows]))


def fx_prune_ticks(keep_days=90):
    """Borra ticks más viejos que keep_days (los diarios quedan). Devuelve cuántas filas se borraron."""
    cutoff = int((time.time() - keep_days * 86400) * 1000)
    with _DB_LOCK, _conn() as c:
        c.execute("DELETE FROM fx_ticks WHERE ts < ?", (cutoff,))
        return c.execute("SELECT changes()").fetchone()[0]


# ══════════════════════════════════════════════════════════════════
# BOND TICKS — snapshot 1-min de precio/TIR por bono
# ══════════════════════════════════════════════════════════════════
def bond_tick_insert_many(rows, ts_ms=None):
    """Inserta batch de ticks. rows = lista de dicts con:
       ticker, bond_type, price, bid, ask, tir, tna, duration."""
    if not rows:
        return 0
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
    n = 0
    with _DB_LOCK, _conn() as c:
        for r in rows:
            tk = r.get("ticker")
            if not tk:
                continue
            try:
                c.execute(
                    "INSERT INTO bond_ticks(ts, ticker, bond_type, price, bid, ask, tir, tna, duration) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        ts, tk, r.get("bond_type"),
                        _f(r.get("price")), _f(r.get("bid")), _f(r.get("ask")),
                        _f(r.get("tir")), _f(r.get("tna")), _f(r.get("duration")),
                    ),
                )
                n += 1
            except sqlite3.IntegrityError:
                pass  # duplicado (mismo ticker+ts)
    return n


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def bond_series(ticker, since_ts_ms=None, limit=10000):
    """Devuelve serie de ticks para un bono ordenada por ts asc."""
    q = "SELECT ts, price, bid, ask, tir, tna, duration FROM bond_ticks WHERE ticker=?"
    args = [ticker]
    if since_ts_ms is not None:
        q += " AND ts >= ?"
        args.append(int(since_ts_ms))
    q += " ORDER BY ts ASC LIMIT ?"
    args.append(int(limit))
    with _DB_LOCK, _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def bond_prune_ticks(keep_days=60):
    """Borra bond_ticks más viejos que keep_days. Devuelve cuántas filas se borraron."""
    cutoff = int((time.time() - keep_days * 86400) * 1000)
    with _DB_LOCK, _conn() as c:
        c.execute("DELETE FROM bond_ticks WHERE ts < ?", (cutoff,))
        return c.execute("SELECT changes()").fetchone()[0]


def bond_compact_old_ticks(keep_raw_days=7, compact_interval_min=15):
    """Downsampling tiered:
    - Ticks más recientes que `keep_raw_days`: se dejan intactos (1-min).
    - Ticks más viejos: se compactan dejando UNO por bucket de
      `compact_interval_min` por ticker (el último del bucket).

    Vuelve idempotente: corre una vez al día y mantiene la DB chica.
    Devuelve cuántas filas se borraron.
    """
    cutoff_ms = int((time.time() - keep_raw_days * 86400) * 1000)
    bucket_ms = int(compact_interval_min * 60 * 1000)
    with _DB_LOCK, _conn() as c:
        # Borra todos los ticks viejos EXCEPTO el (ticker, MAX(ts)) por bucket.
        c.execute(
            """
            DELETE FROM bond_ticks
            WHERE ts < ?
              AND (ticker, ts) NOT IN (
                SELECT ticker, MAX(ts)
                FROM bond_ticks
                WHERE ts < ?
                GROUP BY ticker, (ts / ?)
              )
            """,
            (cutoff_ms, cutoff_ms, bucket_ms),
        )
        deleted = c.execute("SELECT changes()").fetchone()[0]
        # VACUUM async (en autocommit con WAL no se puede VACUUM dentro de transacción)
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
    return deleted


def fx_compact_old_ticks(keep_raw_days=7, compact_interval_min=15):
    """Mismo downsampling tiered pero para fx_ticks."""
    cutoff_ms = int((time.time() - keep_raw_days * 86400) * 1000)
    bucket_ms = int(compact_interval_min * 60 * 1000)
    with _DB_LOCK, _conn() as c:
        c.execute(
            """
            DELETE FROM fx_ticks
            WHERE ts < ?
              AND (code, ts) NOT IN (
                SELECT code, MAX(ts)
                FROM fx_ticks
                WHERE ts < ?
                GROUP BY code, (ts / ?)
              )
            """,
            (cutoff_ms, cutoff_ms, bucket_ms),
        )
        deleted = c.execute("SELECT changes()").fetchone()[0]
    return deleted


# ══════════════════════════════════════════════════════════════════
# GITHUB SYNC del .db
# ══════════════════════════════════════════════════════════════════
def _gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "CERBondAnalyzer/3.0",
    }


def _gh_get_file_meta():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_DB_PATH}?ref={GITHUB_BRANCH}"
    req = urllib.request.Request(url, headers=_gh_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def restore_db_from_github():
    """Descarga data/history.db del repo y lo deja en DB_PATH. Idempotente."""
    if not GITHUB_TOKEN:
        return False, "no token"
    meta = _gh_get_file_meta()
    if not meta:
        return False, "no remote db"
    # Para archivos >1MB GitHub no incluye 'content' inline → usar download_url
    download_url = meta.get("download_url")
    if not download_url:
        return False, "no download_url"
    req = urllib.request.Request(download_url, headers=_gh_headers())
    with urllib.request.urlopen(req, timeout=60) as r:
        blob = r.read()
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    tmp = DB_PATH + ".restore"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, DB_PATH)
    return True, f"{len(blob)} bytes"


def commit_db_to_github(message=None, min_interval_sec=300):
    """PUT data/history.db al repo. Respeta min_interval_sec entre commits."""
    if not GITHUB_TOKEN:
        return False, "no token"
    if not os.path.exists(DB_PATH):
        return False, "no local db"
    with _COMMIT_LOCK:
        last = kv_get("last_db_commit_ts")
        now = int(time.time())
        if last:
            try:
                if now - int(last) < min_interval_sec:
                    return False, f"throttled ({now - int(last)}s < {min_interval_sec}s)"
            except ValueError:
                pass
        # WAL checkpoint para que el .db sea standalone antes de leerlo
        try:
            with _DB_LOCK, _conn() as c:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        with open(DB_PATH, "rb") as f:
            blob = f.read()
        b64 = base64.b64encode(blob).decode("ascii")

        sha = None
        try:
            meta = _gh_get_file_meta()
            if meta:
                sha = meta.get("sha")
        except Exception:
            pass

        msg = message or f"data: history.db {_dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        body = {"message": msg, "content": b64, "branch": GITHUB_BRANCH}
        if sha:
            body["sha"] = sha
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_DB_PATH}"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={**_gh_headers(), "Content-Type": "application/json"},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                kv_set("last_db_commit_ts", str(now))
                return True, f"ok {r.status} ({len(blob)} bytes)"
        except urllib.error.HTTPError as e:
            return False, f"github PUT {e.code}: {e.read().decode('utf-8','replace')[:200]}"


# ══════════════════════════════════════════════════════════════════
# DIAGNOSTICO
# ══════════════════════════════════════════════════════════════════
def stats():
    with _DB_LOCK, _conn() as c:
        bs = c.execute("SELECT COUNT(*) AS n FROM bond_snapshots").fetchone()["n"]
        fx = c.execute("SELECT COUNT(*) AS n FROM fx_ticks").fetchone()["n"]
        fxd = c.execute("SELECT COUNT(*) AS n FROM fx_daily").fetchone()["n"]
    size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    return {
        "db_path": DB_PATH,
        "db_size_bytes": size,
        "bond_snapshots": bs,
        "fx_ticks": fx,
        "fx_daily_rows": fxd,
        "last_db_commit_ts": kv_get("last_db_commit_ts"),
        "github_repo": GITHUB_REPO if GITHUB_TOKEN else None,
    }
