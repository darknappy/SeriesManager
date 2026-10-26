"""Catálogo de anime en emisión y seguimiento de la temporada actual."""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from pathlib import Path
from typing import Any

from .config import Config
from .db import session
from .naming import clean_release_stem, media_kind, parse_episode, parse_folder_info
from .providers.anilist import AniListClient, SEASON_ES, current_season
from .utils import (
    format_date_es,
    json_dumps,
    json_loads,
    normalize_title,
    now_iso,
    parse_iso,
)

SAMPLE_NAME_RE = re.compile(r"\b(sample|trailer|preview|teaser)\b", re.I)

WATCH_STATUSES = ("Pendiente", "Viendo", "Pausado", "Abandonado", "Completado")


def season_info(moment: dt.datetime | None = None) -> dict[str, Any]:
    season, year = current_season(moment)
    return {
        "season": season,
        "year": year,
        "label": f"{SEASON_ES.get(season, season)} {year}",
    }


def discover_season(cfg: Config, season: str | None = None, year: int | None = None) -> list[dict[str, Any]]:
    client = AniListClient(cfg)
    if not season or not year:
        info = season_info()
        season, year = info["season"], info["year"]
    items = client.seasonal(season, int(year), pages=3)
    result = [
        item
        for item in items
        if not item.get("is_adult")
        and (item.get("format") in {"TV", "ONA", "TV_SHORT", "SPECIAL", None})
    ]
    result.sort(key=lambda m: (m.get("popularity") or 0, m.get("average_score") or 0), reverse=True)
    for item in result:
        item["season_es"] = SEASON_ES.get((item.get("season") or "").upper(), item.get("season") or "")
    return result


DOWNLOAD_NOISE_RE = re.compile(
    r"\b(?:sw[\s-]*anime|anime|yk|cr|dw|erai[\s-]*raws|subsplease|horriblesubs|"
    r"macrohex|encoded|encode|subs?|dub|raw|dual[\s-]*audio|multi[\s-]*audio|"
    r"2160p|1080p|720p|480p|576p|4k|uhd|webrip|web-dl|webdl|web|bluray|blu-ray|"
    r"bdrip|bd|dvdrip|dvd|hdtv|x265|hevc|h\.?265|x264|avc|h\.?264|10bit|8bit|"
    r"aac|flac|ddp?|opus)\b",
    re.I,
)
EPISODE_MARK_RE = re.compile(
    r"\b(?:S\d{1,2}\s*E\d{1,3}(?:\s*[-–]\s*E?\d{1,3})?|"
    r"\d{1,2}x\d{2,3}|(?:Episode|Ep|E)\s*\d{1,3}|"
    r"Season\s*\d{1,2}\s*[-–:]\s*\d{1,3})\b",
    re.I,
)
SEASON_SUFFIX_RE = re.compile(
    r"\s+(?:season|temporada)\s*\d{1,2}\s*$|\s+\d{1,2}(?:st|nd|rd|th)\s+season\s*$",
    re.I,
)


def _clean_download_text(value: str) -> str:
    text = str(value or "").replace("_", " ").replace(".", " ")
    text = clean_release_stem(text)
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text)
    text = EPISODE_MARK_RE.sub(" ", text)
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = re.sub(r"\b[A-Fa-f0-9]{7,}\b", " ", text)
    text = DOWNLOAD_NOISE_RE.sub(" ", text)
    text = text.replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip(" ._-–—:")
    return text


