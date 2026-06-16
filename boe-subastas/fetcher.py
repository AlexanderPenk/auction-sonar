"""Höfliche HTTP-Schicht: ein einziger Ort für Delay, Retry, Header, Cookies.

Generisch gehalten — kennt weder BOE noch Portal. Genau das macht sie für
spätere Quellen wiederverwendbar.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests

import config

log = logging.getLogger("fetcher")


class Fetcher:
    def __init__(
        self,
        delay: float = config.REQUEST_DELAY,
        jitter: float = config.REQUEST_JITTER,
        cookies: dict[str, str] | None = None,
    ) -> None:
        self.delay = delay
        self.jitter = jitter
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        if cookies:
            self.session.cookies.update(cookies)
        self._last_request = 0.0

    def _throttle(self) -> None:
        wait = self.delay + random.uniform(0, self.jitter)
        elapsed = time.monotonic() - self._last_request
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last_request = time.monotonic()

    def get(self, url: str, *, params: dict | None = None,
            headers: dict | None = None, stream: bool = False) -> requests.Response:
        """GET mit Throttling und exponentiellem Backoff. Wirft bei Endgültig-Fehler."""
        last_exc: Exception | None = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = self.session.get(
                    url, params=params, headers=headers, stream=stream,
                    timeout=config.REQUEST_TIMEOUT,
                )
                if resp.status_code == 200:
                    return resp
                # 429/5xx → erneut versuchen; 4xx (außer 429) → sofort abbrechen
                if resp.status_code != 429 and 400 <= resp.status_code < 500:
                    resp.raise_for_status()
                log.warning("HTTP %s bei %s (Versuch %s)", resp.status_code, url, attempt)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("Fehler bei %s: %s (Versuch %s)", url, exc, attempt)
            time.sleep(config.BACKOFF_FACTOR ** attempt)
        raise RuntimeError(f"GET fehlgeschlagen nach {config.MAX_RETRIES} Versuchen: {url}") from last_exc

    def get_json(self, url: str, **kw: Any) -> Any:
        headers = {"Accept": "application/json", **kw.pop("headers", {})}
        return self.get(url, headers=headers, **kw).json()

    def get_text(self, url: str, **kw: Any) -> str:
        resp = self.get(url, **kw)
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def download(self, url: str, dest, **kw: Any) -> None:
        resp = self.get(url, stream=True, **kw)
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=8192):
                fh.write(chunk)
