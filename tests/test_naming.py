"""Pruebas del parser de nombres y de la construcción de rutas Jellyfin."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from series_manager.naming import (
    build_episode_filename,
    folder_name_for_series,
    parse_episode,
    parse_folder_info,
    parse_path_season,
    target_paths,
)
from series_manager.utils import sanitize_component


class ParserTests(unittest.TestCase):
    def test_sxxexx(self) -> None:
        parsed = parse_episode("Link Click S04E03")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.season, 4)
        self.assertEqual(parsed.episode, 3)
        self.assertTrue(parsed.explicit_season)

    def test_multi_episode_range(self) -> None:
        parsed = parse_episode("InuYasha 133-134")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.episode, 133)
        self.assertEqual(parsed.episode_end, 134)

    def test_resolution_is_not_episode_range(self) -> None:
        parsed = parse_episode("Smoking-Behind-the-Supermarket-with-You-S02E02-720p-.ja")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.season, 2)
        self.assertEqual(parsed.episode, 2)
        self.assertIsNone(parsed.episode_end)

    def test_resolution_suffix_is_not_decimal_episode(self) -> None:
        parsed = parse_episode("Lanterns.S01E05.720p.10bit.WEBRip.x265-PSA.mkv")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.season, 1)
        self.assertEqual(parsed.episode, 5)
        self.assertIsNone(parsed.episode_end)

    def test_decimal_special(self) -> None:
        parsed = parse_episode("Chobits 8.5")
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed.special)
        self.assertTrue(parsed.decimal_special)
        self.assertEqual(parsed.season, 0)
        self.assertEqual(parsed.episode, 8.5)

    def test_unnumbered_ova(self) -> None:
        parsed = parse_episode("Uma Musume Pretty Derby OVA")
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed.special)
        self.assertTrue(parsed.unnumbered_special)
        self.assertEqual(parsed.episode, 1)

    def test_bnw_hash_special(self) -> None:
        parsed = parse_episode("BNW_no_Chikai_1_19869758")
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed.special)
        self.assertEqual(parsed.episode, 1)
        self.assertEqual(parsed.matched_by, "hash-suffix")

    def test_smoking_season_override_is_parser_level(self) -> None:
        parsed = parse_episode("Smoking Behind the Supermarket with You S02E05")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.season, 2)
        self.assertEqual(parsed.episode, 5)
        # La corrección a Season 01 se hace en el escaneo con ignore_filename_season.

    def test_title_numbers_do_not_steal_episode(self) -> None:
        first = parse_episode("Ranma_1_2_2024_01")
        self.assertIsNotNone(first)
        self.assertEqual(first.season, 1)
        self.assertEqual(first.episode, 1)
        last = parse_episode("Ranma_1_2_2024_12")
        self.assertIsNotNone(last)
        self.assertEqual(last.episode, 12)
        explicit = parse_episode("Ranma_1_2_2024_S02E03")
        self.assertIsNotNone(explicit)
        self.assertEqual(explicit.season, 2)
        self.assertEqual(explicit.episode, 3)

    def test_no_episode(self) -> None:
        self.assertIsNone(parse_episode("Algún archivo sin número"))

    def test_folder_tmdb_tag_does_not_hide_year(self) -> None:
        info = parse_folder_info("InuYasha (2000) [tmdbid-35610]")
        self.assertEqual(info.title_guess, "InuYasha")
        self.assertEqual(info.year, 2000)

    def test_folder_season(self) -> None:
        info = parse_folder_info("The Legend of Vox Machina S02")
        self.assertEqual(info.season, 2)
        self.assertEqual(info.title_guess, "The Legend of Vox Machina")

    def test_folder_specials(self) -> None:
        info = parse_folder_info("Uma Musume Pretty Derby Specials")
        self.assertTrue(info.is_special)
        self.assertEqual(info.season, 0)

    def test_parent_season(self) -> None:
        season, special = parse_path_season(["Serie", "Season 03", "archivo.mkv"])
        self.assertEqual(season, 3)
        self.assertFalse(special)


class TargetNameTests(unittest.TestCase):
    def test_folder_with_tmdb(self) -> None:
        self.assertEqual(
            folder_name_for_series("InuYasha", 2000, "35610"),
            "InuYasha (2000) [tmdbid-35610]",
        )

    def test_slash_is_sanitized(self) -> None:
        self.assertEqual(sanitize_component("Fate/strange Fake"), "Fate strange Fake")

    def test_episode_filename(self) -> None:
        name = build_episode_filename(
            "InuYasha (2000)", 1, 1, None, "The Girl Who Overcame Time", ".mp4"
        )
        self.assertEqual(
            name,
            "InuYasha (2000) - S01E01 - The Girl Who Overcame Time.mp4",
        )

    def test_multi_episode_filename(self) -> None:
        name = build_episode_filename("InuYasha (2000)", 6, 5, 6, "", ".mkv", include_title=False)
        self.assertEqual(name, "InuYasha (2000) - S06E05-E06.mkv")

    def test_target_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            series_dir, season_dir, target = target_paths(
                Path(tmp), "Chobits", 2002, "37527", True, 0, 1, None, "Especial", ".mkv"
            )
            self.assertEqual(series_dir.name, "Chobits (2002) [tmdbid-37527]")
            self.assertEqual(season_dir.name, "Season 00")
            self.assertEqual(target.parent, season_dir)
            self.assertTrue(target.name.startswith("Chobits (2002) - S00E01"))


if __name__ == "__main__":
    unittest.main()
