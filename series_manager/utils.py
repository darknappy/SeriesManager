"""Utilidades comunes."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def local_dt(value: str | int | float | None) -> dt.datetime | None:
    """Convierte ISO o timestamp Unix a datetime local."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value).astimezone()
    parsed = parse_iso(str(value))
    return parsed.astimezone() if parsed else None


def format_date_es(value: str | int | float | dt.datetime | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, dt.datetime):
        d = value
    else:
        d = local_dt(value)
    if not d:
        return "—"
    return f"{WEEKDAYS_ES[d.weekday()]} {d.day} {MONTHS_ES[d.month]} {d.year}"


def format_datetime_es(value: str | int | float | dt.datetime | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, dt.datetime):
        d = value
    else:
        d = local_dt(value)
    if not d:
        return "—"
    hour = d.hour % 12 or 12
    ampm = "a.m." if d.hour < 12 else "p.m."
    return f"{format_date_es(d)}, {hour}:{d.minute:02d} {ampm}"


def countdown_es(value: str | int | float | dt.datetime | None) -> str:
    if value is None:
        return ""
    d = value if isinstance(value, dt.datetime) else local_dt(value)
    if not d:
        return ""
    seconds = (d - dt.datetime.now(d.tzinfo or dt.timezone.utc)).total_seconds()
    if seconds < 0:
        return "ya emitido"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"en {minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"en {hours} h {minutes % 60} min"
    days = hours // 24
    return f"en {days} d {hours % 24} h"


def human_size(size: int | float | None) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "sin-titulo"


def fold_ascii(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return text.encode("ascii", "ignore").decode("ascii")


def normalize_title(text: str) -> str:
    """Normalización para comparar títulos."""
    text = fold_ascii(text or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"\b(the|a|an|el|la|los|las|le|les)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def sanitize_component(text: str, max_len: int = 120, fallback: str = "Sin título") -> str:
    """Limpia un componente de ruta para Windows, conservando acentos."""
    value = unicodedata.normalize("NFC", str(text or "")).strip()
    value = value.replace("\u00a0", " ").replace("\u200b", "")
    value = value.replace("’", "'").replace("‘", "'")
    value = value.replace("“", '"').replace("”", '"')
    value = value.replace("–", "-").replace("—", "-")
    # Windows no permite: < > : " / \ | ? *
    value = value.replace(":", " -")
    value = value.replace("/", " ").replace("\\", " ")
    value = re.sub(r'[<>"|?*]', "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    # Nombres reservados de Windows
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if value.upper().split(".")[0] in reserved:
        value = "_" + value
    if len(value) > max_len:
        cut = value[:max_len].rstrip(" .-")
        # Intentar cortar en un espacio para no partir palabras.
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        value = cut.rstrip(" .-") or value[:max_len]
    return value


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
    )
    return re.sub(r"[ \t]+", " ", text).strip()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def quick_fingerprint(path: str | Path, size: int | None = None) -> str:
    """Huella barata: tamaño + primeros/últimos 1 MB."""
    p = Path(path)
    file_size = size if size is not None else p.stat().st_size
    hasher = hashlib.sha1()
    hasher.update(str(file_size).encode("ascii"))
    with p.open("rb") as handle:
        hasher.update(handle.read(1024 * 1024))
        if file_size > 2 * 1024 * 1024:
            handle.seek(-1024 * 1024, os.SEEK_END)
            hasher.update(handle.read(1024 * 1024))
    return hasher.hexdigest()


def is_subpath(path: str | Path, parent: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False


def relative_display(path: str | Path, root: str | Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return str(path)


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    chunk: list[Any] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def ensure_parent(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