def _download_titles(stem: str, parsed) -> list[str]:
    variants: list[str] = []
    if parsed and getattr(parsed, "title_hint", ""):
        variants.append(_clean_download_text(parsed.title_hint))
    variants.append(_clean_download_text(stem))
    if parsed:
        # El texto anterior al marcador suele ser el título más limpio.
        match = EPISODE_MARK_RE.search(stem.replace("_", " "))
        if match:
            variants.append(_clean_download_text(stem[: match.start()]))
    result: list[str] = []
    seen: set[str] = set()
    for value in variants:
        key = normalize_title(value)
        if len(key) < 3 or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _title_match_score(candidate_titles: list[str], local_titles: list[str]) -> float:
    best = 0.0
    for candidate in candidate_titles:
        normalized_candidate = normalize_title(candidate)
        if not normalized_candidate:
            continue
        candidate_tokens = set(normalized_candidate.split())
        if not candidate_tokens:
            continue
        for local in local_titles:
            normalized_local = normalize_title(local)
            if not normalized_local:
                continue
            if normalized_candidate == normalized_local:
                return 1.0
            if (
                min(len(normalized_candidate), len(normalized_local)) >= 7
                and (
                    normalized_candidate in normalized_local
                    or normalized_local in normalized_candidate
                )
            ):
                best = max(best, 0.93)
            local_tokens = set(normalized_local.split())
            if not local_tokens:
                continue
            intersection = candidate_tokens & local_tokens
            contained = len(intersection) / len(candidate_tokens)
            union = candidate_tokens | local_tokens
            overlap = len(intersection) / len(union) if union else 0.0
            best = max(best, 0.58 * contained + 0.32 * overlap + 0.10 * contained)
    return min(1.0, best)


def _local_series_index(con) -> list[dict[str, Any]]:
    groups: dict[str, list[str]] = {}
    for group in con.execute("SELECT series_key, folder_name FROM source_groups"):
        groups.setdefault(str(group["series_key"]), []).append(str(group["folder_name"]))
    rows: list[dict[str, Any]] = []
    for row in con.execute(
        "SELECT s.*, "
        "(SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id) AS local_episodes, "
        "(SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id AND e.status='present') "
        "AS local_present, "
        "(SELECT COUNT(*) FROM files f WHERE f.series_id=s.id AND f.kind='video' "
        "AND f.status='present') AS local_videos "
        "FROM series s WHERE COALESCE(s.root,'library')<>'staging' "
        "ORDER BY s.title COLLATE NOCASE"
    ):
        item = dict(row)
        names = [item.get("title"), item.get("original_title")]
        aliases = json_loads(item.get("aliases"), []) if item.get("aliases") else []
        if isinstance(aliases, list):
            names.extend(str(alias) for alias in aliases if alias)
        for folder in groups.get(str(item["series_key"]), []):
            info = parse_folder_info(folder)
            names.append(info.title_guess)
        item["_names"] = list(dict.fromkeys(str(name) for name in names if name))
        rows.append(item)
    return rows


def _find_local_series(
    rows: list[dict[str, Any]], media: dict[str, Any], threshold: float = 0.74
) -> tuple[dict[str, Any] | None, float]:
    candidate_titles = [
        media.get("title"),
        media.get("english"),
        media.get("romaji"),
        media.get("native"),
    ]
    base_titles = [
        SEASON_SUFFIX_RE.sub("", str(title or "")).strip()
        for title in candidate_titles
        if title
    ]
    candidate_titles.extend(title for title in base_titles if title)
    best: dict[str, Any] | None = None
    best_score = 0.0
    for row in rows:
        score = _title_match_score(candidate_titles, row.get("_names") or [])
        if media.get("id") and row.get("anilist_id") and int(row["anilist_id"]) == int(media["id"]):
            score = 1.1
        if media.get("year") and row.get("year") and abs(int(media["year"]) - int(row["year"])) > 1:
            score -= 0.18
        if score > best_score:
            best_score = score
            best = row
    if best is None or best_score < threshold:
        return None, best_score
    return best, best_score


