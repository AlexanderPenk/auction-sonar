import datetime as dt
import config
config.DB_PATH = config.DATA_DIR / "smoke.db"
if config.DB_PATH.exists(): config.DB_PATH.unlink()

import boe_api
from portal import (parse_amount, parse_fecha, extract_general,
                    extract_bienes, Portal)
from store import Store
from export import to_dataframe, export_excel

# 1) Parser
assert parse_amount("193.349,25 €") == 193349.25
assert parse_amount("0,00 €") == 0.0 and parse_amount("Sin tramos") is None
assert parse_fecha("x (ISO: 2026-07-02T18:00:00+02:00)") == "2026-07-02T18:00:00+02:00"
print("OK  parser")

real_general = open("sample_general.html", encoding="utf-8").read()
real_bienes  = open("sample_bienes.html",  encoding="utf-8").read()

# 2) ver=1 (echt)
g = extract_general(real_general, "SUB-JA-2026-262986")
assert g.valor_subasta == 193349.25 and g.importe_deposito == 9667.46
assert g.boe_anuncio_id == "BOE-B-2026-19311" and len(g.documentos) == 3
print("OK  extract_general (echt)")

# 3) ver=3 (echt) -> Lotes mit Bienes
lotes = extract_bienes(real_bienes)
assert len(lotes) == 1, len(lotes)
b = lotes[0].bienes[0]
assert b.tipo == "Inmueble" and b.subtipo == "Vivienda", (b.tipo, b.subtipo)
assert b.referencia_catastral == "002006300UF34C0001HA"
assert b.idufir == "29029000722052"
assert b.municipio == "Marbella" and b.provincia == "Málaga"
assert b.codigo_postal == "29600"
assert b.vivienda_habitual == "No"
assert b.situacion_posesoria == "No consta" and b.visitable == "No consta"
assert b.datos_registrales and "FINCA REGISTRAL" in b.datos_registrales
print("OK  extract_bienes (echt):", b.subtipo, "|", b.municipio, b.provincia,
      "| refcat", b.referencia_catastral, "| habitual", b.vivienda_habitual)

# 4) get_subasta multi-tab mit echten ver=1/ver=3
class FakeFetcher:
    def get_text(self, url):
        if "ver=1" in url: return real_general
        if "ver=3" in url: return real_bienes
        return "<html><body></body></html>"
p = Portal(fetcher=FakeFetcher())
sub = p.get_subasta("SUB-JA-2026-262986", on_raw=lambda k,u,h: None)
# Subasta-Beträge auf den Einzel-Lote übertragen?
assert sub.lotes[0].valor_subasta == 193349.25
assert sub.lotes[0].importe_deposito == 9667.46
assert sub.lotes[0].bienes[0].referencia_catastral == "002006300UF34C0001HA"
print("OK  get_subasta -> Lote-Valor + Bien verknüpft")

# 5) Store + Export
st = Store()
st.save_anuncios([{"boe_anuncio_id":"BOE-B-2026-19311","sub_id":"SUB-JA-2026-262986",
                   "titulo":"Edicto subasta","seccion":"5","url_pdf":None,
                   "url_xml":None,"url_html":None,"fecha":"2026-06-12"}])
st.upsert_subasta(sub); st.mark_enriched("SUB-JA-2026-262986")
st.close()
df = to_dataframe()
out = export_excel(config.DATA_DIR / "smoke.xlsx")
print("OK  export ->", len(df), "Zeile(n),", len(df.columns), "Spalten,", out.stat().st_size, "bytes")
row = df.iloc[0]
print("\n  === Eine fertige Tabellenzeile ===")
for k in ["sub_id","tipo_subasta","fecha_fin","valor_subasta","tasacion",
          "importe_deposito","cantidad_reclamada","bien_tipo","subtipo",
          "descripcion","direccion","codigo_postal","municipio","provincia",
          "referencia_catastral","idufir","vivienda_habitual","situacion_posesoria",
          "cargas","datos_registrales"]:
    print(f"    {k:22}: {row[k]}")

print("\nALLE TESTS BESTANDEN")
