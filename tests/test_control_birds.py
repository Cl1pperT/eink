from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from display_control.birds import BirdWeatherCache, BirdWeatherQuery


class BirdWeatherCacheTests(unittest.TestCase):
    def test_refresh_normalizes_species_and_persists_a_fresh_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            def fetcher(repository, query, timeout):
                calls.append((repository, query, timeout))
                return [
                    {"sci": "Poecile gambeli", "com": "Mountain Chickadee", "n": 17},
                    {"sci": "Corvus corax", "com": "Common Raven", "n": 3},
                    {"sci": "Corvus corax", "com": "Duplicate", "n": 1},
                    {"sci": "", "com": "Invalid", "n": 9},
                ]

            clock = lambda: 1_000.0
            cache = BirdWeatherCache(
                root,
                root / "state" / "birds.json",
                fetcher=fetcher,
                clock=clock,
            )
            query = BirdWeatherQuery("84601", "us", 7)
            summary = cache.refresh(query)
            self.assertEqual(summary["freshness"], "fresh")
            self.assertEqual(summary["source_label"], "Nearby BirdWeather reports")
            self.assertEqual(
                [item["slug"] for item in summary["species"]],
                ["poecile-gambeli", "corvus-corax"],
            )
            self.assertEqual(summary["species"][0]["art_url"], "/bird-art/poecile-gambeli.png")
            self.assertTrue((root / "state" / "birds.json").is_file())
            self.assertEqual(calls[0][1], query)
            self.assertEqual(calls[0][2], 4)

    def test_upstream_failure_returns_same_query_last_good_as_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = [1_000.0]
            good = BirdWeatherCache(
                root,
                root / "birds.json",
                fetcher=lambda *_args: [
                    {"sci": "Spinus tristis", "com": "American Goldfinch", "n": 5}
                ],
                ttl_seconds=10,
                retry_seconds=1,
                clock=lambda: now[0],
            )
            query = BirdWeatherQuery("84601", "us", 7)
            good.refresh(query)
            now[0] += 20

            def unavailable(*_args):
                raise TimeoutError("offline")

            offline = BirdWeatherCache(
                root,
                root / "birds.json",
                fetcher=unavailable,
                ttl_seconds=10,
                retry_seconds=1,
                clock=lambda: now[0],
            )
            settings = {
                "birds": {
                    "postal_code": "84601",
                    "country": "us",
                    "lookback_days": 7,
                }
            }
            summary = offline.get(settings, background=False)
            self.assertEqual(summary["freshness"], "stale")
            self.assertEqual(summary["species"][0]["common_name"], "American Goldfinch")
            self.assertIn("temporarily unavailable", summary["error"])

    def test_cache_for_an_old_location_is_not_shown_for_a_new_postal_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = BirdWeatherCache(
                root,
                root / "birds.json",
                fetcher=lambda *_args: [
                    {"sci": "Corvus corax", "com": "Common Raven", "n": 2}
                ],
                clock=lambda: 1_000,
            )
            cache.refresh(BirdWeatherQuery("84601", "us", 7))
            cache.fetcher = lambda *_args: (_ for _ in ()).throw(TimeoutError("offline"))
            summary = cache.get(
                {
                    "birds": {
                        "postal_code": "10001",
                        "country": "us",
                        "lookback_days": 7,
                    }
                },
                background=False,
            )
            self.assertEqual(summary["freshness"], "unavailable")
            self.assertEqual(summary["species"], [])


if __name__ == "__main__":
    unittest.main()
