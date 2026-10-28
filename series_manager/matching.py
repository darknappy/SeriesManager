"""Emparejamiento de series con TVmaze y AniList."""
from __future__ import annotations

import json
import re
from typing import Any

from .config import Config
from .db import add_issue, session
from .providers.anilist import AniListClient
from .providers.tvmaze import TVmazeClient, title_score
from .utils import json_dumps, json_loads, now_iso


def _query_variants(query: str) -> list[str]:
    """Variantes conservadoras para transliteraciones habituales de anime."""
    values: list[str] = []

    def add(value: str) -> None:
        value = re.sub(r"\s+", " ", value).strip()
        if value and value not in values:
            values.append(value)

    add(query)
    # AniList usa "Toaru" y no "To Aru" en muchas fichas.
    add(re.sub(r"\bTo\s+Aru\b", "Toaru", query, flags=re.I))
    # TVmaze a veces omite el artículo inicial.
    add(re.sub(r"^The\s+", "", query, flags=re.I))
    return values


def _queries_for(series) -> list[str]:
    result: list[str] = []
    for value in (series["title"], series["original_title"]):
        if value and value not in result:
            result.append(value)
    aliases = json_loads(series["aliases"], []) or []
    for value in aliases:
        if isinstance(value, str) and value and value not in result:
            result.append(value)
    return result[:4]


def _seed_queries(cfg: Config, series) -> list[str]:
    queries = _queries_for(series)
    try:
        from .seeds import load_series_seeds

        seed = load_series_seeds(cfg).get(series["series_key"])
        if seed:
            for value in seed.get("search_titles") or []:
                if value and value not in queries:
                    queries.append(value)
    except Exception:
        pass
    expanded: list[str] = []
    for query in queries:
        for variant in _query_variants(query):
            if variant not in expanded:
                expanded.append(variant)
    return expanded[:10]


def _candidate_titles(item: dict[str, Any]) -> list[str]:
    values = [
        item.get("title"),
        item.get("english"),
        item.get("romaji"),
        item.get("native"),
        *(item.get("synonyms") or []),
    ]
    return [
        str(value).strip()
        for value in values
        if isinstance(value, str) and value.strip()
    ]


def _candidate_score(query: str, item: dict[str, Any], series_year: int | None) -> float:
    candidate_year = item.get("year")
    scores = [
        title_score(query, value, series_year, candidate_year)
        for value in _candidate_titles(item)
    ]
    return max(scores, default=0.0)


def _format_rank(item: dict[str, Any]) -> int:
    """Prefiere series regulares frente a especiales/OVA para una carpeta."""
    fmt = str(item.get("format") or "").upper()
    if fmt in {"TV", "TV_SHORT"}:
        return 3
    if fmt in {"ONA", "OVA"}:
        return 1
    if fmt == "SPECIAL":
        return 0
    return 2