def staging_downloads(cfg: Config) -> list[dict[str, Any]]:
    """Lee las carpetas de descargas en curso sin modificar su contenido."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in cfg.staging_roots:
        if not root.exists():
            continue
        try:
            paths = sorted(root.rglob("*"), key=lambda p: str(p).casefold())
        except OSError:
            continue
        for path in paths:
            try:
                if not path.is_file() or media_kind(path) != "video":
                    continue
                if SAMPLE_NAME_RE.search(path.stem):
                    continue
                marker = str(path.resolve()).casefold()
                if marker in seen:
                    continue
                seen.add(marker)
                parsed = parse_episode(path.stem)
                if parsed:
                    if parsed.special or parsed.season == 0:
                        label = f"S00E{parsed.episode:02g}"
                    else:
                        label = f"S{int(parsed.season):02d}E{parsed.episode:02g}"
                        if parsed.episode_end:
                            label += f"-E{parsed.episode_end:02g}"
                else:
                    label = path.stem[:36]
                entries.append(
                    {
                        "path": str(path),
                        "relative": str(path.relative_to(root)),
                        "titles": _download_titles(path.stem, parsed),
                        "episode_label": label,
                        "season": parsed.season if parsed else None,
                        "episode": parsed.episode if parsed else None,
                        "size": path.stat().st_size,
                    }
                )
            except OSError:
                continue
    return entries


def annotate_season_downloads(
    cfg: Config,
    items: list[dict[str, Any]],
    staging: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Añade a la cartelera qué series ya están en la biblioteca o descargadas."""
    with session(cfg.db_path) as con:
        series_rows = _local_series_index(con)
        watch_rows = con.execute(
            "SELECT id, provider_id FROM watchlist WHERE provider='anilist'"
        ).fetchall()
    watch_by_id = {str(row["provider_id"]): int(row["id"]) for row in watch_rows}
    staging = staging_downloads(cfg) if staging is None else staging
    summary = {
        "total": len(items),
        "in_library": 0,
        "staged": 0,
        "downloaded": 0,
        "followed": 0,
    }
    for item in items:
        local, score = _find_local_series(series_rows, item)
        candidate_titles = [
            item.get("title"),
            item.get("english"),
            item.get("romaji"),
            item.get("native"),
        ]
        candidate_titles.extend(
            SEASON_SUFFIX_RE.sub("", str(title or "")).strip()
            for title in list(candidate_titles)
            if title
        )
        candidate_titles = [title for title in candidate_titles if title]
        staged: list[dict[str, Any]] = []
        for entry in staging:
            match_score = _title_match_score(candidate_titles, entry["titles"])
            if match_score >= 0.72:
                staged.append({**entry, "score": round(match_score, 3)})
        staged.sort(
            key=lambda row: (
                row.get("season") if row.get("season") is not None else 99,
                row.get("episode") if row.get("episode") is not None else 9999,
                row["path"],
            )
        )
        item["library_series_id"] = int(local["id"]) if local else None
        item["library_title"] = local["title"] if local else ""
        item["library_episodes"] = int(local["local_episodes"] or 0) if local else 0
        item["library_present"] = int(local["local_present"] or 0) if local else 0
        item["library_videos"] = int(local["local_videos"] or 0) if local else 0
        item["library_score"] = round(score, 3)
        item["staging_count"] = len(staged)
        item["staging_episodes"] = [entry["episode_label"] for entry in staged[:12]]
        item["staging_more"] = max(0, len(staged) - 12)
        item["staging_paths"] = [entry["path"] for entry in staged[:6]]
        item["in_library"] = bool(local)
        item["downloaded"] = bool(local or staged)
        item["followed"] = str(item.get("id")) in watch_by_id
        item["watch_id"] = watch_by_id.get(str(item.get("id")))
        if local and staged:
            item["download_state"] = "library-staging"
            item["download_label"] = (
                f"En biblioteca ({local['local_present']} eps) + {len(staged)} en descargas"
            )
        elif local:
            item["download_state"] = "library"
            item["download_label"] = f"En biblioteca · {local['local_present']} eps"
        elif staged:
            item["download_state"] = "staging"
            item["download_label"] = f"Descargada · {len(staged)} eps en curso"
        else:
            item["download_state"] = "none"
            item["download_label"] = "No descargada"
        if local:
            summary["in_library"] += 1
        if staged:
            summary["staged"] += 1
        if item["downloaded"]:
            summary["downloaded"] += 1
        if item["followed"]:
            summary["followed"] += 1
    summary["staging_files"] = len(staging)
    summary["staging_roots"] = [str(path) for path in cfg.staging_roots]
    return summary


