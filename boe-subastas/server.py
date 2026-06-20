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
import time

from fastapi import Depends, FastAPI, HTTPException, Request, status
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
_state: dict = {"running": False, "cancel": False, "last_run": None, "last_error": None,
                "last_summary": None, "last_scope": None,
                "geocoding": False, "geo": {"done": 0, "total": 0},
                "pdfing": False, "pdf": {"done": 0, "total": 0}}
_LAST_RUN_PATH = config.ROOT / "data" / "last_run.json"
_SCHEDULE_PATH = config.ROOT / "data" / "schedule.json"
_FAV_PATH = config.ROOT / "data" / "favorites.json"


def _load_favorites() -> set:
    try:
        return set(json.loads(_FAV_PATH.read_text("utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def _save_favorites(favs: set) -> None:
    try:
        _FAV_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FAV_PATH.write_text(json.dumps(sorted(favs)), "utf-8")
    except Exception:  # noqa: BLE001
        pass

try:
    from zoneinfo import ZoneInfo
    _MADRID = ZoneInfo("Europe/Madrid")
except Exception:  # noqa: BLE001
    _MADRID = None

_DEFAULT_SCHEDULE = {"enabled": False, "hour": 18, "minute": 0, "last_fired": None}


def _load_schedule() -> dict:
    try:
        cfg = json.loads(_SCHEDULE_PATH.read_text("utf-8"))
        return {**_DEFAULT_SCHEDULE, **cfg}
    except Exception:  # noqa: BLE001
        return dict(_DEFAULT_SCHEDULE)


def _save_schedule(cfg: dict) -> None:
    try:
        _SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SCHEDULE_PATH.write_text(json.dumps(cfg), "utf-8")
    except Exception:  # noqa: BLE001
        pass


def _now_madrid() -> dt.datetime:
    return dt.datetime.now(_MADRID) if _MADRID else dt.datetime.now()


def _next_run_iso(cfg: dict) -> str | None:
    """Nächster Auslösezeitpunkt (Madrid) als ISO-String, oder None wenn aus."""
    if not cfg.get("enabled"):
        return None
    now = _now_madrid()
    target = now.replace(hour=int(cfg["hour"]), minute=int(cfg["minute"]),
                         second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target.isoformat(timespec="minutes")


def _scheduler_loop() -> None:
    """Prüft alle 30 s, ob der geplante tägliche Lauf fällig ist (Madrid-Zeit)."""
    while True:
        try:
            cfg = _load_schedule()
            if cfg.get("enabled"):
                now = _now_madrid()
                hhmm = now.strftime("%H:%M")
                want = f"{int(cfg['hour']):02d}:{int(cfg['minute']):02d}"
                today = now.date().isoformat()
                if hhmm == want and cfg.get("last_fired") != today:
                    with _state_lock:
                        busy = _state["running"] or _state.get("geocoding") or _state.get("pdfing")
                    if not busy:
                        cfg["last_fired"] = today
                        _save_schedule(cfg)
                        threading.Thread(target=_run_crawl, kwargs={"full": True},
                                         daemon=True).start()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(30)


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
    # Eigener Tages-Scheduler (Uhrzeit-basiert, Madrid-Zeit) — immer aktiv,
    # feuert aber nur, wenn in schedule.json enabled=true gesetzt ist.
    threading.Thread(target=_scheduler_loop, daemon=True).start()


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
def _run_crawl(force: bool = False, full: bool = False) -> None:
    with _state_lock:
        if _state["running"]:
            return
        _state["running"] = True
        _state["cancel"] = False          # frischer Lauf → Stopp-Flag zurücksetzen
        _state["last_error"] = None
    try:
        days = int(os.getenv("CRAWL_DAYS_BACK", "30"))
        # Begrenzung pro Lauf, damit ein Crawl nie ewig läuft. 0 = unbegrenzt.
        # Ein geplanter Lauf (full=True) läuft serverseitig komplett durch.
        lim = None if full else (int(os.getenv("ENRICH_LIMIT", "40")) or None)
        # PDFs standardmäßig AUS: teuerster Schritt, liefert kaum Mehrwert
        # (Fläche kommt aus dem Kataster). Bei Bedarf ENRICH_PDF=1 setzen.
        with_pdf = os.getenv("ENRICH_PDF", "0") == "1"
        summary = pipeline.crawl_now(days_back=days, limit=lim, force=force,
                                     with_pdf=with_pdf,
                                     should_cancel=lambda: _state.get("cancel"))
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


def _run_geocode() -> None:
    """Nur die Bienes ohne Koordinaten nachträglich verorten (ohne Crawl)."""
    with _state_lock:
        if _state["running"] or _state.get("geocoding"):
            return
        _state["geocoding"] = True
        _state["cancel"] = False
        _state["geo"] = {"done": 0, "total": 0}
    try:
        from geocode import geocode_pending

        def _prog(done: int, total: int) -> None:
            with _state_lock:
                _state["geo"] = {"done": done, "total": total}

        n = geocode_pending(limit=None, on_progress=_prog,
                            should_cancel=lambda: _state.get("cancel"))
        with _state_lock:
            _state["geo"]["located"] = n
        # n_rows/n_sub im last_run aktualisieren, damit die Karte frische Daten zieht
    except Exception as exc:  # noqa: BLE001
        with _state_lock:
            _state["last_error"] = str(exc)
    finally:
        with _state_lock:
            _state["geocoding"] = False


def _start_geocode_thread() -> bool:
    with _state_lock:
        if _state["running"] or _state.get("geocoding"):
            return False
    threading.Thread(target=_run_geocode, daemon=True).start()
    return True


def _run_pdf_backfill() -> None:
    """Valoración-PDFs für Bienes ohne Fläche nachlesen (ohne Crawl)."""
    with _state_lock:
        if _state["running"] or _state.get("geocoding") or _state.get("pdfing"):
            return
        _state["pdfing"] = True
        _state["cancel"] = False
        _state["pdf"] = {"done": 0, "total": 0}
    try:
        from pdf_extract import backfill_superficie_pending

        def _prog(done: int, total: int) -> None:
            with _state_lock:
                _state["pdf"] = {"done": done, "total": total}

        n = backfill_superficie_pending(limit=None, on_progress=_prog,
                                        should_cancel=lambda: _state.get("cancel"))
        with _state_lock:
            _state["pdf"]["filled"] = n
    except Exception as exc:  # noqa: BLE001
        with _state_lock:
            _state["last_error"] = str(exc)
    finally:
        with _state_lock:
            _state["pdfing"] = False


def _start_pdf_thread() -> bool:
    with _state_lock:
        if _state["running"] or _state.get("geocoding") or _state.get("pdfing"):
            return False
    threading.Thread(target=_run_pdf_backfill, daemon=True).start()
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
    st["progress"] = dict(pipeline.PROGRESS)   # Live-Fortschritt des laufenden Laufs
    st["cancelling"] = bool(_state.get("cancel") and _state.get("running"))
    try:
        from store import Store
        _s = Store()
        st["pending"] = _s.pending_count()
        st["sub_total"] = _s.total_sub_ids()
        st["coverage"] = _s.coverage()
        _s.close()
    except Exception:  # noqa: BLE001
        st["pending"] = None
        st["sub_total"] = None
        st["coverage"] = None
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


@app.post("/api/geocode")
def api_geocode(_: None = Depends(auth)) -> JSONResponse:
    """Bienes ohne Koordinaten nachträglich verorten (Photon/Nominatim)."""
    started = _start_geocode_thread()
    code = 202 if started else 409
    return JSONResponse({"started": started}, status_code=code)


@app.post("/api/pdf-backfill")
def api_pdf_backfill(_: None = Depends(auth)) -> JSONResponse:
    """Valoración-PDFs für Bienes ohne Fläche nachlesen (ohne Crawl)."""
    started = _start_pdf_thread()
    code = 202 if started else 409
    return JSONResponse({"started": started}, status_code=code)


@app.get("/api/favorites")
def api_favorites_get(_: None = Depends(auth)) -> JSONResponse:
    return JSONResponse({"favorites": sorted(_load_favorites())})


@app.post("/api/favorites")
async def api_favorites_set(request: Request, _: None = Depends(auth)) -> JSONResponse:
    body = await request.json()
    sub_id = str(body.get("sub_id") or "").strip()
    if not sub_id:
        return JSONResponse({"error": "sub_id required"}, status_code=400)
    favs = _load_favorites()
    if body.get("fav"):
        favs.add(sub_id)
    else:
        favs.discard(sub_id)
    _save_favorites(favs)
    return JSONResponse({"favorites": sorted(favs)})


@app.get("/api/schedule")
def api_schedule_get(_: None = Depends(auth)) -> JSONResponse:
    cfg = _load_schedule()
    return JSONResponse({
        "enabled": bool(cfg.get("enabled")),
        "time": f"{int(cfg['hour']):02d}:{int(cfg['minute']):02d}",
        "next_run": _next_run_iso(cfg),
        "tz": "Europe/Madrid",
    })


@app.post("/api/schedule")
async def api_schedule_set(request: Request, _: None = Depends(auth)) -> JSONResponse:
    body = await request.json()
    cfg = _load_schedule()
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    t = body.get("time")
    if isinstance(t, str) and ":" in t:
        try:
            hh, mm = t.split(":", 1)
            hh, mm = int(hh), int(mm)
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                cfg["hour"], cfg["minute"] = hh, mm
            else:
                return JSONResponse({"error": "time out of range"}, status_code=400)
        except ValueError:
            return JSONResponse({"error": "bad time format, use HH:MM"}, status_code=400)
    # bei Änderung neu „scharf schalten": last_fired zurücksetzen
    cfg["last_fired"] = None
    _save_schedule(cfg)
    return JSONResponse({
        "enabled": bool(cfg.get("enabled")),
        "time": f"{int(cfg['hour']):02d}:{int(cfg['minute']):02d}",
        "next_run": _next_run_iso(cfg),
    })


@app.post("/api/stop")
def api_stop(_: None = Depends(auth)) -> JSONResponse:
    """Laufenden Crawl ODER Geokodierung kooperativ abbrechen."""
    with _state_lock:
        active = _state["running"] or _state.get("geocoding")
        if active:
            _state["cancel"] = True
    return JSONResponse({"stopping": bool(active)})


@app.post("/api/reset")
def api_reset(_: None = Depends(auth)) -> JSONResponse:
    """Alle gesammelten Daten löschen (für einen sauberen Neustart mit einem Scope)."""
    with _state_lock:
        if _state["running"]:
            return JSONResponse({"error": "crawl läuft – erst stoppen"}, status_code=409)
    try:
        from store import Store
        s = Store()
        s.reset()
        s.close()
        for p in (config.CRAWL_STATE_PATH, _LAST_RUN_PATH):
            try:
                p.unlink()
            except Exception:  # noqa: BLE001
                pass
        pipeline.PROGRESS.update(phase="idle", done=0, total=0, note="")
        with _state_lock:
            _state["last_run"] = None
            _state["last_summary"] = None
            _state["last_scope"] = None
        return JSONResponse({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)


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

        # Anreicherung prüfen: erreichen wir Catastro & Nominatim vom Server?
        try:
            rc = "9368704YH1896N0010MX"  # Alcoy (bekannt gültig)
            from catastro import CatastroClient
            cc = CatastroClient()
            try:
                datos = cc.datos(rc)
            except Exception as e:  # noqa: BLE001
                datos = {"error": str(e)[:200]}
            try:
                coords = cc.coords(rc)
            except Exception as e:  # noqa: BLE001
                coords = {"error": str(e)[:200]}
            out["catastro_probe"] = {"rc": rc, "datos": datos, "coords": coords}
        except Exception as e:  # noqa: BLE001
            out["catastro_probe"] = {"error": str(e)[:200]}
        try:
            import requests as _rq
            import config as _cfg
            from geocode import geocode_address
            s = _rq.Session()
            out["geocode_probe"] = {
                "alcoy_alicante": geocode_address(["Alcoy", "Alicante", "España"], session=s),
                "marbella_malaga": geocode_address(["Marbella", "Málaga", "España"], session=s),
                "user_agent": _cfg.USER_AGENT,
            }
        except Exception as e:  # noqa: BLE001
            out["geocode_probe"] = {"error": str(e)[:200]}
    except Exception as e:  # noqa: BLE001
        out["fatal"] = str(e)
    return out
