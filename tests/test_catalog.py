"""Pruebas de reparación de carpetas, aliases y descargas en curso."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from series_manager.catalog import (
    attach_folder_alias,
    cleanup_staging_catalog,
    identify_folder,
    list_unidentified,
    prune_stale_groups,
    reconcile_tmdb_folders,
)
from series_manager.config import Config
from series_manager.db import get_or_create_series, init_db, session
from series_manager.seeds import ensure_group


def make_config(root: Path, **overrides) -> Config:
    library = root / "library"
    airing = root / "airing"
    library.mkdir(parents=True, exist_ok=True)
    airing.mkdir(parents=True, exist_ok=True)
    data = {
        "library_root": str(library),
        "airing_root": str(airing),
        "staging_roots": overrides.pop("staging_roots", []),
        "quarantine_root": str(root / "quarantine"),
        "inbox_root": str(root / "inbox"),
        "reports_root": str(root / "reports"),
        "cache_root": str(root / "cache"),
        "logs_root": str(root / "logs"),
        "db_path": str(root / "catalog.db"),
        "jobs_db_path": str(root / "jobs.db"),
        **overrides,
    }
    config_path = root / "config.json"
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cfg = Config(config_path)
    init_db(cfg.db_path)
    return cfg


def insert_group(con, folder: Path, series_key: str, root: str = "library") -> int:
    con.execute(
        "INSERT INTO source_groups(folder_name, folder_path, series_key, position, root, updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (folder.name, str(folder), series_key, 9999, root, "2026-01-01T00:00:00+00:00"),
    )
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


class CatalogRepairTests(unittest.TestCase):
    def test_ensure_group_uses_tmdb_from_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            folder = cfg.library_root / "InuYasha (2000) [tmdbid-35610]"
            folder.mkdir()
            with session(cfg.db_path) as con:
                get_or_create_series(
                    con,
                    "inuyasha",
                    {
                        "title": "InuYasha",
                        "year": 2000,
                        "tmdb_id": "35610",
                        "match_status": "matched",
                    },
                )
                group = ensure_group(con, cfg, folder.name)
                self.assertEqual(group["series_key"], "inuyasha")
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM series WHERE series_key LIKE 'inuyasha%'").fetchone()[0],
                    1,
                )
            self.assertTrue(folder.is_dir())

    def test_reconcile_moves_files_and_updates_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            folder = cfg.library_root / "InuYasha (2000) [tmdbid-35610]"
            folder.mkdir()
            (folder / "Episode.mp4").write_bytes(b"video")
            with session(cfg.db_path) as con:
                canonical_id = get_or_create_series(
                    con,
                    "inuyasha",
                    {"title": "InuYasha", "tmdb_id": "35610", "match_status": "matched"},
                )
                placeholder_id = get_or_create_series(
                    con,
                    "old-placeholder",
                    {"title": "InuYasha", "match_status": "unmatched", "root": "library"},
                )
                insert_group(con, folder, "old-placeholder")
                con.execute(
                    "INSERT INTO files(path, series_id, kind, status, updated_at) VALUES(?,?,?,?,?)",
                    (str(folder / "Episode.mp4"), placeholder_id, "video", "present", "2026-01-01T00:00:00+00:00"),
                )
                stats = reconcile_tmdb_folders(con, cfg)
                self.assertEqual(len(stats["merged"]), 1)
                self.assertEqual(
                    con.execute("SELECT series_key FROM source_groups WHERE folder_name=?", (folder.name,)).fetchone()[0],
                    "inuyasha",
                )
                self.assertEqual(
                    con.execute("SELECT series_id FROM files WHERE path=?", (str(folder / "Episode.mp4"),)).fetchone()[0],
                    canonical_id,
                )
                self.assertIsNone(
                    con.execute("SELECT 1 FROM series WHERE id=?", (placeholder_id,)).fetchone()
                )

    def test_cleanup_staging_keeps_disk_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "library" / "En emision"
            staging.mkdir(parents=True)
            video = staging / "Capitulo.mp4"
            video.write_bytes(b"video")
            cfg = make_config(root)
            with session(cfg.db_path) as con:
                series_id = get_or_create_series(
                    con,
                    "en-emision",
                    {"title": "En emision", "match_status": "unmatched", "root": "library"},
                )
                group_id = insert_group(con, staging, "en-emision")
                con.execute(
                    "INSERT INTO files(path, series_id, kind, status, updated_at) VALUES(?,?,?,?,?)",
                    (str(video), series_id, "video", "present", "2026-01-01T00:00:00+00:00"),
                )
                stats = cleanup_staging_catalog(con, cfg)
                self.assertEqual(stats["series"], 1)
                self.assertEqual(stats["groups"], 1)
                self.assertEqual(stats["files"], 1)
                self.assertIsNone(con.execute("SELECT 1 FROM series WHERE id=?", (series_id,)).fetchone())
                self.assertIsNone(con.execute("SELECT 1 FROM source_groups WHERE id=?", (group_id,)).fetchone())
                self.assertIsNone(con.execute("SELECT 1 FROM files WHERE path=?", (str(video),)).fetchone())
            self.assertTrue(video.exists())

    def test_identify_modified_folder_merges_into_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            folder = cfg.library_root / "Carpeta modificada"
            folder.mkdir()
            with session(cfg.db_path) as con:
                canonical_id = get_or_create_series(
                    con,
                    "inuyasha",
                    {"title": "InuYasha", "tmdb_id": "35610", "match_status": "matched"},
                )
                placeholder_id = get_or_create_series(
                    con,
                    "carpeta-modificada",
                    {"title": "Carpeta modificada", "match_status": "unmatched"},
                )
                insert_group(con, folder, "carpeta-modificada")
            result = identify_folder(
                cfg,
                folder_name=folder.name,
                title="InuYasha",
                tmdb_id="35610",
            )
            self.assertEqual(result["series_id"], canonical_id)
            self.assertEqual(result["merged"], 1)
            with session(cfg.db_path) as con:
                self.assertIsNone(con.execute("SELECT 1 FROM series WHERE id=?", (placeholder_id,)).fetchone())
                group = con.execute("SELECT * FROM source_groups WHERE folder_name=?", (folder.name,)).fetchone()
                self.assertEqual(group["series_key"], "inuyasha")
                self.assertEqual(
                    con.execute("SELECT match_status FROM series WHERE id=?", (canonical_id,)).fetchone()[0],
                    "manual",
                )

    def test_attach_alias_and_prune_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            current = cfg.library_root / "Serie actual"
            current.mkdir()
            stale = cfg.library_root / "Serie vieja"
            with session(cfg.db_path) as con:
                series_id = get_or_create_series(
                    con, "demo", {"title": "Demo", "match_status": "manual"}
                )
                insert_group(con, current, "demo")
                insert_group(con, stale, "demo")
                removed = prune_stale_groups(con, cfg)
                self.assertEqual(removed, 1)
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM source_groups WHERE series_key='demo'").fetchone()[0],
                    1,
                )
            alias = cfg.library_root / "Otro nombre"
            alias.mkdir()
            result = attach_folder_alias(cfg, series_id, alias.name)
            self.assertEqual(result["series_id"], series_id)
            with session(cfg.db_path) as con:
                self.assertEqual(
                    con.execute("SELECT series_key FROM source_groups WHERE folder_name=?", (alias.name,)).fetchone()[0],
                    "demo",
                )

    def test_unidentified_lists_only_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            folder = cfg.library_root / "Sin identificar"
            folder.mkdir()
            with session(cfg.db_path) as con:
                get_or_create_series(
                    con, "sin-identificar", {"title": "Sin identificar", "match_status": "unmatched"}
                )
                insert_group(con, folder, "sin-identificar")
                rows = list_unidentified(con, cfg)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["folder_name"], folder.name)


if __name__ == "__main__":
    unittest.main()
