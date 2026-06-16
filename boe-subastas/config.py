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
PORTAL_SEARCH = PORTAL_BASE + "/subastas_ava.php"              # erweiterte Suche

# Status-Filter der Portal-Suche (SUBASTA.ESTADO). "" = alle Status.
# Bekannte Codes: EJ = celebrándose (offen, Gebote möglich), PP/PU = próxima,
# CE/CO = concluida, SU = suspendida, CA = cancelada.
PORTAL_SEARCH_ESTADO = "EJ"

# ── Provinz → INE-Code (für BIEN.COD_PROVINCIA in der Portal-Suche) ───────────
PROVINCE_CODES: dict[str, str] = {
    "alava": "01", "araba": "01", "albacete": "02", "alicante": "03", "alacant": "03",
    "almeria": "04", "avila": "05", "badajoz": "06", "illes balears": "07",
    "islas baleares": "07", "baleares": "07", "barcelona": "08", "burgos": "09",
    "caceres": "10", "cadiz": "11", "castellon": "12", "castello": "12",
    "ciudad real": "13", "cordoba": "14", "a coruna": "15", "la coruna": "15",
    "coruna": "15", "cuenca": "16", "girona": "17", "gerona": "17", "granada": "18",
    "guadalajara": "19", "gipuzkoa": "20", "guipuzcoa": "20", "huelva": "21",
    "huesca": "22", "jaen": "23", "leon": "24", "lleida": "25", "lerida": "25",
    "la rioja": "26", "rioja": "26", "lugo": "27", "madrid": "28", "malaga": "29",
    "murcia": "30", "navarra": "31", "nafarroa": "31", "ourense": "32", "orense": "32",
    "asturias": "33", "palencia": "34", "las palmas": "35", "pontevedra": "36",
    "salamanca": "37", "santa cruz de tenerife": "38", "tenerife": "38",
    "cantabria": "39", "segovia": "40", "sevilla": "41", "soria": "42",
    "tarragona": "43", "teruel": "44", "toledo": "45", "valencia": "46",
    "valencia/valencia": "46", "valladolid": "47", "bizkaia": "48", "vizcaya": "48",
    "zamora": "49", "zaragoza": "50", "ceuta": "51", "melilla": "52",
}


def province_code(name: str) -> str | None:
    """Provinzname (auch zweisprachig, mit/ohne Akzent) → INE-Code für die Portal-Suche."""
    import unicodedata
    if not name:
        return None
    # erst vollständig normalisieren (Akzente weg, klein), dann ggf. Teil vor '/'
    def norm(s: str) -> str:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()
        return " ".join(s.split())
    full = norm(name)
    if full in PROVINCE_CODES:
        return PROVINCE_CODES[full]
    head = norm(name.split("/")[0])
    return PROVINCE_CODES.get(head)


# ── Inkrementelles Crawlen ───────────────────────────────────────────────────
# Eine Provinz wird übersprungen, wenn sie innerhalb dieses Fensters schon
# durchsucht wurde (außer bei "Force refresh"). Versteigerungen haben Wochen
# Vorlauf — eine Liste 1–3 Tage später zu sehen ist unkritisch.
CRAWL_FRESH_DAYS = 3
CRAWL_STATE_PATH = DATA_DIR / "crawl_state.json"   # je Provinz: zuletzt durchsucht

# ── Ort → Provinz (für das Vorfiltern der Discovery ohne Portal-Zugriff) ──────
# Der BOE-Sumario-Titel einer Gerichts-Versteigerung ist der Gerichtsort
# (z. B. "ALCOY"). Über diese Tabelle leiten wir daraus die Provinz ab und
# verwerfen Einträge außerhalb des Scopes, BEVOR das Portal angefasst wird.
MUNI_PROVINCIA_PATH = ROOT / "municipios_provincia.json"
_MUNI_CACHE: dict | None = None
_ARTICLES = {"de", "del", "la", "el", "los", "las", "l", "d", "i", "y", "e"}


def _norm_town(title: str) -> list[str]:
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    s = s.replace("'", " ").replace("-", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = [t for t in s.split() if t]
    art = [t for t in toks if t not in _ARTICLES]
    return [" ".join(toks), " ".join(art)]


def town_province_code(title: str) -> str | None:
    """Gerichtsort (BOE-Titel) → INE-Provinzcode, oder None wenn unbekannt/mehrdeutig."""
    global _MUNI_CACHE
    if not title:
        return None
    if _MUNI_CACHE is None:
        try:
            import json
            _MUNI_CACHE = json.loads(MUNI_PROVINCIA_PATH.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            _MUNI_CACHE = {}
    for k in _norm_town(title):
        if k in _MUNI_CACHE:
            return _MUNI_CACHE[k]
    return None


def focus_province_codes() -> set[str]:
    """Die im Scope gewählten Provinzen als Menge von INE-Codes."""
    codes = set()
    for p in FOCUS_PROVINCIAS:
        c = province_code(p)
        if c:
            codes.add(c)
    return codes

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
