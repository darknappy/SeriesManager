"""Sincronización de metadatos y asignación de archivos a episodios."""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from .config import Config
from .db import add_issue, clear_issues, session, upsert_episode
from .missing import actionable_seasons
from .providers.tvmaze import TVmazeClient, next_airing
from .seeds import load_series_seeds
from .utils import json_dumps, json_loads, now_iso


def _today_iso() -> str:
    return dt.date.today().isoformat()


def _aired(air_date: str | None) -> bool:
    return bool(air_date) and air_date <= _today_iso()


def _provider_episodes(series, show: dict[str, Any], seed: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Convierte la lista de TVmaze a la estructura local de temporadas."""
    raw = [e for e in show["episodes"] if e.get("number") is not None and e.get("season", 0) > 0]
    raw.sort(key=lambda e: (int(e["season"]), float(e["number"])))

    layout = (series["season_layout"] or (seed or {}).get("season_layout") or "").lower()
    slice_season = series["tvmaze_season"] or (seed or {}).get("tvmaze_season")
    shift = int((seed or {}).get("season_shift") or 1)
    absolute_seasons = (seed or {}).get("absolute_seasons")

    if slice_season:
        raw = [e for e in raw if int(e["season"]) == int(slice_season)]
        for item in raw:
            item["_local_season"] = shift
            item["_local_number"] = float(item["number"])
        return _clean_provider_items(raw)

    if layout == "single":
        local: list[dict[str, Any]] = []
        counter = 0
        for item in raw:
            counter += 1
            copy = dict(item)
            copy["_local_season"] = 1
            copy["_local_number"] = counter
            local.append(copy)
        return _clean_provider_items(local)

    if absolute_seasons:
        allowed = {int(x) for x in absolute_seasons}
        raw = [e for e in raw if int(e["season"]) in allowed]

    for item in raw:
        item["_local_season"] = int(item["season"])
        item["_local_number"] = float(item["number"])
    return _clean_provider_items(raw)


def _clean_provider_items(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in raw:
        result.append(
            {
                "season": int(item["_local_season"]),
                "episode": float(item["_local_number"]),
                "original_season": int(item["season"]),
                "original_episode": float(item["number"]),
                "title": (item.get("name") or "").strip(),
                "air_date": item.get("airdate") or "",
                "air_stamp": item.get("airstamp") or "",
                "overview": item.get("summary") or "",
                "runtime": item.get("runtime"),
                "image_url": item.get("image_url") or "",
                "provider_id": f"s{item['season']}e{item['number']}",
                "type": item.get("type") or "regular",
            }
        )
    return result


def _upsert_seasons(con, series_id: int, episodes: list[dict[str, Any]], show_status: str) -> None:
    by_season: dict[int, list[dict[str, Any]]] = {}
    for episode in episodes:
        by_season.setdefault(episode["season"], []).append(episode)
    existing = {int(r["number"]) for r in con.execute(
        "SELECT number FROM seasons WHERE series_id=?", (series_id,)
    )}
    for number, items in sorted(by_season.items()):
        aired = sum(1 for e in items if _aired(e["air_date"]))
        future = sorted(
            (e for e in items if e["air_date"] and e["air_date"] > _today_iso()),
            key=lambda e: e["air_date"],
        )
        next_ep = future[0]["episode"] if future else None
        next_at = future[0]["air_date"] if future else None
        is_airing = 1 if future else 0
        title = f"Season {number:02d}" if number > 0 else "Specials"
        values = {
            "title": title,
            "total_episodes": len(items),
            "aired_episodes": aired,
            "status": show_status,
            "is_airing": is_airing,
            "next_episode": next_ep,
            "next_air_at": next_at,
            "source": "tvmaze",
            "updated_at": now_iso(),
        }
        row = con.execute(
            "SELECT id FROM seasons WHERE series_id=? AND number=?", (series_id, number)
        ).fetchone()
        if row:
            assignments = ", ".join(f"{k}=?" for k in values)
            con.execute(
                f"UPDATE seasons SET {assignments} WHERE id=?",
                (*values.values(), int(row["id"])),
            )
        else:
            values.update({"series_id": series_id, "number": number})
            columns = ", ".join(values.keys())
            placeholders = ", ".join("?" for _ in values)
            con.execute(f"INSERT INTO seasons({columns}) VALUES({placeholders})", tuple(values.values()))
        existing.discard(number)
    # Temporadas que ya no existen en el proveedor (solo si están vacías).
    for number in existing:
        con.execute(
            "DELETE FROM seasons WHERE series_id=? AND number=? AND "
            "NOT EXISTS (SELECT 1 FROM files WHERE files.series_id=? AND files.season_number=? "
            "AND files.status='present')",
            (series_id, number, series_id, number),
        )


def _season_id(con, series_id: int, number: int) -> int | None:
    row = con.execute(
        "SELECT id FROM seasons WHERE series_id=? AND number=?", (series_id, number)
    ).fetchone()
    if row:
        return int(row["id"])
    # Series sin proveedor: las temporadas nacen de los propios archivos.
    title = f"Season {number:02d}" if number > 0 else "Specials"
    con.execute(
        "INSERT INTO seasons(series_id, number, title, source, updated_at) VALUES(?,?,?,?,?)",
        (series_id, int(number), title, "file", now_iso()),
    )
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def _upsert_provider_episodes(con, series_id: int, episodes: list[dict[str, Any]]) -> set[tuple[int, float]]:
    keys: set[tuple[int, float]] = set()
    for episode in episodes:
        season_id = _season_id(con, series_id, episode["season"])
        upsert_episode(
            con,
            series_id,
            episode["season"],
            episode["episode"],
            title=episode["title"] or None,
            air_date=episode["air_date"] or None,
            overview=episode["overview"] or None,
            runtime=episode["runtime"],
            image_url=episode["image_url"] or None,
            source="tvmaze",
            provider_id=episode["provider_id"],
            season_id=season_id,
            is_special=0,
        )
        keys.add((episode["season"], float(episode["episode"])))
    if keys:
        rows = con.execute(
            "SELECT id, season_number, episode_number FROM episodes "
            "WHERE series_id=? AND source='tvmaze'",
            (series_id,),
        ).fetchall()
        for row in rows:
            if (int(row["season_number"]), float(row["episode_number"])) not in keys:
                con.execute("DELETE FROM episodes WHERE id=?", (int(row["id"]),))
    return keys


def _synthetic_episodes(series, seed: dict[str, Any] | None) -> list[dict[str, Any]]:
    total = series["total_episodes"] or (seed or {}).get("total_episodes")
    if not total:
        return []
    return [
        {
            "season": 1,
            "episode": float(number),
            "title": "",
            "air_date": "",
            "air_stamp": "",
            "overview": "",
            "runtime": None,
            "image_url": "",
            "provider_id": f"local-{number}",
            "type": "regular",
        }
        for number in range(1, int(total) + 1)
    ]


def _absolute_map(episodes: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    ordered = sorted(episodes, key=lambda e: (e["season"], e["episode"]))
    return {index: episode for index, episode in enumerate(ordered, start=1)}


def _assign_files(con, series, episodes: list[dict[str, Any]], absolute_map: dict[int, dict[str, Any]]) -> dict[str, int]:
    stats = {"assigned": 0, "unassigned": 0, "cross_season": 0}
    files = con.execute(
        "SELECT * FROM files WHERE series_id=? AND kind='video' AND status='present'",
        (int(series["id"]),),
    ).fetchall()
    provider_keys = {(e["season"], float(e["episode"])) for e in episodes}
    for file in files:
        season = file["season_number"]
        number = file["episode_number"]
        episode_end = file["episode_end"] or 0
        absolute = file["absolute_number"]
        absolute_end = file["absolute_end"]
        if file["parsed_special"]:
            season, number = 0, file["episode_number"] or 1
        elif series["series_key"] and absolute and absolute_map:
            # Compatibilidad con catálogos antiguos: si no existe `absolute_end`
            # pero el fin es mayor o igual que el inicio absoluto, es un rango.
            if absolute_end is None and episode_end and float(episode_end) >= float(absolute):
                absolute_end = episode_end
            mapped = absolute_map.get(int(round(float(absolute))))
            if not mapped:
                add_issue(
                    con,
                    "absolute_out_of_range",
                    f"El episodio absoluto {int(absolute)} no existe en la ficha de TVmaze: "
                    f"{file['rel_path']}",
                    "warning",
                    series_id=int(series["id"]),
                    file_id=int(file["id"]),
                )
                stats["unassigned"] += 1
                continue
            season = mapped["season"]
            number = mapped["episode"]
            if absolute_end:
                end_map = absolute_map.get(int(round(float(absolute_end))))
                if end_map:
                    if end_map["season"] != season:
                        add_issue(
                            con,
                            "multi_episode_cross_season",
                            f"El archivo abarca episodios de dos temporadas y no se puede nombrar "
                            f"como un solo episodio: {file['rel_path']}",
                            "warning",
                            series_id=int(series["id"]),
                            file_id=int(file["id"]),
                        )
                        episode_end = 0
                        stats["cross_season"] += 1
                    else:
                        episode_end = float(end_map["episode"])
                else:
                    episode_end = 0
            else:
                episode_end = 0

        if season is None or number is None:
            stats["unassigned"] += 1
            add_issue(
                con,
                "file_without_episode",
                f"No se pudo asignar temporada/episodio: {file['rel_path']}",
                "warning",
                series_id=int(series["id"]),
                file_id=int(file["id"]),
            )
            continue

        season = int(season)
        number = float(number)
        if (
            provider_keys
            and season != 0
            and (season, number) not in provider_keys
        ):
            # Episodio no listado por el proveedor: se conserva como extra.
            add_issue(
                con,
                "episode_not_in_provider",
                f"El archivo apunta a S{season:02d}E{number:g}, que no está en TVmaze: {file['rel_path']}",
                "info",
                series_id=int(series["id"]),
                file_id=int(file["id"]),
            )
        # Si el proveedor ya tiene el episodio individual, el archivo (aunque
        # cubra un rango) debe apuntar a esa fila. El rango se guarda en
        # `files.episode_end`; crear otra fila inflaría el total de la serie.
        base_episode = con.execute(
            "SELECT id FROM episodes WHERE series_id=? AND season_number=? "
            "AND episode_number=? AND episode_end=0 LIMIT 1",
            (int(series["id"]), season, number),
        ).fetchone()
        if base_episode:
            episode_id = int(base_episode["id"])
            con.execute(
                "UPDATE episodes SET season_id=?, status='present', source=COALESCE(source,'file'), "
                "updated_at=? WHERE id=?",
                (_season_id(con, int(series["id"]), season), now_iso(), episode_id),
            )
        else:
            episode_id = upsert_episode(
                con,
                int(series["id"]),
                season,
                number,
                episode_end=float(episode_end or 0),
                season_id=_season_id(con, int(series["id"]), season),
                status="present",
                source="file",
                absolute_number=float(absolute) if absolute else None,
            )
        con.execute(
            "UPDATE files SET episode_id=?, season_number=?, episode_number=?, episode_end=?, "
            "status='present', updated_at=? WHERE id=?",
            (
                episode_id,
                season,
                number,
                float(episode_end or 0),
                now_iso(),
                int(file["id"]),
            ),
        )
        stats["assigned"] += 1
    return stats


def _cleanup_orphan_file_episodes(
    con,
    series_id: int,
    *,
    remove_synthetic: bool = False,
) -> None:
    """Elimina episodios creados por archivos que ya no los referencian."""
    if remove_synthetic:
        con.execute(
            "DELETE FROM episodes WHERE series_id=? AND provider_id LIKE 'local-%' "
            "AND NOT EXISTS (SELECT 1 FROM files WHERE files.episode_id=episodes.id) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM files f WHERE f.series_id=episodes.series_id "
            "  AND f.kind='video' AND f.status='present' AND f.season_number=episodes.season_number "
            "  AND f.episode_end>0 AND f.episode_number<=episodes.episode_number "
            "  AND f.episode_end>=episodes.episode_number"
            ")",
            (series_id,),
        )
    con.execute(
        "DELETE FROM episodes WHERE series_id=? AND provider_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM files WHERE files.episode_id=episodes.id)",
        (series_id,),
    )


def _finalize_episodes(con, series_id: int) -> dict[str, int]:
    stats = {"present": 0, "missing": 0, "unaired": 0}
    today = _today_iso()
    rows = con.execute("SELECT * FROM episodes WHERE series_id=?", (series_id,)).fetchall()
    for episode in rows:
        # Un archivo multi-episodio solo queda enlazado al primer episodio; para
        # saber si el episodio final también está presente hay que mirar el rango.
        count = con.execute(
            "SELECT COUNT(*) FROM files WHERE series_id=? AND kind='video' AND status='present' "
            "AND (episode_id=? OR (season_number=? AND episode_end>episode_number "
            "AND episode_number<=? AND episode_end>=?))",
            (
                series_id,
                int(episode["id"]),
                int(episode["season_number"]),
                float(episode["episode_number"]),
                float(episode["episode_number"]),
            ),
        ).fetchone()[0]
        if count:
            status = "present"
            stats["present"] += 1
        elif episode["air_date"] and episode["air_date"] > today:
            status = "unaired"
            stats["unaired"] += 1
        else:
            status = "missing"
            stats["missing"] += 1
        con.execute("UPDATE episodes SET status=? WHERE id=?", (status, int(episode["id"])))
    return stats


def _series_airing(con, series, episodes: list[dict[str, Any]], horizon_days: int) -> dict[str, Any]:
    today = dt.date.today()
    future = sorted(
        (e for e in episodes if e["air_date"] and e["air_date"] > today.isoformat()),
        key=lambda e: e["air_date"],
    )
    aired = sorted(
        (e for e in episodes if e["air_date"] and e["air_date"] <= today.isoformat()),
        key=lambda e: e["air_date"],
    )
    last_aired = aired[-1]["air_date"] if aired else None
    next_ep = future[0] if future else None
    is_airing = False
    if next_ep:
        is_airing = True
    elif last_aired and (today - dt.date.fromisoformat(last_aired)).days <= horizon_days:
        if (series["status"] or "").lower() not in {"ended", "canceled"}:
            is_airing = True
    return {
        "is_airing": 1 if is_airing else 0,
        "next_episode": next_ep["episode"] if next_ep else None,
        "next_air_at": next_ep["air_stamp"] or next_ep["air_date"] if next_ep else None,
        "last_aired": last_aired,
    }


def sync_metadata(
    cfg: Config,
    progress: Callable[[int, str], None] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    cfg.ensure_dirs()
    seeds = load_series_seeds(cfg)
    client = TVmazeClient(cfg, force=force)
    stats: dict[str, Any] = {
        "series": 0,
        "synced": 0,
        "without_provider": 0,
        "episodes": 0,
        "assigned": 0,
        "unassigned": 0,
        "missing": 0,
        "errors": [],
    }
    horizon = int(cfg.get("airing_horizon_days", 45))

    with session(cfg.db_path) as con:
        for code in (
            "missing_episodes",
            "provider_error",
            "episode_not_in_provider",
            "absolute_out_of_range",
            "multi_episode_cross_season",
            "file_without_episode",
            "series_without_provider",
        ):
            con.execute("DELETE FROM issues WHERE code=?", (code,))
        rows = con.execute("SELECT * FROM series ORDER BY title").fetchall()
        total = max(1, len(rows))
        for index, series in enumerate(rows, start=1):
            if progress:
                progress(int((index - 1) / total * 95), f"Sincronizando {series['title']}")
            series_id = int(series["id"])
            seed = seeds.get(series["series_key"])
            provider_episodes: list[dict[str, Any]] = []
            show_status = series["status"] or "unknown"
            has_video = con.execute(
                "SELECT 1 FROM files WHERE series_id=? AND kind='video' AND status='present' LIMIT 1",
                (series_id,),
            ).fetchone() is not None
            if series["tvmaze_id"]:
                try:
                    show = client.show(int(series["tvmaze_id"]))
                    provider_episodes = _provider_episodes(series, show, seed)
                    show_status = show.get("status") or show_status
                    _upsert_seasons(con, series_id, provider_episodes, show_status)
                    _upsert_provider_episodes(con, series_id, provider_episodes)
                    set_values = {
                        "overview": show.get("summary") or series["overview"],
                        "cover_url": show.get("image_url") or series["cover_url"],
                        "banner_url": show.get("image_url") or series["banner_url"],
                        "genres": json_dumps(show.get("genres") or []),
                        "status": show.get("status") or series["status"],
                        "media_type": series["media_type"] or show.get("type") or "tv",
                        "last_synced": now_iso(),
                        "match_status": "matched",
                        "match_confidence": max(float(series["match_confidence"] or 0), 0.95),
                        "match_reason": "TVmaze",
                        "updated_at": now_iso(),
                    }
                    con.execute(
                        "UPDATE series SET " + ", ".join(f"{k}=?" for k in set_values) + " WHERE id=?",
                        (*set_values.values(), series_id),
                    )
                    stats["synced"] += 1
                except Exception as exc:  # red o datos
                    stats["errors"].append(f"{series['title']}: {exc}")
                    add_issue(
                        con,
                        "provider_error",
                        f"No se pudo sincronizar con TVmaze: {exc}",
                        "error",
                        series_id=series_id,
                    )
            else:
                stats["without_provider"] += 1
                # Si ya hay archivos, la estructura real de temporadas la dan
                # los propios archivos. Generar episodios sintéticos en una
                # sola temporada inventaría huecos (p. ej. Ranma S01/S02/S03).
                if not has_video:
                    provider_episodes = _synthetic_episodes(series, seed)
                    if provider_episodes:
                        _upsert_seasons(con, series_id, provider_episodes, show_status)
                        _upsert_provider_episodes(con, series_id, provider_episodes)
                if seed and (seed.get("tmdb_id") or series["tmdb_id"]):
                    con.execute(
                        "UPDATE series SET match_status='manual', match_confidence=1.0, "
                        "match_reason='TMDB/semilla' WHERE id=? AND match_status IN ('unmatched','seeded')",
                        (series_id,),
                    )

            absolute_map: dict[int, dict[str, Any]] = {}
            if series["series_key"] and (seed or {}).get("absolute_numbering"):
                absolute_map = _absolute_map(provider_episodes)

            assign_stats = _assign_files(con, series, provider_episodes, absolute_map)
            stats["assigned"] += assign_stats["assigned"]
            stats["unassigned"] += assign_stats["unassigned"]
            _cleanup_orphan_file_episodes(
                con,
                series_id,
                remove_synthetic=has_video and not series["tvmaze_id"],
            )
            final_stats = _finalize_episodes(con, series_id)
            stats["missing"] += final_stats["missing"]
            stats["episodes"] += final_stats["present"] + final_stats["missing"] + final_stats["unaired"]

            # Temporadas sin datos de proveedor pero con archivos (p. ej. especiales).
            for season_row in con.execute(
                "SELECT number FROM seasons WHERE series_id=?", (series_id,)
            ).fetchall():
                number = int(season_row["number"])
                count = con.execute(
                    "SELECT COUNT(*) FROM episodes WHERE series_id=? AND season_number=?",
                    (series_id, number),
                ).fetchone()[0]
                if count == 0:
                    continue
                present = con.execute(
                    "SELECT COUNT(*) FROM episodes WHERE series_id=? AND season_number=? AND status='present'",
                    (series_id, number),
                ).fetchone()[0]
                con.execute(
                    "UPDATE seasons SET total_episodes=?, aired_episodes=?, updated_at=? "
                    "WHERE series_id=? AND number=?",
                    (count, present, now_iso(), series_id, number),
                )

            airing = _series_airing(con, series, provider_episodes, horizon)
            total_eps = con.execute(
                "SELECT COUNT(*) FROM episodes WHERE series_id=?", (series_id,)
            ).fetchone()[0]
            season_count = con.execute(
                "SELECT COUNT(*) FROM seasons WHERE series_id=?", (series_id,)
            ).fetchone()[0]
            con.execute(
                "UPDATE series SET is_airing=?, next_episode=?, next_air_at=?, last_aired=?, "
                "total_episodes=?, season_count=?, updated_at=? WHERE id=?",
                (
                    airing["is_airing"],
                    airing["next_episode"],
                    airing["next_air_at"],
                    airing["last_aired"],
                    total_eps,
                    season_count,
                    now_iso(),
                    series_id,
                ),
            )

            gap_seasons = {
                int(item["season_number"])
                for item in actionable_seasons(con, series_id)
                if item["is_gap"]
            }
            missing: list[Any] = []
            if gap_seasons:
                placeholders = ",".join("?" for _ in gap_seasons)
                missing = con.execute(
                    "SELECT season_number, episode_number, title, air_date FROM episodes "
                    "WHERE series_id=? AND status='missing' AND season_number IN "
                    f"({placeholders}) AND air_date IS NOT NULL AND air_date<=? "
                    "ORDER BY season_number, episode_number",
                    (series_id, *sorted(gap_seasons), _today_iso()),
                ).fetchall()
            if missing:
                labels = [
                    f"S{int(m['season_number']):02d}E{float(m['episode_number']):g}"
                    for m in missing
                ]
                add_issue(
                    con,
                    "missing_episodes",
                    f"Huecos aislados en temporadas ya empezadas: {', '.join(labels[:18])}"
                    + ("…" if len(labels) > 18 else ""),
                    "warning",
                    series_id=series_id,
                    details={
                        "count": len(missing),
                        "episodes": [
                            {
                                "season": int(m["season_number"]),
                                "episode": float(m["episode_number"]),
                                "title": m["title"],
                                "air_date": m["air_date"],
                            }
                            for m in missing
                        ],
                    },
                )

        con.execute(
            "INSERT INTO meta(key, value) VALUES('last_sync', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (now_iso(),),
        )
    if progress:
        progress(100, "Sincronización terminada")
    return stats
