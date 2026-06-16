"""Orchestrierung: bindet API-Discovery, Portal-Enrichment, PDF und Store zusammen."""
from __future__ import annotations

import datetime as dt
import logging

import config
from boe_api import BoeApi
from fetcher import Fetcher
from pdf_extract import PdfExtractor
from portal import Portal
from store import Store, append_raw

log = logging.getLogger("pipeline")


def discover(start: dt.date, end: dt.date, store: Store) -> int:
    """Stufe 1: offizielle API nach Subasta-Anuncios absuchen, SUB-IDs ableiten."""
    api = BoeApi()
    records = api.find_range(start, end)
    # SUB-ID nachladen, wo sie nicht im Titel stand (aus dem Anuncio-XML).
    for r in records:
        if not r.get("sub_id") and r.get("url_xml"):
            r["sub_id"] = api.sub_id_from_anuncio_xml(r["url_xml"])
    append_raw("anuncios", None, records)
    saved = store.save_anuncios(records)
    with_sub = sum(1 for r in records if r.get("sub_id"))
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


def enrich(store: Store, limit: int | None = None, with_pdf: bool = True) -> int:
    """Stufe 2: zu jeder offenen SUB-ID die Portal-Detailseite (+ PDFs) holen."""
    portal_fetcher = Fetcher(cookies=config.PORTAL_COOKIES or None)
    portal = Portal(fetcher=portal_fetcher)
    pdfx = PdfExtractor(fetcher=portal_fetcher)

    sub_ids = store.pending_sub_ids(limit=limit)
    log.info("Enrichment: %s SUB-IDs offen", len(sub_ids))
    done = skipped = 0
    for sub_id in sub_ids:
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


def crawl_now(days_back: int = 30, limit: int | None = None) -> dict:
    """Vollständiger Lauf für den Live-Server: Discovery (Scope-gesteuert) →
    Enrich → Catastro → Geocode → Idealista. Externe Schritte sind fehlertolerant,
    damit fehlende Keys/Netzwerkprobleme den Lauf nicht abbrechen.
    Liest den Suchraum aus config (inkl. scope.json). Gibt eine Zusammenfassung zurück.
    """
    import config

    config.reload_scope()
    summary: dict = {"discovered": 0, "enriched": 0, "steps": {}}
    store = Store()
    try:
        if config.SEARCH_URLS:
            summary["discovered"] = discover_via_search(list(config.SEARCH_URLS), store)
            summary["mode"] = "search"
        else:
            end = dt.date.today()
            start = end - dt.timedelta(days=days_back)
            summary["discovered"] = discover(start, end, store)
            summary["mode"] = "api"
            summary["window"] = [start.isoformat(), end.isoformat()]
        summary["enriched"] = enrich(store, limit=limit)
    finally:
        store.close()

    for name, fn in (("catastro", _step_catastro),
                     ("geocode", _step_geocode),
                     ("idealista", _step_idealista)):
        try:
            summary["steps"][name] = fn(limit)
        except Exception as exc:  # noqa: BLE001 - bewusst tolerant
            log.warning("Schritt %s übersprungen: %s", name, exc)
            summary["steps"][name] = f"skipped: {exc}"
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
