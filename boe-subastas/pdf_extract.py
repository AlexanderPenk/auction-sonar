"""PDF-Schicht: herunterladen und Text/Tabellen extrahieren (pdfplumber).

pdfplumber ist stark bei tabellarischen Inhalten (Cargas, Lotes-Listen). Für
reinen Fließtext reicht ebenfalls pdfplumber; bei Massenverarbeitung wäre
PyMuPDF schneller, hier aber bewusst eine Abhängigkeit weniger.

Zusätzlich: aus dem Valoración-PDF (Art. 666 LEC) die bewertete Fläche und ggf.
den Tasationswert herausziehen, um sie gegen die Katasterfläche gegenzulesen.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

import pdfplumber

import config
from fetcher import Fetcher
from models import Documento

log = logging.getLogger("pdf")

# Flächen-Muster (spanische Gutachten, viele Schreibweisen).
_M2 = r"(?:m\s*[2²]|m\.?\s*c\b|metros?\s+cuadrados?)"
_SUP_PATTERNS = [
    re.compile(rf"superficie\s+construida[^0-9]{{0,25}}([\d.]+,?\d*)\s*{_M2}", re.I),
    re.compile(rf"superficie[^0-9]{{0,18}}([\d.]+,?\d*)\s*{_M2}", re.I),
    re.compile(rf"([\d.]+,?\d*)\s*{_M2}"),
]
_VAL_PATTERNS = [
    re.compile(r"valor(?:aci[oó]n)?(?:\s+de\s+tasaci[oó]n)?[^0-9]{0,25}([\d.]+,\d{2})\s*€", re.I),
    re.compile(r"([\d.]+,\d{2})\s*€", re.I),
]


def _num(s: str) -> float | None:
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def extract_superficie(text: str | None) -> float | None:
    """Plausibelste bewertete Fläche in m² (10–10000) aus dem Gutachtentext."""
    if not text:
        return None
    for pat in _SUP_PATTERNS:
        for m in pat.finditer(text):
            v = _num(m.group(1))
            if v is not None and 10 <= v <= 10000:
                return v
    return None


def extract_valor(text: str | None) -> float | None:
    if not text:
        return None
    for pat in _VAL_PATTERNS:
        m = pat.search(text)
        if m:
            v = _num(m.group(1))
            if v and v > 1000:
                return v
    return None


def is_valoracion(doc: Documento) -> bool:
    n = (doc.nombre or "").lower()
    return "valora" in n or "666" in n or "tasaci" in n


def superficie_from_docs(docs: list[Documento]) -> float | None:
    """Sucht das Valoración-Dokument und zieht die Fläche aus seinem Text."""
    for d in docs:
        if is_valoracion(d) and d.texto:
            sup = extract_superficie(d.texto)
            if sup:
                return sup
    return None


def _safe_name(url: str) -> str:
    return url.split("/")[-1].split("?")[-1].replace("=", "_").replace("&", "_")[:120] or "doc.pdf"


class PdfExtractor:
    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self.fetcher = fetcher or Fetcher(cookies=config.PORTAL_COOKIES or None)

    def process(self, doc: Documento) -> Documento:
        """Lädt das PDF (falls noch nicht da) und extrahiert den Text."""
        if not doc.url:
            return doc
        name = doc.nombre and f"{doc.nombre}.pdf" or _safe_name(doc.url)
        dest = config.PDF_DIR / _safe_name(name)
        try:
            if not dest.exists():
                self.fetcher.download(doc.url, dest)
            doc.local_path = str(dest)
            doc.texto = self.extract_text(dest)
        except Exception as exc:  # noqa: BLE001 — ein kaputtes PDF darf die Pipeline nicht killen
            log.warning("PDF fehlgeschlagen %s: %s", doc.url, exc)
        return doc

    @staticmethod
    def extract_text(path: Path) -> str:
        parts: list[str] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(parts).strip()

    @staticmethod
    def extract_tables(path: Path) -> list[list[list[str]]]:
        tables: list[list[list[str]]] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                tables.extend(page.extract_tables() or [])
        return tables


def backfill_superficie_pending(db_path: Path = config.DB_PATH, *, limit: int | None = None,
                                on_progress=None, should_cancel=None) -> int:
    """Feldbasierter PDF-Nachlese-Schritt (kein Neu-Crawl).

    Für bereits gespeicherte Bienes OHNE jede Fläche (weder Kataster- noch
    Gutachten-Fläche) werden die zugehörigen, bereits erfassten Valoración-PDF-URLs
    nachgeladen und ``superficie_valoracion`` gesetzt. So bekommen auch bestehende
    Objekte ihre Fläche, ohne den langsamen Erst-Scan zu wiederholen.

    Gibt die Anzahl der Subastas zurück, für die eine Fläche ergänzt wurde.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT DISTINCT b.sub_id AS sub_id FROM bienes b "
            "WHERE b.superficie_valoracion IS NULL AND b.superficie_m2 IS NULL "
            "AND EXISTS (SELECT 1 FROM documentos d "
            "            WHERE d.sub_id = b.sub_id AND d.url IS NOT NULL)"
        ).fetchall()
        sub_ids = [r["sub_id"] for r in rows if r["sub_id"]]
        if limit:
            sub_ids = sub_ids[:limit]
        total = len(sub_ids)
        log.info("PDF-Backfill: %s Subastas ohne Fläche", total)
        pdfx = PdfExtractor()
        done = 0
        for idx, sub_id in enumerate(sub_ids, 1):
            if should_cancel and should_cancel():
                log.info("PDF-Backfill abgebrochen")
                break
            try:
                drows = conn.execute(
                    "SELECT nombre, url, local_path, texto FROM documentos "
                    "WHERE sub_id = ? AND url IS NOT NULL", (sub_id,)).fetchall()
                docs = [Documento(nombre=d["nombre"], url=d["url"],
                                  local_path=d["local_path"], texto=d["texto"])
                        for d in drows]
                val_docs = [d for d in docs if is_valoracion(d)]
                for d in val_docs:
                    if not d.texto:
                        pdfx.process(d)            # lädt + extrahiert Text
                sup = superficie_from_docs(val_docs)
                if sup:
                    conn.execute(
                        "UPDATE bienes SET superficie_valoracion = ? "
                        "WHERE sub_id = ? AND superficie_valoracion IS NULL",
                        (sup, sub_id))
                    conn.commit()
                    done += 1
            except Exception as exc:  # noqa: BLE001 — ein kaputtes PDF stoppt den Lauf nicht
                log.warning("PDF-Backfill fehlgeschlagen %s: %s", sub_id, exc)
            if on_progress:
                on_progress(idx, total)
        log.info("PDF-Backfill: %s Subastas mit Fläche ergänzt", done)
        return done
    finally:
        conn.close()
