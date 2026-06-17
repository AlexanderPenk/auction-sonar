"""Catastro-Anreicherung über die freien Web-Services der D.G. del Catastro.

Zwei freie, schlüssellose Services (nur nicht-geschützte Daten — Fläche, Baujahr,
Nutzung, Koordinaten sind frei; Eigentümer und Katasterwert sind geschützt):

  • Consulta_DNPRC  → Superficie construida (m²), Año de construcción, Uso, Dirección
    GET .../OVCSWLocalizacionRC/OVCCallejero.asmx/Consulta_DNPRC?Provincia=&Municipio=&RC=<rc>

  • Consulta_CPMRC  → Koordinaten des Parzellen-Zentroids (mit SRS=EPSG:4326 → lon/lat)
    GET .../OVCSWLocalizacionRC/OVCCoordenadas.asmx/Consulta_CPMRC?Provincia=&Municipio=&SRS=EPSG:4326&RC=<rc>

Die RC ist die volle 20-stellige Referencia catastral (dann sind Provincia/Municipio
optional). Antworten sind XML; Namespaces werden vor dem Parsen entfernt.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

import config

log = logging.getLogger("catastro")

BASE = "https://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC"
DNPRC = BASE + "/OVCCallejero.asmx/Consulta_DNPRC"
CPMRC = BASE + "/OVCCoordenadas.asmx/Consulta_CPMRC"

_NS = re.compile(r'\sxmlns(:\w+)?="[^"]*"')


def _parse(text: str) -> ET.Element:
    """Namespaces entfernen, damit .find('.//tag') ohne Präfix funktioniert."""
    return ET.fromstring(_NS.sub("", text))


def _text(root: ET.Element, tag: str) -> str | None:
    el = root.find(f".//{tag}")
    return el.text.strip() if el is not None and el.text else None


def _errors(root: ET.Element) -> str | None:
    """Liefert die Fehlerbeschreibung, falls die Antwort einen Fehler meldet."""
    cuerr = _text(root, "cuerr")
    if cuerr and cuerr != "0":
        return _text(root, "des") or "Catastro-Fehler"
    return None


class CatastroClient:
    def __init__(self, delay: float = 0.5) -> None:
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})

    def _get(self, url: str, params: dict) -> ET.Element | None:
        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return _parse(resp.text)
        except (requests.RequestException, ET.ParseError) as exc:
            log.warning("Catastro-Aufruf fehlgeschlagen (%s): %s", url, exc)
            return None
        finally:
            time.sleep(self.delay)

    def datos(self, rc: str) -> dict | None:
        """Nicht-geschützte Sachdaten zur RC: superficie_m2, anio, uso, direccion."""
        root = self._get(DNPRC, {"Provincia": "", "Municipio": "", "RC": rc})
        if root is None:
            return None
        if (err := _errors(root)):
            log.info("DNPRC %s: %s", rc, err)
            return None
        sfc = _text(root, "sfc")          # superficie construida (m²)
        return {
            "superficie_m2": float(sfc) if sfc and sfc.isdigit() else None,
            "anio_construccion": _text(root, "ant"),   # año de antigüedad
            "uso_catastral": _text(root, "luso"),      # uso principal
            "direccion_catastro": _text(root, "ldt"),  # dirección literal
        }

    def coords(self, rc: str) -> tuple[float, float] | None:
        """(lat, lon) des Parzellen-Zentroids in EPSG:4326.
        Consulta_CPMRC erwartet die 14-stellige Parzellen-RC; die volle 20-stellige
        führt teils zu einem Fehler. Daher zuerst 14-stellig, dann voll versuchen."""
        candidates = []
        rc = (rc or "").strip()
        if len(rc) >= 14:
            candidates.append(rc[:14])
        if rc and rc not in candidates:
            candidates.append(rc)
        for cand in candidates:
            root = self._get(CPMRC, {"Provincia": "", "Municipio": "",
                                     "SRS": "EPSG:4326", "RC": cand})
            if root is None or _errors(root):
                continue
            lon = _text(root, "xcen")   # mit SRS=EPSG:4326: xcen = longitud
            lat = _text(root, "ycen")   #                    ycen = latitud
            try:
                if lat and lon:
                    return (float(lat), float(lon))
            except ValueError:
                continue
        return None


def enrich_pending(db_path: Path = config.DB_PATH, *, limit: int | None = None,
                   delay: float = 0.5) -> int:
    """Alle Bienes mit RC, denen Fläche/Baujahr oder Koordinaten fehlen, anreichern."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, referencia_catastral, latitud, superficie_m2 FROM bienes "
           "WHERE referencia_catastral IS NOT NULL "
           "AND (superficie_m2 IS NULL OR latitud IS NULL)")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    log.info("Catastro: %s Bienes offen", len(rows))
    client = CatastroClient(delay=delay)
    done = 0
    for r in rows:
        rc = r["referencia_catastral"]
        updates: dict = {}
        if r["superficie_m2"] is None:
            d = client.datos(rc)
            if d:
                updates.update({k: v for k, v in d.items()
                                if k in ("superficie_m2", "anio_construccion", "uso_catastral") and v is not None})
        if r["latitud"] is None:
            c = client.coords(rc)
            if c:
                updates["latitud"], updates["longitud"] = c
        if updates:
            cols = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE bienes SET {cols} WHERE id = ?", (*updates.values(), r["id"]))
            conn.commit()
            done += 1
    conn.close()
    log.info("Catastro: %s Bienes angereichert", done)
    return done
