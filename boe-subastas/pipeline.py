"""Orchestrierung: bindet API-Discovery, Portal-Enrichment, PDF und Store zusammen."""
from __future__ import annotations

import datetime as dt
import json
import logging

import config
from boe_api import BoeApi
from fetcher import Fetcher
from pdf_extract import PdfExtractor
from portal import Portal
from store import Store, append_raw

log = logging.getLogger("pipeline")

# Live-Fortschritt des laufenden Crawls (vom Server über /api/status sichtbar).
PROGRESS: dict = {"phase": "idle", "done": 0, "total": 0, "note": ""}


def _progress(phase: str, done: int = 0, total: int = 0, note: str = "") -> None:
    PROGRESS.update(phase=phase, done=done, total=total, note=note)


def discover(start: dt.date, end: dt.date, store: Store,
             focus_codes: set[str] | None = None, should_cancel=None) -> int:
    """Stufe 1: offizielle API nach Subasta-Anuncios absuchen, SUB-IDs ableiten.

    Wenn focus_codes gesetzt ist, werden Einträge VOR dem Dokument-/Portal-Abruf
    anhand des Gerichtsorts (Titel → Provinz) verworfen, sofern sie sicher in
    einer anderen Provinz liegen. Unbekannte/mehrdeutige Orte bleiben drin, damit
    nie eine echte Versteigerung der Zielprovinz verloren geht (Exaktfilter folgt
    beim Enrich über die echte Portal-Provinz)."""
    api = BoeApi()
    records = api.find_range(start, end, should_cancel=should_cancel)
    if should_cancel and should_cancel():
        return 0
    if focus_codes:
        kept = []
        for r in records:
            code = config.town_province_code(r.get("titulo") or "")
            if code is None or code in focus_codes:
                kept.append(r)
        log.info("Provinz-Vorfilter: %s von %s Einträgen behalten", len(kept), len(records))
        records = kept
    # Bereits bekannte Anuncios nicht erneut auflösen (macht Wiederholungsläufe billig).
    ids = [r.get("boe_anuncio_id") for r in records if r.get("boe_anuncio_id")]
    known = store.known_anuncio_ids(ids)
    new_records = [r for r in records if r.get("boe_anuncio_id") not in known]
    log.info("Discovery: %s neue Anuncios (von %s), %s bereits bekannt",
             len(new_records), len(records), len(known))
    # SUB-ID nachladen, wo sie nicht im Titel stand (aus dem Anuncio-XML).
    total = len(new_records)
    _progress("discover", 0, total, "SUB-IDs ableiten")
    cancelled = False
    for i, r in enumerate(new_records, 1):
        if should_cancel and should_cancel():
            log.info("Discovery abgebrochen (Stopp angefordert)")
            cancelled = True
            break
        if not r.get("sub_id") and r.get("url_xml"):
            r["sub_id"] = api.sub_id_from_anuncio_xml(r["url_xml"])
        if i % 5 == 0 or i == total:
            _progress("discover", i, total, "SUB-IDs ableiten")
    if cancelled:
        # Lauf abgebrochen: diese Charge nicht persistieren, damit ein späterer
        # Lauf sie sauber (mit aufgelösten SUB-IDs) neu erfassen kann.
        return 0
    append_raw("anuncios", None, new_records)
    saved = store.save_anuncios(new_records)
    with_sub = sum(1 for r in new_records if r.get("sub_id"))
    log.info("Discovery: %s Anuncios gespeichert, davon %s mit SUB-ID", saved, with_sub)
    return saved


def discover_via_search(search_urls, store: Store, *, max_pages: int = 50) -> int:
    """Provinz-gezielte Discovery: Portal-Such-URLs durchblättern, SUB-IDs merken."""
    portal = Portal()
    all_ids: list[str] = []
    for url in search_urls:
        all_ids.extend(portal.search_sub_ids(url, max_pages=max_pages))
    all_ids = list(dict.fromkeys(all_ids))
    saved = store.save_sub_ids(all_ids)
    log.info("Such-Discovery: %s SUB-IDs gemerkt", len(all_ids))
    return saved


