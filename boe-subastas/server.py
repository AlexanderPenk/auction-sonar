"""Live-Server für Auction Sonar.

Liefert das Dashboard live aus der SQLite, bietet eine kleine JSON-API, einen
geschützten „Crawl now"-Trigger und einen Hintergrund-Scheduler für regelmäßige
Läufe. Alles hinter HTTP-Basic-Auth.

Start (lokal):
    pip install -r requirements.txt
    uvicorn server:app --host 0.0.0.0 --port 8000

Umgebungsvariablen:
    AUTH_USER, AUTH_PASS         Basic-Auth (wenn leer: offen — nur lokal nutzen!)
    CRAWL_INTERVAL_HOURS=24      Intervall des automatischen Laufs (0 = aus)
    CRAWL_DAYS_BACK=30           Zeitfenster der API-Discovery
    CRAWL_ON_START=0             1 = beim Start einmal crawlen
    IDEALISTA_API_KEY/SECRET     optional, für Marktwert-Schätzung
"""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import threading

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import config
import export
import pipeline
from store import Store

app = FastAPI(title="Auction Sonar", docs_url=None, redoc_url=None)
security = HTTPBasic(auto_error=False)

# ── Zustand des Crawl-Jobs (mit Persistenz) ─────────────────────────────
_state_lock = threading.Lock()
_state: dict = {"running": False, "last_run": None, "last_error": None,
                "last_summary": None, "last_scope": None}
_LAST_RUN_PATH = config.ROOT / "data" / "last_run.json"


def _load_last_run() -> None:
    if not _LAST_RUN_PATH.exists():
        return
    try:
        d = json.loads(_LAST_RUN_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    with _state_lock:
        for k in ("last_run", "last_scope", "last_summary"):
            if k in d:
                _state[k] = d[k]


def _save_last_run(scope: dict, summary: dict, rows: int, subastas: int) -> None:
    payload = {
        "last_run": dt.datetime.now().isoformat(timespec="seconds"),
        "last_scope": scope,
        "last_summary": summary,
        "rows": rows,
        "subastas": subastas,
    }
    try:
        _LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LAST_RUN_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    except OSError:
        pass
    with _state_lock:
        _state["last_run"] = payload["last_run"]
        _state["last_scope"] = scope
        _state["last_summary"] = summary


def _ensure_schema() -> None:
    """Legt leere Tabellen an, falls noch keine DB existiert."""
    Store().close()


@app.on_event("startup")
def _startup() -> None:
    _ensure_schema()
    _load_last_run()
    # Auto-Scheduler ist standardmäßig AUS (0). Wer wöchentlich automatisch
    # crawlen will, setzt CRAWL_INTERVAL_HOURS=168.
    hours = float(os.getenv("CRAWL_INTERVAL_HOURS", "0") or 0)
    if hours > 0:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            sch = BackgroundScheduler(daemon=True)
            sch.add_job(_run_crawl, "interval", hours=hours, id="crawl",
                        next_run_time=dt.datetime.now() + dt.timedelta(minutes=2))
            sch.start()
            app.state.scheduler = sch
        except Exception:  # noqa: BLE001
            pass
    if os.getenv("CRAWL_ON_START", "0") == "1":
        _start_crawl_thread()


# ── Auth ────────────────────────────────────────────────────────────────
def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
    user = os.getenv("AUTH_USER", "")
    pw = os.getenv("AUTH_PASS", "")
    if not user and not pw:
        return  # offen (nur für lokale Nutzung gedacht)
    ok = bool(creds) and secrets.compare_digest(creds.username, user) and \
        secrets.compare_digest(creds.password, pw)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Auth required",
                            headers={"WWW-Authenticate": "Basic"})


# ── Crawl-Ausführung ────────────────────────────────────────────────────
def _run_crawl() -> None:
    with _state_lock:
        if _state["running"]:
            return
        _state["running"] = True
        _state["last_error"] = None
    try:
        days = int(os.getenv("CRAWL_DAYS_BACK", "30"))
        summary = pipeline.crawl_now(days_back=days)
        # tatsächlich verwendeten Suchraum festhalten (config nach reload_scope)
        scope = {
            "focus_provincias": list(config.FOCUS_PROVINCIAS),
            "focus_tipos": list(config.FOCUS_TIPOS),
            "focus_subtipos": list(config.FOCUS_SUBTIPOS),
            "search_urls": list(config.SEARCH_URLS),
        }
        try:
            rows = export._rows_with_docs()
            n_rows, n_sub = len(rows), len({r["sub_id"] for r in rows})
        except Exception:  # noqa: BLE001
            n_rows = n_sub = 0
        _save_last_run(scope, summary, n_rows, n_sub)
    except Exception as exc:  # noqa: BLE001
        with _state_lock:
            _state["last_error"] = str(exc)
    finally:
        with _state_lock:
            _state["running"] = False


def _start_crawl_thread() -> bool:
    with _state_lock:
        if _state["running"]:
            return False
    threading.Thread(target=_run_crawl, daemon=True).start()
    return True


# ── Routen ──────────────────────────────────────────────────────────────
@app.get("/healthz")
def healthz() -> dict:
    """Offener Health-Check (ohne Auth) für den Hosting-Provider."""
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def dashboard(_: None = Depends(auth)) -> HTMLResponse:
    try:
        html = export.build_html()
    except Exception:  # noqa: BLE001 - leere/fehlende DB
        tmpl = (config.ROOT / "dashboard_template.html").read_text(encoding="utf-8")
        html = tmpl.replace("/*__DATA__*/[]", "[]").replace("/*__SCOPE__*/{}",
                                                            json.dumps(export._current_scope()))
    return HTMLResponse(html)


@app.get("/api/data")
def api_data(_: None = Depends(auth)) -> JSONResponse:
    try:
        return JSONResponse(export._rows_with_docs())
    except Exception:  # noqa: BLE001
        return JSONResponse([])


@app.get("/api/status")
def api_status(_: None = Depends(auth)) -> dict:
    with _state_lock:
        st = dict(_state)
    try:
        rows = export._rows_with_docs()
        st["rows"] = len(rows)
        st["subastas"] = len({r["sub_id"] for r in rows})
    except Exception:  # noqa: BLE001
        st["rows"] = 0
        st["subastas"] = 0
    st["scheduler_hours"] = float(os.getenv("CRAWL_INTERVAL_HOURS", "0") or 0)
    st["days_since"] = None
    if st.get("last_run"):
        try:
            delta = dt.datetime.now() - dt.datetime.fromisoformat(st["last_run"])
            st["days_since"] = round(delta.total_seconds() / 86400, 1)
        except ValueError:
            pass
    return st


@app.get("/api/scope")
def api_scope_get(_: None = Depends(auth)) -> dict:
    return export._current_scope()


@app.post("/api/scope")
def api_scope_set(scope: dict, _: None = Depends(auth)) -> dict:
    allowed = {"focus_provincias", "focus_tipos", "focus_subtipos", "search_urls"}
    clean = {k: scope.get(k, []) for k in allowed}
    (config.ROOT / "scope.json").write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    config.reload_scope()
    return {"saved": True, "scope": clean}


@app.post("/api/crawl")
def api_crawl(_: None = Depends(auth)) -> JSONResponse:
    started = _start_crawl_thread()
    code = 202 if started else 409
    return JSONResponse({"started": started}, status_code=code)
