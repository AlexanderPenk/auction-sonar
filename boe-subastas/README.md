# BOE Subastas Crawler

Hybrid-Crawler für spanische Versteigerungen (subastas.boe.es), gebaut als
**generische Engine + seiten-spezifische Extractor**, damit später weitere
Quellen mit minimalem Aufwand andocken.

```
Offizielle BOE-API (Discovery)        Portal-Detailseiten (Tiefe)
  boe.es/datosabiertos/api  ──┐         subastas.boe.es/detalleSubasta.php
   (Anuncios BOE-B-…, SUB-ID) │           (Tasación, Cargas, Catastro, Lotes)
                              ▼                        │
                    ┌───────────────────────────────────────────┐
                    │  RAW-Schicht (JSONL, append-only)          │  ← einmal holen,
                    │  data/raw/*.jsonl                          │    beliebig oft parsen
                    └───────────────────────────────────────────┘
                              │ normalisieren
                              ▼
                    ┌───────────────────────────────────────────┐
                    │  SQLite (kanonisch): subasta · lote · bien │  ← Quelle der Wahrheit
                    │  data/subastas.db                          │    + spätere API-Anreicherung
                    └───────────────────────────────────────────┘
                              │ flatten (1 Zeile pro Bien)
                              ▼
                          Export .xlsx
```

## Schichten

| Datei            | Aufgabe |
|------------------|---------|
| `config.py`      | Rate-Limits, Pfade, User-Agent, Filter |
| `fetcher.py`     | Höfliche HTTP-Session (Delay, Retry, optional Login-Cookie) |
| `boe_api.py`     | Offizielle BOE-Sumario-API: Anuncios holen + Subasta-Filter |
| `portal.py`      | subastas.boe.es: Suche crawlen + Detailseite extrahieren |
| `pdf_extract.py` | PDF herunterladen + Text/Tabellen auslesen |
| `catastro.py`    | Referencia catastral → m², Baujahr, Uso, exakte Koordinaten (Catastro OVC) |
| `geocode.py`     | Adresse → Koordinaten (Nominatim/OSM, Fallback) |
| `idealista.py`   | Marktwert-Schätzung über Vergleichsangebote (Comps) |
| `models.py`      | Datenmodell `Subasta` → `Lote` → `Bien` |
| `store.py`       | RAW-JSONL schreiben + SQLite-Upsert |
| `export.py`      | SQLite → Excel (1 Zeile pro Bien) |
| `pipeline.py`    | Orchestrierung der Stufen |
| `main.py`        | CLI |

## Setup

```bash
pip install -r requirements.txt
```

## Nutzung

```bash
# 1a) Provinz-gezielt: im Portal suchen, die Such-URL kopieren, durchblättern
#     (nur deine Provinzen — empfohlen, schont fremde Server)
python main.py search --url "https://subastas.boe.es/subastas_ava.php?...idBus=..."

# 1b) Oder ganz Spanien über die offizielle API (Zeitraum)
python main.py discover --from 2026-06-01 --to 2026-06-15

# 2) Detailseiten + PDFs zu den gefundenen SUB-IDs holen
python main.py enrich

# 2b) (optional) Catastro-Anreicherung: m², Baujahr, Uso + exakte Koordinaten
python main.py catastro

# 2c) (optional, Fallback) Adressen grob verorten, falls kein Catastro-Treffer
python main.py geocode

# 2d) (optional) Marktwert-Schätzung über Idealista-Vergleichsangebote (API-Key nötig)
export IDEALISTA_API_KEY=... IDEALISTA_SECRET=...
python main.py idealista --limit 50

# 3a) Excel exportieren
python main.py export --out subastas.xlsx

# 3b) Interaktives Dashboard exportieren (filterbar, im Browser öffnen)
python main.py export --html subastas.html

# Alles in einem Rutsch
python main.py run --from 2026-06-01 --to 2026-06-15 --out subastas.xlsx
```

## Suchraum im Dashboard festlegen (Scope-Tab)

