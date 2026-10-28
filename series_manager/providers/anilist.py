"""Proveedor AniList: búsqueda, temporada actual y próximos episodios."""
from __future__ import annotations

import datetime as dt
from typing import Any

from ..config import Config
from ..utils import normalize_title, strip_html
from .http import HttpError, HttpClient

API = "https://graphql.anilist.co"

MEDIA_FIELDS = """
id
title { romaji english native }
synonyms
description(asHtml: false)
coverImage { extraLarge large color }
bannerImage
format
season
seasonYear
status
episodes
duration
genres
averageScore
popularity
isAdult
siteUrl
startDate { year month day }
endDate { year month day }
nextAiringEpisode { episode airingAt timeUntilAirirng }
studios(isMain: true) { nodes { name } }
"""

# Corregimos el nombre del campo en una constante limpia (AniList usa timeUntilAiring).
MEDIA_FIELDS = MEDIA_FIELDS.replace("timeUntilAirirng", "timeUntilAiring")

SEARCH_QUERY = f"""
query ($search: String) {{
  Page(page: 1, perPage: 12) {{
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {{
      {MEDIA_FIELDS}
    }}
  }}
}}
"""

SEASON_QUERY = f"""
query ($season: MediaSeason, $year: Int, $page: Int) {{
  Page(page: $page, perPage: 50) {{
    pageInfo {{ hasNextPage currentPage lastPage }}
    media(season: $season, seasonYear: $year, type: ANIME, sort: POPULARITY_DESC) {{
      {MEDIA_FIELDS}
    }}
  }}
}}
"""

MEDIA_QUERY = f"""
query ($id: Int) {{
  Media(id: $id, type: ANIME) {{
    {MEDIA_FIELDS}
  }}
}}
"""

MEDIA_MANY_QUERY = f"""
query ($ids: [Int]) {{
  Page(page: 1, perPage: 50) {{
    media(id_in: $ids, type: ANIME) {{
      {MEDIA_FIELDS}
    }}
  }}
}}
"""


