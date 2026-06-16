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
