"""Persistenz: append-only JSONL-Rohschicht + kanonische SQLite.

Trennung mit Absicht: erst alles roh nach JSONL (einmal holen), dann
normalisiert nach SQLite. Verfeinerst du später die Extraktion, parst du die
JSONL neu — ohne erneut zu crawlen.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import config
from models import Subasta

log = logging.getLogger("store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS anuncios (
    boe_anuncio_id TEXT PRIMARY KEY,
    sub_id   TEXT, titulo TEXT, seccion TEXT,
    url_pdf  TEXT, url_xml TEXT, url_html TEXT, fecha TEXT,
    enriched INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS subastas (
    sub_id TEXT PRIMARY KEY, boe_anuncio_id TEXT,
    tipo_subasta TEXT, estado TEXT, cuenta_expediente TEXT,
    fecha_inicio TEXT, fecha_fin TEXT,
    autoridad_gestora TEXT, acreedor TEXT,
    valor_subasta REAL, tasacion REAL, cantidad_reclamada REAL,
    importe_deposito REAL, puja_minima REAL, tramos REAL,
    lotes_info TEXT, detail_url TEXT, extra_json TEXT
);
CREATE TABLE IF NOT EXISTS lotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id TEXT, numero INTEGER,
    cantidad_reclamada REAL, valor_subasta REAL, importe_deposito REAL,
    puja_minima REAL, tramos REAL, extra_json TEXT,
    FOREIGN KEY(sub_id) REFERENCES subastas(sub_id)
);
CREATE TABLE IF NOT EXISTS bienes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, lote_id INTEGER, sub_id TEXT,
    tipo TEXT, subtipo TEXT, descripcion TEXT, direccion TEXT, municipio TEXT,
    provincia TEXT, codigo_postal TEXT, referencia_catastral TEXT, idufir TEXT,
    datos_registrales TEXT, vivienda_habitual TEXT, situacion_posesoria TEXT,
    visitable TEXT, cargas TEXT, valor_catastral REAL,
    latitud REAL, longitud REAL,
    superficie_m2 REAL, anio_construccion TEXT, uso_catastral TEXT,
    superficie_valoracion REAL, mercado_eur_m2 REAL, valor_mercado_est REAL,
    comps_n INTEGER, extra_json TEXT,
    FOREIGN KEY(lote_id) REFERENCES lotes(id)
);
CREATE TABLE IF NOT EXISTS documentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id TEXT,
    nombre TEXT, url TEXT, local_path TEXT, texto TEXT
);
CREATE INDEX IF NOT EXISTS idx_bienes_refcat ON bienes(referencia_catastral);
"""


def append_raw(kind: str, url: str | None, payload: Any) -> None:
    """Ein Rohartefakt append-only nach data/raw/<kind>.jsonl."""
    path: Path = config.RAW_DIR / f"{kind}.jsonl"
    record = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
              "url": url, "payload": payload}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


