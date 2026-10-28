"""Detección de temporadas/episodios y construcción de nombres estilo Jellyfin."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .utils import sanitize_component

VIDEO_EXTS = {".mp4", ".mkv", ".ts", ".avi", ".webm", ".m4v", ".mov", ".wmv", ".mpg", ".mpeg"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
NFO_EXTS = {".nfo"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz"}

QUALITY_RE = re.compile(r"\b(2160p|1080p|720p|480p|576p|4k|uhd)\b", re.I)
SOURCE_RE = re.compile(r"\b(bluray|blu-ray|bdrip|bd|webrip|web-dl|webdl|web|dvdrip|dvd|hdtv|tvrip)\b", re.I)
CODEC_RE = re.compile(r"\b(x265|hevc|h\.?265|x264|avc|h\.?264|10bit|8bit)\b", re.I)
RELEASE_GROUP_PREFIX = re.compile(r"^\s*\[[^\]]{1,40}\]\s*")
KNOWN_PREFIX = re.compile(r"^(?:YK|Sw-Anime|CR|Dw|Erai-raws|SubsPlease|HorribleSubs|MacroHEX)[-_ ]+", re.I)
RESOLUTION_NUMBERS = {480, 576, 720, 1080, 2160, 1440}
EPISODE_MARKER_RE = re.compile(
    r"^\s*S(\d{1,3})\s*E(\d{1,4})(?:\s*[-–]\s*E?\d{1,4})?\s*[-–—:]\s*",
    re.I,
)
SEPARATORS = " -–—:·|"


@dataclass
class FolderInfo:
    raw: str
    title_guess: str
    year: int | None = None
    season: int | None = None
    is_special: bool = False


@dataclass
class ParsedEpisode:
    season: int
    episode: float
    episode_end: float | None = None
    absolute: float | None = None
    special: bool = False
    unnumbered_special: bool = False
    decimal_special: bool = False
    explicit_season: bool = False
    title_hint: str = ""
    resolution: str = ""
    quality: str = ""
    matched_by: str = ""
    matched_text: str = ""
    raw_stem: str = ""


def normalize_underscores(text: str) -> str:
    return re.sub(r"[_\u00a0]+", " ", text)


def clean_release_stem(stem: str) -> str:
    value = unicodedata.normalize("NFC", stem).strip()
    value = RELEASE_GROUP_PREFIX.sub("", value)
    value = KNOWN_PREFIX.sub("", value)
    value = re.sub(r"-thumb$", "", value, flags=re.I)
    value = value.replace("´", "'").replace("’", "'")
    return value.strip()


def parse_folder_info(folder_name: str) -> FolderInfo:
    raw = folder_name
    value = unicodedata.normalize("NFC", folder_name).strip()
    value = value.replace("´", "'").replace("’", "'")
    value = value.replace("_", " ")
    # Los nombres organizados por Jellyfin añaden [tmdbid-…] al final. No
    # forma parte del título y, si no se retira, impide detectar el año.
    value = re.sub(
        r"\s*\[(?:tmdb|tvdb|imdb)id-[^\]]+\]\s*$",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s+", " ", value).strip()

    year = None
    year_match = re.search(r"\((\d{4})\)\s*$", value)
    if year_match:
        year = int(year_match.group(1))
        value_no_year = (value[: year_match.start()] + value[year_match.end():]).strip(" -")
    else:
        value_no_year = value

    season = None
    is_special = False
    matched = False

    # 2nd Season, 3rd Season...
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\s+Season\b", value_no_year, re.I)
    if m:
        season = int(m.group(1))
        value_no_year = (value_no_year[: m.start()] + value_no_year[m.end():]).strip(" -")
        matched = True

    # Season 2 / Temporada 2
    if not matched:
        m = re.search(r"\b(?:Season|Temporada)\s*(\d{1,2})\b", value_no_year, re.I)
        if m:
            season = int(m.group(1))
            value_no_year = (value_no_year[: m.start()] + value_no_year[m.end():]).strip(" -")
            matched = True

    # S04 / S4 al final o separado por guion
    if not matched:
        m = re.search(r"(?:^|[\s\-_.])S(\d{1,2})\s*$", value_no_year, re.I)
        if m:
            season = int(m.group(1))
            value_no_year = (value_no_year[: m.start()] + value_no_year[m.end():]).strip(" -")
            matched = True

    # "2nd GIG" (Ghost in the Shell SAC)
    if not matched:
        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\s+GIG\b", value_no_year, re.I)
        if m:
            season = int(m.group(1))
            value_no_year = (value_no_year[: m.start()] + value_no_year[m.end():]).strip(" -")
            matched = True

    # Especiales / Specials / OVA como carpeta
    if re.search(r"\b(?:Specials?|Especiales?|Extras?|OVAs?)\b", value_no_year, re.I):
        is_special = True
        season = 0
        value_no_year = re.sub(
            r"\b(?:Specials?|Especiales?|Extras?|OVAs?)\b", " ", value_no_year, flags=re.I
        ).strip(" -")

    title_guess = re.sub(r"\s+", " ", value_no_year).strip(" -")
    # Quitar prefijos de release que a veces vienen en la carpeta.
    title_guess = clean_release_stem(title_guess)
    return FolderInfo(raw=raw, title_guess=title_guess, year=year, season=season, is_special=is_special)


def parse_path_season(relative_parts: list[str]) -> tuple[int | None, bool]:
    """Detecta Season 02 / S02 / Specials en carpetas padre."""
    for part in reversed(relative_parts[:-1]):
        text = part.strip()
        m = re.match(r"^(?:Season|Temporada)\s*0*(\d{1,2})$", text, re.I)
        if m:
            return int(m.group(1)), int(m.group(1)) == 0
        m = re.match(r"^S(\d{1,2})$", text, re.I)
        if m:
            return int(m.group(1)), int(m.group(1)) == 0
        if re.match(r"^(?:Specials?|Especiales?|Extras?)$", text, re.I):
            return 0, True
    return None, False


def _is_plausible_episode(number: float, generic: bool = False) -> bool:
    if number < 0 or number > 2000:
        return False
    if generic:
        if int(number) in RESOLUTION_NUMBERS:
            return False
        if 1900 <= int(number) <= 2100:
            return False
    return True


def _quality_from_stem(stem: str) -> tuple[str, str]:
    res = ""
    source = ""
    codec = ""
    m = QUALITY_RE.search(stem)
    if m:
        res = m.group(1).lower().replace("4k", "2160p").replace("uhd", "2160p")
        if res.isdigit():
            res += "p"
    m = SOURCE_RE.search(stem)
    if m:
        raw = m.group(1).lower()
        mapping = {
            "bluray": "BD", "blu-ray": "BD", "bdrip": "BD", "bd": "BD",
            "webrip": "WEB", "web-dl": "WEB", "webdl": "WEB", "web": "WEB",
            "dvdrip": "DVD", "dvd": "DVD", "hdtv": "HDTV", "tvrip": "TV",
        }
        source = mapping.get(raw, raw.upper())
    m = CODEC_RE.search(stem)
    if m:
        codec = m.group(1).lower().replace("h.265", "HEVC").replace("h265", "HEVC")
        codec = codec.upper() if codec in {"hevc", "avc"} else codec
    parts = [p for p in (source, res, codec) if p]
    return res, " ".join(parts)


def _series_name_variants(
    series_title: str | None,
    series_year: int | None = None,
    series_folder: str | None = None,
) -> list[tuple[str, int | None]]:
    """Devuelve (título, año) sin el tag de Jellyfin ni el año entre paréntesis."""
    values: list[tuple[str, int | None]] = []
    for raw in (series_title, series_folder):
        if not raw:
            continue
        value = unicodedata.normalize("NFC", str(raw)).strip()
        value = re.sub(r"\s*\[(?:tmdb|tvdb|imdb)id-[^\]]+\]\s*$", "", value, flags=re.I)
        year = None
        year_match = re.search(r"\s*\((\d{4})\)\s*$", value)
        if year_match:
            year = int(year_match.group(1))
            value = value[: year_match.start()].strip()
        value = value.strip(" -–—:")
        if value:
            values.append((value, series_year or year))
    result: list[tuple[str, int | None]] = []
    seen: set[str] = set()
    for name, year in values:
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append((name, year))
    return result


def _strip_series_prefix(
    text: str,
    series_title: str | None = None,
    series_year: int | None = None,
    series_folder: str | None = None,
) -> str:
    """Quita prefijos de la ficha y marcas SxxExx repetidas.

    Los nombres generados por Jellyfin incluyen el título de la serie antes de
    ``S01E01``. Si ese nombre se vuelve a usar como título del episodio, el
    siguiente plan añade el prefijo otra vez. Esta función corta todas las
    repeticiones iniciales y deja solo el título real del episodio.
    """
    value = unicodedata.normalize("NFC", str(text or "")).strip()
    if not value:
        return ""
    names = _series_name_variants(series_title, series_year, series_folder)
    if not names:
        return value

    for _ in range(8):
        before = value
        marker = EPISODE_MARKER_RE.match(value)
        if marker:
            value = value[marker.end():]
        for name, year in names:
            year_part = (
                rf"(?:\s*\(\s*{int(year)}\s*\))?"
                if year
                else r"(?:\s*\(\s*\d{4}\s*\))?"
            )
            pattern = re.compile(
                rf"^\s*{re.escape(name)}{year_part}\s*(?:[-–—:·|]\s*|$)",
                re.I,
            )
            match = pattern.match(value)
            if match:
                value = value[match.end():].lstrip(SEPARATORS + " ")
                break
        value = re.sub(rf"^[\s{re.escape(SEPARATORS)}]+", "", value)
        value = re.sub(r"\s+", " ", value).strip(SEPARATORS + " ")
        if value == before:
            break
    return value


def clean_episode_title(
    title: str | None,
    series_title: str | None = None,
    series_year: int | None = None,
    series_folder: str | None = None,
) -> str:
    """Limpia un título de episodio sin tocar el título propio de la serie.

    Es idempotente: si recibe un nombre ya canónico
    ``Serie (2008) - S00E02 - Toaru Majutsu no Index`` devuelve
    ``Toaru Majutsu no Index``, no una segunda copia del prefijo.
    """
    if not title:
        return ""
    value = unicodedata.normalize("NFC", str(title)).strip()
    value = value.replace("_", " ")
    value = re.sub(r"\s+", " ", value)
    value = _strip_series_prefix(value, series_title, series_year, series_folder)
    value = re.sub(rf"^[\s{re.escape(SEPARATORS)}]+", "", value)
    value = re.sub(rf"[\s{re.escape(SEPARATORS)}]+$", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _clean_title_hint(
    text: str,
    series_title: str | None = None,
    series_year: int | None = None,
    series_folder: str | None = None,
) -> str:
    value = text
    value = RELEASE_GROUP_PREFIX.sub("", value)
    value = KNOWN_PREFIX.sub("", value)
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", value)
    value = QUALITY_RE.sub(" ", value)
    value = SOURCE_RE.sub(" ", value)
    value = CODEC_RE.sub(" ", value)
    value = re.sub(r"\b(?:BD|DVD|WEB|HDTV|10bit|8bit|AAC|FLAC|DDP?|x264|x265|HEVC)\b", " ", value, flags=re.I)
    value = value.replace("_", " ").replace(".", " ")
    value = _strip_series_prefix(value, series_title, series_year, series_folder)
    value = re.sub(rf"^[\s{re.escape(SEPARATORS)}]+|[\s{re.escape(SEPARATORS)}]+$", "", value)
    value = re.sub(r"\s+", " ", value).strip(SEPARATORS + " ")
    if len(value) < 3:
        return ""
    if re.fullmatch(r"[\d\s.\-]+", value):
        return ""
    return value


def parse_episode(
    stem: str,
    folder_season: int | None = None,
    folder_special: bool = False,
    *,
    series_title: str | None = None,
    series_year: int | None = None,
    series_folder: str | None = None,
) -> ParsedEpisode | None:
    """Extrae temporada/episodio del nombre de archivo.

    Prioriza patrones inequívocos (SxxExx, 1x01, Cap.01). Si no hay número,
    devuelve None para que el archivo se revise manualmente.
    """
    original = stem
    work = clean_release_stem(stem)
    work = normalize_underscores(work)
    work = re.sub(r"\s+", " ", work).strip()

    resolution, quality = _quality_from_stem(stem)
    matched_by = ""
    matched_text = ""
    explicit_season: int | None = None
    special = folder_special
    unnumbered_special = False
    decimal_special = False
    episode: float | None = None
    episode_end: float | None = None

    # 1) S01E01 / S01E01-E02 (evitar "S01E05.720p" y "S01E01-720p")
    m = re.search(
        r"\bS(\d{1,2})\s*E(\d{1,3}(?:\.\d)?)(?!\d*[pP]\b)"
        r"(?:\s*[-–]\s*E?(\d{1,3}(?:\.\d)?)(?!\d*[pP]\b))?",
        work,
        re.I,
    )
    if m:
        explicit_season = int(m.group(1))
        episode = float(m.group(2))
        if m.group(3):
            episode_end = float(m.group(3))
        matched_by = "SxxExx"
        matched_text = m.group(0)

    # 2) 1x01
    if episode is None:
        m = re.search(r"\b(\d{1,2})x(\d{2,3}(?:\.\d)?)\b", work, re.I)
        if m:
            explicit_season = int(m.group(1))
            episode = float(m.group(2))
            matched_by = "1x01"
            matched_text = m.group(0)

    # 3) Season 2 - 01
    if episode is None:
        m = re.search(r"\b(?:Season|Temporada)\s*(\d{1,2})\s*[-–:]\s*(\d{1,3}(?:\.\d)?)", work, re.I)
        if m:
            explicit_season = int(m.group(1))
            episode = float(m.group(2))
            matched_by = "Season-N"
            matched_text = m.group(0)

    # 4) S2 - 01 / S02 - 01
    if episode is None:
        m = re.search(r"\bS(\d{1,2})\b\s*[-–:]\s*(\d{1,3}(?:\.\d)?)\b", work, re.I)
        if m:
            explicit_season = int(m.group(1))
            episode = float(m.group(2))
            matched_by = "S-N"
            matched_text = m.group(0)

    # 5) Cap.01 / Capítulo 01
    if episode is None:
        m = re.search(r"\bCap(?:[ií]tulo)?\.?\s*(\d{1,3}(?:\.\d)?)", work, re.I)
        if m:
            episode = float(m.group(1))
            matched_by = "Cap"
            matched_text = m.group(0)

    # 6) Episodio 9
    if episode is None:
        m = re.search(r"\bEpisodio\s+(\d{1,3}(?:\.\d)?)", work, re.I)
        if m:
            episode = float(m.group(1))
            matched_by = "Episodio"
            matched_text = m.group(0)

    # 7) OVA 01 / Especial 1 / Special 2
    if episode is None:
        m = re.search(r"\b(?:OVA|OAV|Special|Especial)\s*0*(\d{1,3}(?:\.\d)?)?\b", work, re.I)
        if m:
            special = True
            if m.group(1):
                episode = float(m.group(1))
            else:
                episode = 1.0
                unnumbered_special = True
            matched_by = "OVA/Special"
            matched_text = m.group(0)

    # 8) Index-tan specials
    if episode is None:
        m = re.search(r"\btan[ _-]?0*(\d{1,3})\b", work, re.I)
        if m:
            special = True
            episode = float(m.group(1))
            matched_by = "tan"
            matched_text = m.group(0)

    # 8b) Especiales con hash al final: BNW_no_Chikai_1_19869758
    if episode is None:
        m = re.search(r"[\s_-](\d{1,2})[\s_-][A-Fa-f0-9]{6,}\b", work)
        if m:
            special = True
            episode = float(m.group(1))
            matched_by = "hash-suffix"
            matched_text = m.group(0)

    # 9) Episode 01 / Ep 01 / E01
    if episode is None:
        m = re.search(r"\b(?:Episode|Ep|E)\s*0*(\d{1,3}(?:\.\d)?)\b", work, re.I)
        if m:
            episode = float(m.group(1))
            matched_by = "Episode"
            matched_text = m.group(0)

    # 10) _01_ (To Aru / Railgun). Si el título contiene números (p. ej.
    # "Ranma_1_2_2024_01") se usa el último número suelto, no el primero.
    if episode is None:
        matches = list(
            re.finditer(
                r"(?:^|[\s])_?0*(\d{1,3}(?:\.\d)?)_?(?:[\s]|$)",
                work,
            )
        )
        if matches:
            m = matches[-1]
            candidate = float(m.group(1))
            if _is_plausible_episode(candidate, generic=True):
                episode = candidate
                matched_by = "_NN_"
                matched_text = m.group(0)

    # 11) " - 01" / " 01 [" / " 01 (" / " 01" al final
    if episode is None:
        m = re.search(r"(?:^|[\s\-–])(\d{1,3})\s*[-–]\s*(\d{1,3})\s*(?:\[|\(|$)", work)
        if m:
            candidate = float(m.group(1))
            if _is_plausible_episode(candidate, generic=True):
                episode = candidate
                episode_end = float(m.group(2))
                matched_by = "range"
                matched_text = m.group(0)

    # 12) " - 01" / " 01 [" / " 01 (" / " 01" al final
    if episode is None:
        m = re.search(r"(?:^|[\s\-–])(\d{1,3}(?:\.\d)?)\s*(?:\[|\(|$)", work)
        if m:
            candidate = float(m.group(1))
            if _is_plausible_episode(candidate, generic=True):
                episode = candidate
                matched_by = "trailing-number"
                matched_text = m.group(0)

    # 13) "01" al inicio
    if episode is None:
        m = re.match(r"^0*(\d{1,3}(?:\.\d)?)(?:[\s\-–]|$)", work)
        if m:
            candidate = float(m.group(1))
            if _is_plausible_episode(candidate, generic=True):
                episode = candidate
                matched_by = "leading-number"
                matched_text = m.group(0)

    if episode is None or not _is_plausible_episode(episode, generic=not matched_by.startswith(("SxxExx", "1x01"))):
        return None

    # Decimales sueltos (8.5, 18.5...) se tratan como especiales.
    if (
        explicit_season is None
        and not special
        and abs(float(episode) - round(float(episode))) > 1e-9
    ):
        special = True
        decimal_special = True

    # Rango 01-02 al inicio (multi-episodio) si no se detectó antes.
    if episode_end is None:
        m = re.match(r"^0*(\d{1,3})\s*[-–]\s*0*(\d{1,3})(?:\s|$)", work)
        if m and float(m.group(1)) == episode:
            episode_end = float(m.group(2))

    if special:
        season = 0
    elif explicit_season is not None:
        season = explicit_season
    elif folder_season is not None:
        season = int(folder_season)
    else:
        season = 1

    title_hint = ""
    if matched_text:
        start = work.lower().find(matched_text.lower())
        if start >= 0:
            remainder = work[:start] + " " + work[start + len(matched_text):]
            title_hint = _clean_title_hint(
                remainder, series_title, series_year, series_folder
            )

    return ParsedEpisode(
        season=int(season),
        episode=float(episode),
        episode_end=episode_end,
        absolute=float(episode) if season == 1 and explicit_season is None else None,
        special=bool(special or season == 0),
        unnumbered_special=unnumbered_special,
        decimal_special=decimal_special,
        explicit_season=explicit_season is not None,
        title_hint=title_hint,
        resolution=resolution,
        quality=quality,
        matched_by=matched_by,
        matched_text=matched_text,
        raw_stem=original,
    )


def format_number(number: float | int) -> str:
    number = float(number)
    if abs(number - round(number)) < 1e-9:
        return f"{int(round(number)):02d}"
    # Jellyfin no maneja bien decimales; se usan solo como último recurso.
    return f"{number:05.1f}".replace(".", ".")


def folder_name_for_series(
    title: str,
    year: int | None,
    tmdb_id: str | None = None,
    include_tmdb_id: bool = True,
) -> str:
    clean_title = sanitize_component(title, 110)
    if year:
        base = f"{clean_title} ({int(year)})"
    else:
        base = clean_title
    if include_tmdb_id and tmdb_id:
        base += f" [tmdbid-{str(tmdb_id).strip()}]"
    return base


def season_folder_name(season: int) -> str:
    return "Season 00" if int(season) == 0 else f"Season {int(season):02d}"


def build_episode_filename(
    series_folder: str,
    season: int,
    episode: float,
    episode_end: float | None,
    title: str | None,
    extension: str,
    quality: str = "",
    include_title: bool = True,
    include_quality: bool = False,
    title_max_len: int = 90,
    *,
    series_title: str | None = None,
    series_year: int | None = None,
) -> str:
    season_int = int(season)
    ep_text = format_number(episode)
    base = f"{series_folder} - S{season_int:02d}E{ep_text}"
    if episode_end:
        base += f"-E{format_number(episode_end)}"
    if include_title and title:
        # Si el título ya trae el prefijo de la serie (por ejemplo, porque se
        # releyó un nombre generado antes), se elimina antes de volver a
        # añadirlo. Así el nombre es idempotente y no acumula prefijos.
        clean_title = clean_episode_title(
            title,
            series_title=series_title,
            series_year=series_year,
            series_folder=series_folder,
        )
        clean_title = sanitize_component(clean_title, title_max_len)
        if clean_title:
            base += f" - {clean_title}"
    if include_quality and quality:
        base += f" [{sanitize_component(quality, 30)}]"
    ext = extension if extension.startswith(".") else "." + extension
    return base + ext


def target_paths(
    root: Path,
    series_title: str,
    year: int | None,
    tmdb_id: str | None,
    include_tmdb_id: bool,
    season: int,
    episode: float,
    episode_end: float | None,
    episode_title: str | None,
    extension: str,
    quality: str = "",
    include_title: bool = True,
    include_quality: bool = False,
    title_max_len: int = 90,
    file_title: str | None = None,
) -> tuple[Path, Path, Path]:
    series_folder_name = folder_name_for_series(series_title, year, tmdb_id, include_tmdb_id)
    season_folder = season_folder_name(season)
    series_dir = root / series_folder_name
    season_dir = series_dir / season_folder
    display = file_title or series_title
    if year and not file_title:
        display = f"{series_title} ({int(year)})"
    filename = build_episode_filename(
        sanitize_component(display, 120),
        season,
        episode,
        episode_end,
        episode_title,
        extension,
        quality,
        include_title,
        include_quality,
        title_max_len,
        series_title=series_title,
        series_year=year,
    )
    return series_dir, season_dir, season_dir / filename


def media_kind(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in SUBTITLE_EXTS:
        return "subtitle"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in NFO_EXTS:
        return "nfo"
    if ext in ARCHIVE_EXTS:
        return "archive"
    return "other"


def image_role(path: str | Path) -> str:
    name = Path(path).name.lower()
    stem = Path(path).stem.lower()
    if re.match(r"^(folder|poster|cover)$", stem):
        return "poster"
    if re.match(r"^(backdrop|fanart|background|banner)$", stem):
        return "backdrop"
    if re.match(r"^(landscape|thumb)$", stem):
        return "landscape"
    if re.match(r"^(logo|clearlogo)$", stem):
        return "logo"
    if re.match(r"^season\s*0*\d+\s*[-_]?(poster|folder|cover)?$", stem):
        return "season_poster"
    if stem.endswith("-thumb") or stem.endswith("_thumb"):
        return "episode_thumb"
    if "poster" in stem:
        return "poster"
    if "backdrop" in stem or "fanart" in stem:
        return "backdrop"
    if "logo" in stem:
        return "logo"
    return "other"


def subtitle_language(path: str | Path) -> str:
    stem = Path(path).stem
    parts = stem.split(".")
    if len(parts) >= 2:
        candidate = parts[-1].lower()
        if re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,4})?", candidate):
            return candidate
    return ""


def clean_empty_dirs(root: Path, keep: set[Path] | None = None) -> list[Path]:
    """Elimina carpetas vacías bajo root, de más profunda a más superficial."""
    removed: list[Path] = []
    keep = {p.resolve() for p in (keep or set())}
    if not root.exists():
        return removed
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not path.is_dir():
            continue
        if path.resolve() in keep:
            continue
        try:
            if not any(path.iterdir()):
                path.rmdir()
                removed.append(path)
        except OSError:
            pass
    return removed
