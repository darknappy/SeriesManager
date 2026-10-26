"""Reparación y aliases del catálogo de carpetas.

Este módulo mantiene separada la lógica que relaciona carpetas físicas con
fichas canónicas. No mueve, renombra ni elimina archivos de la biblioteca.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import Config
from .db import get_or_create_series, session
from .naming import parse_folder_info
from .seeds import find_group, tmdb_id_from_folder
from .utils import is_subpath, json_loads, normalize_title, now_iso, slugify


def _folder_location(cfg: Config, folder_name: str) -> tuple[str, Path] | None:
    """Valida que sea una carpeta de primer nivel de alguna biblioteca."""
    name = str(folder_name or "").strip()
    if not name or name in {".", ".."}:
        return None
    path = Path(name)
    if path.is_absolute() or len(path.parts) != 1:
        return None
    for root_name, root in (("library", cfg.library_root), ("airing", cfg.airing_root)):
        candidate = root / name
        try:
            if candidate.is_dir():
                return root_name, candidate.resolve()
        except OSError:
            continue
    return None


def _is_staging_path(cfg: Config, path: str | Path) -> bool:
    for root in cfg.staging_roots:
        if is_subpath(path, root):
            return True
    return False


def _series(con, series_id: int):
    return con.execute("SELECT * FROM series WHERE id=?", (int(series_id),)).fetchone()


def _count(con, table: str, series_id: int) -> int:
    return int(
        con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE series_id=?", (int(series_id),)
        ).fetchone()[0]
    )


def _aliases(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    parsed = json_loads(value, []) if isinstance(value, str) else []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _append_alias(con, series_id: int, *names: str) -> None:
    row = con.execute("SELECT aliases FROM series WHERE id=?", (int(series_id),)).fetchone()
    if not row:
        return
    current = _aliases(row["aliases"])
    known = {normalize_title(alias) for alias in current}
    changed = False
    for name in names:
        name = " ".join(str(name or "").split())
        if not name or normalize_title(name) in known:
            continue
        current.append(name)
        known.add(normalize_title(name))
        changed = True
    if changed:
        con.execute(
            "UPDATE series SET aliases=?, updated_at=? WHERE id=?",
            (json.dumps(current, ensure_ascii=False), now_iso(), int(series_id)),
        )


def _copy_missing_metadata(con, source, target) -> None:
    fields = (
        "original_title", "year", "provider", "provider_id", "tvmaze_id",
        "tvmaze_season", "anilist_id", "tmdb_id", "imdb_id", "tvdb_id",
        "overview", "cover_url", "banner_url", "genres", "studio",
        "total_episodes", "season_count", "next_episode", "next_air_at",
        "last_aired", "notes", "season_layout",
    )
    values: dict[str, Any] = {}
    for field in fields:
        current = target[field]
        incoming = source[field]
        if (current is None or current == "") and incoming not in (None, ""):
            values[field] = incoming
    if values:
        assignments = ", ".join(f"{field}=?" for field in values)
        con.execute(
            f"UPDATE series SET {assignments}, updated_at=? WHERE id=?",
            (*values.values(), now_iso(), int(target["id"])),
        )


def merge_series_records(con, source_id: int, target_id: int) -> dict[str, int]:
    """Fusiona una ficha provisional con la canónica sin tocar archivos físicos."""
    source_id = int(source_id)
    target_id = int(target_id)
    if source_id == target_id:
        return {"files": 0, "groups": 0, "removed": 0}
    source = _series(con, source_id)
    target = _series(con, target_id)
    if not source or not target:
        raise ValueError("Serie de origen o destino no encontrada.")
    if _count(con, "episodes", source_id) or _count(con, "seasons", source_id):
        raise ValueError(
            "La ficha provisional ya tiene episodios/temporadas; revísala antes de fusionarla."
        )
    files = con.execute(
        "UPDATE files SET series_id=?, updated_at=? WHERE series_id=?",
        (target_id, now_iso(), source_id),
    ).rowcount
    groups = con.execute(
        "UPDATE source_groups SET series_key=?, updated_at=? WHERE series_key=?",
        (target["series_key"], now_iso(), source["series_key"]),
    ).rowcount
    con.execute(
        "UPDATE match_candidates SET series_id=? WHERE series_id=?",
        (target_id, source_id),
    )
    _copy_missing_metadata(con, source, target)
    _append_alias(con, target_id, source["title"], source["series_key"])
    remaining = any(
        _count(con, table, source_id)
        for table in ("files", "episodes", "seasons", "match_candidates")
    )
    remaining = remaining or bool(
        con.execute(
            "SELECT 1 FROM source_groups WHERE series_key=?", (source["series_key"],)
        ).fetchone()
    )
    removed = 0
    if not remaining:
        con.execute("DELETE FROM series WHERE id=?", (source_id,))
        removed = 1
    return {"files": int(files), "groups": int(groups), "removed": removed}


def reconcile_tmdb_folders(con, cfg: Config) -> dict[str, Any]:
    """Une fichas provisionales con la ficha canónica que ya tiene su TMDB.

    Es la reparación que hace que una carpeta organizada como
    ``InuYasha (2000) [tmdbid-35610]`` vuelva a la ficha histórica de InuYasha
    en vez de crear una serie nueva en cada escaneo.
    """
    stats: dict[str, Any] = {"merged": [], "labelled": 0, "groups": 0}
    groups = con.execute(
        "SELECT * FROM source_groups ORDER BY id"
    ).fetchall()
    for group in groups:
        tmdb_id = tmdb_id_from_folder(group["folder_name"])
        if not tmdb_id:
            continue
        current = con.execute(
            "SELECT * FROM series WHERE series_key=?", (group["series_key"],)
        ).fetchone()
        if not current:
            continue
        canonical = con.execute(
            "SELECT * FROM series WHERE tmdb_id=? AND id<>? ORDER BY "
            "(SELECT COUNT(*) FROM episodes e WHERE e.series_id=series.id) DESC, id",
            (str(tmdb_id), int(current["id"])),
        ).fetchone()
        if canonical and current["match_status"] == "unmatched":
            if _count(con, "episodes", int(current["id"])) or _count(
                con, "seasons", int(current["id"])
            ):
                continue
            try:
                result = merge_series_records(con, int(current["id"]), int(canonical["id"]))
            except ValueError:
                continue
            con.execute(
                "UPDATE source_groups SET series_key=?, updated_at=? WHERE id=?",
                (canonical["series_key"], now_iso(), int(group["id"])),
            )
            stats["merged"].append(
                {
                    "from": current["series_key"],
                    "to": canonical["series_key"],
                    "files": result["files"],
                }
            )
            stats["groups"] += 1
            continue
        if not canonical and not current["tmdb_id"]:
            con.execute(
                "UPDATE series SET tmdb_id=?, match_status='manual', match_confidence=1.0, "
                "match_reason='TMDB en el nombre de la carpeta', updated_at=? WHERE id=?",
                (str(tmdb_id), now_iso(), int(current["id"])),
            )
            stats["labelled"] += 1
        try:
            location = _folder_location(cfg, group["folder_name"])
        except OSError:
            location = None
        if location:
            root_name, path = location
            con.execute(
                "UPDATE source_groups SET folder_path=?, root=?, updated_at=? WHERE id=?",
                (str(path), root_name, now_iso(), int(group["id"])),
            )
    return stats


def prune_stale_groups(con, cfg: Config) -> int:
    """Elimina aliases de carpetas que ya no existen tras un renombrado.

    Solo se elimina el alias si la misma ficha conserva otra carpeta existente.
    Así no se pierde la relación de una serie cuyo disco esté desconectado.
    """
    groups = con.execute("SELECT * FROM source_groups ORDER BY id").fetchall()
    existing_keys: set[str] = set()
    for group in groups:
        path = group["folder_path"] or str(cfg.library_root / group["folder_name"])
        try:
            if Path(path).exists():
                existing_keys.add(str(group["series_key"]))
        except OSError:
            continue
    removed = 0
    for group in groups:
        path = group["folder_path"] or str(cfg.library_root / group["folder_name"])
        try:
            if Path(path).exists():
                continue
        except OSError:
            continue
        if _is_staging_path(cfg, path):
            continue
        series = con.execute(
            "SELECT id FROM series WHERE series_key=?", (group["series_key"],)
        ).fetchone()
        if not series or str(group["series_key"]) not in existing_keys:
            continue
        con.execute("DELETE FROM source_groups WHERE id=?", (int(group["id"]),))
        removed += 1
    return removed


def cleanup_staging_catalog(con, cfg: Config) -> dict[str, int]:
    """Retira del catálogo las fichas falsas creadas por carpetas de descargas.

    Solo elimina registros derivados (files/grupos/ficha provisional). Los
    archivos del disco no se tocan.
    """
    stats = {"series": 0, "groups": 0, "files": 0}
    roots = cfg.staging_roots
    if not roots:
        return stats
    for group in con.execute("SELECT * FROM source_groups ORDER BY id").fetchall():
        path = group["folder_path"] or str(cfg.library_root / group["folder_name"])
        if not _is_staging_path(cfg, path):
            continue
        series = con.execute(
            "SELECT * FROM series WHERE series_key=?", (group["series_key"],)
        ).fetchone()
        if series:
            auto = (
                (series["match_status"] or "unmatched") == "unmatched"
                and not series["provider"]
                and not series["tmdb_id"]
                and not series["tvmaze_id"]
                and not series["anilist_id"]
                and not _count(con, "episodes", int(series["id"]))
                and not _count(con, "seasons", int(series["id"]))
            )
            if not auto:
                continue
            file_rows = con.execute(
                "SELECT id, path FROM files WHERE series_id=?", (int(series["id"]),)
            ).fetchall()
            if any(not _is_staging_path(cfg, row["path"]) for row in file_rows):
                continue
            con.execute("DELETE FROM files WHERE series_id=?", (int(series["id"]),))
            stats["files"] += len(file_rows)
            con.execute("DELETE FROM source_groups WHERE id=?", (int(group["id"]),))
            stats["groups"] += 1
            con.execute("DELETE FROM series WHERE id=?", (int(series["id"]),))
            stats["series"] += 1
        else:
            con.execute("DELETE FROM source_groups WHERE id=?", (int(group["id"]),))
            stats["groups"] += 1
    return stats


def _series_by_provider(con, *, tmdb_id: str | None, tvmaze_id: int | None, anilist_id: int | None):
    clauses = []
    params: list[Any] = []
    if tmdb_id:
        clauses.append("tmdb_id=?")
        params.append(str(tmdb_id))
    if tvmaze_id:
        clauses.append("tvmaze_id=?")
        params.append(int(tvmaze_id))
    if anilist_id:
        clauses.append("anilist_id=?")
        params.append(int(anilist_id))
    if not clauses:
        return None
    return con.execute(
        "SELECT * FROM series WHERE " + " OR ".join(clauses) + " ORDER BY "
        "(SELECT COUNT(*) FROM episodes e WHERE e.series_id=series.id) DESC, id",
        tuple(params),
    ).fetchone()


def _make_key(
    con,
    title: str,
    year: int | None,
    requested: str = "",
    exclude_id: int | None = None,
) -> str:
    raw = requested.strip() or f"{title} {year or ''}".strip()
    base = slugify(raw)
    key = base
    suffix = 2
    while con.execute(
        "SELECT 1 FROM series WHERE series_key=? AND id<>?",
        (key, int(exclude_id or 0)),
    ).fetchone():
        key = f"{base}-{suffix}"
        suffix += 1
    return key


def identify_folder(
    cfg: Config,
    *,
    folder_name: str,
    title: str,
    year: int | None = None,
    tmdb_id: str | None = None,
    tvmaze_id: int | None = None,
    anilist_id: int | None = None,
    series_key: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Crea o actualiza el identificador manual de una carpeta no reconocida."""
    location = _folder_location(cfg, folder_name)
    if not location:
        raise ValueError("La carpeta no existe en la biblioteca principal ni en la de emisión.")
    root_name, path = location
    if _is_staging_path(cfg, path):
        raise ValueError("Esa carpeta es de descargas en curso, no una serie.")
    clean_title = " ".join(str(title or "").split())
    if not clean_title:
        raise ValueError("El título no puede estar vacío.")
    tmdb_value = str(tmdb_id).strip() if tmdb_id else tmdb_id_from_folder(folder_name)
    if tmdb_value and not str(tmdb_value).isdigit():
        raise ValueError("El TMDB ID debe ser numérico.")
    with session(cfg.db_path) as con:
        group = find_group(con, folder_name)
        current = (
            con.execute(
                "SELECT * FROM series WHERE series_key=?", (group["series_key"],)
            ).fetchone()
            if group
            else None
        )
        canonical = _series_by_provider(
            con,
            tmdb_id=tmdb_value,
            tvmaze_id=tvmaze_id,
            anilist_id=anilist_id,
        )
        created = False
        merged = 0
        if current and canonical and int(current["id"]) != int(canonical["id"]):
            if (current["match_status"] or "unmatched") != "unmatched":
                raise ValueError(
                    "La carpeta ya está asignada a otra serie. Desvincúlala antes de cambiarla."
                )
            result = merge_series_records(con, int(current["id"]), int(canonical["id"]))
            merged = result["removed"]
            current = canonical
        if current is None:
            key = _make_key(con, clean_title, year, series_key)
            series_id = get_or_create_series(
                con,
                key,
                {
                    "title": clean_title,
                    "year": year,
                    "tmdb_id": tmdb_value or None,
                    "tvmaze_id": int(tvmaze_id) if tvmaze_id else None,
                    "anilist_id": int(anilist_id) if anilist_id else None,
                    "provider": "tvmaze" if tvmaze_id else ("anilist" if anilist_id else None),
                    "provider_id": str(tvmaze_id) if tvmaze_id else (
                        str(anilist_id) if anilist_id else None
                    ),
                    "root": root_name,
                    "match_status": "manual",
                    "match_confidence": 1.0,
                    "match_reason": "identificador manual",
                    "notes": notes or None,
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                },
            )
            created = True
        else:
            series_id = int(current["id"])
            values: dict[str, Any] = {
                "updated_at": now_iso(),
                "match_status": "manual",
                "match_confidence": 1.0,
                "match_reason": "identificador manual",
            }
            if not current["match_reason"] or current["match_status"] == "unmatched":
                values["title"] = clean_title
            if year is not None:
                values["year"] = int(year)
            if tmdb_value:
                values["tmdb_id"] = str(tmdb_value)
            if tvmaze_id:
                values["tvmaze_id"] = int(tvmaze_id)
                values["provider"] = "tvmaze"
                values["provider_id"] = str(tvmaze_id)
            if anilist_id:
                values["anilist_id"] = int(anilist_id)
            if notes:
                values["notes"] = notes
            if not current["root_override"]:
                values["root"] = root_name
            if series_key and series_key.strip():
                new_key = _make_key(
                    con, clean_title, year, series_key, exclude_id=current["id"]
                )
                if new_key != current["series_key"]:
                    values["series_key"] = new_key
            assignments = ", ".join(f"{field}=?" for field in values)
            con.execute(
                f"UPDATE series SET {assignments} WHERE id=?",
                (*values.values(), series_id),
            )
        if group:
            con.execute(
                "UPDATE source_groups SET series_key=?, folder_path=?, root=?, updated_at=? "
                "WHERE id=?",
                (
                    con.execute(
                        "SELECT series_key FROM series WHERE id=?", (series_id,)
                    ).fetchone()["series_key"],
                    str(path),
                    root_name,
                    now_iso(),
                    int(group["id"]),
                ),
            )
        else:
            info = parse_folder_info(folder_name)
            series = con.execute("SELECT series_key FROM series WHERE id=?", (series_id,)).fetchone()
            con.execute(
                "INSERT INTO source_groups(folder_name, folder_path, series_key, season, absolute, "
                "ignore_filename_season, position, root, updated_at) VALUES(?,?,?,?,0,0,?,?,?)",
                (
                    folder_name,
                    str(path),
                    series["series_key"],
                    info.season if info.season is not None else None,
                    9999,
                    root_name,
                    now_iso(),
                ),
            )
        _append_alias(con, series_id, folder_name, clean_title)
        return {
            "series_id": series_id,
            "created": created,
            "merged": merged,
            "series_key": con.execute(
                "SELECT series_key FROM series WHERE id=?", (series_id,)
            ).fetchone()["series_key"],
        }


