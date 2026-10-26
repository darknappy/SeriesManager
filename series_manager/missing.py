"""Criterio para distinguir huecos olvidados de temporadas no descargadas.

El usuario suele borrar temporadas completas o episodios iniciales ya vistos.
Para no llenar el panel de falsos avisos, solo se consideran accionables los
huecos aislados dentro de una temporada que ya tiene la mayoría de episodios.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

# Un hueco accionable es un faltante aislado, no una temporada entera ausente.
GAP_MAX_MISSING = 3
GAP_MIN_PRESENT_RATIO = 0.5


def _today_iso() -> str:
    return dt.date.today().isoformat()


def _is_actionable(present: int, missing: int) -> bool:
    if present <= 0 or missing <= 0 or missing > GAP_MAX_MISSING:
        return False
    return present >= missing * (1 / GAP_MIN_PRESENT_RATIO)


def actionable_seasons(con, series_id: int | None = None) -> list[dict[str, Any]]:
    """Temporadas con huecos aislados que merece la pena revisar."""
    where = ["e.season_number > 0"]
    params: list[Any] = [_today_iso()]
    if series_id is not None:
        where.append("e.series_id = ?")
        params.append(int(series_id))
    rows = con.execute(
        "SELECT s.id AS series_id, s.title, s.year, e.season_number, "
        "SUM(CASE WHEN e.status='present' THEN 1 ELSE 0 END) AS present_count, "
        "SUM(CASE WHEN e.status='missing' AND e.air_date IS NOT NULL "
        "AND e.air_date<=? THEN 1 ELSE 0 END) AS missing_count, "
        "COUNT(e.id) AS total_count "
        "FROM episodes e JOIN series s ON s.id=e.series_id "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY s.id, e.season_number "
        "HAVING missing_count > 0 "
        "ORDER BY s.title COLLATE NOCASE, e.season_number",
        params,
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["present_count"] = int(item["present_count"] or 0)
        item["missing_count"] = int(item["missing_count"] or 0)
        item["is_gap"] = _is_actionable(item["present_count"], item["missing_count"])
        result.append(item)
    return result


def actionable_episodes(con, series_id: int) -> list[dict[str, Any]]:
    """Episodios faltantes que pertenecen a una temporada con huecos aislados."""
    seasons = {
        int(item["season_number"])
        for item in actionable_seasons(con, series_id)
        if item["is_gap"]
    }
    if not seasons:
        return []
    placeholders = ",".join("?" for _ in seasons)
    rows = con.execute(
        "SELECT * FROM episodes WHERE series_id=? AND status='missing' "
        "AND air_date IS NOT NULL AND air_date<=? "
        f"AND season_number IN ({placeholders}) "
        "ORDER BY season_number, episode_number",
        (int(series_id), _today_iso(), *sorted(seasons)),
    ).fetchall()
    return [dict(row) for row in rows]


def actionable_summary(con) -> dict[str, Any]:
    seasons = [item for item in actionable_seasons(con) if item["is_gap"]]
    by_series: dict[int, dict[str, Any]] = {}
    for item in seasons:
        entry = by_series.setdefault(
            int(item["series_id"]),
            {
                "id": int(item["series_id"]),
                "title": item["title"],
                "year": item["year"],
                "missing_count": 0,
                "total_count": 0,
                "seasons": [],
            },
        )
        entry["missing_count"] += int(item["missing_count"])
        entry["total_count"] += int(item["total_count"])
        entry["seasons"].append(
            {
                "season_number": int(item["season_number"]),
                "present_count": int(item["present_count"]),
                "missing_count": int(item["missing_count"]),
            }
        )
    rows = sorted(
        by_series.values(),
        key=lambda item: (-item["missing_count"], item["title"].casefold()),
    )
    return {
        "rows": rows,
        "series": len(rows),
        "missing": sum(item["missing_count"] for item in rows),
        "seasons": seasons,
    }
