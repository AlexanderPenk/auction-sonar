FROM python:3.12-slim
WORKDIR /app

# System-Deps für lxml/pdfplumber
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libxml2-dev libxslt1-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Daten liegen auf einem Volume (persistente SQLite)
ENV CRAWL_INTERVAL_HOURS=0 CRAWL_DAYS_BACK=30
VOLUME ["/app/data"]
EXPOSE 8000
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