def attach_folder_alias(cfg: Config, series_id: int, folder_name: str) -> dict[str, Any]:
    """Vincula una carpeta modificada a una ficha existente."""
    location = _folder_location(cfg, folder_name)
    if not location:
        raise ValueError("La carpeta no existe en la biblioteca principal ni en la de emisión.")
    root_name, path = location
    if _is_staging_path(cfg, path):
        raise ValueError("Esa carpeta es de descargas en curso, no una serie.")
    with session(cfg.db_path) as con:
        target = _series(con, series_id)
        if not target:
            raise ValueError("Serie no encontrada.")
        group = find_group(con, folder_name)
        merged = 0
        if group:
            current = con.execute(
                "SELECT * FROM series WHERE series_key=?", (group["series_key"],)
            ).fetchone()
            if current and int(current["id"]) != int(series_id):
                if (current["match_status"] or "unmatched") != "unmatched":
                    raise ValueError("Esa carpeta ya está vinculada a otra serie.")
                result = merge_series_records(con, int(current["id"]), int(series_id))
                merged = result["removed"]
            con.execute(
                "UPDATE source_groups SET series_key=?, folder_path=?, root=?, updated_at=? "
                "WHERE id=?",
                (target["series_key"], str(path), root_name, now_iso(), int(group["id"])),
            )
        else:
            info = parse_folder_info(folder_name)
            con.execute(
                "INSERT INTO source_groups(folder_name, folder_path, series_key, season, absolute, "
                "ignore_filename_season, position, root, updated_at) VALUES(?,?,?,?,0,0,?,?,?)",
                (
                    folder_name,
                    str(path),
                    target["series_key"],
                    info.season if info.season is not None else None,
                    9999,
                    root_name,
                    now_iso(),
                ),
            )
        _append_alias(con, series_id, folder_name)
        return {"series_id": int(series_id), "merged": merged, "folder": folder_name}


