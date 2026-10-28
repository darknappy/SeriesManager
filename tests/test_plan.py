"""Pruebas de planificación idempotente."""
from __future__ import annotations

import unittest
from pathlib import Path

from series_manager.plan import _key, _schedule_move


class PlanTests(unittest.TestCase):
    def test_kept_file_is_marked_as_scheduled(self) -> None:
        source = Path("D:/Series/Serie/season01-poster.jpg")
        actions: list[dict] = []
        scheduled: set[str] = set()
        summary = {"keep": 0, "move": 0, "quarantine": 0, "conflict": 0, "bytes": 0}
        warnings: list[str] = []

        _schedule_move(
            actions,
            {},
            scheduled,
            summary,
            warnings,
            source,
            source,
            {"id": 1},
            {"id": 1, "fingerprint": None, "size": 10},
            None,
            "image",
        )

        self.assertEqual(actions, [])
        self.assertEqual(summary["keep"], 1)
        self.assertIn(_key(source), scheduled)


if __name__ == "__main__":
    unittest.main()
