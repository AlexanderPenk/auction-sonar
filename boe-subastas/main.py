"""CLI-Einstiegspunkt.

  python main.py discover --from 2026-06-01 --to 2026-06-15
  python main.py enrich   [--limit 20] [--no-pdf]
  python main.py export   [--out subastas.xlsx | --csv subastas.csv]
  python main.py run      --from 2026-06-01 --to 2026-06-15 --out subastas.xlsx
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging

import pipeline
from store import Store


def _date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="BOE Subastas Crawler (Hybrid: API + Portal)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="API-Discovery der Subasta-Anuncios (ganz Spanien)")
    d.add_argument("--from", dest="start", type=_date, required=True)
    d.add_argument("--to", dest="end", type=_date, required=True)

    se = sub.add_parser("search", help="Provinz-gezielte Discovery über Portal-Such-URLs")
    se.add_argument("--url", action="append", default=[], help="Portal-Such-URL (mehrfach möglich)")
    se.add_argument("--max-pages", type=int, default=50)

    e = sub.add_parser("enrich", help="Portal-Detailseiten + PDFs holen")
    e.add_argument("--limit", type=int, default=None)
    e.add_argument("--no-pdf", action="store_true")

    g = sub.add_parser("geocode", help="Adressen zu Koordinaten (OSM/Nominatim)")
    g.add_argument("--limit", type=int, default=None)

    cat = sub.add_parser("catastro", help="m²/Baujahr/Uso + exakte Koordinaten aus dem Catastro")
    cat.add_argument("--limit", type=int, default=None)

    ide = sub.add_parser("idealista", help="Marktwert-Schätzung über Vergleichsangebote (API-Key nötig)")
    ide.add_argument("--limit", type=int, default=None)

    x = sub.add_parser("export", help="Export nach Excel/CSV/HTML")
    x.add_argument("--out", default="subastas.xlsx")
    x.add_argument("--csv", default=None)
    x.add_argument("--html", default=None)

    r = sub.add_parser("run", help="Discover + Enrich + Export")
    r.add_argument("--from", dest="start", type=_date, required=True)
    r.add_argument("--to", dest="end", type=_date, required=True)
    r.add_argument("--out", default="subastas.xlsx")
    r.add_argument("--limit", type=int, default=None)

    args = p.parse_args()

    if args.cmd == "discover":
        store = Store()
        try:
            pipeline.discover(args.start, args.end, store)
        finally:
            store.close()
    elif args.cmd == "search":
        import config
        urls = args.url or list(config.SEARCH_URLS)
        if not urls:
            p.error("Keine Such-URL: --url angeben oder SEARCH_URLS in config setzen.")
        store = Store()
        try:
            n = pipeline.discover_via_search(urls, store, max_pages=args.max_pages)
            print(f"Gemerkte SUB-IDs: {n}")
        finally:
            store.close()
    elif args.cmd == "enrich":
        store = Store()
        try:
            pipeline.enrich(store, limit=args.limit, with_pdf=not args.no_pdf)
        finally:
            store.close()
    elif args.cmd == "geocode":
        from geocode import geocode_pending
        n = geocode_pending(limit=args.limit)
        print(f"Verortet: {n} Bienes")
    elif args.cmd == "catastro":
        from catastro import enrich_pending
        n = enrich_pending(limit=args.limit)
        print(f"Catastro angereichert: {n} Bienes")
    elif args.cmd == "idealista":
        from idealista import enrich_pending
        n = enrich_pending(limit=args.limit)
        print(f"Marktwert geschätzt: {n} Bienes")
    elif args.cmd == "export":
        from export import export_csv, export_excel, export_html
        if args.csv:
            print("CSV:", export_csv(args.csv))
        elif args.html:
            print("HTML:", export_html(args.html))
        else:
            print("Excel:", export_excel(args.out))
    elif args.cmd == "run":
        pipeline.run(args.start, args.end, args.out, limit=args.limit)


if __name__ == "__main__":
    main()
