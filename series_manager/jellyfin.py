"""Integración con Jellyfin (API y base de datos local)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import requests

from .config import Config
from .db import session
from .utils import now_iso


class JellyfinError(RuntimeError):
    pass


class JellyfinClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        settings = cfg.get("jellyfin", {}) or {}
        self.url = str(settings.get("url") or "").rstrip("/")
        self.api_key = str(settings.get("api_key") or "")
        self.timeout = 15

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_key)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise JellyfinError(
                "Falta la clave API de Jellyfin. Créala en Jellyfin → Panel → API Keys "
                "y pégala en Ajustes."
            )
        return {"X-Emby-Token": self.api_key, "Accept": "application/json"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = requests.get(
            f"{self.url}{path}", headers=self._headers(), params=params, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json() if response.content else None

    def _post(self, path: str, params: dict[str, Any] | None = None, json: Any = None) -> requests.Response:
        response = requests.post(
            f"{self.url}{path}", headers=self._headers(), params=params, json=json, timeout=self.timeout
        )
        response.raise_for_status()
        return response

    def info(self) -> dict[str, Any]:
        return self._get("/System/Info") or {}

    def users(self) -> list[dict[str, Any]]:
        return self._get("/Users") or []

    def libraries(self) -> list[dict[str, Any]]:
        return self._get("/Library/VirtualFolders") or []

    def refresh_library(self) -> None:
        self._post("/Library/Refresh")

    def create_library(self, name: str, path: str) -> None:
        self._post(
            "/Library/VirtualFolders",
            params={
                "name": name,
                "collectionType": "tvshows",
                "paths": path,
                "refreshLibrary": "true",
            },
        )

    def ensure_airing_library(self) -> dict[str, Any]:
        settings = self.cfg.get("jellyfin", {})
        name = settings.get("airing_library_name", "Anime en emisión")
        path = settings.get("airing_library_path", str(self.cfg.airing_root))
        libraries = self.libraries()
        for library in libraries:
            locations = library.get("Locations") or []
            if library.get("Name") == name or any(
                str(Path(loc)) == str(Path(path)) for loc in locations
            ):
                return {"created": False, "library": library}
        self.create_library(name, path)
        return {"created": True, "name": name, "path": path}

    def test(self) -> dict[str, Any]:
        info = self.info()
        return {
            "ok": True,
            "server": info.get("ServerName") or info.get("Name") or "Jellyfin",
            "version": info.get("Version") or "",
            "url": self.url,
            "users": [u.get("Name") for u in self.users()],
            "libraries": [
                {"name": lib.get("Name"), "locations": lib.get("Locations") or []}
                for lib in self.libraries()
            ],
        }


def backup_watch_state(cfg: Config) -> dict[str, int]:
    """Copia el estado visto de Jellyfin a nuestra base para poder restaurarlo."""
    settings = cfg.get("jellyfin", {}) or {}
    db_path = Path(settings.get("db_path") or "")
    stats = {"rows": 0, "users": 0}
    if not db_path.exists():
        raise JellyfinError(f"No se encuentra la base de datos de Jellyfin: {db_path}")
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        users = {
            row["Id"]: row["Username"]
            for row in source.execute("SELECT Id, Username FROM Users")
        }
        stats["users"] = len(users)
        rows = source.execute(
            "SELECT u.UserId, u.ItemId, u.Played, u.PlaybackPositionTicks, "
            "b.Name, b.SeriesName, b.ParentIndexNumber, b.IndexNumber "
            "FROM UserData u JOIN BaseItems b ON b.Id=u.ItemId "
            "WHERE b.Type LIKE '%TV.Episode'"
        ).fetchall()
        with session(cfg.db_path) as con:
            for row in rows:
                con.execute(
                    "INSERT INTO jellyfin_state(user_id, item_id, series_title, season_number, "
                    "episode_number, episode_title, played, position_ticks, backed_up_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(user_id, item_id) DO UPDATE SET "
                    "series_title=excluded.series_title, season_number=excluded.season_number, "
                    "episode_number=excluded.episode_number, episode_title=excluded.episode_title, "
                    "played=excluded.played, position_ticks=excluded.position_ticks, "
                    "backed_up_at=excluded.backed_up_at",
                    (
                        str(row["UserId"]),
                        str(row["ItemId"]),
                        row["SeriesName"],
                        row["ParentIndexNumber"],
                        row["IndexNumber"],
                        row["Name"],
                        1 if row["Played"] else 0,
                        int(row["PlaybackPositionTicks"] or 0),
                        now_iso(),
                    ),
                )
                stats["rows"] += 1
    finally:
        source.close()
    return stats