class Store:
    def __init__(self, db_path: Path = config.DB_PATH) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ── Discovery ────────────────────────────────────────────────────────────
    def save_anuncios(self, records: Iterable[dict]) -> int:
        rows = [(r["boe_anuncio_id"], r.get("sub_id"), r.get("titulo"),
                 r.get("seccion"), r.get("url_pdf"), r.get("url_xml"),
                 r.get("url_html"), r.get("fecha")) for r in records]
        self.conn.executemany(
            "INSERT OR IGNORE INTO anuncios "
            "(boe_anuncio_id, sub_id, titulo, seccion, url_pdf, url_xml, url_html, fecha) "
            "VALUES (?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def save_sub_ids(self, sub_ids: Iterable[str], titulo: str = "(portal search)") -> int:
        """SUB-IDs aus der Portal-Suche merken (ohne BOE-Anuncio-Kontext)."""
        rows = [(sid, sid, titulo) for sid in sub_ids]
        self.conn.executemany(
            "INSERT OR IGNORE INTO anuncios (boe_anuncio_id, sub_id, titulo) "
            "VALUES (?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def known_anuncio_ids(self, ids: Iterable[str]) -> set[str]:
        """Welche dieser BOE-Anuncio-IDs sind bereits gespeichert?"""
        ids = [i for i in ids if i]
        out: set[str] = set()
        for i in range(0, len(ids), 500):          # SQLite-Parametergrenze beachten
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT boe_anuncio_id FROM anuncios WHERE boe_anuncio_id IN ({q})", chunk)
            out.update(r["boe_anuncio_id"] for r in rows)
        return out

    def pending_count(self) -> int:
        """Wie viele SUB-IDs sind noch nicht angereichert (für Fortschritt/Auto-Fortsetzung)."""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT sub_id) AS n FROM anuncios "
            "WHERE sub_id IS NOT NULL AND enriched = 0").fetchone()
        return int(row["n"]) if row else 0

    def total_sub_ids(self) -> int:
        """Wie viele SUB-IDs insgesamt bekannt sind (angereichert + wartend)."""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT sub_id) AS n FROM anuncios "
            "WHERE sub_id IS NOT NULL").fetchone()
        return int(row["n"]) if row else 0

    def pending_sub_ids(self, limit: int | None = None) -> list[str]:
        sql = ("SELECT DISTINCT sub_id FROM anuncios "
               "WHERE sub_id IS NOT NULL AND enriched = 0")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["sub_id"] for r in self.conn.execute(sql)]

    def mark_enriched(self, sub_id: str) -> None:
        self.conn.execute("UPDATE anuncios SET enriched = 1 WHERE sub_id = ?", (sub_id,))
        self.conn.commit()

    # ── Normalisierte Subasta speichern (idempotent) ──────────────────────────
    def upsert_subasta(self, sub: Subasta) -> None:
        c = self.conn
        c.execute("DELETE FROM bienes WHERE sub_id = ?", (sub.sub_id,))
        c.execute("DELETE FROM lotes WHERE sub_id = ?", (sub.sub_id,))
        c.execute("DELETE FROM documentos WHERE sub_id = ?", (sub.sub_id,))
        c.execute("""INSERT OR REPLACE INTO subastas
            (sub_id, boe_anuncio_id, tipo_subasta, estado, cuenta_expediente,
             fecha_inicio, fecha_fin, autoridad_gestora, acreedor,
             valor_subasta, tasacion, cantidad_reclamada,
             importe_deposito, puja_minima, tramos, lotes_info,
             detail_url, extra_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sub.sub_id, sub.boe_anuncio_id, sub.tipo_subasta, sub.estado,
             sub.cuenta_expediente, sub.fecha_inicio, sub.fecha_fin,
             sub.autoridad_gestora, sub.acreedor, sub.valor_subasta, sub.tasacion,
             sub.cantidad_reclamada, sub.importe_deposito, sub.puja_minima,
             sub.tramos, sub.lotes_info, sub.detail_url,
             json.dumps(sub.extra, ensure_ascii=False)))
        for lote in sub.lotes:
            cur = c.execute("""INSERT INTO lotes
                (sub_id, numero, cantidad_reclamada, valor_subasta, importe_deposito,
                 puja_minima, tramos, extra_json) VALUES (?,?,?,?,?,?,?,?)""",
                (sub.sub_id, lote.numero, lote.cantidad_reclamada, lote.valor_subasta,
                 lote.importe_deposito, lote.puja_minima, lote.tramos,
                 json.dumps(lote.extra, ensure_ascii=False)))
            lote_pk = cur.lastrowid
            for b in lote.bienes:
                c.execute("""INSERT INTO bienes
                    (lote_id, sub_id, tipo, subtipo, descripcion, direccion, municipio,
                     provincia, codigo_postal, referencia_catastral, idufir,
                     datos_registrales, vivienda_habitual, situacion_posesoria,
                     visitable, cargas, valor_catastral, latitud, longitud,
                     superficie_m2, anio_construccion, uso_catastral,
                     superficie_valoracion, mercado_eur_m2, valor_mercado_est,
                     comps_n, extra_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (lote_pk, sub.sub_id, b.tipo, b.subtipo, b.descripcion, b.direccion,
                     b.municipio, b.provincia, b.codigo_postal, b.referencia_catastral,
                     b.idufir, b.datos_registrales, b.vivienda_habitual,
                     b.situacion_posesoria, b.visitable, b.cargas, b.valor_catastral,
                     b.latitud, b.longitud, b.superficie_m2, b.anio_construccion,
                     b.uso_catastral, b.superficie_valoracion, b.mercado_eur_m2,
                     b.valor_mercado_est, b.comps_n, json.dumps(b.extra, ensure_ascii=False)))
        for d in sub.documentos:
            c.execute("INSERT INTO documentos (sub_id, nombre, url, local_path, texto) "
                      "VALUES (?,?,?,?,?)",
                      (sub.sub_id, d.nombre, d.url, d.local_path, d.texto))
        c.commit()

    def close(self) -> None:
        self.conn.close()
