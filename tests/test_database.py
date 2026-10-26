"""Pruebas de base de datos, huellas y utilidades de seguridad."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from series_manager.db import get_or_create_series, init_db, session, upsert_episode
from series_manager.utils import is_subpath, quick_fingerprint, sanitize_component


class DatabaseTests(unittest.TestCase):
    def test_schema_and_episode_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "catalog.db"
            init_db(db)
            with session(db) as con:
                series_id = get_or_create_series(
                    con, "demo", {"title": "Demo", "year": 2024}
                )
                episode_id = upsert_episode(
                    con, series_id, 1, 3, title="Tres", status="present"
                )
                again = upsert_episode(
                    con, series_id, 1, 3, title="Tres bis", status="present"
                )
                self.assertEqual(episode_id, again)
                row = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
                self.assertEqual(row["title"], "Tres bis")
                count = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
                self.assertEqual(count, 1)

    def test_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "catalog.db"
            init_db(db)
            with self.assertRaises(Exception):
                with session(db) as con:
                    con.execute(
                        "INSERT INTO seasons(series_id, number) VALUES(9999, 1)"
                    )


class UtilityTests(unittest.TestCase):
    def test_fingerprint_equal_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "a.bin"
            second = root / "b.bin"
            data = b"x" * (2 * 1024 * 1024)
            first.write_bytes(data)
            second.write_bytes(data)
            self.assertEqual(quick_fingerprint(first), quick_fingerprint(second))

    def test_fingerprint_different_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "a.bin"
            second = root / "b.bin"
            first.write_bytes(b"a" * 1024)
            second.write_bytes(b"b" * 1024)
            self.assertNotEqual(quick_fingerprint(first), quick_fingerprint(second))

    def test_subpath(self) -> None:
        self.assertTrue(is_subpath(r"D:\Series\Serie\archivo.mkv", r"D:\Series"))
        self.assertFalse(is_subpath(r"D:\Otra\archivo.mkv", r"D:\Series"))

    def test_sanitize_windows(self) -> None:
        self.assertEqual(sanitize_component('A: B/C\\D*E?'), "A - B C DE")
        self.assertEqual(sanitize_component("CON"), "_CON")


if __name__ == "__main__":
    unittest.main()
