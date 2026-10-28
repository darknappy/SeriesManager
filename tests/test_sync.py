"""Pruebas de sincronización de series sin proveedor externo."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from series_manager.config import Config
from series_manager.db import get_or_create_series, init_db, session
from series_manager.sync import sync_metadata


def make_config(root: Path) -> Config:
    library = root / "library"
    airing = root / "airing"
    library.mkdir(parents=True, exist_ok=True)
    airing.mkdir(parents=True, exist_ok=True)
    data = {
        "library_root": str(library),
        "airing_root": str(airing),
        "staging_roots": [],
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


class SyncWithoutProviderTests(unittest.TestCase):
    def test_file_seasons_are_not_replaced_by_synthetic_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            with session(cfg.db_path) as con:
                series_id = get_or_create_series(
                    con,
                    "ranma",
                    {
                        "title": "Ranma",
                        "match_status": "unmatched",
                        "total_episodes": 27,
                        "season_count": 1,
                    },
                )
                for season, episode in ((1, 1), (1, 2), (2, 1), (3, 1)):
                    path = cfg.library_root / "Ranma" / f"S{season:02d}E{episode:02d}.mp4"
                    con.execute(
                        "INSERT INTO files(path, rel_path, series_id, kind, status, season_number, "
                        "episode_number, parsed_season, parsed_episode, parsed_from, updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            str(path),
                            str(path.relative_to(cfg.library_root)),
                            series_id,
                            "video",
                            "present",
                            season,
                            episode,
                            season,
                            episode,
                            "SxxExx",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
            stats = sync_metadata(cfg)
            self.assertEqual(stats["unassigned"], 0)
            with session(cfg.db_path) as con:
                episodes = con.execute(
                    "SELECT season_number, episode_number, status FROM episodes "
                    "WHERE series_id=? ORDER BY season_number, episode_number",
                    (series_id,),
                ).fetchall()
                self.assertEqual(len(episodes), 4)
                self.assertTrue(all(row["status"] == "present" for row in episodes))
                self.assertEqual(
                    [(row["season_number"], row["episode_number"]) for row in episodes],
                    [(1, 1), (1, 2), (2, 1), (3, 1)],
                )
                series = con.execute(
                    "SELECT total_episodes, season_count FROM series WHERE id=?", (series_id,)
                ).fetchone()
                self.assertEqual(series["total_episodes"], 4)
                self.assertEqual(series["season_count"], 3)
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM episodes WHERE series_id=? AND status='missing'",
                        (series_id,),
                    ).fetchone()[0],
                    0,
                )

    def test_file_created_season_zero_episode_is_special(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            with session(cfg.db_path) as con:
                series_id = get_or_create_series(
                    con,
                    "a-certain-magical-index",
                    {
                        "title": "A Certain Magical Index",
                        "year": 2008,
                        "match_status": "unmatched",
                    },
                )
                path = (
                    cfg.library_root
                    / "A Certain Magical Index (2008) [tmdbid-30980]"
                    / "Season 00"
                    / "A Certain Magical Index (2008) - S00E02 - Toaru Majutsu no Index.mp4"
                )
                con.execute(
                    "INSERT INTO files(path, rel_path, series_id, kind, status, season_number, "
                    "episode_number, parsed_season, parsed_episode, parsed_special, parsed_from, "
                    "title_hint, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(path),
                        str(path.relative_to(cfg.library_root)),
                        series_id,
                        "video",
                        "present",
                        0,
                        2,
                        0,
                        2,
                        1,
                        "SxxExx",
                        "Toaru Majutsu no Index",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
            stats = sync_metadata(cfg)
            self.assertEqual(stats["unassigned"], 0)
            with session(cfg.db_path) as con:
                episode = con.execute(
                    "SELECT season_number, episode_number, is_special, source "
                    "FROM episodes WHERE series_id=? AND season_number=0 AND episode_number=2",
                    (series_id,),
                ).fetchone()
                self.assertIsNotNone(episode)
                self.assertEqual(episode["is_special"], 1)
                self.assertEqual(episode["source"], "file")


if __name__ == "__main__":
    unittest.main()
