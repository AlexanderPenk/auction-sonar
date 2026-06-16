"""Seedet eine Demo-SQLite (echtes Objekt + Beispielzeilen mit Koordinaten) und baut das Dashboard."""
import config
config.DB_PATH = config.DATA_DIR / "demo.db"
if config.DB_PATH.exists(): config.DB_PATH.unlink()

from portal import Portal
from models import Subasta, Lote, Bien, Documento
from store import Store
from export import export_html, export_excel

st = Store()

# 1) ECHTES Objekt aus den hochgeladenen HTML-Dateien (+ ungefähre Koordinate Marbella)
real_general = open("sample_general.html", encoding="utf-8").read()
real_bienes  = open("sample_bienes.html",  encoding="utf-8").read()
class Fake:
    def get_text(self,u):
        return real_general if "ver=1" in u else real_bienes if "ver=3" in u else "<html></html>"
sub = Portal(fetcher=Fake()).get_subasta("SUB-JA-2026-262986", on_raw=lambda *a: None)
sub.lotes[0].bienes[0].latitud = 36.5101   # Marbella, Bellavista (approx)
sub.lotes[0].bienes[0].longitud = -4.8856
sub.lotes[0].bienes[0].superficie_m2 = 168.0
sub.lotes[0].bienes[0].anio_construccion = "2003"
sub.lotes[0].bienes[0].uso_catastral = "Residencial"
sub.lotes[0].bienes[0].superficie_valoracion = 172.0
sub.lotes[0].bienes[0].mercado_eur_m2 = 3150.0
sub.lotes[0].bienes[0].valor_mercado_est = round(3150.0*168)
sub.lotes[0].bienes[0].comps_n = 18
st.save_anuncios([{"boe_anuncio_id":sub.boe_anuncio_id,"sub_id":sub.sub_id,"titulo":"Edicto",
                   "seccion":"5","url_pdf":None,"url_xml":None,"url_html":None,"fecha":"2026-06-12"}])
st.upsert_subasta(sub)

def demo(sub_id,boe,tipo,fin,valor,tasa,recl,dep,prov,muni,cp,subtipo,desc,refcat,hab,pos,lat,lon,sup,anio,uso,sup_val,mkt_m2,comps,docs=None):
    s=Subasta(sub_id=sub_id,boe_anuncio_id=boe,tipo_subasta=tipo,fecha_fin=fin,valor_subasta=valor,
              tasacion=tasa,cantidad_reclamada=recl,importe_deposito=dep,lotes_info="Sin lotes",
              detail_url=f"https://subastas.boe.es/detalleSubasta.php?idSub={sub_id}&ver=1")
    b=Bien(tipo="Inmueble",subtipo=subtipo,descripcion=desc,direccion=desc.split(',')[0],provincia=prov,
           municipio=muni,codigo_postal=cp,referencia_catastral=refcat,vivienda_habitual=hab,
           situacion_posesoria=pos,visitable="No consta",latitud=lat,longitud=lon,
           superficie_m2=sup,anio_construccion=anio,uso_catastral=uso,
           superficie_valoracion=sup_val,mercado_eur_m2=mkt_m2,comps_n=comps,
           valor_mercado_est=round(mkt_m2*sup) if (mkt_m2 and sup) else None,
           datos_registrales="Registro de la Propiedad — finca de ejemplo")
    s.lotes=[Lote(numero=1,valor_subasta=valor,importe_deposito=dep,cantidad_reclamada=recl,bienes=[b])]
    for n,u in (docs or []): s.documentos.append(Documento(nombre=n,url=u))
    return s

rows=[
 demo("SUB-JA-2026-251001","BOE-B-2026-18001","JUDICIAL EN VÍA DE APREMIO","2026-06-20T18:00:00+02:00",
   142000,210000,98000,14200,"Madrid","Alcalá de Henares","28804","Vivienda",
   "Piso 3º con plaza de garaje, 92 m²","1234567VK1234S0001AB","No","Ocupado por el ejecutado",40.4818,-3.3643,92,"1998","Residencial",90,2650,22,
   docs=[("Edicto","https://subastas.boe.es/x"),("Certificación cargas","https://subastas.boe.es/y")]),
 demo("SUB-JA-2026-251042","BOE-B-2026-18044","JUDICIAL EN VÍA DE APREMIO","2026-06-18T18:00:00+02:00",
   89000,165000,120000,8900,"Valencia","Gandía","46700","Vivienda",
   "Apartamento a 300 m de la playa, 68 m²","9876543YJ1098N0001QP","Sí","No consta",38.9676,-0.1815,68,"2006","Residencial",70,2950,15),
 demo("SUB-NS-2026-090117","BOE-B-2026-18120","NOTARIAL EN VENTA","2026-07-10T18:00:00+02:00",
   315000,540000,290000,31500,"Barcelona","Sitges","08870","Vivienda",
   "Casa adosada con jardín, 180 m²","5551234DF2055A0001KK","No","Libre de ocupantes",41.2371,1.8055,180,"2010","Residencial",178,3400,12,
   docs=[("Edicto","https://subastas.boe.es/z")]),
 demo("SUB-AT-2026-040088","BOE-B-2026-18210","ADMINISTRATIVA (AEAT)","2026-06-30T18:00:00+02:00",
   47500,52000,47500,4750,"Sevilla","Dos Hermanas","41700","Local",
   "Local comercial en planta baja, 110 m²","2223334GH3344B0001LM","No","No consta",37.2839,-5.9223,110,"1985","Comercial",112,1450,9),
 demo("SUB-JA-2026-251199","BOE-B-2026-18260","JUDICIAL EN VÍA DE APREMIO","2026-06-17T18:00:00+02:00",
   62000,98000,75000,6200,"Alicante","Torrevieja","03180","Vivienda",
   "Bungalow planta baja, 55 m²","4445556HJ5566C0001NO","No","Ocupado, situación desconocida",37.9787,-0.6822,55,"2004","Residencial",54,1700,20),
 demo("SUB-JA-2026-250777","BOE-B-2026-17990","JUDICIAL EN VÍA DE APREMIO","2026-06-14T18:00:00+02:00",
   178000,178000,160000,17800,"Madrid","Getafe","28901","Vivienda",
   "Piso 4 dormitorios, 110 m²","7778889KL7788D0001PQ","Sí","Ocupado por el ejecutado",40.3083,-3.7325,110,"1979","Residencial",108,2100,16),
 demo("SUB-NS-2026-090210","BOE-B-2026-18330","NOTARIAL EN VENTA","2026-08-05T18:00:00+02:00",
   24000,38000,30000,2400,"Málaga","Vélez-Málaga","29700","Garaje",
   "Plaza de garaje + trastero","8889990MN8899E0001RS","No","Libre de ocupantes",36.7847,-4.1009,28,"2008","Almacén-Estacionamiento",None,1200,7),
]
for s in rows:
    st.save_anuncios([{"boe_anuncio_id":s.boe_anuncio_id,"sub_id":s.sub_id,"titulo":"demo",
                       "seccion":"5","url_pdf":None,"url_xml":None,"url_html":None,"fecha":"2026-06-12"}])
    st.upsert_subasta(s)
st.close()

out_html=export_html(config.DATA_DIR/"subastas_dashboard.html")
export_excel(config.DATA_DIR/"subastas.xlsx")
print("Dashboard:",out_html.stat().st_size,"bytes")
import re,json
data=json.loads(re.search(r'const DATA = (\[.*?\]);',out_html.read_text(),re.S).group(1))
print("Zeilen:",len(data),"| mit Koordinaten:",sum(1 for d in data if d['latitud'] is not None))
