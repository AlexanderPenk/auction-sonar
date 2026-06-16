"""Geocoding-Schicht: Adresse → Koordinaten über Nominatim (OpenStreetMap).

Kostenlos und ohne API-Key. Nominatim-Nutzungsrichtlinie verlangt:
max. 1 Anfrage/Sekunde und einen identifizierenden User-Agent. Beides ist hier
eingehalten. Ergebnisse werden in der SQLite gespeichert, also wird jede Adresse
nur einmal aufgelöst.

Für große Mengen oder gewerbliche Nutzung: eigenen Nominatim-Server hosten oder
einen kommerziellen Geocoder verwenden.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

import requests

import config

log = logging.getLogger("geocode")
NOMINATIM = "https://nominatim.openstreetmap.org/search"


def geocode_address(parts: list[str | None], *, session: requests.Session) -> tuple[float, float] | None:
    """Adressbestandteile → (lat, lon) oder None."""
    query = ", ".join(p for p in parts if p)
    if not query:
        return None
    try:
        resp = session.get(NOMINATIM, params={"q": query, "format": "json", "limit": 1,
                                              "countrycodes": "es"},
                           headers={"User-Agent": config.USER_AGENT}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning("Geocoding fehlgeschlagen für %r: %s", query, exc)
    return None


def geocode_pending(db_path: Path = config.DB_PATH, *, delay: float = 1.1,
                    limit: int | None = None) -> int:
    """Alle Bienes ohne Koordinaten auflösen (1 Anfrage/Sekunde)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, direccion, codigo_postal, municipio, provincia FROM bienes "
           "WHERE latitud IS NULL AND (direccion IS NOT NULL OR municipio IS NOT NULL)")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    log.info("Geocoding: %s Bienes offen", len(rows))
    session = requests.Session()
    done = 0
    for r in rows:
        coords = geocode_address(
            [r["direccion"], r["codigo_postal"], r["municipio"], r["provincia"], "España"],
            session=session)
        if coords:
            conn.execute("UPDATE bienes SET latitud = ?, longitud = ? WHERE id = ?",
                         (coords[0], coords[1], r["id"]))
            conn.commit()
            done += 1
        time.sleep(delay)  # Nominatim: max. 1 req/s
    conn.close()
    log.info("Geocoding: %s Bienes verortet", done)
    return done
