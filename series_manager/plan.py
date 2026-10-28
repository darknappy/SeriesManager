"""Construcción del plan de renombrado/movimiento (siempre en seco)."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .db import session
from .naming import (
    IMAGE_EXTS,
    clean_episode_title,
    folder_name_for_series,
    season_folder_name,
    target_paths,
)
from .utils import (
    human_size,
    is_subpath,
    json_dumps,
    json_loads,
    now_iso,
    relative_display,
)

RESOLUTION_RANK = {"2160p": 5, "1440p": 4, "1080p": 3, "720p": 2, "576p": 1, "480p": 1}


def _key(path: str | Path) -> str:
    return os.path.normcase(str(path)).casefold()


def _display_series(series) -> str:
    if series["year"]:
        return f"{series['title']} ({int(series['year'])})"
    return series["title"]


def _resolution_rank(value: str | None) -> int:
    return RESOLUTION_RANK.get((value or "").lower(), 0)


def _choose_primary(rows: list[Any]) -> Any:
    return sorted(
        rows,
        key=lambda row: (
            1 if row["status"] == "present" else 0,
            1 if row["episode_id"] else 0,
            _resolution_rank(row["resolution"]),
            int(row["size"] or 0),
            float(row["mtime"] or 0),
            -len(str(row["path"])),
        ),
        reverse=True,
    )[0]


def _fingerprint_duplicate_victims(con) -> set[int]:
    """Archivos que la fase global de duplicados enviará a cuarentena."""
    rows = con.execute(
        "SELECT * FROM files WHERE kind='video' AND status IN ('present','unparsed') "
        "AND fingerprint IS NOT NULL AND fingerprint<>'' ORDER BY fingerprint, path"
    ).fetchall()
    groups: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        groups[row["fingerprint"]].append(row)
    victims: set[int] = set()
    for items in groups.values():
        if len(items) < 2:
            continue
        primary = _choose_primary(items)
        victims.update(int(row["id"]) for row in items if row["id"] != primary["id"])
    return victims


def _unique_target(target: Path) -> Path:
    if not target.exists():
        return target
    suffix = target.suffix
    stem = target.stem
    for number in range(2, 50):
        candidate = target.with_name(f"{stem} ({number}){suffix}")
        if not candidate.exists():
            return candidate
    return target


def _episode_title(episode, file_row, config: Config, series=None) -> str:
    series_title = series["title"] if series else None
    series_year = series["year"] if series else None
    title = clean_episode_title(
        (episode["title"] if episode else "") or "",
        series_title,
        series_year,
    )
    if title:
        return title
    hint = clean_episode_title(
        (file_row["title_hint"] or "").strip(),
        series_title,
        series_year,
    )
    if len(hint) >= 3:
        return hint
    if episode and (
        int(episode["is_special"] or 0)
        or int(episode["season_number"] or 0) == 0
    ):
        return f"Special {int(episode['episode_number']):02d}"
    return ""


def _target_for_episode(
    cfg: Config,
    series,
    root: Path,
    season: int,
    episode_number: float,
    episode_end: float,
    title: str,
    file_row,
) -> tuple[Path, Path, Path]:
    series_folder = folder_name_for_series(
        series["title"],
        series["year"],
        series["tmdb_id"],
        bool(cfg.get("include_tmdb_id", True)),
    )
    file_prefix = _display_series(series)
    ext = Path(file_row["path"]).suffix
    series_dir, season_dir, target = target_paths(
        root,
        series["title"],
        series["year"],
        series["tmdb_id"],
        bool(cfg.get("include_tmdb_id", True)),
        season,
        episode_number,
        episode_end if episode_end and episode_end > episode_number else None,
        title if cfg.get("include_episode_title", True) else "",
        ext,
        quality=file_row["quality"] or "",
        include_title=bool(cfg.get("include_episode_title", True)),
        include_quality=bool(cfg.get("include_quality", False)),
        title_max_len=int(cfg.get("episode_title_max_len", 90)),
        file_title=file_prefix,
    )
    if len(str(target)) > 245:
        # Acortar el título del episodio para no superar el límite de Windows.
        series_dir, season_dir, target = target_paths(
            root,
            series["title"],
            series["year"],
            series["tmdb_id"],
            bool(cfg.get("include_tmdb_id", True)),
            season,
            episode_number,
            episode_end if episode_end and episode_end > episode_number else None,
            title if cfg.get("include_episode_title", True) else "",
            ext,
            quality=file_row["quality"] or "",
            include_title=bool(cfg.get("include_episode_title", True)),
            include_quality=bool(cfg.get("include_quality", False)),
            title_max_len=40,
            file_title=file_prefix,
        )
    if len(str(target)) > 250:
        series_dir, season_dir, target = target_paths(
            root,
            series["title"],
            series["year"],
            series["tmdb_id"],
            bool(cfg.get("include_tmdb_id", True)),
            season,
            episode_number,
            episode_end if episode_end and episode_end > episode_number else None,
            "",
            ext,
            quality="",
            include_title=False,
            include_quality=False,
            file_title=file_prefix,
        )
    return series_dir, season_dir, target


def _quarantine_target(cfg: Config, plan_id: int, series_folder: str, source: Path, used: set[str]) -> Path:
    base = cfg.quarantine_root / f"plan-{plan_id}" / series_folder
    try:
        rel = source.name
    except Exception:
        rel = source.name
    target = base / rel
    if _key(target) in used or target.exists():
        stem, ext = target.stem, target.suffix
        for number in range(2, 100):
            candidate = target.with_name(f"{stem} ({number}){ext}")
            if _key(candidate) not in used and not candidate.exists():
                target = candidate
                break
    used.add(_key(target))
    return target


def build_plan(
    cfg: Config,
    mode: str = "full",
    progress: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    cfg.ensure_dirs()
    with session(cfg.db_path) as con:
        plan_id = _create_plan(con, cfg, mode)
        actions: list[dict[str, Any]] = []
        claimed: dict[str, dict[str, Any]] = {}
        scheduled_sources: set[str] = set()
        quarantine_used: set[str] = set()
        warnings: list[str] = []
        summary = {
            "move": 0,
            "quarantine": 0,
            "conflict": 0,
            "keep": 0,
            "bytes": 0,
            "series": 0,
            "files": 0,
            "warnings": warnings,
        }

        series_rows = con.execute("SELECT * FROM series ORDER BY title").fetchall()
        duplicate_victims = _fingerprint_duplicate_victims(con)
        total = max(1, len(series_rows))
        for index, series in enumerate(series_rows, start=1):
            if progress:
                progress(int(index / total * 85), f"Planificando {series['title']}")
            root = cfg.airing_root if series["root"] == "airing" else cfg.library_root
            series_folder = folder_name_for_series(
                series["title"], series["year"], series["tmdb_id"],
                bool(cfg.get("include_tmdb_id", True)),
            )
            series_dir = root / series_folder
            videos = con.execute(
                "SELECT * FROM files WHERE series_id=? AND kind='video' AND status='present' "
                "ORDER BY season_number, episode_number, path",
                (int(series["id"]),),
            ).fetchall()
            if not videos and not con.execute(
                "SELECT 1 FROM files WHERE series_id=? AND kind='image' AND status='present'",
                (int(series["id"]),),
            ).fetchone():
                continue
            summary["series"] += 1

            # Agrupar por episodio asignado; los no asignados se revisan aparte.
            by_episode: dict[int, list[Any]] = defaultdict(list)
            unassigned: list[Any] = []
            for video in videos:
                if video["episode_id"]:
                    by_episode[int(video["episode_id"])].append(video)
                else:
                    unassigned.append(video)

            for episode_id, rows in sorted(by_episode.items()):
                episode = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
                if not episode:
                    unassigned.extend(rows)
                    continue
                primary = _choose_primary(rows)
                season = int(episode["season_number"])
                number = float(episode["episode_number"])
                # El rango multi-episodio vive en el archivo; la fila del
                # episodio se mantiene individual para no duplicar el total.
                end = max(
                    float(episode["episode_end"] or 0),
                    float(primary["episode_end"] or 0),
                )
                if end and end < number:
                    end = 0
                title = _episode_title(episode, primary, cfg, series)
                series_dir2, season_dir, target = _target_for_episode(
                    cfg, series, root, season, number, end, title, primary
                )
                for row in rows:
                    source = Path(row["path"])
                    if row["id"] != primary["id"]:
                        qtarget = _quarantine_target(
                            cfg, plan_id, series_folder,
                            source, quarantine_used,
                        )
                        actions.append(
                            _action(
                                "quarantine", source, qtarget, series["id"], row["id"],
                                episode_id, "video", row["size"],
                                "Duplicado del mismo episodio; se guarda en cuarentena",
                            )
                        )
                        scheduled_sources.add(_key(source))
                        summary["quarantine"] += 1
                        summary["bytes"] += int(row["size"] or 0)
                        continue
                    _schedule_move(
                        actions, claimed, scheduled_sources, summary, warnings,
                        source, target, series, row, episode_id, "video",
                    )

            for row in unassigned:
                source = Path(row["path"])
                if int(row["id"]) in duplicate_victims:
                    # La fase global de duplicados lo enviará a cuarentena.
                    continue
                if row["status"] == "unparsed":
                    warnings.append(
                        f"Sin episodio detectado: {relative_display(source, cfg.library_root)}"
                    )
                    continue
                warnings.append(
                    f"Archivo sin episodio asignado: {relative_display(source, cfg.library_root)}"
                )

            # Carátulas y arte.
            _plan_artwork(
                con, cfg, plan_id, series, series_dir, actions, summary,
                quarantine_used, scheduled_sources, warnings,
            )

            # Subtítulos, miniaturas y NFO junto al vídeo.
            _plan_sidecars(
                con, cfg, plan_id, series, series_folder, actions, summary,
                quarantine_used, scheduled_sources, warnings,
            )

        # Duplicados exactos entre series/episodios.
        _plan_fingerprint_duplicates(
            con, cfg, plan_id, actions, summary, quarantine_used, scheduled_sources,
        )

        # Ordenar: primero los movimientos cuyos destinos están libres.
        ordered = _order_actions(actions)
        for seq, action in enumerate(ordered, start=1):
            con.execute(
                "INSERT INTO plan_actions(plan_id, seq, action, source, target, series_id, "
                "file_id, episode_id, kind, size, status, reason, meta) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id, seq, action["action"], action["source"], action["target"],
                    action["series_id"], action["file_id"], action["episode_id"],
                    action["kind"], action["size"], "pending", action["reason"],
                    action.get("meta"),
                ),
            )
        summary["files"] = len(ordered)
        summary_json = json_dumps(summary)
        con.execute(
            "UPDATE plans SET summary=?, status='ready' WHERE id=?",
            (summary_json, plan_id),
        )
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        result = {
            "plan_id": plan_id,
            "summary": summary,
            "plan": dict(plan),
            "actions": len(ordered),
        }

    if progress:
        progress(95, "Generando informe")
    try:
        from .reports import write_plan_report

        result["report"] = str(write_plan_report(cfg, plan_id))
    except Exception as exc:
        result["report_error"] = str(exc)
    if progress:
        progress(100, "Plan listo")
    return result


def _create_plan(con, cfg: Config, mode: str) -> int:
    con.execute(
        "INSERT INTO plans(created_at, status, mode, summary, config_snapshot) "
        "VALUES(?,?,?,?,?)",
        (
            now_iso(),
            "building",
            mode,
            "{}",
            json_dumps(
                {
                    "library_root": str(cfg.library_root),
                    "airing_root": str(cfg.airing_root),
                    "include_tmdb_id": cfg.get("include_tmdb_id"),
                    "include_episode_title": cfg.get("include_episode_title"),
                    "include_quality": cfg.get("include_quality"),
                    "keep_episode_thumbs": cfg.get("keep_episode_thumbs"),
                }
            ),
        ),
    )
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def _action(
    action: str,
    source: Path | str | None,
    target: Path | str | None,
    series_id: int | None,
    file_id: int | None,
    episode_id: int | None,
    kind: str,
    size: int | None,
    reason: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "source": str(source) if source else None,
        "target": str(target) if target else None,
        "series_id": series_id,
        "file_id": file_id,
        "episode_id": episode_id,
        "kind": kind,
        "size": size,
        "reason": reason,
        "meta": json_dumps(meta) if meta else None,
    }


def _schedule_move(
    actions: list[dict[str, Any]],
    claimed: dict[str, dict[str, Any]],
    scheduled_sources: set[str],
    summary: dict[str, Any],
    warnings: list[str],
    source: Path,
    target: Path,
    series,
    row,
    episode_id: int | None,
    kind: str,
) -> None:
    target_key = _key(target)
    source_key = _key(source)
    if source_key == target_key:
        summary["keep"] += 1
        return
    if source_key in scheduled_sources:
        return

    existing = claimed.get(target_key)
    if existing:
        if existing.get("fingerprint") and row["fingerprint"] and existing["fingerprint"] == row["fingerprint"]:
            # Ya hay un archivo idéntico programado para ese destino.
            actions.append(
                _action(
                    "quarantine", source, target.with_name(target.stem + " (duplicado)" + target.suffix),
                    series["id"], row["id"], episode_id, kind, row["size"],
                    "Duplicado exacto del destino",
                )
            )
            summary["quarantine"] += 1
            summary["bytes"] += int(row["size"] or 0)
            scheduled_sources.add(source_key)
        else:
            actions.append(
                _action(
                    "conflict", source, target, series["id"], row["id"], episode_id,
                    kind, row["size"],
                    f"El destino ya está reclamado por {existing['source']}",
                )
            )
            summary["conflict"] += 1
            warnings.append(f"Conflicto de destino: {target}")
        return

    if target.exists() and _key(target) != source_key:
        if row["fingerprint"]:
            try:
                from .utils import quick_fingerprint

                if quick_fingerprint(target) == row["fingerprint"]:
                    actions.append(
                        _action(
                            "quarantine", source,
                            target.with_name(target.stem + " (duplicado)" + target.suffix),
                            series["id"], row["id"], episode_id, kind, row["size"],
                            "Ya existe un archivo idéntico en el destino",
                        )
                    )
                    summary["quarantine"] += 1
                    summary["bytes"] += int(row["size"] or 0)
                    scheduled_sources.add(source_key)
                    return
            except OSError:
                pass
        actions.append(
            _action(
                "conflict", source, target, series["id"], row["id"], episode_id,
                kind, row["size"], "El destino ya existe y es distinto",
            )
        )
        summary["conflict"] += 1
        warnings.append(f"El destino ya existe: {target}")
        return

    claimed[target_key] = {
        "source": str(source),
        "fingerprint": row["fingerprint"],
    }
    actions.append(
        _action(
            "move" if source.name != target.name else "rename",
            source, target, series["id"], row["id"], episode_id, kind, row["size"],
            "Renombrar/mover a la estructura Jellyfin",
        )
    )
    scheduled_sources.add(source_key)
    summary["move"] += 1
    summary["bytes"] += int(row["size"] or 0)


def _plan_artwork(
    con,
    cfg: Config,
    plan_id: int,
    series,
    series_dir: Path,
    actions: list[dict[str, Any]],
    summary: dict[str, Any],
    quarantine_used: set[str],
    scheduled_sources: set[str],
    warnings: list[str],
) -> None:
    roles = ("poster", "backdrop", "landscape", "logo")
    for role in roles:
        rows = con.execute(
            "SELECT * FROM files WHERE series_id=? AND kind='image' AND role=? AND status='present' "
            "ORDER BY size DESC, path",
            (int(series["id"]), role),
        ).fetchall()
        if not rows:
            continue
        chosen = rows[0]
        target = series_dir / f"{role}{Path(chosen['path']).suffix.lower()}"
        source = Path(chosen["path"])
        _schedule_move(
            actions, {}, scheduled_sources, summary, warnings,
            source, target, series, chosen, None, "image",
        )
        for row in rows[1:]:
            other = Path(row["path"])
            if _key(other) in scheduled_sources:
                continue
            qtarget = _quarantine_target(cfg, plan_id, series_dir.name, other, quarantine_used)
            actions.append(
                _action(
                    "quarantine", other, qtarget, series["id"], row["id"], None,
                    "image", row["size"], f"Arte duplicado ({role})",
                )
            )
            scheduled_sources.add(_key(other))
            summary["quarantine"] += 1
            summary["bytes"] += int(row["size"] or 0)

    # Pósters de temporada.
    rows = con.execute(
        "SELECT * FROM files WHERE series_id=? AND kind='image' AND role='season_poster' "
        "AND status='present' ORDER BY size DESC, path",
        (int(series["id"]),),
    ).fetchall()
    chosen_by_season: dict[int, Any] = {}
    for row in rows:
        season = _season_from_poster(row["path"])
        if season is None:
            continue
        chosen_by_season.setdefault(season, row)
    for season, chosen in chosen_by_season.items():
        target = series_dir / f"season{season:02d}-poster{Path(chosen['path']).suffix.lower()}"
        _schedule_move(
            actions, {}, scheduled_sources, summary, warnings,
            Path(chosen["path"]), target, series, chosen, None, "image",
        )
    for row in rows:
        source = Path(row["path"])
        if _key(source) in scheduled_sources:
            continue
        qtarget = _quarantine_target(cfg, plan_id, series_dir.name, source, quarantine_used)
        actions.append(
            _action(
                "quarantine", source, qtarget, series["id"], row["id"], None,
                "image", row["size"], "Póster de temporada duplicado",
            )
        )
        scheduled_sources.add(_key(source))
        summary["quarantine"] += 1
        summary["bytes"] += int(row["size"] or 0)


def _season_from_poster(path: str | Path) -> int | None:
    import re

    match = re.search(r"(?:season|temporada)\s*0*(\d{1,2})", Path(path).stem, re.I)
    if match:
        return int(match.group(1))
    return None


def _plan_sidecars(
    con,
    cfg: Config,
    plan_id: int,
    series,
    series_folder: str,
    actions: list[dict[str, Any]],
    summary: dict[str, Any],
    quarantine_used: set[str],
    scheduled_sources: set[str],
    warnings: list[str],
) -> None:
    videos = con.execute(
        "SELECT * FROM files WHERE series_id=? AND kind='video' AND status='present'",
        (int(series["id"]),),
    ).fetchall()
    # Buscar destino por nombre base de origen.
    target_by_stem: dict[tuple[str, str], Path] = {}
    for action in actions:
        if action["kind"] != "video" or not action["source"] or not action["target"]:
            continue
        src = Path(action["source"])
        target_by_stem[(str(src.parent).casefold(), src.stem.casefold())] = Path(action["target"])
    # Los vídeos que no cambian de nombre también cuentan.
    for video in videos:
        src = Path(video["path"])
        key = (str(src.parent).casefold(), src.stem.casefold())
        if key not in target_by_stem:
            target_by_stem[key] = src

    sidecars = con.execute(
        "SELECT * FROM files WHERE series_id=? AND kind IN ('subtitle','image','nfo') "
        "AND status='present' ORDER BY path",
        (int(series["id"]),),
    ).fetchall()
    for row in sidecars:
        if row["kind"] == "image" and row["role"] not in {"episode_thumb"}:
            continue
        source = Path(row["path"])
        if row["kind"] == "image" and row["role"] == "episode_thumb":
            base = source.stem
            if base.lower().endswith("-thumb"):
                base = base[: -len("-thumb")]
            video_target = target_by_stem.get((str(source.parent).casefold(), base.casefold()))
            if video_target is None:
                qtarget = _quarantine_target(cfg, plan_id, series_folder, source, quarantine_used)
                actions.append(
                    _action(
                        "quarantine", source, qtarget, series["id"], row["id"], None,
                        "image", row["size"],
                        "Miniatura sin vídeo; se guarda en cuarentena",
                    )
                )
                scheduled_sources.add(_key(source))
                summary["quarantine"] += 1
                summary["bytes"] += int(row["size"] or 0)
                continue
            if not cfg.get("keep_episode_thumbs", False):
                qtarget = _quarantine_target(cfg, plan_id, series_folder, source, quarantine_used)
                actions.append(
                    _action(
                        "quarantine", source, qtarget, series["id"], row["id"], None,
                        "image", row["size"], "Miniatura no utilizada (Jellyfin descarga las suyas)",
                    )
                )
                scheduled_sources.add(_key(source))
                summary["quarantine"] += 1
                summary["bytes"] += int(row["size"] or 0)
                continue
            raw_target = video_target.with_name(video_target.stem + "-thumb" + source.suffix.lower())
            _schedule_move(
                actions, {}, scheduled_sources, summary, warnings,
                source, raw_target, series, row, None, "image",
            )
            continue

        if row["kind"] == "subtitle":
            base = source.stem
            language = ""
            parts = base.split(".")
            if len(parts) >= 2 and len(parts[-1]) in (2, 3):
                language = "." + parts[-1]
                base = ".".join(parts[:-1])
            video_target = target_by_stem.get((str(source.parent).casefold(), base.casefold()))
            if video_target is None:
                qtarget = _quarantine_target(cfg, plan_id, series_folder, source, quarantine_used)
                actions.append(
                    _action(
                        "quarantine", source, qtarget, series["id"], row["id"], None,
                        "subtitle", row["size"],
                        "Subtítulo sin vídeo; se guarda en cuarentena",
                    )
                )
                scheduled_sources.add(_key(source))
                summary["quarantine"] += 1
                summary["bytes"] += int(row["size"] or 0)
                continue
            raw_target = video_target.with_name(video_target.stem + language + source.suffix.lower())
            _schedule_move(
                actions, {}, scheduled_sources, summary, warnings,
                source, raw_target, series, row, None, "subtitle",
            )
            continue

        if row["kind"] == "nfo":
            base = source.stem
            video_target = target_by_stem.get((str(source.parent).casefold(), base.casefold()))
            if video_target is None:
                qtarget = _quarantine_target(cfg, plan_id, series_folder, source, quarantine_used)
                actions.append(
                    _action(
                        "quarantine", source, qtarget, series["id"], row["id"], None,
                        "nfo", row["size"],
                        "NFO sin vídeo; se guarda en cuarentena",
                    )
                )
                scheduled_sources.add(_key(source))
                summary["quarantine"] += 1
                summary["bytes"] += int(row["size"] or 0)
                continue
            raw_target = video_target.with_name(video_target.stem + source.suffix.lower())
            _schedule_move(
                actions, {}, scheduled_sources, summary, warnings,
                source, raw_target, series, row, None, "nfo",
            )


def _plan_fingerprint_duplicates(
    con,
    cfg: Config,
    plan_id: int,
    actions: list[dict[str, Any]],
    summary: dict[str, Any],
    quarantine_used: set[str],
    scheduled_sources: set[str],
) -> None:
    rows = con.execute(
        "SELECT * FROM files WHERE kind='video' AND status IN ('present','unparsed') "
        "AND fingerprint IS NOT NULL AND fingerprint<>'' ORDER BY fingerprint, path"
    ).fetchall()
    groups: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        groups[row["fingerprint"]].append(row)
    for fingerprint, items in groups.items():
        if len(items) < 2:
            continue
        primary = _choose_primary(items)
        for row in items:
            if row["id"] == primary["id"]:
                continue
            source = Path(row["path"])
            if _key(source) in scheduled_sources:
                continue
            series = con.execute("SELECT * FROM series WHERE id=?", (row["series_id"],)).fetchone()
            series_folder = folder_name_for_series(
                series["title"], series["year"], series["tmdb_id"],
                bool(cfg.get("include_tmdb_id", True)),
            ) if series else "duplicados"
            qtarget = _quarantine_target(cfg, plan_id, series_folder, source, quarantine_used)
            reason = "Duplicado exacto de otro archivo de la biblioteca"
            if row["status"] == "unparsed":
                reason = "Duplicado exacto sin episodio detectado; se guarda en cuarentena"
            actions.append(
                _action(
                    "quarantine", source, qtarget, row["series_id"], row["id"], row["episode_id"],
                    "video", row["size"], reason,
                )
            )
            scheduled_sources.add(_key(source))
            summary["quarantine"] += 1
            summary["bytes"] += int(row["size"] or 0)


def _order_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    moves = [a for a in actions if a["action"] in {"move", "rename"}]
    others = [a for a in actions if a["action"] not in {"move", "rename"}]
    source_keys = {_key(a["source"]) for a in moves if a["source"]}
    remaining = moves[:]
    ordered: list[dict[str, Any]] = []
    while remaining:
        ready = [a for a in remaining if _key(a["target"]) not in source_keys]
        if not ready:
            ready = [remaining[0]]
        for action in ready:
            ordered.append(action)
            remaining.remove(action)
            if action["source"]:
                source_keys.discard(_key(action["source"]))
    return ordered + others
