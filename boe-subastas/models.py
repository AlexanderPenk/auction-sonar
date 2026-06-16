"""Datenmodell. Eine Subasta hat mehrere Lotes, ein Lote mehrere Bienes.

Für die Tabelle wird später auf *eine Zeile pro Bien* geflattet, mit den
Subasta-Feldern wiederholt. `referencia_catastral` ist der Join-Key für die
spätere API-Anreicherung (Catastro, Idealista, …).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Bien:
    bien_id: str | None = None          # portal-interne Lauf-ID, falls vorhanden
    tipo: str | None = None             # "Inmueble" / "Vehículo" / …
    subtipo: str | None = None          # "Vivienda" / "Local" / "Garaje" …
    descripcion: str | None = None
    direccion: str | None = None
    municipio: str | None = None
    provincia: str | None = None
    codigo_postal: str | None = None
    referencia_catastral: str | None = None   # ← Join-Key (Catastro)
    idufir: str | None = None                 # ← Join-Key (Registro, Finca única)
    datos_registrales: str | None = None
    vivienda_habitual: str | None = None      # relevant für 60/70%-Regel
    situacion_posesoria: str | None = None    # Besetzung — kritisch für Fix-and-Flip
    visitable: str | None = None
    cargas: str | None = None
    valor_catastral: float | None = None
    latitud: float | None = None
    longitud: float | None = None
    superficie_m2: float | None = None       # Catastro: superficie construida
    anio_construccion: str | None = None      # Catastro: año de construcción
    uso_catastral: str | None = None          # Catastro: uso principal
    superficie_valoracion: float | None = None  # aus dem Valoración-PDF (Art. 666 LEC)
    mercado_eur_m2: float | None = None       # Idealista: Median-€/m² vergleichbarer Angebote
    valor_mercado_est: float | None = None    # geschätzter Marktwert (€/m² × Fläche)
    comps_n: int | None = None                # Anzahl Vergleichsangebote
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Lote:
    lote_id: str | None = None
    numero: int | None = None
    cantidad_reclamada: float | None = None
    valor_subasta: float | None = None
    importe_deposito: float | None = None
    puja_minima: float | None = None
    tramos: float | None = None
    bienes: list[Bien] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Documento:
    nombre: str | None = None
    url: str | None = None
    local_path: str | None = None
    texto: str | None = None            # extrahierter PDF-Text (gekürzt speicherbar)


@dataclass
class Subasta:
    sub_id: str                          # SUB-JA-2026-262986
    boe_anuncio_id: str | None = None    # BOE-B-2026-19311
    tipo_subasta: str | None = None      # "JUDICIAL EN VÍA DE APREMIO" / notarial / …
    estado: str | None = None            # Celebrándose / Próxima / …
    cuenta_expediente: str | None = None
    fecha_inicio: str | None = None      # ISO-String
    fecha_fin: str | None = None
    autoridad_gestora: str | None = None # Juzgado / Notaría / AEAT
    acreedor: str | None = None
    valor_subasta: float | None = None
    tasacion: float | None = None
    cantidad_reclamada: float | None = None
    importe_deposito: float | None = None  # subasta-Ebene bei "Sin lotes"
    puja_minima: float | None = None
    tramos: float | None = None
    lotes_info: str | None = None          # z. B. "Sin lotes" / "3 lotes"
    detail_url: str | None = None
    lotes: list[Lote] = field(default_factory=list)
    documentos: list[Documento] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
