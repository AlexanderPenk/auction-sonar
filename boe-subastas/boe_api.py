"""Offizielle BOE-Open-Data-API (Discovery-Schicht).

GET https://boe.es/datosabiertos/api/boe/sumario/YYYYMMDD  (Accept: application/json)

Liefert das Tagessumario. Wir laufen den Baum rekursiv ab, sammeln alle
Anuncio-Items (sie haben ein `identificador` BOE-B-…), filtern auf Subastas
und ziehen die SUB-Kennung heraus, die zur Portal-Detailseite führt.

Die BOE-API hat eine Eigenheit: Felder sind mal ein Objekt, mal eine Liste
(je nachdem, ob 1 oder n Elemente). `_aslist` glättet das.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Iterable, Iterator

import config
from fetcher import Fetcher

log = logging.getLogger("boe_api")
_SUB_RE = re.compile(config.SUB_ID_PATTERN)
_SITE = "https://www.boe.es"   # für die Vervollständigung relativer Dokument-URLs


def _abs(url: Any) -> Any:
    """Macht eine BOE-Dokument-URL absolut. Die API liefert sie oft verkürzt
    (z. B. '/diario_boe/xml.php?id=…') — ohne Host lässt sie sich nicht abrufen."""
    if isinstance(url, str) and url.startswith("/"):
        return _SITE + url
    return url


def _aslist(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _pdf_url(item: dict) -> str | None:
    """url_pdf ist mal ein String, mal {"texto": "https://…", "szBytes": …}."""
    raw = item.get("url_pdf")
    if isinstance(raw, dict):
        return raw.get("texto") or raw.get("url")
    return raw


def _iter_items(node: Any) -> Iterator[dict]:
    """Rekursiv jedes Dict mit 'identificador' (= ein Anuncio/Disposición) liefern."""
    if isinstance(node, dict):
        if "identificador" in node and "titulo" in node:
            yield node
        else:
            for v in node.values():
                yield from _iter_items(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_items(v)


def daterange(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


class BoeApi:
    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self.fetcher = fetcher or Fetcher()

    def fetch_sumario(self, day: dt.date) -> dict | None:
        """Tagessumario als JSON. None, wenn es an dem Tag kein BOE gab (404)."""
        url = f"{config.BOE_API_BASE}/{day:%Y%m%d}"
        try:
            return self.fetcher.get_json(url)
        except RuntimeError:
            log.info("Kein Sumario für %s (vermutlich kein BOE-Tag)", day)
            return None

    def _is_subasta(self, item: dict, section_code: str | None) -> bool:
        if config.SUBASTA_SECTION_CODES and section_code not in config.SUBASTA_SECTION_CODES:
            return False
        title = (item.get("titulo") or "").lower()
        return any(kw in title for kw in config.SUBASTA_KEYWORDS)

    def find_subastas(self, day: dt.date) -> list[dict]:
        """Subasta-Anuncios eines Tages als flache Records (inkl. SUB-ID, wenn erkennbar)."""
        sumario = self.fetch_sumario(day)
        if not sumario:
            return []
        data = sumario.get("data", sumario)
        records: list[dict] = []
        for diario in _aslist(_dig(data, "sumario", "diario")):
            for seccion in _aslist(diario.get("seccion")):
                code = str(seccion.get("codigo")) if isinstance(seccion, dict) else None
                for item in _iter_items(seccion):
                    if not self._is_subasta(item, code):
                        continue
                    title = item.get("titulo") or ""
                    m = _SUB_RE.search(title)
                    records.append({
                        "boe_anuncio_id": item.get("identificador"),
                        "titulo": title,
                        "seccion": code,
                        "url_pdf": _abs(_pdf_url(item)),
                        "url_xml": _abs(item.get("url_xml")),
                        "url_html": _abs(item.get("url_html")),
                        "sub_id": m.group(0) if m else None,
                        "fecha": day.isoformat(),
                    })
        log.info("%s: %s Subasta-Anuncios", day, len(records))
        return records

    def find_range(self, start: dt.date, end: dt.date) -> list[dict]:
        out: list[dict] = []
        for day in daterange(start, end):
            out.extend(self.find_subastas(day))
        return out

    def sub_id_from_anuncio_xml(self, url_xml: str) -> str | None:
        """Fallback: SUB-Kennung aus dem XML-Volltext des Anuncios ziehen,
        falls sie nicht schon im Titel stand."""
        if not url_xml:
            return None
        try:
            text = self.fetcher.get_text(url_xml)
        except RuntimeError:
            return None
        m = _SUB_RE.search(text)
        return m.group(0) if m else None


def _dig(node: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node
