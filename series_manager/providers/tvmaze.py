"""Proveedor TVmaze: fichas, temporadas y episodios."""
from __future__ import annotations

import datetime as dt
from typing import Any

from ..config import Config
from ..utils import normalize_title, strip_html
from .http import HttpClient

BASE = "https://api.tvmaze.com"


class TVmazeClient:
    def __init__(self, cfg: Config, force: bool = False) -> None:
        delay = int(cfg.get("http", {}).get("tvmaze_delay_ms", 350))
        self.http = HttpClient(cfg, "tvmaze", delay_ms=delay)
        self.force = force

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self.http.get_json(f"{BASE}/search/shows", {"q": query}, force=self.force)
        results: list[dict[str, Any]] = []
        for item in data or []:
            show = item.get("show") or {}
            results.append(
                {
                    "provider": "tvmaze",
                    "provider_id": str(show.get("id")),
                    "title": show.get("name") or "",
                    "year": _year(show.get("premiered")),
                    "media_type": show.get("type") or "",
                    "score": float(item.get("score") or 0.0),
                    "payload": show,
                }
            )
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    def show(self, show_id: int | str) -> dict[str, Any]:
        data = self.http.get_json(
            f"{BASE}/shows/{show_id}",
            {"embed[]": ["seasons", "episodes"]},
            force=self.force,
        )
        return normalize_show(data)

    def episodes(self, show_id: int | str) -> list[dict[str, Any]]:
        return self.show(show_id)["episodes"]


def _year(value: str | None) -> int | None:
    if not value or len(value) < 4:
        return None
    try:
        return int(value[:4])
    except ValueError:
        return None


def clean_episode(raw: dict[str, Any]) -> dict[str, Any]:
    image = raw.get("image") or {}
    return {
        "season": int(raw.get("season") or 0),
        "number": raw.get("number"),
        "name": (raw.get("name") or "").strip(),
        "airdate": raw.get("airdate") or "",
        "airstamp": raw.get("airstamp") or "",
        "runtime": raw.get("runtime"),
        "summary": strip_html(raw.get("summary")),
        "image_url": image.get("original") or image.get("medium") or "",
        "type": raw.get("type") or "regular",
    }


def normalize_show(raw: dict[str, Any]) -> dict[str, Any]:
    embedded = raw.get("_embedded") or {}
    image = raw.get("image") or {}
    externals = raw.get("externals") or {}
    seasons_raw = embedded.get("seasons") or []
    episodes_raw = embedded.get("episodes") or []

    episodes = [clean_episode(e) for e in episodes_raw]
    by_season: dict[int, list[dict[str, Any]]] = {}
    for episode in episodes:
        by_season.setdefault(episode["season"], []).append(episode)
    for items in by_season.values():
        items.sort(key=lambda e: (e["number"] is None, e["number"] or 0))

    seasons: list[dict[str, Any]] = []
    for season in seasons_raw:
        number = int(season.get("number") or 0)
        if number == 0:
            continue
        season_episodes = by_season.get(number, [])
        # TVmaze a veces deja una temporada duplicada vacía (InuYasha S8).
        if not season_episodes and not season.get("episodeOrder"):
            continue
        seasons.append(
            {
                "number": number,
                "title": season.get("name") or "",
                "episode_order": season.get("episodeOrder"),
                "premiere": season.get("premiereDate") or "",
                "end": season.get("endDate") or "",
                "episodes": season_episodes,
            }
        )
    seasons.sort(key=lambda s: s["number"])

    return {
        "provider": "tvmaze",
        "id": int(raw.get("id") or 0),
        "name": raw.get("name") or "",
        "type": raw.get("type") or "",
        "language": raw.get("language") or "",
        "status": raw.get("status") or "",
        "premiered": raw.get("premiered") or "",
        "ended": raw.get("ended") or "",
        "runtime": raw.get("runtime"),
        "genres": raw.get("genres") or [],
        "summary": strip_html(raw.get("summary")),
        "image_url": image.get("original") or image.get("medium") or "",
        "official_site": raw.get("officialSite") or "",
        "rating": (raw.get("rating") or {}).get("average"),
        "externals": {
            "imdb": externals.get("imdb") or "",
            "thetvdb": externals.get("thetvdb"),
            "tvrage": externals.get("tvrage"),
        },
        "network": ((raw.get("network") or {}).get("name")) or ((raw.get("webChannel") or {}).get("name")) or "",
        "seasons": seasons,
        "episodes": episodes,
        "_by_season": by_season,
    }


def next_airing(episodes: list[dict[str, Any]], now: dt.datetime | None = None) -> dict[str, Any] | None:
    now = now or dt.datetime.now(dt.timezone.utc)
    future: list[tuple[dt.datetime, dict[str, Any]]] = []
    for episode in episodes:
        stamp = episode.get("airstamp")
        if not stamp:
            continue
        try:
            moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment > now:
            future.append((moment, episode))
    if not future:
        return None
    future.sort(key=lambda item: item[0])
    return future[0][1]


def title_score(query: str, candidate: str, year: int | None = None, candidate_year: int | None = None) -> float:
    a = normalize_title(query)
    b = normalize_title(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        score = 1.0
    elif a in b or b in a:
        score = 0.86
    else:
        tokens_a = set(a.split())
        tokens_b = set(b.split())
        if not tokens_a or not tokens_b:
            return 0.0
        overlap = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))
        score = 0.35 + 0.55 * overlap
    if year and candidate_year:
        gap = abs(int(year) - int(candidate_year))
        if gap == 0:
            score += 0.10
        elif gap <= 1:
            score += 0.04
        elif gap >= 4:
            score -= 0.12
    return max(0.0, min(1.0, score))
