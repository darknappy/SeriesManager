"""Configuración persistente del gestor de series."""
from __future__ import annotations

import copy
import json
import os
import unicodedata
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent.parent

# Carpetas de descarga en curso que pueden vivir dentro de la biblioteca
# principal. No son series: se excluyen del catálogo y se usan para reconocer
# qué animes de la temporada ya están descargados.
STAGING_FOLDER_NAMES = {
    "en emision", "en emisión", "inbox", "descargas", "staging",
    "nuevos", "pendientes", "descargas en curso",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "library_root": r"D:\Series",
    "airing_root": r"D:\SeriesEnEmision",
    "staging_roots": [],
    "app_root": str(APP_ROOT),
    "inbox_root": str(APP_ROOT / "inbox"),
    "quarantine_root": str(APP_ROOT / "quarantine"),
    "reports_root": str(APP_ROOT / "data" / "reports"),
    "cache_root": str(APP_ROOT / "data" / "cache"),
    "logs_root": str(APP_ROOT / "data" / "logs"),
    "db_path": str(APP_ROOT / "data" / "catalog.db"),
    "jobs_db_path": str(APP_ROOT / "data" / "jobs.db"),
    "title_language": "english",
    "include_tmdb_id": True,
    "include_quality": False,
    "include_episode_title": True,
    "episode_title_max_len": 90,
    "keep_episode_thumbs": False,
    "quarantine_unparsed": False,
    "special_decimals_to_season_zero": True,
    "auto_match_threshold": 0.80,
    "airing_horizon_days": 45,
    "auto_graduate": False,
    "web": {"host": "127.0.0.1", "port": 5055, "open_browser": True},
    "jellyfin": {
        "url": "http://127.0.0.1:8096",
        "api_key": "",
        "user_id": "",
        "db_path": r"C:\ProgramData\Jellyfin\Server\data\jellyfin.db",
        "main_library_path": r"D:\Series",
        "airing_library_name": "Anime en emisión",
        "airing_library_path": r"D:\SeriesEnEmision",
    },
    "providers": {
        "tvmaze": True,
        "anilist": True,
        "tmdb": {"enabled": False, "api_key": ""},
    },
    "http": {
        "timeout": 30,
        "cache_ttl_hours": 12,
        "anilist_delay_ms": 850,
        "tvmaze_delay_ms": 350,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """Acceso a config.json con valores por defecto sensatos."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path:
            self.path = Path(path)
        else:
            self.path = Path(os.environ.get("SERIESADMIN_CONFIG") or (APP_ROOT / "config.json"))
        self.data: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data = _deep_merge(self.data, loaded)
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"No se pudo leer {self.path}: {exc}") from exc
        # El app_root siempre es la carpeta real del programa.
        self.data["app_root"] = str(APP_ROOT)
        self.data.setdefault("db_path", str(APP_ROOT / "data" / "catalog.db"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def update(self, values: dict[str, Any]) -> None:
        self.data = _deep_merge(self.data, values)

    # Rutas ---------------------------------------------------------------
    @property
    def library_root(self) -> Path:
        return Path(self.data["library_root"])

    @property
    def airing_root(self) -> Path:
        return Path(self.data["airing_root"])

    @property
    def staging_roots(self) -> list[Path]:
        """Carpetas de descargas que no deben escanearse como series.

        Se admiten rutas explícitas en ``staging_roots`` y, además, se detectan
        carpetas habituales dentro de la biblioteca principal (por ejemplo
        ``En emision``) para no crear fichas falsas.
        """
        roots: list[Path] = []
        for raw in self.data.get("staging_roots") or []:
            if not str(raw).strip():
                continue
            path = Path(str(raw)).expanduser()
            if not path.is_absolute():
                path = self.app_root / path
            roots.append(path)
        try:
            children = sorted(self.library_root.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            children = []
        for child in children:
            if not child.is_dir():
                continue
            key = unicodedata.normalize("NFC", child.name).casefold()
            key = " ".join(key.replace("_", " ").split())
            if key in STAGING_FOLDER_NAMES:
                roots.append(child)
        result: list[Path] = []
        seen: set[str] = set()
        for path in roots:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            marker = str(resolved).casefold()
            if marker in seen:
                continue
            seen.add(marker)
            result.append(resolved)
        return result

    @property
    def app_root(self) -> Path:
        return Path(self.data["app_root"])

    @property
    def db_path(self) -> Path:
        return Path(self.data["db_path"])

    @property
    def jobs_db_path(self) -> Path:
        return Path(self.data.get("jobs_db_path") or (self.app_root / "data" / "jobs.db"))

    @property
    def reports_root(self) -> Path:
        return Path(self.data["reports_root"])

    @property
    def cache_root(self) -> Path:
        return Path(self.data["cache_root"])

    @property
    def logs_root(self) -> Path:
        return Path(self.data["logs_root"])

    @property
    def quarantine_root(self) -> Path:
        return Path(self.data["quarantine_root"])

    def ensure_dirs(self) -> None:
        for path in (
            self.app_root,
            self.airing_root,
            Path(self.data["inbox_root"]),
            self.quarantine_root,
            self.reports_root,
            self.cache_root,
            self.logs_root,
            self.db_path.parent,
            self.jobs_db_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def as_public_dict(self) -> dict[str, Any]:
        """Config sin secretos, para mostrar en la web."""
        data = copy.deepcopy(self.data)
        if data.get("jellyfin", {}).get("api_key"):
            data["jellyfin"]["api_key"] = "********"
        if data.get("providers", {}).get("tmdb", {}).get("api_key"):
            data["providers"]["tmdb"]["api_key"] = "********"
        return data
