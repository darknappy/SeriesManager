"""Pruebas de detección de descargas y anotación de la temporada."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from series_manager.airing import annotate_season_downloads, staging_downloads
from series_manager.config import Config
from series_manager.db import get_or_create_series, init_db, session, upsert_episode


def make_config(root: Path, staging: Path) -> Config:
    library = root / "library"
    airing = root / "airing"
    library.mkdir(parents=True, exist_ok=True)
    airing.mkdir(parents=True, exist_ok=True)
    data = {
        "library_root": str(library),
        "airing_root": str(airing),
        "staging_roots": [str(staging)],
        "quarantine_root": str(root / "quarantine"),
        "inbox_root": str(root / "inbox"),
        "reports_root": str(root / "reports"),
        "cache_root": str(root / "cache"),
        "logs_root": str(root / "logs"),
        "db_path": str(root / "catalog.db"),
        "jobs_db_path": str(root / "jobs.db"),
    }
    config_path = root / "config.json"
    config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cfg = Config(config_path)
    init_db(cfg.db_path)
    return cfg


class AiringDownloadTests(unittest.TestCase):
    def test_staging_scan_cleans_release_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            (staging / "Sw_Anime_A_Tale_of_the_Secret_Saint_S01E01_720p_Encoded.mp4").write_bytes(b"v")
            (staging / "YK_Dreamland_S01E02_720p.mp4").write_bytes(b"v")
            (staging / "poster.jpg").write_bytes(b"i")
            cfg = make_config(root, staging)
            entries = staging_downloads(cfg)
            self.assertEqual(len(entries), 2)
            titles = {title for entry in entries for title in entry["titles"]}
            self.assertIn("A Tale of the Secret Saint", titles)
            self.assertIn("Dreamland", titles)
            self.assertEqual({entry["episode_label"] for entry in entries}, {"S01E01", "S01E02"})

    def test_annotate_marks_library_and_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            (staging / "A_Tale_of_the_Secret_Saint_S01E01_720p.mp4").write_bytes(b"v")
            cfg = make_config(root, staging)
            with session(cfg.db_path) as con:
                series_id = get_or_create_series(
                    con,
                    "inuyasha",
                    {"title": "InuYasha", "year": 2000, "match_status": "matched"},
                )
                upsert_episode(con, series_id, 1, 1, status="present", title="Uno")
                con.execute(
                    "INSERT INTO files(path, series_id, kind, status, updated_at) VALUES(?,?,?,?,?)",
                    (str(cfg.library_root / "InuYasha" / "S01E01.mp4"), series_id, "video", "present", "2026-01-01T00:00:00+00:00"),
                )
            items = [
                {"id": 1, "title": "InuYasha", "english": "InuYasha", "romaji": "", "native": "", "year": 2000},
                {"id": 2, "title": "A Tale of the Secret Saint", "english": "A Tale of the Secret Saint", "romaji": "", "native": "", "year": 2026},
                {"id": 3, "title": "No descargada", "english": "Not Downloaded", "romaji": "", "native": "", "year": 2026},
            ]
            summary = annotate_season_downloads(cfg, items)
            self.assertTrue(items[0]["in_library"])
            self.assertEqual(items[0]["library_series_id"], series_id)
            self.assertIn("En biblioteca", items[0]["download_label"])
            self.assertEqual(items[1]["staging_episodes"], ["S01E01"])
            self.assertEqual(items[1]["download_state"], "staging")
            self.assertTrue(items[1]["downloaded"])
            self.assertFalse(items[2]["downloaded"])
            self.assertEqual(summary["in_library"], 1)
            self.assertEqual(summary["staged"], 1)
            self.assertEqual(summary["downloaded"], 2)


if __name__ == "__main__":
    unittest.main()
