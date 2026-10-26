"""Cliente HTTP con caché en disco, reintentos y límite de velocidad."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import requests

from ..config import Config
from ..utils import now_iso


class HttpError(RuntimeError):
    pass


class HttpClient:
    def __init__(
        self,
        cfg: Config,
        namespace: str,
        *,
        delay_ms: int = 0,
        ttl_hours: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.namespace = namespace
        self.delay_ms = max(0, int(delay_ms))
        self.ttl_hours = (
            ttl_hours
            if ttl_hours is not None
            else float(cfg.get("http", {}).get("cache_ttl_hours", 12))
        )
        self.cache_dir = cfg.cache_root / "http" / namespace
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "SeriesAdmin/1.0 (local media manager)",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
            }
        )
        if headers:
            self.session.headers.update(headers)
        self._last_request = 0.0

    def _cache_path(self, url: str, params: dict[str, Any] | None) -> Path:
        key = url
        if params:
            key += "?" + json.dumps(params, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        fetched = data.get("fetched_at")
        if not fetched:
            return None
        data["_age_hours"] = max(
            0.0, (time.time() - float(data.get("fetched_ts", 0))) / 3600.0
        )
        return data

    def _write_cache(self, path: Path, url: str, payload: Any) -> None:
        tmp = path.with_suffix(".tmp")
        data = {
            "url": url,
            "fetched_at": now_iso(),
            "fetched_ts": time.time(),
            "payload": payload,
        }
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        force: bool = False,
        ttl_hours: float | None = None,
    ) -> Any:
        cache_path = self._cache_path(url, params)
        cached = self._read_cache(cache_path)
        ttl = self.ttl_hours if ttl_hours is None else ttl_hours
        if cached and not force and ttl > 0 and cached.get("_age_hours", 1e9) < ttl:
            return cached.get("payload")

        if self.delay_ms:
            elapsed = (time.monotonic() - self._last_request) * 1000
            if elapsed < self.delay_ms:
                time.sleep((self.delay_ms - elapsed) / 1000.0)

        last_error: Exception | None = None
        timeout = float(self.cfg.get("http", {}).get("timeout", 30))
        for attempt in range(3):
            try:
                response = self.session.get(url, params=params, timeout=timeout)
                self._last_request = time.monotonic()
                if response.status_code == 404:
                    raise HttpError(f"404 en {url}")
                if response.status_code in (429, 500, 502, 503, 504):
                    retry_after = response.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * (attempt + 1)
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                payload = response.json()
                self._write_cache(cache_path, url, payload)
                return payload
            except (requests.RequestException, ValueError, HttpError) as exc:
                last_error = exc
                self._last_request = time.monotonic()
                time.sleep(0.6 * (attempt + 1))

        if cached:
            return cached.get("payload")
        raise HttpError(f"No se pudo consultar {url}: {last_error}")