Im Dashboard gibt es einen dritten Tab **„Scope"**: dort klickst du Provinzen und
Typen an und exportierst sie als **`scope.json`** (Button „scope.json herunterladen").
Legst du diese Datei in den Projektordner, liest die Pipeline sie automatisch — der
Suchraum ist damit per UI definiert, ohne `config.py` zu editieren. Eine Vorlage
liegt als `scope.json.example` bei. Das Dashboard selbst crawlt nicht; es definiert
nur, was der nächste Pipeline-Lauf zieht.

## Dashboard

`export_html()` erzeugt aus der SQLite ein eigenständiges HTML
(`dashboard_template.html` + eingebettete Daten) — kein Server, kein Build.
Filter nach Provinz, Typ, Immobilienart, Wertspanne, Depósito, Erstwohnsitz und
Besetzung; Volltextsuche; sortierbare Spalten; pro Zeile eine Wert-Spread-Leiste
(Valor gegen Tasación) und eine Frist-Anzeige; Klick öffnet alle Felder samt
Links zur BOE-Seite und den PDFs. Da es reines HTML/JS ist, lässt es sich direkt
als Startpunkt für eine eigene App weiterentwickeln.

Zwei Ansichten: eine **dichte Tabelle** zum schnellen Scannen (sortierbar, jede
Adresse als Google-Maps-Link) und eine **Kartenansicht** mit Pins (Leaflet/OSM).
Die Pins brauchen Koordinaten aus `python main.py geocode` (Nominatim, kostenlos,
max. 1 Anfrage/Sekunde).

## ⚠️ Wichtiger Hinweis: Zugriffswege

- **`boe.es/datosabiertos`** ist die offizielle Open-Data-API und ausdrücklich
  zur Wiederverwendung gedacht. Sie ist der bevorzugte, „saubere" Weg und wird
  hier für die Discovery genutzt.
- **`subastas.boe.es`** untersagt in seiner `robots.txt` automatisierte Zugriffe.
  Der Portal-Teil dieses Crawlers geht gegen diese Vorgabe. Nutze ihn maßvoll
  (niedrige Frequenz, nur für die eigene Recherche, keine Weiterverbreitung der
  Rohdaten) und prüfe die Nutzungsbedingungen selbst. Das ist keine Rechtsberatung.

## Aufbau einer Detailseite (Tabs)

Eine Subasta ist im Portal auf fünf Tabs verteilt (`detalleSubasta.php?idSub=…&ver=N`):

| `ver` | Tab                 | Liefert |
|-------|---------------------|---------|
| 1     | Información general | Typ, Fechas, Valor, Tasación, Depósito, BOE-Kennung, Dokumente |
| 2     | Autoridad gestora   | Juzgado / Notaría / AEAT |
| 3     | **Bienes**          | Adresse, **Referencia catastral**, Cargas — Kern für Fix-and-Flip |
| 4     | Relacionados        | verknüpfte Subastas |
| 5     | Pujas               | aktuelle Gebote (Detail nur mit Login) |

`Portal.get_subasta()` holt ver=1/2/3 und fügt sie zusammen.

## Status der Extraktion

- **ver=1 (general): an echtem HTML verifiziert** ✔
- **ver=3 (bienes): an echtem HTML verifiziert** ✔ — inkl. mehrerer Lotes/Bienes,
  Referencia catastral, IDUFIR, Vivienda habitual, Situación posesoria, Visitable
  und der Finca registral aus `div.caja`.
- **ver=2 (autoridad gestora): generische Label-Ernte, mit `# VERIFY` markiert.**
  Läuft, sollte aber gegen echtes HTML dieses Tabs justiert werden.

Hinweis: `cargas` stehen je nach Objekt nicht in der ver=3-Tabelle, sondern im
PDF *Certificación cargas*. Dieses wird über `documentos` + `pdf_extract.py`
heruntergeladen und ausgelesen.



## Catastro-Anreicherung

`python main.py catastro` löst jede Referencia catastral über die freien
Web-Services der D.G. del Catastro auf (kein Schlüssel, nur nicht-geschützte Daten):

- `Consulta_DNPRC` → Superficie construida (m²), Año de construcción, Uso
- `Consulta_CPMRC` → exakte Parzellen-Koordinaten (EPSG:4326) für pin-genaue Pins

Daraus berechnet das Dashboard die **€/m²-Spalte** (Valor de subasta ÷ Superficie) —
sortier- und filterbar. Geschützt (und daher nicht abrufbar) sind Eigentümer und
Katasterwert; dafür bräuchte es ein elektronisches Zertifikat.


## Marktwert-Schätzung (Idealista)

`python main.py idealista` schätzt den Marktwert über Vergleichsangebote: ähnliche
Verkaufsangebote in der Nähe → Median-€/m² → × Fläche. Daraus die Dashboard-Spalten
**Mercado** (geschätzter Marktwert) und **Upside** (Differenz zum Valor de subasta).

Wichtig: Es gibt keinen offiziellen Schätzwert-Endpunkt. Die Idealista-API muss
beantragt/freigeschaltet werden, die Gratis-Quote ist klein (~100 Anfragen/Monat),
und Comps sind eine grobe Orientierung, kein Gutachten. Daher mit `--limit` nur auf
einer Auswahl laufen lassen; Ergebnisse werden in der SQLite gecacht.

Zusätzlich liest `pdf_extract.py` die **bewertete Fläche aus dem Valoración-PDF**
(Art. 666 LEC) aus und stellt sie der Katasterfläche gegenüber (Spalte „m² c/t"),
weil beide Flächen abweichen können — und genau diese Differenz ist kalkulationsrelevant.


## Nur bestimmte Provinzen crawlen (Scope)

Du musst nicht ganz Spanien crawlen. Zwei Hebel:

1. **Such-basierte Discovery (empfohlen).** Suche im Portal nach deinen Provinzen
   (und Typen), kopiere die Ergebnis-URL (enthält `idBus=`) und gib sie an
   `python main.py search --url "..."` (mehrfach möglich) oder trage sie in
   `SEARCH_URLS` in `config.py` ein. Der Crawler blättert nur durch diese Treffer
   und holt sich genau deren Detailseiten — statt durch ganz Spanien.

2. **Fokus-Filter als Sicherheitsnetz.** Setze `FOCUS_PROVINCIAS` (und optional
   `FOCUS_TIPOS`) in `config.py`, z. B. `("Madrid", "Málaga", "Valencia")`. Beim
   Enrichment werden Subastas außerhalb dieser Provinzen/Typen übersprungen, bevor
   die teuren Schritte (PDF, Catastro, Idealista) laufen.

Hinweis: Die Provinz steht erst auf dem Bienes-Tab (ver=3), nicht in der BOE-API.
Darum filtert der Fokus-Filter nach dem Detail-Abruf; die Such-Discovery vermeidet
den unnötigen Abruf von vornherein.

## Live-Server (gehostet)

Statt das Dashboard als Datei zu öffnen, kann das Projekt als privater Webdienst laufen: `server.py` liefert das Dashboard live aus der SQLite, bietet einen geschützten **„Crawl now“**-Button und einen Scheduler für regelmäßige Läufe — alles hinter Basic-Auth. Anleitung in **DEPLOY.md**.

```bash
pip install -r requirements.txt
AUTH_USER=alex AUTH_PASS=test uvicorn server:app --port 8000
```
