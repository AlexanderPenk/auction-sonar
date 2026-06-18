"""Marktwert-Schätzung OHNE Idealista – über lokale €/m²-Richtwerte.

Pro Biene: €/m² aus config.precio_m2_lookup (Barrio → Gemeinde → Provinz),
dann geschätzter Marktwert = €/m² × Wohnfläche. Füllt nur Bienes, denen noch
kein Marktwert (z. B. aus Idealista) zugewiesen wurde — Idealista hat Vorrang.

comps_n = 0 markiert: Schätzung aus Flächen-Richtwert, nicht aus Vergleichs-
angeboten. (Idealista setzt comps_n >= 3.)
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import config

log = logging.getLogger("market_local")


def enrich_pending(db_path: Path = config.DB_PATH, *, limit: int | None = None) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, municipio, provincia, direccion, "
           "COALESCE(superficie_m2, superficie_valoracion) AS sup "
           "FROM bienes WHERE mercado_eur_m2 IS NULL")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    log.info("Markt (lokal): %s Bienes offen", len(rows))
    done = 0
    for r in rows:
        eur_m2, source = config.precio_m2_lookup(r["municipio"], r["provincia"], r["direccion"])
        if not eur_m2:
            continue
        sup = r["sup"]
        valor = round(eur_m2 * sup) if sup else None
        conn.execute(
            "UPDATE bienes SET mercado_eur_m2 = ?, valor_mercado_est = ?, comps_n = 0 "
            "WHERE id = ?",
            (round(eur_m2, 1), valor, r["id"]))
        conn.commit()
        done += 1
    conn.close()
    log.info("Markt (lokal): %s Bienes geschätzt", done)
    return done