def follow_anilist_ids(cfg: Config, ids: list[int]) -> dict[str, Any]:
    """Añade varios animes de AniList a la lista de seguimiento."""
    clean_ids = list(dict.fromkeys(int(value) for value in ids if int(value) > 0))
    if not clean_ids:
        raise ValueError("No has seleccionado ningún anime.")
    with session(cfg.db_path) as con:
        existing = {
            str(row["provider_id"])
            for row in con.execute(
                "SELECT provider_id FROM watchlist WHERE provider='anilist'"
            )
        }
    media_items = AniListClient(cfg).media_many(clean_ids)
    added = 0
    updated = 0
    for media in media_items:
        was_followed = str(media.get("id")) in existing
        add_watch(cfg, media)
        if was_followed:
            updated += 1
        else:
            added += 1
    return {
        "requested": len(clean_ids),
        "added": added,
        "updated": updated,
        "missing": max(0, len(clean_ids) - len(media_items)),
        "titles": [media.get("title") for media in media_items],
    }


def add_watch(cfg: Config, media: dict[str, Any], notes: str | None = None) -> int:
    with session(cfg.db_path) as con:
        provider = media.get("provider") or "anilist"
        provider_id = str(media.get("provider_id") or media.get("id") or "")
        if not provider_id:
            raise ValueError("Falta el id del anime")
        row = con.execute(
            "SELECT id FROM watchlist WHERE provider=? AND provider_id=?",
            (provider, provider_id),
        ).fetchone()
        values = {
            "title": media.get("title") or "",
            "year": media.get("year"),
            "status": media.get("status") or "",
            "next_episode": media.get("next_episode"),
            "next_air_at": media.get("next_air_at"),
            "last_aired": media.get("last_aired"),
            "total_episodes": media.get("episodes"),
            "cover_url": media.get("cover_url") or "",
            "banner_url": media.get("banner_url") or "",
            "genres": json_dumps(media.get("genres") or []),
            "last_synced": now_iso(),
            "updated_at": now_iso(),
        }
        if notes is not None:
            values["notes"] = notes
        elif not row:
            values["notes"] = media.get("notes") or ""
        if row:
            assignments = ", ".join(f"{k}=?" for k in values)
            con.execute(
                f"UPDATE watchlist SET {assignments} WHERE id=?",
                (*values.values(), int(row["id"])),
            )
            return int(row["id"])
        values.update(
            {
                "provider": provider,
                "provider_id": provider_id,
                "watch_status": "Viendo",
                "progress_episode": 0,
                "added_at": now_iso(),
            }
        )
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        con.execute(f"INSERT INTO watchlist({columns}) VALUES({placeholders})", tuple(values.values()))
        return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def add_manual_watch(
    cfg: Config,
    title: str,
    total_episodes: int | None = None,
    notes: str = "",
    watch_status: str = "Viendo",
) -> int:
    title = " ".join(str(title or "").split())
    if not title:
        raise ValueError("El título no puede estar vacío.")
    if watch_status not in WATCH_STATUSES:
        watch_status = "Viendo"
    if total_episodes is not None and total_episodes < 0:
        raise ValueError("El total de episodios no puede ser negativo.")
    with session(cfg.db_path) as con:
        values = {
            "provider": "manual",
            "provider_id": f"manual-{uuid.uuid4().hex[:12]}",
            "title": title,
            "status": "Manual",
            "total_episodes": total_episodes,
            "notes": notes or "",
            "watch_status": watch_status,
            "progress_episode": 0,
            "added_at": now_iso(),
            "updated_at": now_iso(),
        }
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        con.execute(f"INSERT INTO watchlist({columns}) VALUES({placeholders})", tuple(values.values()))
        return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def update_watch(
    cfg: Config,
    watch_id: int,
    *,
    watch_status: str | None = None,
    progress_episode: Any = None,
    rating: Any = None,
    notes: str | None = None,
    title: str | None = None,
    total_episodes: Any = None,
) -> dict[str, Any]:
    with session(cfg.db_path) as con:
        row = con.execute("SELECT * FROM watchlist WHERE id=?", (watch_id,)).fetchone()
        if not row:
            raise ValueError("Título de seguimiento no encontrado.")
        values: dict[str, Any] = {"updated_at": now_iso()}
        if watch_status is not None:
            if watch_status not in WATCH_STATUSES:
                raise ValueError("Estado de seguimiento inválido.")
            values["watch_status"] = watch_status
        if notes is not None:
            values["notes"] = notes
        if rating is not None:
            parsed_rating = _number(rating)
            if parsed_rating is not None and not 0 <= parsed_rating <= 10:
                raise ValueError("La nota debe estar entre 0 y 10.")
            values["rating"] = int(parsed_rating) if parsed_rating is not None else None
        total = _number(total_episodes)
        if total is not None and total < 0:
            raise ValueError("El total de episodios no puede ser negativo.")
        if total_episodes is not None:
            values["total_episodes"] = int(total) if total is not None else None
        if title is not None:
            clean_title = " ".join(str(title).split())
            if not clean_title:
                raise ValueError("El título no puede estar vacío.")
            values["title"] = clean_title
        if progress_episode is not None:
            progress = _number(progress_episode)
            if progress is not None and progress < 0:
                raise ValueError("El episodio visto no puede ser negativo.")
            maximum = total if total is not None else _number(row["total_episodes"])
            if progress is not None and maximum is not None and maximum > 0:
                progress = min(progress, maximum)
            values["progress_episode"] = progress
            if progress is not None and _number(row["progress_episode"]) != progress:
                values["last_watched_at"] = now_iso()
        assignments = ", ".join(f"{k}=?" for k in values)
        con.execute(
            f"UPDATE watchlist SET {assignments} WHERE id=?",
            (*values.values(), watch_id),
        )
        updated = con.execute("SELECT * FROM watchlist WHERE id=?", (watch_id,)).fetchone()
        return dict(updated)


