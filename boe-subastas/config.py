"""Zentrale Konfiguration. Alles, was man tunen will, an einer Stelle."""
from __future__ import annotations

from pathlib import Path

# ── Pfade ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"            # JSONL-Rohschicht (append-only)
PDF_DIR = DATA_DIR / "pdfs"           # heruntergeladene PDFs
DB_PATH = DATA_DIR / "subastas.db"    # kanonische SQLite

for _p in (DATA_DIR, RAW_DIR, PDF_DIR):
    _p.mkdir(parents=True, exist_ok=True)

# ── Höfliches Crawlen ────────────────────────────────────────────────────────
# Ehrlicher User-Agent mit Kontakt ist Pflicht, wenn man gegen robots.txt geht.
USER_AGENT = "boe-subastas-research/0.1 (+kontakt@example.com)"
REQUEST_DELAY = 2.5      # Sekunden Grundpause zwischen Requests
REQUEST_JITTER = 1.5     # zufälliger Aufschlag 0..JITTER, damit kein Takt entsteht
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF_FACTOR = 2.0     # Wartezeit verdoppelt sich pro Fehlversuch

# Optionaler Login-Cookie (manche Edicto-/Pliego-PDFs verlangen eine Sitzung).
# Nach manuellem Login im Browser den Cookie-Header hier hinterlegen, z. B.:
# PORTAL_COOKIES = {"PHPSESSID": "abc123..."}
PORTAL_COOKIES: dict[str, str] = {}

# Idealista-API (optional, für Marktwert-Schätzung). Besser über Umgebungs-
# variablen IDEALISTA_API_KEY / IDEALISTA_SECRET setzen als hier im Klartext.
IDEALISTA_API_KEY = ""
IDEALISTA_SECRET = ""

# ── Endpunkte ────────────────────────────────────────────────────────────────
BOE_API_BASE = "https://boe.es/datosabiertos/api/boe/sumario"   # /YYYYMMDD
PORTAL_BASE = "https://subastas.boe.es"
PORTAL_DETAIL = PORTAL_BASE + "/detalleSubasta.php"             # ?idSub=SUB-...
PORTAL_DOC = PORTAL_BASE + "/verDocumento.php"                  # ?idSub=...&doc=...

# ── Subasta-Filter für die API-Discovery ─────────────────────────────────────
# Anuncios, deren Titel eines dieser Wörter enthält, gelten als Versteigerung.
# (Nur relevant, wenn KEINE Sektionscodes gesetzt sind — siehe unten.)
SUBASTA_KEYWORDS = ("subasta",)  # bewusst lowercase-Match
# Versteigerungs-Anuncios stehen in Sektion IV (gerichtlich) und V-B (behördlich, AEAT).
# Ihr Index-Titel ist nur der Gerichtsort (z. B. "ALCOY"), nicht "subasta" — daher
# filtern wir nach Sektion und erkennen die echte Versteigerung an der SUB-ID im Dokument.
SUBASTA_SECTION_CODES: tuple[str, ...] = ("4", "5B")

# Regex, um die SUB-Kennung aus Anuncio-Text/Titel zu ziehen.
# Achtung: behördliche Kennungen enthalten Buchstaben (z. B. SUB-AT-2025-24R3586002036),
# daher Buchstaben+Ziffern im letzten Block, nicht nur \d.
SUB_ID_PATTERN = r"SUB-[A-Z]{1,3}-\d{4}-[A-Z0-9]+"

# ── Scope: nur bestimmte Provinzen/Typen verarbeiten ─────────────────────────
# Leer = alles. Schreibweise wie im Portal (mit Akzenten), z. B. "Málaga".
FOCUS_PROVINCIAS: tuple[str, ...] = ()      # z. B. ("Madrid", "Málaga", "Valencia")
FOCUS_TIPOS: tuple[str, ...] = ()           # Teilstring-Match auf tipo_subasta, z. B. ("JUDICIAL",)
FOCUS_SUBTIPOS: tuple[str, ...] = ()        # Immobilienart, z. B. ("Vivienda", "Local")

# Provinz-gezielte Discovery über die Portal-Suche: hier die Such-URLs eintragen,
# die das Portal nach einer gefilterten Suche vergibt (enthalten id_busqueda).
# Eine pro Provinz/Suche. Alternativ via CLI: python main.py search --url "..."
SEARCH_URLS: tuple[str, ...] = ()

# Wenn eine scope.json existiert (z. B. aus dem Dashboard-"Scope"-Tab exportiert),
# überschreibt sie die obigen Scope-Einstellungen — Suchraum per UI definierbar.
SCOPE_PATH = ROOT / "scope.json"


def reload_scope() -> None:
    """Liest scope.json (falls vorhanden) und überschreibt die FOCUS_*/SEARCH_URLS.
    Wird zur Importzeit und vom Live-Server vor jedem Lauf aufgerufen, damit per
    API gespeicherte Scopes ohne Neustart greifen.
    """
    global FOCUS_PROVINCIAS, FOCUS_TIPOS, FOCUS_SUBTIPOS, SEARCH_URLS
    if not SCOPE_PATH.exists():
        return
    import json as _json
    try:
        _scope = _json.loads(SCOPE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    FOCUS_PROVINCIAS = tuple(_scope.get("focus_provincias") or ())
    FOCUS_TIPOS = tuple(_scope.get("focus_tipos") or ())
    FOCUS_SUBTIPOS = tuple(_scope.get("focus_subtipos") or ())
    SEARCH_URLS = tuple(_scope.get("search_urls") or ())


reload_scope()
