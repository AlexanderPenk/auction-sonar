"""Portal-Schicht: subastas.boe.es.

ACHTUNG: Das Portal verbietet automatisierte Zugriffe per robots.txt
(siehe README). Maßvoll und nur für eigene Recherche nutzen.

Aufbau einer Detailseite (server-gerendertes PHP, kein JS-Rendering nötig):
die Daten sind über fünf Tabs verteilt, ansteuerbar über `&ver=N`:
    ver=1  Información general  → Subasta-Ebene (Typ, Fechas, Valor, Depósito, …)
    ver=2  Autoridad gestora    → Juzgado/Notaría/AEAT
    ver=3  Bienes               → Adresse, Referencia catastral, Cargas  ← Kern für Fix-and-Flip
    ver=4  Relacionados
    ver=5  Pujas                → aktuelle Höchstgebote (Detail nur mit Login)

ver=1 ist hier an echtem HTML verifiziert. ver=2/ver=3 nutzen generische
Label-Ernte mit den erwarteten BOE-Labels und sind mit # VERIFY markiert,
bis ein echtes HTML dieser Tabs vorliegt.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

import config
from fetcher import Fetcher
from models import Bien, Documento, Lote, Subasta

log = logging.getLogger("portal")

_AMOUNT_RE = re.compile(r"-?\d{1,3}(?:\.\d{3})*(?:,\d+)?")
_ISO_RE = re.compile(r"ISO:\s*([0-9T:+\-]+)")


def parse_amount(text: str | None) -> float | None:
    """'193.349,25 €' → 193349.25 ; 'Sin tramos' → None ; '0,00 €' → 0.0."""
    if not text:
        return None
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def parse_fecha(text: str | None) -> str | None:
    """Bevorzugt den eingebetteten ISO-Zeitstempel '(ISO: 2026-07-02T18:00:00+02:00)'."""
    if not text:
        return None
    m = _ISO_RE.search(text)
    return m.group(1) if m else text.strip()


def build_detail_url(sub_id: str, ver: int = 1, id_bus: str | None = None) -> str:
    url = f"{config.PORTAL_DETAIL}?idSub={sub_id}&ver={ver}"
    if id_bus:
        url += f"&idBus={id_bus}"
    return url


# Feldreihenfolge der erweiterten Portal-Suche (subastas_ava.php), abgeleitet aus
# einer echten geteilten Such-URL. Wir füllen nur Status, Bien-Typ und Provinz.
_SEARCH_FIELDS = [
    ("SUBASTA.ORIGEN", ""),                       # 0
    ("SUBASTA.AUTORIDAD", ""),                    # 1
    ("SUBASTA.ESTADO", None),                     # 2  ← estado
    ("BIEN.TIPO", None),                          # 3  ← tipo de bien
    (None, ""),                                   # 4  (nur dato[4]=)
    ("BIEN.DIRECCION", ""),                       # 5
    ("BIEN.CODPOSTAL", ""),                       # 6
    ("BIEN.LOCALIDAD", ""),                       # 7
    ("BIEN.COD_PROVINCIA", None),                 # 8  ← Provinz-Code
    ("SUBASTA.POSTURA_MINIMA_MINIMA_LOTES", ""),  # 9
    ("SUBASTA.NUM_CUENTA_EXPEDIENTE_1", ""),      # 10
    ("SUBASTA.NUM_CUENTA_EXPEDIENTE_2", ""),      # 11
    ("SUBASTA.NUM_CUENTA_EXPEDIENTE_3", ""),      # 12
    ("SUBASTA.NUM_CUENTA_EXPEDIENTE_4", ""),      # 13
    ("SUBASTA.NUM_CUENTA_EXPEDIENTE_5", ""),      # 14
    ("SUBASTA.ID_SUBASTA_BUSCAR", ""),            # 15
]


def build_search_url(cod_provincia: str, *, estado: str = "", tipo: str = "") -> str:
    """Erweiterte Portal-Suche, gefiltert auf eine Provinz (INE-Code).
    Optional Status (z. B. "EJ" = offen) und Bien-Typ. Liefert Trefferseite 1."""
    import urllib.parse as _url
    subst = {2: estado, 3: tipo, 8: cod_provincia}
    pairs: list[tuple[str, str]] = []
    for i, (campo, fixed) in enumerate(_SEARCH_FIELDS):
        if campo is not None:
            pairs.append((f"campo[{i}]", campo))
        pairs.append((f"dato[{i}]", subst.get(i, fixed) or ""))
    # Datumsfelder als Array (leer = keine Einschränkung)
    for i, campo in ((16, "SUBASTA.FECHA_FIN_YMD"), (17, "SUBASTA.FECHA_INICIO_YMD")):
        pairs.append((f"campo[{i}]", campo))
        pairs.append((f"dato[{i}][0]", ""))
        pairs.append((f"dato[{i}][1]", ""))
    return f"{config.PORTAL_SEARCH}?{_url.urlencode(pairs)}"


def harvest_pairs(soup: BeautifulSoup, scope_selector: str | None = None) -> dict[str, str]:
    """{label_lowercase: wert} aus th/td- und dt/dd-Strukturen.

    `scope_selector` grenzt auf einen Container ein (präziser). Ohne Scope wird
    die ganze Seite geerntet.
    """
    roots = soup.select(scope_selector) if scope_selector else [soup]
    pairs: dict[str, str] = {}
    for root in roots:
        for row in root.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True).lower().rstrip(":")
                if label:
                    pairs.setdefault(label, cells[1].get_text(" ", strip=True))
        for dt_ in root.find_all("dt"):
            dd = dt_.find_next_sibling("dd")
            if dd:
                pairs.setdefault(dt_.get_text(strip=True).lower().rstrip(":"),
                                 dd.get_text(" ", strip=True))
    return pairs


# ── ver=1: Información general (an echtem HTML verifiziert) ───────────────────
def extract_general(html: str, sub_id: str, id_bus: str | None = None) -> Subasta:
    soup = BeautifulSoup(html, "lxml")
    p = harvest_pairs(soup, "#idBloqueDatos1 table")

    sub = Subasta(
        sub_id=sub_id,
        detail_url=build_detail_url(sub_id, ver=1, id_bus=id_bus),
        tipo_subasta=p.get("tipo de subasta"),
        cuenta_expediente=p.get("cuenta expediente"),
        fecha_inicio=parse_fecha(p.get("fecha de inicio")),
        fecha_fin=parse_fecha(p.get("fecha de conclusión")),
        cantidad_reclamada=parse_amount(p.get("cantidad reclamada")),
        boe_anuncio_id=p.get("anuncio boe"),
        valor_subasta=parse_amount(p.get("valor subasta")),
        tasacion=parse_amount(p.get("tasación")),
        importe_deposito=parse_amount(p.get("importe del depósito")),
        puja_minima=parse_amount(p.get("puja mínima")),
        tramos=parse_amount(p.get("tramos entre pujas")),
        lotes_info=p.get("lotes"),
        estado=p.get("estado"),
    )

    # Dokumente (Edicto, Certificación cargas, Valoración inmueble …)
    for a in soup.select("ul.enlaces a[href*='verDocumento']"):
        href = a.get("href", "")
        url = href if href.startswith("http") else f"{config.PORTAL_BASE}/{href.lstrip('./')}"
        sub.documentos.append(Documento(nombre=a.get_text(strip=True) or None, url=url))
    return sub


# ── ver=2: Autoridad gestora (erwartete Labels, # VERIFY) ─────────────────────
AUTORIDAD_LABELS = {
    "código": "codigo",
    "descripción": "descripcion",
    "dirección": "direccion",
    "teléfono": "telefono",
    "fax": "fax",
    "correo electrónico": "email",
}


def extract_autoridad(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    p = harvest_pairs(soup, "#idBloqueDatos2") or harvest_pairs(soup)  # VERIFY: Container-ID
    return {AUTORIDAD_LABELS[k]: v for k, v in p.items() if k in AUTORIDAD_LABELS}


# ── ver=3: Bienes (an echtem HTML verifiziert) ───────────────────────────────
BIEN_LABELS = {
    "descripción": "descripcion",
    "dirección": "direccion",
    "localidad": "municipio",
    "municipio": "municipio",
    "provincia": "provincia",
    "código postal": "codigo_postal",
    "referencia catastral": "referencia_catastral",
    "idufir": "idufir",
    "vivienda habitual": "vivienda_habitual",
    "situación posesoria": "situacion_posesoria",
    "visitable": "visitable",
    "cargas": "cargas",
}
_H4_RE = re.compile(r"Bien\s*\d+\s*-\s*([^(]+?)(?:\s*\(([^)]+)\))?\s*$")


def extract_bienes(html: str) -> list[Lote]:
    """ver=3 → Liste von Lotes mit ihren Bienes.

    Struktur (verifiziert):
      #idBloqueDatos3 > div.bloque#idBloqueLoteN          (= ein Lote)
        ├─ div.caja                                       (Finca registral)
        └─ h4 'Bien N - Tipo (Subtipo)' + <table>         (je ein Bien)
    """
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("#idBloqueDatos3")
    if not container:
        return []

    lotes: list[Lote] = []
    bloques = container.select("div.bloque") or [container]
    for idx, bloque in enumerate(bloques, start=1):
        caja = bloque.select_one("div.caja")
        registral = caja.get_text(" ", strip=True) if caja else None

        lote = Lote(numero=idx, lote_id=bloque.get("id"))
        for h4 in bloque.find_all("h4"):
            tbl = h4.find_next("table")
            if not tbl:
                continue
            pairs = harvest_pairs(BeautifulSoup(str(tbl), "lxml"))
            bien = Bien(datos_registrales=registral)

            m = _H4_RE.search(h4.get_text(" ", strip=True))
            if m:
                bien.tipo = (m.group(1) or "").strip() or None
                bien.subtipo = (m.group(2) or "").strip() or None

            for label, value in pairs.items():
                f = BIEN_LABELS.get(label)
                if f:
                    setattr(bien, f, value)
            lote.bienes.append(bien)
        lotes.append(lote)
    return lotes


# ── Suche / Discovery direkt über das Portal (provinz-gezielt) ────────────────
def parse_search_results(html: str) -> tuple[list[str], str | None]:
    """→ (sub_ids dieser Trefferseite, URL der nächsten Seite oder None).

    Verifiziert: Treffer verlinken per detalleSubasta.php?idSub=... . Die
    Paginierung folgt dem 'Siguiente'-Link. # VERIFY am echten Trefferlisten-HTML,
    falls das Portal den Next-Link anders auszeichnet.
    """
    soup = BeautifulSoup(html, "lxml")
    ids = []
    for a in soup.select("a[href*='detalleSubasta']"):
        m = re.search(r"idSub=(SUB-[A-Z0-9-]+)", a.get("href", ""))
        if m:
            ids.append(m.group(1))
    nxt = soup.select_one("a[rel='next'], a.siguiente, a.paginacionSiguiente, "
                          "a[title*='Siguiente'], a[title*='siguiente']")
    next_url = None
    if nxt and nxt.get("href"):
        href = nxt["href"]
        next_url = href if href.startswith("http") else f"{config.PORTAL_BASE}/{href.lstrip('./')}"
    return list(dict.fromkeys(ids)), next_url


class Portal:
    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self.fetcher = fetcher or Fetcher(cookies=config.PORTAL_COOKIES or None)

    def search_sub_ids(self, search_url: str, *, max_pages: int = 50) -> list[str]:
        """Eine Portal-Such-URL durchblättern und alle SUB-IDs einsammeln."""
        seen: list[str] = []
        url: str | None = search_url
        pages = 0
        while url and pages < max_pages:
            html = self.fetcher.get_text(url)
            ids, url = parse_search_results(html)
            for i in ids:
                if i not in seen:
                    seen.append(i)
            pages += 1
            if not ids:
                break
        log.info("Suche: %s SUB-IDs auf %s Seite(n)", len(seen), pages)
        return seen

    def get_subasta(self, sub_id: str, *, with_bienes: bool = True,
                    with_autoridad: bool = True, id_bus: str | None = None,
                    on_raw=None) -> Subasta:
        """Holt die nötigen Tabs und fügt sie zu einer Subasta zusammen.

        `on_raw(kind, url, text)` wird je geholtem Tab aufgerufen (für die
        JSONL-Rohschicht), ohne dass diese Klasse den Store kennen muss.
        """
        def fetch(ver: int, kind: str) -> str:
            url = build_detail_url(sub_id, ver, id_bus)
            html = self.fetcher.get_text(url)
            if on_raw:
                on_raw(kind, url, html)
            return html

        sub = extract_general(fetch(1, "portal_general"), sub_id, id_bus)

        if with_autoridad:
            try:
                aut = extract_autoridad(fetch(2, "portal_autoridad"))
                sub.autoridad_gestora = aut.get("descripcion") or sub.autoridad_gestora
                if aut:
                    sub.extra["autoridad"] = aut
            except Exception as exc:  # noqa: BLE001
                log.warning("ver=2 (autoridad) fehlgeschlagen %s: %s", sub_id, exc)

        lotes: list[Lote] = []
        if with_bienes:
            try:
                lotes = extract_bienes(fetch(3, "portal_bienes"))
            except Exception as exc:  # noqa: BLE001
                log.warning("ver=3 (bienes) fehlgeschlagen %s: %s", sub_id, exc)

        if not lotes:
            lotes = [Lote(numero=1)]
        # Bei genau einem Lote (häufig: "Sin lotes") tragen die Subasta-Beträge
        # auf diesen Lote über, damit der Export pro Bien vollständig ist.
        if len(lotes) == 1:
            l = lotes[0]
            l.valor_subasta = l.valor_subasta or sub.valor_subasta
            l.importe_deposito = l.importe_deposito or sub.importe_deposito
            l.puja_minima = l.puja_minima or sub.puja_minima
            l.tramos = l.tramos or sub.tramos
            l.cantidad_reclamada = l.cantidad_reclamada or sub.cantidad_reclamada
        sub.lotes = lotes
        return sub