def advance_watch(cfg: Config, watch_id: int) -> dict[str, Any]:
    """Marca el siguiente episodio como visto, sin cambiar la lista de AniList."""
    with session(cfg.db_path) as con:
        row = con.execute("SELECT * FROM watchlist WHERE id=?", (watch_id,)).fetchone()
        if not row:
            raise ValueError("Título de seguimiento no encontrado.")
        progress = _number(row["progress_episode"]) or 0
        total = _number(row["total_episodes"])
        progress += 1
        if total is not None and total > 0:
            progress = min(progress, total)
        watch_status = row["watch_status"] or "Viendo"
        if total is not None and total > 0 and progress >= total:
            watch_status = "Completado"
        con.execute(
            "UPDATE watchlist SET progress_episode=?, watch_status=?, last_watched_at=?, "
            "updated_at=? WHERE id=?",
            (progress, watch_status, now_iso(), now_iso(), watch_id),
        )
        updated = con.execute("SELECT * FROM watchlist WHERE id=?", (watch_id,)).fetchone()
        return dict(updated)



def remove_watch(cfg: Config, watch_id: int) -> None:
    with session(cfg.db_path) as con:
        con.execute("DELETE FROM watchlist WHERE id=?", (watch_id,))


def list_watch(cfg: Config) -> list[dict[str, Any]]:
    with session(cfg.db_path) as con:
        rows = con.execute(
            "SELECT * FROM watchlist ORDER BY "
            "CASE WHEN next_air_at IS NULL THEN 1 ELSE 0 END, next_air_at, title"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["genres_list"] = json_loads(item.get("genres"), []) or []
            item["next_air_label"] = format_date_es(item.get("next_air_at")) if item.get("next_air_at") else "—"
            item["last_aired_label"] = format_date_es(item.get("last_aired")) if item.get("last_aired") else "—"
            progress = _number(item.get("progress_episode"))
            total = _number(item.get("total_episodes"))
            item["progress_number"] = progress
            item["progress_label"] = (
                f"{progress:g}/{total:g}" if progress is not None and total
                else (f"{progress:g}" if progress is not None else "—")
            )
            item["pending_episode"] = int(progress) + 1 if progress is not None else None
            item["progress_percent"] = (
                min(100, round(progress / total * 100)) if progress is not None and total and total > 0
                else 0
            )
            item["watch_status"] = item.get("watch_status") or "Viendo"
            result.append(item)
        return result


def sync_watchlist(cfg: Config, progress=None) -> dict[str, Any]:
    client = AniListClient(cfg)
    stats = {"updated": 0, "errors": 0, "messages": []}
    rows = list_watch(cfg)
    total = max(1, len(rows))
    for index, row in enumerate(rows, start=1):
        if progress:
            progress(int(index / total * 100), f"Actualizando {row['title']}")
        if row["provider"] != "anilist":
            continue
        try:
            media = client.media(int(row["provider_id"]))
            add_watch(cfg, media, notes=row.get("notes") or "")
            stats["updated"] += 1
        except Exception as exc:
            stats["errors"] += 1
            stats["messages"].append(f"{row['title']}: {exc}")
    return stats


def upcoming(cfg: Config, days: int = 14) -> list[dict[str, Any]]:
    now = dt.datetime.now(dt.timezone.utc)
    limit = now + dt.timedelta(days=days)
    result = []
    for row in list_watch(cfg):
        moment = parse_iso(row.get("next_air_at"))
        if moment and now <= moment <= limit:
            item = dict(row)
            item["when"] = moment.astimezone()
            result.append(item)
    result.sort(key=lambda item: item["when"])
    return result


def library_airing(cfg: Config, recent_days: int = 60) -> list[dict[str, Any]]:
    cutoff = (dt.date.today() - dt.timedelta(days=recent_days)).isoformat()
    with session(cfg.db_path) as con:
        rows = con.execute(
            "SELECT * FROM series WHERE is_airing=1 OR (last_aired IS NOT NULL AND last_aired>=?) "
            "ORDER BY CASE WHEN next_air_at IS NULL THEN 1 ELSE 0 END, next_air_at, "
            "last_aired DESC, title",
            (cutoff,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["last_aired_label"] = format_date_es(item.get("last_aired"))
            item["next_air_label"] = format_date_es(item.get("next_air_at")) if item.get("next_air_at") else "—"
            if item.get("next_air_at"):
                item["state"] = "En emisión"
            elif item.get("last_aired") and item["last_aired"] >= cutoff:
                item["state"] = "Finalizada hace poco"
            else:
                item["state"] = "En pausa"
            result.append(item)
        return result


def set_series_root(cfg: Config, series_id: int, root: str) -> None:
    if root not in {"library", "airing"}:
        raise ValueError("Destino inválido")
    with session(cfg.db_path) as con:
        con.execute(
            "UPDATE series SET root=?, root_override=1, updated_at=? WHERE id=?",
            (root, now_iso(), series_id),
        )


def set_series_airing(cfg: Config, series_id: int, airing: bool) -> None:
    with session(cfg.db_path) as con:
        con.execute(
            "UPDATE series SET is_airing=?, updated_at=? WHERE id=?",
            (1 if airing else 0, now_iso(), series_id),
        )
