"""Pruebas de aplicación y reversión de planes en un directorio temporal."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from series_manager.apply import apply_plan, undo_plan
from series_manager.config import Config
from series_manager.db import init_db, session


class ApplyTests(unittest.TestCase):
    def test_apply_and_undo_moves_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "library"
            airing = root / "airing"
            quarantine = root / "quarantine"
            old_dir = library / "Serie vieja"
            old_dir.mkdir(parents=True)
            source = old_dir / "capitulo.mp4"
            source.write_bytes(b"contenido de prueba")
            duplicate = old_dir / "duplicado.mp4"
            duplicate.write_bytes(b"duplicado")
            target = library / "Serie (2020) [tmdbid-1]" / "Season 01" / "Serie (2020) - S01E01.mp4"
            qtarget = quarantine / "duplicado.mp4"

            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "library_root": str(library),
                        "airing_root": str(airing),
                        "quarantine_root": str(quarantine),
                        "inbox_root": str(root / "inbox"),
                        "reports_root": str(root / "reports"),
                        "cache_root": str(root / "cache"),
                        "logs_root": str(root / "logs"),
                        "db_path": str(root / "catalog.db"),
                    }
                ),
                encoding="utf-8",
            )
            cfg = Config(config_path)
            init_db(cfg.db_path)
            with session(cfg.db_path) as con:
                con.execute(
                    "INSERT INTO plans(created_at, status, mode, summary) VALUES(?, 'ready', 'full', '{}')",
                    ("2026-01-01T00:00:00+00:00",),
                )
                plan_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
                con.execute(
                    "INSERT INTO plan_actions(plan_id, seq, action, source, target, kind, size, status) "
                    "VALUES(?, 1, 'move', ?, ?, 'video', ?, 'pending')",
                    (plan_id, str(source), str(target), source.stat().st_size),
                )
                con.execute(
                    "INSERT INTO plan_actions(plan_id, seq, action, source, target, kind, size, status) "
                    "VALUES(?, 2, 'quarantine', ?, ?, 'video', ?, 'pending')",
                    (plan_id, str(duplicate), str(qtarget), duplicate.stat().st_size),
                )

            result = apply_plan(cfg, plan_id)
            self.assertEqual(result["errors"], 0)
            self.assertEqual(result["done"], 2)
            self.assertTrue(target.exists())
            self.assertTrue(qtarget.exists())
            self.assertFalse(source.exists())
            with session(cfg.db_path) as con:
                self.assertEqual(con.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()[0], "applied")
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM journal WHERE plan_id=? AND status='done'", (plan_id,)).fetchone()[0],
                    2,
                )

            result = undo_plan(cfg, plan_id)
            self.assertEqual(result["errors"], 0)
            self.assertEqual(result["done"], 2)
            self.assertTrue(source.exists())
            self.assertTrue(duplicate.exists())
            self.assertFalse(target.exists())
            with session(cfg.db_path) as con:
                self.assertEqual(con.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()[0], "undone")


if __name__ == "__main__":
    unittest.main()