def list_unidentified(con, cfg: Config) -> list[dict[str, Any]]:
    """Carpetas que aún no tienen una ficha reconocible."""
    staging = {str(path.resolve()).casefold() for path in cfg.staging_roots}
    result: list[dict[str, Any]] = []
    groups = con.execute(
        "SELECT g.*, s.id AS series_id, s.title AS series_title, s.year AS series_year, "
        "s.match_status, s.tmdb_id, s.tvmaze_id, s.anilist_id, "
        "(SELECT COUNT(*) FROM files f WHERE f.series_id=s.id AND f.kind='video' "
        "AND f.status IN ('present','unparsed')) AS video_count, "
        "(SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id) AS episode_count "
        "FROM source_groups g LEFT JOIN series s ON s.series_key=g.series_key "
        "ORDER BY g.folder_name COLLATE NOCASE"
    ).fetchall()
    for group in groups:
        path = group["folder_path"] or str(cfg.library_root / group["folder_name"])
        try:
            marker = str(Path(path).resolve()).casefold()
        except OSError:
            marker = str(path).casefold()
        if marker in staging or _is_staging_path(cfg, path):
            continue
        if group["series_id"] is None or (
            (group["match_status"] or "unmatched") == "unmatched"
            and not group["tmdb_id"]
            and not group["tvmaze_id"]
            and not group["anilist_id"]
        ):
            result.append(dict(group))
    return result


def folder_choices(cfg: Config) -> list[str]:
    """Carpetas de primer nivel disponibles para identificar."""
    choices: list[str] = []
    for root in (cfg.library_root, cfg.airing_root):
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and not _is_staging_path(cfg, child):
                choices.append(child.name)
    return sorted(dict.fromkeys(choices), key=str.casefold)