# ── Inkrementelles, provinz-gezieltes Crawlen ────────────────────────────────
def _load_crawl_state() -> dict:
    try:
        return json.loads(config.CRAWL_STATE_PATH.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_crawl_state(state: dict) -> None:
    try:
        config.CRAWL_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("crawl_state nicht gespeichert: %s", exc)


def discover_via_province_search(store: Store, provinces, *, estado: str | None = None,
                                 tipo: str = "", max_pages: int = 50,
                                 force: bool = False, fresh_days: int | None = None) -> dict:
    """Sucht je Provinz gezielt im Portal (nur diese Provinz!) und merkt die SUB-IDs.
    Überspringt Provinzen, die innerhalb des Schonfensters schon durchsucht wurden,
    außer bei force=True. So bleibt der wöchentliche Lauf kurz und portal-schonend."""
    from portal import Portal, build_search_url
    estado = config.PORTAL_SEARCH_ESTADO if estado is None else estado
    fresh_days = config.CRAWL_FRESH_DAYS if fresh_days is None else fresh_days
    state = _load_crawl_state()
    now = dt.datetime.now(dt.timezone.utc)
    portal = Portal()
    all_ids: list[str] = []
    per_prov: dict = {}
    skipped: list[str] = []
    for prov in provinces:
        code = config.province_code(prov)
        if not code:
            per_prov[prov] = {"error": "kein Provinz-Code bekannt"}
            continue
        last = (state.get(code) or {}).get("last_search")
        if not force and last:
            try:
                age = (now - dt.datetime.fromisoformat(last)).total_seconds() / 86400
                if age < fresh_days:
                    skipped.append(prov)
                    per_prov[prov] = {"skipped_fresh": True, "age_days": round(age, 1)}
                    continue
            except Exception:  # noqa: BLE001
                pass
        url = build_search_url(code, estado=estado, tipo=tipo)
        ids = portal.search_sub_ids(url, max_pages=max_pages)
        all_ids.extend(ids)
        per_prov[prov] = {"found": len(ids)}
        state[code] = {"province": prov, "last_search": now.isoformat(), "found": len(ids)}
    all_ids = list(dict.fromkeys(all_ids))
    saved = store.save_sub_ids(all_ids)
    _save_crawl_state(state)
    log.info("Provinz-Suche: %s SUB-IDs (%s Provinzen durchsucht, %s übersprungen)",
             len(all_ids), len(provinces) - len(skipped), len(skipped))
    return {"sub_ids": len(all_ids), "saved": saved,
            "per_province": per_prov, "skipped_fresh": skipped}


def _in_focus(sub) -> bool:
    """True, wenn die Subasta in den konfigurierten Provinzen/Typen liegt (oder kein Filter)."""
    if config.FOCUS_TIPOS:
        t = (sub.tipo_subasta or "").upper()
        if not any(f.upper() in t for f in config.FOCUS_TIPOS):
            return False
    if config.FOCUS_PROVINCIAS:
        provs = [(b.provincia or "") for l in sub.lotes for b in l.bienes]
        # Teilstring-Match: das Portal schreibt z. B. "Alicante/Alacant",
        # "Valencia/València" — ein exakter Vergleich würde diese verfehlen.
        if not any(f.lower() in p.lower() for p in provs for f in config.FOCUS_PROVINCIAS):
            return False
    if config.FOCUS_SUBTIPOS:
        subs = [(b.subtipo or "") for l in sub.lotes for b in l.bienes]
        if not any(f.lower() in s.lower() for s in subs for f in config.FOCUS_SUBTIPOS):
            return False
    return True


def enrich(store: Store, limit: int | None = None, with_pdf: bool = True,
           should_cancel=None) -> int:
    """Stufe 2: zu jeder offenen SUB-ID die Portal-Detailseite (+ PDFs) holen."""
    portal_fetcher = Fetcher(cookies=config.PORTAL_COOKIES or None)
    portal = Portal(fetcher=portal_fetcher)
    pdfx = PdfExtractor(fetcher=portal_fetcher)

    sub_ids = store.pending_sub_ids(limit=limit)
    log.info("Enrichment: %s SUB-IDs offen", len(sub_ids))
    total = len(sub_ids)
    _progress("enrich", 0, total, "Detailseiten holen")
    done = skipped = 0
    for idx, sub_id in enumerate(sub_ids, 1):
        if should_cancel and should_cancel():
            log.info("Enrichment abgebrochen (Stopp angefordert)")
            break
        try:
            sub = portal.get_subasta(
                sub_id,
                on_raw=lambda kind, url, html: append_raw(kind, url, html),
            )
            # Scope-Filter: außerhalb der Fokus-Provinzen/Typen nicht weiter anreichern
            if not _in_focus(sub):
                store.mark_enriched(sub_id)
                skipped += 1
                continue
            if with_pdf:
                sub.documentos = [pdfx.process(d) for d in sub.documentos]
                from pdf_extract import superficie_from_docs
                sup_val = superficie_from_docs(sub.documentos)
                if sup_val:
                    for lote in sub.lotes:
                        for b in lote.bienes:
                            b.superficie_valoracion = sup_val
            store.upsert_subasta(sub)
            store.mark_enriched(sub_id)
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Enrichment fehlgeschlagen %s: %s", sub_id, exc)
        _progress("enrich", idx, total, "Detailseiten holen")
    log.info("Enrichment: %s verarbeitet, %s außerhalb des Scopes übersprungen", done, skipped)
    return done


def run(start: dt.date, end: dt.date, out: str, limit: int | None = None) -> None:
    store = Store()
    try:
        discover(start, end, store)
        enrich(store, limit=limit)
    finally:
        store.close()
    from export import export_excel
    path = export_excel(out)
    log.info("Export geschrieben: %s", path)


def crawl_now(days_back: int = 30, limit: int | None = None, *, force: bool = False,
              should_cancel=None, with_pdf: bool = False) -> dict:
    """Vollständiger Lauf für den Live-Server: Discovery → Enrich → Catastro →
    Geocode → Idealista. Externe Schritte sind fehlertolerant.

    Discovery-Strategie:
      • Provinzen im Scope gesetzt → gezielte Portal-Suche je Provinz (schnell,
        portal-schonend, inkrementell: kürzlich durchsuchte Provinzen werden
        übersprungen, außer force=True).
      • sonst SEARCH_URLS → diese durchblättern.
      • sonst → API-Discovery über das Tagessumario (spanienweit, langsamer).
    Enrich läuft nur über noch nicht angereicherte SUB-IDs (inkrementell).
    """
    import config

    config.reload_scope()
    summary: dict = {"discovered": 0, "enriched": 0, "steps": {}}
    store = Store()
    try:
        if config.FOCUS_PROVINCIAS:
            # Provinz-gezielt über das offizielle Sumario + Ort→Provinz-Vorfilter.
            focus_codes = config.focus_province_codes()
            state = _load_crawl_state()
            end = dt.date.today()
            start = end - dt.timedelta(days=days_back)
            if not force:                      # inkrementell: nur neue Tage scannen
                ls = state.get("last_scanned_date")
                if ls:
                    try:
                        start = max(start, dt.date.fromisoformat(ls) - dt.timedelta(days=2))
                    except Exception:  # noqa: BLE001
                        pass
            summary["discovered"] = discover(start, end, store, focus_codes=focus_codes,
                                              should_cancel=should_cancel)
            summary["mode"] = "api-province"
            summary["window"] = [start.isoformat(), end.isoformat()]
            summary["focus_codes"] = sorted(focus_codes)
            state["last_scanned_date"] = end.isoformat()
            _save_crawl_state(state)
        elif config.SEARCH_URLS:
            summary["discovered"] = discover_via_search(list(config.SEARCH_URLS), store)
            summary["mode"] = "search"
        else:
            end = dt.date.today()
            start = end - dt.timedelta(days=days_back)
            summary["discovered"] = discover(start, end, store, should_cancel=should_cancel)
            summary["mode"] = "api"
            summary["window"] = [start.isoformat(), end.isoformat()]
        summary["enriched"] = enrich(store, limit=limit, with_pdf=with_pdf,
                                     should_cancel=should_cancel)
    finally:
        store.close()

    if should_cancel and should_cancel():
        summary["cancelled"] = True
        _progress("done", 0, 0, "")
        return summary

    for name, fn in (("geocode", _step_geocode),
                     ("catastro", _step_catastro),
                     ("idealista", _step_idealista),
                     ("market", _step_market_local)):
        if should_cancel and should_cancel():
            summary["cancelled"] = True
            break
        _progress(name, 0, 0, "anreichern")
        try:
            summary["steps"][name] = fn(limit)
        except Exception as exc:  # noqa: BLE001 - bewusst tolerant
            log.warning("Schritt %s übersprungen: %s", name, exc)
            summary["steps"][name] = f"skipped: {exc}"
    _progress("done", 0, 0, "")
    return summary


def _step_catastro(limit):
    from catastro import enrich_pending
    return enrich_pending(limit=limit)


def _step_geocode(limit):
    from geocode import geocode_pending
    return geocode_pending(limit=limit)


def _step_idealista(limit):
    from idealista import enrich_pending
    return enrich_pending(limit=limit)


def _step_market_local(limit):
    from market_local import enrich_pending
    return enrich_pending(limit=limit)