def _pick_best(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    ordered = sorted(
        candidates,
        key=lambda c: (
            float(c.get("score") or 0.0),
            _format_rank(c),
            c.get("provider") == "tvmaze",
        ),
        reverse=True,
    )
    best = ordered[0]
    # TVmaze da episodios; si está casi empatado, se prefiere.
    tvmaze_best = next(
        (item for item in ordered if item.get("provider") == "tvmaze"),
        None,
    )
    if (
        tvmaze_best
        and best.get("provider") != "tvmaze"
        and float(best.get("score") or 0.0) - float(tvmaze_best.get("score") or 0.0) < 0.06
    ):
        best = tvmaze_best
    return best


def search_candidates(cfg: Config, series, limit: int = 8) -> list[dict[str, Any]]:
    tvmaze = TVmazeClient(cfg)
    anilist = AniListClient(cfg)
    queries = _seed_queries(cfg, series)
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for query in queries:
        try:
            for item in tvmaze.search(query, limit=limit):
                score = max(
                    _candidate_score(query, item, series["year"]),
                    item.get("score", 0.0) * 0.35,
                )
                key = ("tvmaze", item["provider_id"])
                current = results.get(key)
                if not current or score > current["score"]:
                    item["score"] = round(score, 4)
                    item["query"] = query
                    results[key] = item
        except Exception:
            continue
        try:
            for item in anilist.search(query, limit=limit):
                score = _candidate_score(query, item, series["year"])
                key = ("anilist", item["provider_id"])
                current = results.get(key)
                if not current or score > current["score"]:
                    item["score"] = round(score, 4)
                    item["year"] = item.get("year") or series["year"]
                    item["query"] = query
                    results[key] = item
        except Exception:
            continue
    ordered = sorted(
        results.values(),
        key=lambda c: (
            c.get("score", 0.0),
            _format_rank(c),
            c.get("provider") == "tvmaze",
        ),
        reverse=True,
    )
    return ordered[: max(limit, 10)]


def store_candidates(con, series_id: int, candidates: list[dict[str, Any]]) -> None:
    con.execute("DELETE FROM match_candidates WHERE series_id=?", (series_id,))
    for item in candidates:
        payload = item.get("payload") or item
        con.execute(
            "INSERT INTO match_candidates(series_id, provider, provider_id, title, year, "
            "media_type, score, payload, selected, created_at) VALUES(?,?,?,?,?,?,?,?,0,?)",
            (
                series_id,
                item.get("provider") or "",
                str(item.get("provider_id") or ""),
                item.get("title") or "",
                item.get("year"),
                item.get("media_type") or item.get("format") or "",
                float(item.get("score") or 0.0),
                json_dumps(payload),
                now_iso(),
            ),
        )


def apply_candidate(con, series, candidate: dict[str, Any], status: str = "manual") -> None:
    series_id = int(series["id"])
    provider = candidate.get("provider")
    provider_id = str(candidate.get("provider_id") or "")
    values: dict[str, Any] = {
        "match_status": status,
        "match_confidence": float(candidate.get("score") or 1.0),
        "match_reason": f"elegido en {provider}",
        "last_synced": None,
        "updated_at": now_iso(),
    }
    if provider == "tvmaze":
        values["tvmaze_id"] = int(provider_id) if provider_id.isdigit() else None
        values["provider"] = "tvmaze"
        values["provider_id"] = provider_id
    elif provider == "anilist":
        values["anilist_id"] = int(provider_id) if provider_id.isdigit() else None
        values["provider"] = "anilist"
        values["provider_id"] = provider_id
        if not series["original_title"] and candidate.get("romaji"):
            values["original_title"] = candidate["romaji"]
        aliases = json_loads(series["aliases"], []) or []
        if not isinstance(aliases, list):
            aliases = []
        for value in [
            candidate.get("english"),
            candidate.get("romaji"),
            candidate.get("native"),
            *(candidate.get("synonyms") or []),
        ]:
            if (
                isinstance(value, str)
                and value.strip()
                and value.strip() != series["title"]
                and value.strip() not in aliases
            ):
                aliases.append(value.strip())
        if aliases:
            values["aliases"] = json_dumps(aliases)
    assignments = ", ".join(f"{k}=?" for k in values)
    con.execute(f"UPDATE series SET {assignments} WHERE id=?", (*values.values(), series_id))
    con.execute(
        "UPDATE match_candidates SET selected=1 WHERE series_id=? AND provider=? AND provider_id=?",
        (series_id, provider, provider_id),
    )


def auto_match(cfg: Config, series, candidates: list[dict[str, Any]] | None = None) -> bool:
    """Devuelve True si encontró un emparejamiento aceptable."""
    threshold = float(cfg.get("auto_match_threshold", 0.80))
    candidates = candidates if candidates is not None else search_candidates(cfg, series)
    best = _pick_best(candidates)
    if not best or float(best.get("score") or 0.0) < threshold:
        return False
    with session(cfg.db_path) as con:
        apply_candidate(con, series, best, status="auto")
    return True


def refresh_all(
    cfg: Config,
    progress=None,
    *,
    only_without_candidates: bool = False,
) -> dict[str, int]:
    """Busca proveedor para las fichas sin TVmaze.

    Con ``only_without_candidates`` solo consulta las que nunca se han
    buscado. Así el escaneo puede intentar identificar carpetas nuevas sin
    repetir cada vez las búsquedas que ya quedaron pendientes de revisión.
    """
    stats = {"series": 0, "matched": 0, "review": 0, "candidates": 0}
    with session(cfg.db_path) as con:
        sql = (
            "SELECT * FROM series WHERE tvmaze_id IS NULL "
            "AND match_status IN ('unmatched','seeded') "
            "AND (EXISTS (SELECT 1 FROM source_groups g WHERE g.series_key=series.series_key) "
            "OR EXISTS (SELECT 1 FROM files f WHERE f.series_id=series.id))"
        )
        if only_without_candidates:
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM match_candidates mc "
                "WHERE mc.series_id=series.id)"
            )
        sql += " ORDER BY title"
        rows = con.execute(sql).fetchall()
        total = max(1, len(rows))
        for index, series in enumerate(rows, start=1):
            if progress:
                progress(int(index / total * 100), f"Buscando {series['title']}")
            candidates = search_candidates(cfg, series)
            store_candidates(con, int(series["id"]), candidates)
            stats["candidates"] += len(candidates)
            stats["series"] += 1
            best = _pick_best(candidates)
            if best and float(best.get("score") or 0.0) >= float(
                cfg.get("auto_match_threshold", 0.80)
            ):
                apply_candidate(con, series, best, status="auto")
                stats["matched"] += 1
            else:
                stats["review"] += 1
                add_issue(
                    con,
                    "needs_match",
                    f"Revisa el emparejamiento de {series['title']}",
                    "warning",
                    series_id=int(series["id"]),
                )
    return stats
