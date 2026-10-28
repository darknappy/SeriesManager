"""Escaneo de la biblioteca y clasificación de archivos."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

from .catalog import (
    cleanup_staging_catalog,
    prune_orphan_provisional_series,
    prune_stale_groups,
    reconcile_tmdb_folders,
)
from .config import Config
from .db import add_issue, clear_issues, session
from .naming import (
    ParsedEpisode,
    clean_release_stem,
    image_role,
    media_kind,
    parse_episode,
    parse_path_season,
    subtitle_language,
)
from .seeds import apply_seeds, ensure_group, normalize_folder_name
from .utils import is_subpath, now_iso, quick_fingerprint, relative_display

SKIP_DIRS = {
    "@eadir", "#recycle", "$recycle.bin", "system volume information",
    ".git", "__pycache__", "extrafanart", "metadata", ".thumbnails",
}
SAMPLE_RE = re.compile(r"\b(sample|trailer|preview|teaser)\b", re.I)


def _iter_group_files(folder: Path):
    for current, dirs, files in os.walk(folder):
        dirs[:] = sorted(
            d for d in dirs if d.casefold() not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(files):
            path = Path(current) / name
            if name.startswith(".") or name.startswith("._"):
                continue
            yield path


def _relative_parts(path: Path, folder: Path) -> list[str]:
    try:
        return list(path.relative_to(folder).parts)
    except ValueError:
        return [path.name]


def _reuse_fingerprint(row, size: int, mtime: float) -> str | None:
    if row and row["fingerprint"] and row["size"] == size and abs((row["mtime"] or 0) - mtime) < 1:
        return row["fingerprint"]
    return None


def _upsert_file(con, path: Path, values: dict[str, Any]) -> int:
    row = con.execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
    values = {**values, "updated_at": now_iso()}
    if row:
        assignments = ", ".join(f"{k}=?" for k in values)
        con.execute(
            f"UPDATE files SET {assignments} WHERE id=?",
            (*values.values(), int(row["id"])),
        )
        return int(row["id"])
    values["path"] = str(path)
    columns = ", ".join(values.keys())
    placeholders = ", ".join("?" for _ in values)
    con.execute(f"INSERT INTO files({columns}) VALUES({placeholders})", tuple(values.values()))
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def _special_sort_key(row) -> tuple:
    parsed_from = (row["parsed_from"] or "").lower()
    episode = float(row["parsed_episode"] or row["episode_number"] or 0)
    if row["parsed_unnumbered"]:
        # OVA/especial sin número: va después de los especiales numerados.
        return (2, 0.0, str(row["path"]).casefold())
    if row["parsed_special"] and abs(episode - round(episode)) > 1e-9:
        return (0, episode, str(row["path"]).casefold())
    return (1, episode, str(row["path"]).casefold())


def scan_library(
    cfg: Config,
    progress: Callable[[int, str], None] | None = None,
    *,
    fingerprint: bool = True,
    auto_match: bool = True,
) -> dict[str, Any]:
    cfg.ensure_dirs()
    stats: dict[str, Any] = {
        "folders": 0,
        "videos": 0,
        "parsed": 0,
        "unparsed": 0,
        "specials": 0,
        "sidecars": 0,
        "images": 0,
        "missing": 0,
        "duplicates": 0,
        "merged_series": 0,
        "staging_cleaned": 0,
        "orphan_series_cleaned": 0,
        "matched": 0,
        "review": 0,
    }
    seen_paths: set[str] = set()

    def report(pct: int, message: str) -> None:
        if progress:
            progress(max(0, min(100, pct)), message)

    with session(cfg.db_path) as con:
        clear_issues(con)
        # Las carpetas de descargas (p. ej. "En emision") no son series. Se
        # retiran sus registros derivados antes de escanear y nunca se toca el
        # contenido del disco.
        staging_stats = cleanup_staging_catalog(con, cfg)
        reconcile_stats = reconcile_tmdb_folders(con, cfg)
        stats["staging_cleaned"] = staging_stats["series"]
        stats["merged_series"] = len(reconcile_stats["merged"])
        apply_seeds(con, cfg, overwrite=True)
        roots = [("library", cfg.library_root)]
        if cfg.airing_root.exists():
            roots.append(("airing", cfg.airing_root))
        staging_paths: set[str] = set()
        for staging_path in cfg.staging_roots:
            try:
                staging_paths.add(str(staging_path.resolve()).casefold())
            except OSError:
                staging_paths.add(str(staging_path).casefold())

        folders: list[tuple[str, Path]] = []
        for root_name, root_path in roots:
            if not root_path.exists():
                continue
            for entry in sorted(root_path.iterdir(), key=lambda p: p.name.casefold()):
                try:
                    marker = str(entry.resolve()).casefold()
                except OSError:
                    marker = str(entry).casefold()
                if marker in staging_paths:
                    continue
                if entry.is_dir() and entry.name.casefold() not in SKIP_DIRS:
                    folders.append((root_name, entry))

        total_folders = max(1, len(folders))
        for index, (root_name, folder) in enumerate(folders, start=1):
            report(int((index - 1) / total_folders * 85), f"Escaneando {folder.name}")
            group = ensure_group(con, cfg, folder.name)
            if not group:
                continue
            con.execute(
                "UPDATE source_groups SET folder_path=?, root=?, updated_at=? WHERE id=?",
                (str(folder), root_name, now_iso(), int(group["id"])),
            )
            series = con.execute(
                "SELECT * FROM series WHERE series_key=?", (group["series_key"],)
            ).fetchone()
            if not series:
                continue
            series_id = int(series["id"])
            if root_name == "airing" and not series["root_override"]:
                con.execute("UPDATE series SET root='airing' WHERE id=?", (series_id,))
            group_season = group["season"]
            group_absolute = bool(group["absolute"])
            ignore_filename_season = bool(group["ignore_filename_season"])
            group_position = int(group["position"] or 0)
            stats["folders"] += 1

            for path in _iter_group_files(folder):
                seen_paths.add(str(path))
                rel_parts = _relative_parts(path, folder)
                rel_path = str(Path(*rel_parts))
                kind = media_kind(path)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                size = stat.st_size
                mtime = stat.st_mtime
                existing = con.execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
                base_values: dict[str, Any] = {
                    "rel_path": rel_path,
                    "root": root_name,
                    "group_folder": folder.name,
                    "group_position": group_position,
                    "kind": kind,
                    "size": size,
                    "mtime": mtime,
                    "series_id": series_id,
                    "status": "present",
                    "reason": None,
                }

                if kind == "video":
                    stats["videos"] += 1
                    parent_season, parent_special = parse_path_season(
                        [folder.name, *rel_parts]
                    )
                    parsed = parse_episode(
                        path.stem,
                        folder_season=group_season if parent_season is None else parent_season,
                        folder_special=parent_special or group_season == 0,
                        series_title=series["title"],
                        series_year=series["year"],
                        series_folder=folder.name,
                    )
                    fingerprint_value = _reuse_fingerprint(existing, size, mtime)
                    if fingerprint and not fingerprint_value:
                        try:
                            fingerprint_value = quick_fingerprint(path, size)
                        except OSError:
                            fingerprint_value = None

                    if not parsed:
                        base_values.update(
                            {
                                "role": None,
                                "season_number": None,
                                "episode_number": None,
                                "parsed_season": None,
                                "parsed_episode": None,
                                "parsed_special": 0,
                                "parsed_unnumbered": 0,
                                "parsed_from": None,
                                "resolution": None,
                                "quality": None,
                                "title_hint": None,
                                "absolute_number": None,
                                "absolute_end": None,
                                "fingerprint": fingerprint_value,
                                "status": "unparsed",
                                "reason": "No se pudo detectar temporada/episodio",
                            }
                        )
                        file_id = _upsert_file(con, path, base_values)
                        stats["unparsed"] += 1
                        if not SAMPLE_RE.search(path.stem):
                            add_issue(
                                con,
                                "unparsed_video",
                                f"No se pudo interpretar el nombre: {rel_path}",
                                "warning",
                                series_id=series_id,
                                file_id=file_id,
                            )
                        continue

                    if parsed.special:
                        season = 0
                    elif ignore_filename_season and group_season is not None:
                        season = int(group_season)
                    elif parsed.explicit_season:
                        season = int(parsed.season)
                    elif group_season is not None:
                        season = int(group_season)
                    else:
                        season = int(parsed.season)

                    episode_end = parsed.episode_end if not parsed.special else None
                    base_values.update(
                        {
                            "role": None,
                            "season_number": None if group_absolute else season,
                            "episode_number": None if group_absolute else float(parsed.episode),
                            # En grupos absolutos el rango de origen se guarda aparte;
                            # `episode_end` quedará en numeración local tras sync.
                            "episode_end": None if group_absolute else episode_end,
                            "parsed_season": float(parsed.season),
                            "parsed_episode": float(parsed.episode),
                            "parsed_special": 1 if (parsed.special or season == 0) else 0,
                            "parsed_unnumbered": 1 if parsed.unnumbered_special else 0,
                            "parsed_from": parsed.matched_by,
                            "resolution": parsed.resolution or None,
                            "quality": parsed.quality or None,
                            "title_hint": parsed.title_hint or None,
                            "absolute_number": float(parsed.episode) if group_absolute else None,
                            "absolute_end": (
                                float(parsed.episode_end)
                                if group_absolute and parsed.episode_end
                                else None
                            ),
                            "fingerprint": fingerprint_value,
                            "status": "present",
                        }
                    )
                    file_id = _upsert_file(con, path, base_values)
                    stats["parsed"] += 1
                    if parsed.special or season == 0:
                        stats["specials"] += 1
                    continue

                if kind == "image":
                    stats["images"] += 1
                    role = image_role(path)
                    if SAMPLE_RE.search(path.stem):
                        role = "sample"
                    base_values["role"] = role
                    _upsert_file(con, path, base_values)
                    continue

                if kind == "subtitle":
                    stats["sidecars"] += 1
                    base_values["role"] = subtitle_language(path)
                    _upsert_file(con, path, base_values)
                    continue

                if kind == "nfo":
                    stats["sidecars"] += 1
                    base_values["role"] = "nfo"
                    _upsert_file(con, path, base_values)
                    continue

                base_values["role"] = "other"
                _upsert_file(con, path, base_values)

        # Alias de carpetas renombradas que ya no existen. Se conservan si son
        # la única referencia de una ficha (por ejemplo, un disco desconectado).
        prune_stale_groups(con, cfg)
        stats["orphan_series_cleaned"] = prune_orphan_provisional_series(con)

        # Archivos que ya no existen.
        report(88, "Revisando archivos eliminados")
        for row in con.execute("SELECT id, path, status FROM files").fetchall():
            if row["path"] in seen_paths:
                continue
            # La cuarentena está fuera de las raíces escaneadas; sus archivos
            # siguen existiendo y no deben convertirse en "missing".
            if row["status"] == "quarantined" or is_subpath(
                row["path"], cfg.quarantine_root
            ):
                continue
            if row["status"] != "missing":
                con.execute(
                    "UPDATE files SET status='missing', updated_at=? WHERE id=?",
                    (now_iso(), int(row["id"])),
                )
                stats["missing"] += 1

        # Asignar especiales secuenciales por serie y orden de carpetas.
        report(92, "Numerando especiales")
        _assign_specials(con, stats)

        # Huellas duplicadas (solo videos presentes).
        report(96, "Buscando duplicados")
        duplicates = con.execute(
            "SELECT fingerprint, COUNT(*) c FROM files WHERE kind='video' "
            "AND status IN ('present','unparsed') "
            "AND fingerprint IS NOT NULL AND fingerprint<>'' GROUP BY fingerprint HAVING c>1"
        ).fetchall()
        stats["duplicates"] = len(duplicates)
        for dup in duplicates:
            rows = con.execute(
                "SELECT id, series_id, path FROM files WHERE fingerprint=? AND kind='video' "
                "AND status IN ('present','unparsed')",
                (dup["fingerprint"],),
            ).fetchall()
            names = ", ".join(Path(r["path"]).name for r in rows)
            add_issue(
                con,
                "duplicate_fingerprint",
                f"Archivos idénticos: {names}",
                "warning",
                series_id=rows[0]["series_id"],
                details=[r["path"] for r in rows],
            )

        # Series sin televisión reconocible.
        for series in con.execute(
            "SELECT id, title FROM series WHERE COALESCE(root,'library')<>'staging'"
        ).fetchall():
            count = con.execute(
                "SELECT COUNT(*) FROM files WHERE series_id=? AND kind='video' AND status='present'",
                (series["id"],),
            ).fetchone()[0]
            if count == 0:
                add_issue(
                    con,
                    "series_without_video",
                    f"La serie {series['title']} no tiene archivos de vídeo.",
                    "info",
                    series_id=int(series["id"]),
                )

        con.execute(
            "INSERT INTO meta(key, value) VALUES('last_scan', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (now_iso(),),
        )

    # Las carpetas nuevas sin semilla se intentan identificar solas. Solo se
    # consultan las que aún no tienen candidatos guardados, para no repetir
    # búsquedas ya revisadas en cada escaneo.
    if auto_match:
        report(96, "Identificando series nuevas")
        try:
            from .matching import refresh_all

            def match_progress(pct: int, message: str) -> None:
                report(96 + int(max(0, min(100, pct)) * 3 / 100), message)

            match_stats = refresh_all(
                cfg,
                match_progress,
                only_without_candidates=True,
            )
            stats["matched"] = int(match_stats.get("matched", 0))
            stats["review"] = int(match_stats.get("review", 0))
        except Exception as exc:  # una caída de red no debe romper el escaneo
            stats["match_error"] = str(exc)
    report(100, "Escaneo terminado")
    return stats


def _assign_specials(con, stats: dict[str, Any]) -> None:
    rows = con.execute(
        "SELECT f.* FROM files f "
        "WHERE f.kind='video' AND f.status='present' AND f.parsed_special=1 "
        "ORDER BY f.series_id, f.group_position, f.path"
    ).fetchall()
    by_series: dict[int, list[Any]] = {}
    for row in rows:
        by_series.setdefault(int(row["series_id"]), []).append(row)

    for series_id, items in by_series.items():
        items.sort(
            key=lambda r: (
                int(r["group_position"] or 0),
                _special_sort_key(r),
            )
        )
        for number, row in enumerate(items, start=1):
            con.execute(
                "UPDATE files SET season_number=0, episode_number=?, episode_end=0, "
                "absolute_number=NULL, status='present', updated_at=? WHERE id=?",
                (float(number), now_iso(), int(row["id"])),
            )
            stats["specials"] = stats.get("specials", 0)


def series_for_group(con, folder_name: str):
    group = con.execute(
        "SELECT * FROM source_groups WHERE folder_name=?", (folder_name,)
    ).fetchone()
    if not group:
        for candidate in con.execute("SELECT * FROM source_groups"):
            if normalize_folder_name(candidate["folder_name"]) == normalize_folder_name(folder_name):
                group = candidate
                break
    if not group:
        return None
    return con.execute(
        "SELECT * FROM series WHERE series_key=?", (group["series_key"],)
    ).fetchone()
