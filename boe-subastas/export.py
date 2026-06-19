"""Export-Schicht: SQLite → Excel. Eine Zeile pro Bien, Subasta-Felder wiederholt.

Die „schöne Tabelle" wird immer aus der kanonischen SQLite erzeugt, nie von
Hand gepflegt. Für CSV einfach `.to_csv` statt `.to_excel`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

import config

# Join über die ganze Hierarchie; LEFT JOIN, damit auch Subastas ohne erkannte
# Bienes (noch) eine Zeile bekommen.
_QUERY = """
SELECT
    s.sub_id, s.boe_anuncio_id, s.tipo_subasta, s.estado, s.cuenta_expediente,
    s.fecha_inicio, s.fecha_fin, s.autoridad_gestora, s.acreedor,
    s.tasacion, s.valor_subasta, s.cantidad_reclamada,
    s.importe_deposito, s.puja_minima, s.tramos, s.lotes_info,
    l.numero AS lote,
    b.tipo AS bien_tipo, b.subtipo, b.descripcion, b.direccion, b.municipio,
    b.provincia, b.codigo_postal, b.referencia_catastral, b.idufir,
    b.vivienda_habitual, b.situacion_posesoria, b.visitable,
    b.datos_registrales, b.cargas, b.latitud, b.longitud,
    b.superficie_m2, b.anio_construccion, b.uso_catastral,
    b.superficie_valoracion, b.mercado_eur_m2, b.valor_mercado_est, b.comps_n,
    CASE WHEN COALESCE(b.superficie_m2, b.superficie_valoracion) > 0 AND s.valor_subasta IS NOT NULL
         THEN ROUND(s.valor_subasta / COALESCE(b.superficie_m2, b.superficie_valoracion)) END AS precio_m2,
    CASE WHEN b.valor_mercado_est > 0 AND s.valor_subasta IS NOT NULL
         THEN ROUND(100.0 * (b.valor_mercado_est - s.valor_subasta) / s.valor_subasta)
         END AS upside_pct,
    s.detail_url
FROM subastas s
LEFT JOIN lotes  l ON l.sub_id = s.sub_id
LEFT JOIN bienes b ON b.lote_id = l.id
ORDER BY s.fecha_fin, s.sub_id, l.numero
"""


def to_dataframe(db_path: Path = config.DB_PATH) -> pd.DataFrame:
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql_query(_QUERY, conn)


def export_excel(out_path: str | Path = "subastas.xlsx",
                 db_path: Path = config.DB_PATH) -> Path:
    df = to_dataframe(db_path)
    out = Path(out_path)
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Subastas")
        ws = writer.sheets["Subastas"]
        for col_idx, col in enumerate(df.columns, start=1):
            lengths = df[col].astype(str).str.len()
            longest = int(lengths.max()) if lengths.notna().any() else 12
            width = min(60, max(12, longest + 2))
            ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = width
    return out


def export_csv(out_path: str | Path = "subastas.csv",
               db_path: Path = config.DB_PATH) -> Path:
    out = Path(out_path)
    to_dataframe(db_path).to_csv(out, index=False)
    return out


_TEMPLATE = config.ROOT / "dashboard_template.html"


def _rows_with_docs(db_path: Path = config.DB_PATH) -> list[dict]:
    """Eine Zeile pro Bien (als dicts), inkl. angehängter PDF-Dokumente.
    Bereinigt NaN→None und numpy-Skalare→nativ, damit das JSON sowohl im
    Dashboard als auch in der strikten API (allow_nan=False) gültig ist.
    """
    import math
    import sqlite3

    df = to_dataframe(db_path)
    rows = df.to_dict(orient="records")
    for r in rows:
        for k, v in list(r.items()):
            if hasattr(v, "item"):          # numpy-Skalar -> python
                v = v.item()
                r[k] = v
            if isinstance(v, float) and math.isnan(v):
                r[k] = None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        docs: dict[str, list] = {}
        for d in conn.execute("SELECT sub_id, nombre, url FROM documentos"):
            docs.setdefault(d["sub_id"], []).append({"nombre": d["nombre"], "url": d["url"]})
    for r in rows:
        r["documentos"] = docs.get(r["sub_id"], [])
    return rows


def _current_scope() -> dict:
    import json
    sp = config.ROOT / "scope.json"
    if sp.exists():
        try:
            return json.loads(sp.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def build_html(db_path: Path = config.DB_PATH, template: Path = _TEMPLATE) -> str:
    """Rendert das Dashboard-HTML als String aus der SQLite (für Datei *und* Server)."""
    import json

    rows = _rows_with_docs(db_path)
    payload = json.dumps(rows, ensure_ascii=False, default=str)
    html = template.read_text(encoding="utf-8").replace("/*__DATA__*/[]", payload)
    html = html.replace("/*__SCOPE__*/{}", json.dumps(_current_scope(), ensure_ascii=False))
    return html


def export_html(out_path: str | Path = "subastas.html",
                db_path: Path = config.DB_PATH,
                template: Path = _TEMPLATE) -> Path:
    """Generiert das interaktive Dashboard aus der SQLite und schreibt es als Datei."""
    out = Path(out_path)
    out.write_text(build_html(db_path, template), encoding="utf-8")
    return out
