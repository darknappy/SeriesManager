"""Carga de semillas de series y carpetas de origen."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from .config import Config
from .db import get_or_create_series, session
from .naming import parse_folder_info
from .utils import json_loads, normalize_title, now_iso, slugify

TMDB_FOLDER_RE = re.compile(r"\[tmdbid-(\d+)\]", re.I)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"No se pudo leer {path}: {exc}") from exc


def load_series_seeds(cfg: Config) -> dict[str, dict[str, Any]]:
    path = cfg.app_root / "data" / "series_seed.json"
    data = _read_json(path, {"series": []})
    result: dict[str, dict[str, Any]] = {}
    for item in data.get("series", []):
        key = str(item.get("key", "")).strip()
        if not key:
            continue
        result[key] = item
    return result


def load_group_seeds(cfg: Config) -> list[dict[str, Any]]:
    path = cfg.app_root / "data" / "groups_seed.json"
    data = _read_json(path, {"groups": []})
    return [g for g in data.get("groups", []) if g.get("folder") and g.get("series")]


def normalize_folder_name(name: str) -> str:
    value = unicodedata.normalize("NFC", str(name or ""))
    value = value.replace("´", "'").replace("’", "'").replace("`", "'")
    value = value.replace("–", "-").replace("—", "-")
    value = " ".join(value.split())
    return value.casefold()


def tmdb_id_from_folder(folder_name: str) -> str | None:
    """Extrae el TMDB ID de un nombre Jellyfin: ``[tmdbid-35610]``."""
    match = TMDB_FOLDER_RE.search(str(folder_name or ""))
    return match.group(1) if match else None


def _series_by_tmdb(con, tmdb_id: str):
    """Devuelve la ficha canónica con ese TMDB, prefiriendo la que tiene episodios."""
    rows = con.execute(
        "SELECT * FROM series WHERE tmdb_id=? ORDER BY "
        "(SELECT COUNT(*) FROM episodes e WHERE e.series_id=series.id) DESC, "
        "CASE WHEN match_status IN ('matched','manual','seeded') THEN 0 ELSE 1 END, id",
        (str(tmdb_id),),
    ).fetchall()
    return rows[0] if rows else None


def _series_names(row) -> list[str]:
    names = [row["title"], row["original_title"]]
    aliases = json_loads(row["aliases"], []) if row["aliases"] else []
    if isinstance(aliases, list):
        names.extend(str(alias) for alias in aliases if alias)
    return [name for name in names if name]


def match_series_by_folder(con, folder_name: str):
    """Busca una ficha canónica por título/año cuando la carpeta no tiene TMDB.

    Es deliberadamente conservador: exige el mismo título normalizado y no
    acepta años distintos. Así una carpeta renombrada sin ID se puede vincular
    manualmente sin que el escaneo invente una ficha nueva.
    """
    info = parse_folder_info(folder_name)
    target = normalize_title(info.title_guess)
    if len(target) < 3:
        return None
    candidates: list[tuple[int, Any]] = []
    for row in con.execute("SELECT * FROM series WHERE COALESCE(root,'library')<>'staging'"):
        if info.year and row["year"] and abs(int(row["year"]) - int(info.year)) > 1:
            continue
        for name in _series_names(row):
            if normalize_title(name) != target:
                continue
            score = 0
            if info.year and row["year"] and int(row["year"]) == int(info.year):
                score += 20
            if row["match_status"] in {"matched", "manual", "seeded"}:
                score += 5
            episodes = con.execute(
                "SELECT COUNT(*) FROM episodes WHERE series_id=?", (row["id"],)
            ).fetchone()[0]
            score += min(int(episodes or 0), 100)
            candidates.append((score, row))
            break
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -int(item[1]["id"])), reverse=True)
    return candidates[0][1]


def _insert_group(con, cfg: Config, folder_name: str, series_key: str, info) -> int:
    con.execute(
        "INSERT INTO source_groups(folder_name, folder_path, series_key, season, absolute, "
        "ignore_filename_season, position, root, updated_at) VALUES(?,?,?,?,0,0,?,?,?)",
        (
            folder_name,
            str(cfg.library_root / folder_name),
            series_key,
            info.season if info.season is not None else None,
            9999,
            "library",
            now_iso(),
        ),
    )
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def series_defaults(seed: dict[str, Any]) -> dict[str, Any]:
    aliases = seed.get("aliases") or []
    return {
        "title": seed.get("title") or seed["key"].replace("-", " ").title(),
        "original_title": seed.get("original_title"),
        "year": seed.get("year"),
        "provider": seed.get("provider") or ("tvmaze" if seed.get("tvmaze_id") else None),
        "provider_id": str(seed.get("tvmaze_id")) if seed.get("tvmaze_id") else None,
        "tvmaze_id": seed.get("tvmaze_id"),
        "tvmaze_season": seed.get("tvmaze_season"),
        "anilist_id": seed.get("anilist_id"),
        "tmdb_id": str(seed.get("tmdb_id")) if seed.get("tmdb_id") else None,
        "imdb_id": seed.get("imdb_id"),
        "tvdb_id": seed.get("tvdb_id"),
        "aliases": json.dumps(aliases, ensure_ascii=False) if aliases else None,
        "media_type": seed.get("media_type") or "tv",
        "status": seed.get("status") or "unknown",
        "total_episodes": seed.get("total_episodes"),
        "season_layout": seed.get("season_layout"),
        "notes": seed.get("notes"),
        "match_status": "seeded" if (seed.get("tvmaze_id") or seed.get("tmdb_id")) else "unmatched",
        "match_confidence": 1.0 if seed.get("tvmaze_id") or seed.get("tmdb_id") else 0.0,
        "match_reason": "semilla",
        "root": seed.get("root") or "library",
        "updated_at": now_iso(),
    }


def apply_seeds(con, cfg: Config, overwrite: bool = True) -> dict[str, int]:
    """Crea/actualiza series y grupos de origen a partir de los JSON."""
    series_seeds = load_series_seeds(cfg)
    group_seeds = load_group_seeds(cfg)
    created = 0
    updated = 0
    for key, seed in series_seeds.items():
        defaults = series_defaults(seed)
        row = con.execute("SELECT id FROM series WHERE series_key=?", (key,)).fetchone()
        if row:
            series_id = int(row["id"])
            if overwrite:
                values = {
                    k: v
                    for k, v in defaults.items()
                    if k not in {"root", "notes", "match_status", "match_confidence", "match_reason"}
                }
                values["updated_at"] = now_iso()
                assignments = ", ".join(f"{k}=?" for k in values)
                con.execute(
                    f"UPDATE series SET {assignments} WHERE id=?",
                    (*values.values(), series_id),
                )
                if defaults.get("notes"):
                    con.execute(
                        "UPDATE series SET notes=? WHERE id=? AND (notes IS NULL OR notes='')",
                        (defaults["notes"], series_id),
                    )
                updated += 1
        else:
            series_id = get_or_create_series(con, key, defaults)
            created += 1

        # Ficha de TMDB conocida: se evita que un candidato malo la reemplace.
        if defaults.get("tmdb_id"):
            con.execute(
                "UPDATE series SET tmdb_id=? WHERE id=? AND (tmdb_id IS NULL OR tmdb_id='')",
                (defaults["tmdb_id"], series_id),
            )

    for position, group in enumerate(group_seeds):
        folder = str(group["folder"])
        row = con.execute(
            "SELECT id FROM source_groups WHERE folder_name=?", (folder,)
        ).fetchone()
        values = {
            "folder_path": str(cfg.library_root / folder),
            "series_key": str(group["series"]),
            "season": group.get("season"),
            "absolute": 1 if group.get("absolute") else 0,
            "ignore_filename_season": 1 if group.get("ignore_filename_season") else 0,
            "position": position,
            "root": group.get("root") or "library",
            "notes": group.get("notes"),
            "updated_at": now_iso(),
        }
        if row:
            assignments = ", ".join(f"{k}=?" for k in values)
            con.execute(
                f"UPDATE source_groups SET {assignments} WHERE id=?",
                (*values.values(), int(row["id"])),
            )
        else:
            columns = ", ".join(["folder_name", *values.keys()])
            placeholders = ", ".join("?" for _ in range(len(values) + 1))
            con.execute(
                f"INSERT INTO source_groups({columns}) VALUES({placeholders})",
                (folder, *values.values()),
            )
    return {"series_created": created, "series_updated": updated, "groups": len(group_seeds)}


def find_group(con, folder_name: str):
    row = con.execute(
        "SELECT * FROM source_groups WHERE folder_name=?", (folder_name,)
    ).fetchone()
    if row:
        return row
    target = normalize_folder_name(folder_name)
    for candidate in con.execute("SELECT * FROM source_groups"):
        if normalize_folder_name(candidate["folder_name"]) == target:
            return candidate
    return None


def ensure_group(con, cfg: Config, folder_name: str):
    """Devuelve el grupo de una carpeta, creando un alias si hace falta.

    Prioridad:
    1. Alias guardado explícitamente para esa carpeta.
    2. TMDB ID incrustado por Jellyfin (``[tmdbid-...]``).
    3. Título/año idénticos a una ficha canónica.
    4. Ficha provisional para identificar manualmente.
    """
    row = find_group(con, folder_name)
    if row:
        return row
    info = parse_folder_info(folder_name)

    tmdb_id = tmdb_id_from_folder(folder_name)
    if tmdb_id:
        series = _series_by_tmdb(con, tmdb_id)
        if series:
            _insert_group(con, cfg, folder_name, series["series_key"], info)
            return con.execute(
                "SELECT * FROM source_groups WHERE folder_name=?", (folder_name,)
            ).fetchone()

    matched = match_series_by_folder(con, folder_name)
    if matched:
        _insert_group(con, cfg, folder_name, matched["series_key"], info)
        return con.execute(
            "SELECT * FROM source_groups WHERE folder_name=?", (folder_name,)
        ).fetchone()

    base_key = slugify(info.title_guess or folder_name)
    key = base_key
    suffix = 2
    while con.execute("SELECT 1 FROM series WHERE series_key=?", (key,)).fetchone():
        key = f"{base_key}-{suffix}"
        suffix += 1
    defaults = {
        "title": info.title_guess or folder_name,
        "year": info.year,
        "root": "library",
        "match_status": "unmatched",
        "match_confidence": 0.0,
        "match_reason": "carpeta sin semilla",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    get_or_create_series(con, key, defaults)
    _insert_group(con, cfg, folder_name, key, info)
    return con.execute("SELECT * FROM source_groups WHERE folder_name=?", (folder_name,)).fetchone()


def apply_all(cfg: Config, overwrite: bool = True) -> dict[str, int]:
    with session(cfg.db_path) as con:
        return apply_seeds(con, cfg, overwrite=overwrite)