class AniListClient:
    def __init__(self, cfg: Config) -> None:
        delay = int(cfg.get("http", {}).get("anilist_delay_ms", 850))
        self.http = HttpClient(
            cfg,
            "anilist",
            delay_ms=delay,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        self.cfg = cfg

    def _query(self, query: str, variables: dict[str, Any], ttl_hours: float | None = None) -> dict[str, Any]:
        # El cliente HTTP solo hace GET; para GraphQL usamos el mismo sistema de
        # caché mediante una URL estable con el hash de la consulta.
        import hashlib
        import json

        digest = hashlib.sha1(
            (query + json.dumps(variables, sort_keys=True)).encode("utf-8")
        ).hexdigest()
        url = f"{API}?query_hash={digest}"
        cache_path = self.http._cache_path(url, None)
        cached = self.http._read_cache(cache_path)
        ttl = self.http.ttl_hours if ttl_hours is None else ttl_hours
        if cached and ttl > 0 and cached.get("_age_hours", 1e9) < ttl:
            return cached.get("payload")

        if self.http.delay_ms:
            import time

            elapsed = (time.monotonic() - self.http._last_request) * 1000
            if elapsed < self.http.delay_ms:
                time.sleep((self.http.delay_ms - elapsed) / 1000.0)
        import time

        timeout = float(self.cfg.get("http", {}).get("timeout", 30))
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.http.session.post(
                    API,
                    json={"query": query, "variables": variables},
                    timeout=timeout,
                )
                self.http._last_request = time.monotonic()
                if response.status_code == 429:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                response.raise_for_status()
                payload = response.json()
                if payload.get("errors"):
                    raise HttpError(str(payload["errors"]))
                self.http._write_cache(cache_path, API, payload)
                return payload
            except (Exception, HttpError) as exc:  # requests + JSON + GraphQL
                last_error = exc
                time.sleep(1.0 * (attempt + 1))
        if cached:
            return cached.get("payload")
        raise HttpError(f"No se pudo consultar AniList: {last_error}")

    def search(self, title: str, limit: int = 10) -> list[dict[str, Any]]:
        payload = self._query(SEARCH_QUERY, {"search": title})
        media = ((payload.get("data") or {}).get("Page") or {}).get("media") or []
        results = [normalize_media(item) for item in media]
        results.sort(
            key=lambda m: (
                media_title_score(title, m),
                m.get("popularity") or 0,
            ),
            reverse=True,
        )
        return results[:limit]

    def seasonal(self, season: str, year: int, pages: int = 3) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            payload = self._query(
                SEASON_QUERY, {"season": season.upper(), "year": int(year), "page": page}
            )
            page_data = (payload.get("data") or {}).get("Page") or {}
            media = page_data.get("media") or []
            results.extend(normalize_media(item) for item in media)
            info = page_data.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
        return results

    def media(self, media_id: int) -> dict[str, Any]:
        payload = self._query(MEDIA_QUERY, {"id": int(media_id)})
        data = (payload.get("data") or {}).get("Media")
        if not data:
            raise HttpError(f"AniList no devolvió el anime {media_id}")
        return normalize_media(data)

    def media_many(self, media_ids: list[int]) -> list[dict[str, Any]]:
        """Consulta varias fichas en pocas peticiones (para el seguimiento masivo)."""
        unique = [int(value) for value in dict.fromkeys(media_ids) if int(value) > 0]
        results: list[dict[str, Any]] = []
        for start in range(0, len(unique), 50):
            chunk = unique[start : start + 50]
            payload = self._query(MEDIA_MANY_QUERY, {"ids": chunk})
            page = (payload.get("data") or {}).get("Page") or {}
            results.extend(normalize_media(item) for item in (page.get("media") or []))
        return results


def normalize_media(raw: dict[str, Any]) -> dict[str, Any]:
    title = raw.get("title") or {}
    cover = raw.get("coverImage") or {}
    next_air = raw.get("nextAiringEpisode") or {}
    studios = ((raw.get("studios") or {}).get("nodes") or [])
    start = raw.get("startDate") or {}
    end = raw.get("endDate") or {}
    year = raw.get("seasonYear") or start.get("year")
    english = (title.get("english") or "").strip()
    romaji = (title.get("romaji") or "").strip()
    display = english or romaji or (title.get("native") or "").strip()
    airing_at = next_air.get("airingAt")
    next_air_iso = ""
    if airing_at:
        next_air_iso = dt.datetime.fromtimestamp(int(airing_at), dt.timezone.utc).isoformat()
    return {
        "provider": "anilist",
        "provider_id": str(raw.get("id")),
        "id": int(raw.get("id") or 0),
        "title": display,
        "english": english,
        "romaji": romaji,
        "native": (title.get("native") or "").strip(),
        "synonyms": [
            str(value).strip()
            for value in (raw.get("synonyms") or [])
            if str(value).strip()
        ],
        "year": int(year) if year else None,
        "season": raw.get("season") or "",
        "season_year": raw.get("seasonYear"),
        "status": raw.get("status") or "",
        "format": raw.get("format") or "",
        "episodes": raw.get("episodes"),
        "duration": raw.get("duration"),
        "genres": raw.get("genres") or [],
        "description": strip_html(raw.get("description")),
        "cover_url": cover.get("extraLarge") or cover.get("large") or "",
        "banner_url": raw.get("bannerImage") or "",
        "average_score": raw.get("averageScore"),
        "popularity": raw.get("popularity"),
        "is_adult": bool(raw.get("isAdult")),
        "site_url": raw.get("siteUrl") or "",
        "next_episode": next_air.get("episode"),
        "next_air_at": next_air_iso,
        "studios": [s.get("name") for s in studios if s.get("name")],
        "start_date": start,
        "end_date": end,
    }


def media_title_score(query: str, media: dict[str, Any]) -> float:
    """Puntúa una ficha usando todos sus títulos conocidos (incluidos synonyms)."""
    values = [
        media.get("title"),
        media.get("english"),
        media.get("romaji"),
        media.get("native"),
        *(media.get("synonyms") or []),
    ]
    scores = [
        title_score(query, str(value))
        for value in values
        if isinstance(value, str) and value.strip()
    ]
    return max(scores, default=0.0)


def title_score(query: str, candidate: str, year: int | None = None) -> float:
    a = normalize_title(query)
    b = normalize_title(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        score = 1.0
    elif a in b or b in a:
        score = 0.88
    else:
        ta = set(a.split())
        tb = set(b.split())
        overlap = len(ta & tb) / max(len(ta), len(tb)) if ta and tb else 0.0
        score = 0.25 + 0.65 * overlap
    return max(0.0, min(1.0, score))


def current_season(moment: dt.datetime | None = None) -> tuple[str, int]:
    moment = moment or dt.datetime.now()
    month = moment.month
    if month <= 3:
        season = "WINTER"
    elif month <= 6:
        season = "SPRING"
    elif month <= 9:
        season = "SUMMER"
    else:
        season = "FALL"
    return season, moment.year


SEASON_ES = {
    "WINTER": "Invierno",
    "SPRING": "Primavera",
    "SUMMER": "Verano",
    "FALL": "Otoño",
}
