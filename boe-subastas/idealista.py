"""Idealista-Marktwertschätzung über Vergleichsangebote (Comps).

Es gibt keinen offiziellen Schätzwert-Endpunkt. Wir nutzen die offizielle Such-API
und bilden aus vergleichbaren Verkaufsangeboten in der Nähe einen Median-€/m².
Daraus: geschätzter Marktwert = Median-€/m² × Fläche.

Voraussetzungen / Grenzen (bewusst transparent):
  • API-Zugang muss bei Idealista beantragt und freigeschaltet werden.
  • Authentifizierung: OAuth2 client_credentials (API-Key + Secret).
  • Die Gratis-Quote ist klein (historisch ~100 Anfragen/Monat) — daher nur auf
    einer Auswahl laufen lassen (`--limit`) und Ergebnisse werden gecacht.
  • Comps ≠ Gutachten: grobe Orientierung, kein belastbares AVM.

Schlüssel über Umgebungsvariablen IDEALISTA_API_KEY / IDEALISTA_SECRET
oder in config setzen. Fehlen sie, wird der Schritt sauber übersprungen.
"""
from __future__ import annotations

import base64
import logging
import os
import sqlite3
import statistics
import time
from pathlib import Path

import requests

import config

log = logging.getLogger("idealista")

TOKEN_URL = "https://api.idealista.com/oauth/token"
SEARCH_URL = "https://api.idealista.com/3.5/es/search"

# Subtipo (BOE) → propertyType (Idealista)
PROPERTY_TYPE = {"Vivienda": "homes", "Local": "premises", "Garaje": "garages",
                 "Almacén-Estacionamiento": "garages"}


def _credentials() -> tuple[str, str] | None:
    key = os.getenv("IDEALISTA_API_KEY", getattr(config, "IDEALISTA_API_KEY", ""))
    secret = os.getenv("IDEALISTA_SECRET", getattr(config, "IDEALISTA_SECRET", ""))
    return (key, secret) if key and secret else None


class IdealistaClient:
    def __init__(self, delay: float = 1.0) -> None:
        creds = _credentials()
        if not creds:
            raise RuntimeError("Idealista-Zugangsdaten fehlen (IDEALISTA_API_KEY / IDEALISTA_SECRET).")
        self.key, self.secret = creds
        self.delay = delay
        self.session = requests.Session()
        self._token: str | None = None

    def _auth(self) -> str:
        if self._token:
            return self._token
        basic = base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
        resp = self.session.post(TOKEN_URL,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": "read"}, timeout=30)
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def comps(self, lat: float, lon: float, *, property_type: str = "homes",
              distance: int = 1000, max_items: int = 40) -> list[dict]:
        """Vergleichsangebote (Verkauf) in der Nähe."""
        token = self._auth()
        resp = self.session.post(SEARCH_URL,
            headers={"Authorization": f"Bearer {token}"},
            data={"operation": "sale", "propertyType": property_type,
                  "center": f"{lat},{lon}", "distance": distance,
                  "maxItems": max_items, "numPage": 1, "locale": "es"}, timeout=30)
        resp.raise_for_status()
        time.sleep(self.delay)
        return resp.json().get("elementList", [])

    def estimate(self, lat: float, lon: float, superficie: float | None,
                 *, property_type: str = "homes") -> dict | None:
        """Median-€/m² der Comps und – falls Fläche bekannt – geschätzter Marktwert."""
        listings = self.comps(lat, lon, property_type=property_type)
        ratios = [l["price"] / l["size"] for l in listings
                  if l.get("price") and l.get("size") and l["size"] > 10]
        if len(ratios) < 3:
            return None
        med = statistics.median(ratios)
        return {"mercado_eur_m2": round(med, 1),
                "valor_mercado_est": round(med * superficie) if superficie else None,
                "comps_n": len(ratios)}


def enrich_pending(db_path: Path = config.DB_PATH, *, limit: int | None = None) -> int:
    """Bienes mit Koordinaten + Fläche, denen ein Marktwert fehlt, schätzen."""
    try:
        client = IdealistaClient()
    except RuntimeError as exc:
        log.warning("%s — Schritt übersprungen.", exc)
        return 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, latitud, longitud, subtipo, "
           "COALESCE(superficie_m2, superficie_valoracion) AS sup "
           "FROM bienes WHERE latitud IS NOT NULL AND valor_mercado_est IS NULL")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    log.info("Idealista: %s Bienes offen", len(rows))
    done = 0
    for r in rows:
        ptype = PROPERTY_TYPE.get(r["subtipo"], "homes")
        try:
            est = client.estimate(r["latitud"], r["longitud"], r["sup"], property_type=ptype)
        except requests.RequestException as exc:
            log.warning("Idealista-Aufruf fehlgeschlagen (id %s): %s", r["id"], exc)
            continue
        if est:
            conn.execute("UPDATE bienes SET mercado_eur_m2 = ?, valor_mercado_est = ?, comps_n = ? "
                         "WHERE id = ?",
                         (est["mercado_eur_m2"], est["valor_mercado_est"], est["comps_n"], r["id"]))
            conn.commit()
            done += 1
    conn.close()
    log.info("Idealista: %s Bienes geschätzt", done)
    return done
