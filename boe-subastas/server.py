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
def _run_crawl(force: bool = False) -> None:
    with _state_lock:
        if _state["running"]:
            return
        _state["running"] = True
        _state["last_error"] = None
    try:
        days = int(os.getenv("CRAWL_DAYS_BACK", "30"))
        summary = pipeline.crawl_now(days_back=days, force=force)
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


def _start_crawl_thread(force: bool = False) -> bool:
    with _state_lock:
        if _state["running"]:
            return False
    threading.Thread(target=_run_crawl, kwargs={"force": force}, daemon=True).start()
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
def api_crawl(force: int = 0, _: None = Depends(auth)) -> JSONResponse:
    started = _start_crawl_thread(force=bool(force))
    code = 202 if started else 409
    return JSONResponse({"started": started, "force": bool(force)}, status_code=code)


@app.get("/api/debug")
def api_debug(_: None = Depends(auth)) -> dict:
    """Tiefen-Probe des jüngsten Sumarios: Sektionen, wie Justiz-Einträge betitelt
    sind, und ob sich aus einem Justiz-Dokument eine SUB-ID ziehen lässt."""
    import datetime as _dt
    out: dict = {}
    try:
        import boe_api
        from boe_api import BoeApi, _abs
        api = BoeApi()
        day = None
        sumario = None
        for back in range(0, 12):
            d = _dt.date.today() - _dt.timedelta(days=back)
            s = api.fetch_sumario(d)
            if s:
                day, sumario = d, s
                break
        out["day"] = day.isoformat() if day else None
        if not sumario:
            out["note"] = "kein Sumario gefunden"
            return out
        data = sumario.get("data", sumario)
        diarios = boe_api._aslist(boe_api._dig(data, "sumario", "diario"))
        sections, subasta_samples, justice_samples = [], [], []
        first_justice = None
        for diario in diarios:
            for seccion in boe_api._aslist(diario.get("seccion")):
                code = str(seccion.get("codigo")) if isinstance(seccion, dict) else None
                items = list(boe_api._iter_items(seccion))
                n_sub = sum(1 for it in items if "subasta" in (it.get("titulo") or "").lower())
                sections.append({"codigo": code, "n_items": len(items), "n_subasta_title": n_sub})
                for it in items:
                    t = it.get("titulo") or ""
                    if "subasta" in t.lower() and len(subasta_samples) < 6:
                        subasta_samples.append({"codigo": code, "titulo": t[:85]})
                    if code and code.startswith("4"):
                        if len(justice_samples) < 6:
                            justice_samples.append(t[:85])
                        if first_justice is None and it.get("url_xml"):
                            first_justice = it
        out["sections"] = sections
        out["subasta_title_samples"] = subasta_samples
        out["justice_section_samples"] = justice_samples
        if first_justice:
            url = _abs(first_justice.get("url_xml"))
            try:
                txt = api.fetcher.get_text(url)
                m = boe_api._SUB_RE.search(txt)
                out["justice_doc_probe"] = {
                    "titulo": (first_justice.get("titulo") or "")[:85],
                    "sub_id_in_doc": m.group(0) if m else None,
                    "has_subasta_word": "subasta" in txt.lower(),
                }
            except Exception as e:  # noqa: BLE001
                out["justice_doc_probe"] = {"url": url, "error": str(e)}
        else:
            out["justice_doc_probe"] = "keine Sektion-IV-Items gefunden"

        # KRITISCH: ist die Portal-Detailseite vom Server aus erreichbar?
        probe = out.get("justice_doc_probe")
        sub_id = probe.get("sub_id_in_doc") if isinstance(probe, dict) else None
        if sub_id:
            try:
                from portal import Portal
                sub = Portal().get_subasta(sub_id)
                biens = [b for l in sub.lotes for b in l.bienes]
                out["portal_detail"] = {
                    "ok": True, "sub_id": sub_id,
                    "tipo_subasta": getattr(sub, "tipo_subasta", None),
                    "n_bienes": len(biens),
                    "first_bien": ({"municipio": biens[0].municipio,
                                    "provincia": biens[0].provincia,
                                    "ref_catastral": biens[0].referencia_catastral}
                                   if biens else None),
                }
            except Exception as e:  # noqa: BLE001
                out["portal_detail"] = {"ok": False, "sub_id": sub_id, "error": str(e)[:300]}

        # NEU: provinz-gezielte Portal-Suche testen (kalibriert estado).
        try:
            import config as _cfg
            from portal import build_search_url
            _cfg.reload_scope()
            test_prov = (list(_cfg.FOCUS_PROVINCIAS) or ["Madrid"])[0]
            code = _cfg.province_code(test_prov)
            probe = {"province": test_prov, "cod_provincia": code}
            if code:
                for estado in (_cfg.PORTAL_SEARCH_ESTADO, "", "EJ"):
                    try:
                        url = build_search_url(code, estado=estado)
                        ids = api.fetcher.get_text(url)
                        import re as _re
                        found = list(dict.fromkeys(
                            _re.findall(r"idSub=(SUB-[A-Z0-9-]+)", ids)))
                        probe[f"estado='{estado}'"] = {"n_found": len(found),
                                                       "sample": found[:3]}
                    except Exception as e:  # noqa: BLE001
                        probe[f"estado='{estado}'"] = {"error": str(e)[:200]}
            out["province_search_probe"] = probe
        except Exception as e:  # noqa: BLE001
            out["province_search_probe"] = {"error": str(e)[:200]}
    except Exception as e:  # noqa: BLE001
        out["fatal"] = str(e)
    return out
