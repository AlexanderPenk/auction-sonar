# Auction Sonar — Live-Deployment

Der `server.py` macht aus dem Projekt einen privaten Webdienst:

- liefert das Dashboard **live aus der SQLite** (kein manueller Export mehr),
- kleine JSON-API (`/api/data`, `/api/status`, `/api/scope`),
- geschützter **„Crawl now"**-Button (`POST /api/crawl`),
- den Zeitpunkt des **letzten Laufs** dauerhaft (in `data/last_run.json` auf dem Volume),
- optionaler **Scheduler** (Standard: **aus** — manuell per Button; wöchentlich via `CRAWL_INTERVAL_HOURS=168`),
- alles hinter **HTTP-Basic-Auth**.

> **Privat halten.** Der Dienst fragt das BOE-Portal ab (robots.txt verbietet
> automatisierten Zugriff). Discovery läuft über die offizielle BOE-Open-Data-API,
> die Detailseiten werden höflich und niederfrequent geholt. Betreibe das nur für
> dich privat (Auth aktiv, moderates Intervall), nicht als öffentlichen Service.

## Umgebungsvariablen

| Variable | Default | Zweck |
|---|---|---|
| `AUTH_USER` / `AUTH_PASS` | – | Basic-Auth. **Immer setzen.** Leer = offen (nur lokal). |
| `CRAWL_INTERVAL_HOURS` | `0` | Auto-Lauf. **Standard aus** (manuell per Button). `168` = wöchentlich. |
| `CRAWL_DAYS_BACK` | `30` | Zeitfenster der API-Discovery. |
| `CRAWL_ON_START` | `0` | `1` = einmal beim Start crawlen. |
| `IDEALISTA_API_KEY` / `IDEALISTA_SECRET` | – | optional, Marktwert-Schätzung. |
| `PORT` | `8000` | wird vom Host gesetzt. |

Der Suchraum (Provinzen/Typen/Immobilienart/Such-URLs) kommt aus `scope.json` —
am einfachsten direkt im Dashboard im **Scope-Tab** setzen und mit
**„Save scope to server"** speichern.

## Option A — Render.com (am einfachsten, mit `render.yaml`)

1. Repo zu GitHub pushen.
2. Auf Render → **New → Blueprint** → Repo wählen. Render liest `render.yaml`
   (Docker-Web-Service + 1 GB Volume unter `/app/data` für die persistente DB).
3. Im Dashboard `AUTH_USER` / `AUTH_PASS` (und optional die Idealista-Keys) setzen.
4. Deploy. Die App ist unter `https://<name>.onrender.com` erreichbar — Browser
   fragt nach Benutzer/Passwort.
5. Erster Lauf: im Scope-Tab Provinzen wählen → „Save scope to server" → „Crawl now".

> Den **Starter-Plan** nehmen, nicht Free — der Free-Plan schläft ein und das
> Volume/der Scheduler wäre nicht zuverlässig.

## Option B — Railway / Fly.io / eigener VPS (Docker)

Es liegt ein `Dockerfile` bei. Generisch:

```bash
docker build -t auction-sonar .
docker run -d -p 8000:8000 \
  -e AUTH_USER=alex -e AUTH_PASS='langes-passwort' \
  -e CRAWL_INTERVAL_HOURS=24 \
  -v auction_data:/app/data \
  auction-sonar
```

- **Railway:** „Deploy from Repo", erkennt das Dockerfile; ein Volume auf
  `/app/data` mounten; Variablen setzen.
- **Fly.io:** `fly launch` (nutzt das Dockerfile), ein Volume anlegen
  (`fly volumes create auction_data`) und in `fly.toml` auf `/app/data` mounten.
- **VPS:** obiges `docker run` hinter einen Reverse-Proxy (Caddy/Nginx) mit HTTPS.

Das benannte Volume sorgt dafür, dass die SQLite (`data/subastas.db`) Deploys übersteht.

## Option C — leichtgewichtig (nur Dashboard hosten)

Wenn du den Server nicht betreiben willst: den Crawler weiter **lokal** laufen
lassen und nur das exportierte HTML statisch hosten.

- Lokal `python main.py run --from … --to … && python main.py export --html public/index.html`,
  dann `public/` auf Vercel/Netlify/GitHub Pages.
- Oder per **GitHub Actions** (Cron) den Crawler laufen lassen und das erzeugte
  HTML committen/deployen. (Hinweis: Discovery dort nur über die BOE-API halten.)

In diesem Modus gibt es keinen „Crawl now"-Button — das Dashboard erkennt das
automatisch und bleibt im Download-Modus (Scope-Tab exportiert `scope.json`).

## Lokal starten

```bash
pip install -r requirements.txt
AUTH_USER=alex AUTH_PASS=test uvicorn server:app --reload --port 8000
# http://localhost:8000
```
