from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from display_simulator.sources import sky_events
from display_simulator.sources.sky_events import (
    SkyEvent,
    SkyEventReport,
    _EventCache,
    _parse_tle_catalog,
    _satellite_catalog,
    _satellite_payload,
    aurora_events,
    collect_sky_events,
    conjunction_events,
    eclipse_events,
    featured_event,
    meteor_events,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EPHEMERIS_PATH = PROJECT_ROOT / "de421.bsp"
HAS_SKYFIELD = importlib.util.find_spec("skyfield") is not None


def observing_night(
    day: date = date(2026, 7, 29),
    *,
    timezone_name: str = "America/Denver",
) -> SimpleNamespace:
    zone = ZoneInfo(timezone_name)
    rendered_at = datetime.combine(day, datetime.min.time(), tzinfo=zone).replace(
        hour=18
    )
    return SimpleNamespace(
        rendered_at=rendered_at,
        observation_time=rendered_at.replace(hour=21),
        sunset=rendered_at.replace(hour=20, minute=15),
        sunrise=datetime.combine(
            day + timedelta(days=1),
            datetime.min.time(),
            tzinfo=zone,
        ).replace(hour=5, minute=45),
        night_date=day,
    )


def event(
    identifier: str,
    *,
    kind: str = "meteor",
    priority: int = 80,
    tonight: bool = False,
    peak: datetime | None = None,
) -> SkyEvent:
    return SkyEvent(
        id=identifier,
        kind=kind,
        title=identifier,
        timing="Test timing",
        detail="Test detail",
        priority=priority,
        confidence="high",
        source="Test source",
        is_tonight=tonight,
        peaks_at=peak,
    )


def aurora_payload(
    intensity: int,
    *,
    observation: str = "2026-07-30T02:00:00Z",
    forecast: str = "2026-07-30T02:30:00Z",
) -> bytes:
    return json.dumps(
        {
            "Observation Time": observation,
            "Forecast Time": forecast,
            "Data Format": "[Longitude, Latitude, Aurora]",
            "coordinates": [
                [255, 40, intensity],
                [20, -30, 99],
            ],
            "type": "Feature",
        }
    ).encode("utf-8")


def tle_triplet(name: str, catalog_number: int) -> str:
    return "\n".join(
        (
            name,
            (
                f"1 {catalog_number:05d}U 98067A   26210.50000000  "
                ".00010000  00000-0  18000-3 0  9991"
            ),
            (
                f"2 {catalog_number:05d}  51.6400 120.0000 0005000 "
                "120.0000 240.0000 15.50000000123456"
            ),
        )
    )


class SkyEventManifestTests(unittest.TestCase):
    def test_manifest_and_digest_ignore_plotting_metadata_and_stay_stable(self):
        start = datetime(2026, 7, 29, 20, 15, tzinfo=timezone.utc)
        peak = datetime(2026, 7, 30, 2, 30, tzinfo=timezone.utc)
        end = datetime(2026, 7, 30, 3, 0, tzinfo=timezone.utc)
        first = SkyEvent(
            id="aurora:20260730T0230",
            kind="aurora",
            title="Aurora may be visible",
            timing="NOAA forecast near 8:30 PM",
            detail="Modeled local viewing estimate 24%",
            priority=91,
            confidence="medium",
            source="NOAA SWPC OVATION",
            is_tonight=True,
            starts_at=start,
            peaks_at=peak,
            ends_at=end,
            direction="N",
            altitude_degrees=42.126,
            azimuth_degrees=0.004,
            separation_degrees=float("nan"),
            marker_radec=(10.0, 20.0),
            track_radec=((10.0, 20.0), (11.0, 21.0)),
        )
        second = SkyEvent(
            **{
                **{
                    key: getattr(first, key)
                    for key in (
                        "id",
                        "kind",
                        "title",
                        "timing",
                        "detail",
                        "priority",
                        "confidence",
                        "source",
                        "is_tonight",
                        "starts_at",
                        "peaks_at",
                        "ends_at",
                        "direction",
                        "altitude_degrees",
                        "azimuth_degrees",
                        "separation_degrees",
                    )
                },
                "marker_radec": (200.0, -40.0),
                "secondary_marker_radec": (201.0, -41.0),
                "track_radec": ((200.0, -40.0),),
            }
        )
        expected_manifest = {
            "id": "aurora:20260730T0230",
            "kind": "aurora",
            "title": "Aurora may be visible",
            "timing": "NOAA forecast near 8:30 PM",
            "detail": "Modeled local viewing estimate 24%",
            "priority": 91,
            "confidence": "medium",
            "source": "NOAA SWPC OVATION",
            "is_tonight": True,
            "starts_at": "2026-07-29T20:15:00+00:00",
            "peaks_at": "2026-07-30T02:30:00+00:00",
            "ends_at": "2026-07-30T03:00:00+00:00",
            "direction": "N",
            "altitude_degrees": 42.13,
            "azimuth_degrees": 0.0,
        }

        first_report = SkyEventReport(start, (first,))
        second_report = SkyEventReport(end, (second,))

        self.assertEqual(first.as_manifest(), expected_manifest)
        self.assertEqual(second.as_manifest(), expected_manifest)
        self.assertEqual(first_report.manifest_events, second_report.manifest_events)
        self.assertEqual(first_report.digest, second_report.digest)
        self.assertEqual(
            first_report.digest,
            "bee84724939d7c46a8fbea823d906e1b29a63a3000ab46c556323100094d069b",
        )

    def test_manifest_rejects_naive_timestamps(self):
        value = event(
            "meteor:naive",
            peak=datetime(2026, 7, 30, 2, 0),
        )

        with self.assertRaisesRegex(ValueError, "timezone"):
            value.as_manifest()


class MeteorEventTests(unittest.TestCase):
    def test_current_meteor_alerts_label_zhr_as_ideal_not_personal_rate(self):
        night = observing_night()
        moon = SimpleNamespace(illumination=0.85, altitude=30)

        events = meteor_events(
            night,
            moon,
            lambda _ra, _dec: (45.0, 135.0),
        )

        self.assertEqual(
            [value.title for value in events],
            ["Southern Delta Aquariids", "Alpha Capricornids"],
        )
        by_title = {value.title: value for value in events}
        self.assertTrue(by_title["Southern Delta Aquariids"].is_tonight)
        self.assertEqual(
            by_title["Southern Delta Aquariids"].timing,
            "Peaks overnight",
        )
        self.assertFalse(by_title["Alpha Capricornids"].is_tonight)
        self.assertIn("peaks in 2 nights", by_title["Alpha Capricornids"].timing)
        for value in events:
            with self.subTest(shower=value.title):
                self.assertIn("Ideal ZHR", value.detail)
                self.assertIn("your count will be lower", value.detail)
                self.assertIn("Moonlight may hide", value.detail)
                self.assertNotIn("expected hourly", value.detail.casefold())


class AuroraEventTests(unittest.TestCase):
    def test_threshold_labels_are_conservative(self):
        now = datetime(2026, 7, 30, 2, 5, tzinfo=timezone.utc)
        cases = (
            (9, None, None, None),
            (10, "Possible aurora glow", "low", 84),
            (20, "Aurora may be visible", "medium", 91),
            (40, "Strong aurora potential", "high", 98),
        )
        with tempfile.TemporaryDirectory() as directory:
            for intensity, title, confidence, priority in cases:
                with self.subTest(intensity=intensity):
                    cache = _EventCache(Path(directory) / f"{intensity}.json")
                    fetcher = Mock(return_value=aurora_payload(intensity))

                    events = aurora_events(
                        observing_night(),
                        39.7392,
                        -104.9903,
                        now,
                        cache,
                        offline=False,
                        fetcher=fetcher,
                    )

                    fetcher.assert_called_once_with(
                        sky_events._AURORA_URL,
                        sky_events._NETWORK_TIMEOUT_SECONDS,
                        sky_events._AURORA_MAX_BYTES,
                    )
                    if title is None:
                        self.assertEqual(events, [])
                    else:
                        self.assertEqual(len(events), 1)
                        self.assertEqual(events[0].title, title)
                        self.assertEqual(events[0].confidence, confidence)
                        self.assertEqual(events[0].priority, priority)
                        self.assertIn(
                            f"estimate {intensity}%",
                            events[0].detail,
                        )

    def test_forecast_freshness_is_independent_of_cache_freshness(self):
        now = datetime(2026, 7, 30, 3, 45, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            cache = _EventCache(Path(directory) / "aurora.json")
            fetcher = Mock(return_value=aurora_payload(50))

            events = aurora_events(
                observing_night(),
                39.7392,
                -104.9903,
                now,
                cache,
                offline=False,
                fetcher=fetcher,
            )

        fetcher.assert_called_once()
        self.assertEqual(events, [])

    def test_cache_is_reused_only_for_the_same_location(self):
        now = datetime(2026, 7, 30, 2, 5, tzinfo=timezone.utc)
        calls: list[tuple[str, float, int]] = []

        def fetcher(url: str, timeout: float, maximum: int) -> bytes:
            calls.append((url, timeout, maximum))
            return aurora_payload(25)

        with tempfile.TemporaryDirectory() as directory:
            cache = _EventCache(Path(directory) / "aurora.json")
            first = aurora_events(
                observing_night(),
                39.7392,
                -104.9903,
                now,
                cache,
                offline=False,
                fetcher=fetcher,
            )
            cached = aurora_events(
                observing_night(),
                39.7392,
                -104.9903,
                now + timedelta(minutes=5),
                cache,
                offline=False,
                fetcher=fetcher,
            )
            moved = aurora_events(
                observing_night(),
                39.7700,
                -104.9903,
                now + timedelta(minutes=6),
                cache,
                offline=False,
                fetcher=fetcher,
            )

        self.assertEqual(len(first), 1)
        self.assertEqual(len(cached), 1)
        self.assertEqual(len(moved), 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(
            all(maximum == sky_events._AURORA_MAX_BYTES for _, _, maximum in calls)
        )

    def test_corrupt_cache_is_ignored_offline_without_fetching(self):
        now = datetime(2026, 7, 30, 2, 5, tzinfo=timezone.utc)
        fetcher = Mock(side_effect=AssertionError("offline fetch attempted"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sky-events.json"
            path.write_text("{not-json", encoding="utf-8")
            cache = _EventCache(path)

            aurora = aurora_events(
                observing_night(),
                39.7392,
                -104.9903,
                now,
                cache,
                offline=True,
                fetcher=fetcher,
            )
            satellites = _satellite_catalog(
                now,
                cache,
                offline=True,
                fetcher=fetcher,
            )

        self.assertEqual(aurora, [])
        self.assertIsNone(satellites)
        fetcher.assert_not_called()


class SatelliteCatalogTests(unittest.TestCase):
    def test_tle_parser_bounds_names_and_starlink_filter(self):
        maximum_name = "V" * 80
        overlong_name = "X" * 81
        catalog = "\n".join(
            (
                tle_triplet("ISS (ZARYA)", 25544),
                tle_triplet("STARLINK-1234", 48001),
                tle_triplet(maximum_name, 48002),
                tle_triplet(overlong_name, 48003),
                "\n".join(
                    (
                        "BROKEN",
                        "not a line one",
                        "2 48004  53.0000 120.0000 0001000 0 0 15.0",
                    )
                ),
            )
        )

        parsed = _parse_tle_catalog(catalog)
        starlink = _parse_tle_catalog(catalog, starlink_only=True)

        self.assertEqual(
            [name for name, _first, _second in parsed],
            ["ISS (ZARYA)", "STARLINK-1234", maximum_name],
        )
        self.assertEqual(
            [name for name, _first, _second in starlink],
            ["STARLINK-1234"],
        )

    def test_satellite_payload_requires_starlink_in_recent_catalog(self):
        visual = tle_triplet("ISS (ZARYA)", 25544).encode("ascii")
        recent = tle_triplet("RECENT ROCKET BODY", 48001).encode("ascii")

        with self.assertRaisesRegex(ValueError, "Starlink"):
            _satellite_payload(visual, recent)

    def test_catalog_fetch_uses_bounded_requests_and_then_cache(self):
        now = datetime(2026, 7, 30, 2, 5, tzinfo=timezone.utc)
        calls: list[tuple[str, float, int]] = []

        def fetcher(url: str, timeout: float, maximum: int) -> bytes:
            calls.append((url, timeout, maximum))
            if url == sky_events._VISUAL_TLE_URL:
                return tle_triplet("ISS (ZARYA)", 25544).encode("ascii")
            return tle_triplet("STARLINK-1234", 48001).encode("ascii")

        with tempfile.TemporaryDirectory() as directory:
            cache = _EventCache(Path(directory) / "satellites.json")
            first = _satellite_catalog(
                now,
                cache,
                offline=False,
                fetcher=fetcher,
            )
            second = _satellite_catalog(
                now + timedelta(hours=1),
                cache,
                offline=False,
                fetcher=fetcher,
            )

        self.assertEqual(first, second)
        self.assertEqual(
            [url for url, _timeout, _maximum in calls],
            [sky_events._VISUAL_TLE_URL, sky_events._RECENT_TLE_URL],
        )
        self.assertTrue(
            all(maximum == sky_events._TLE_MAX_BYTES for _, _, maximum in calls)
        )


class SkyEventSelectionTests(unittest.TestCase):
    def test_collection_deduplicates_bounds_and_ranks_tonight_first(self):
        night = observing_night()
        now = night.rendered_at
        meteor = event(
            "meteor:tonight",
            priority=90,
            tonight=True,
            peak=now + timedelta(hours=8),
        )
        satellite = event(
            "satellite:tonight",
            kind="satellite",
            priority=72,
            tonight=True,
            peak=now + timedelta(hours=5),
        )
        eclipse = event(
            "eclipse:tomorrow",
            kind="eclipse",
            priority=100,
            peak=now + timedelta(hours=24),
        )
        filler = [
            event(
                f"conjunction:{index}",
                kind="conjunction",
                priority=80 - index,
                peak=now + timedelta(days=index + 2),
            )
            for index in range(8)
        ]

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                sky_events,
                "meteor_events",
                return_value=[meteor, meteor],
            ),
            patch.object(
                sky_events,
                "conjunction_events",
                return_value=filler,
            ),
            patch.object(
                sky_events,
                "eclipse_events",
                return_value=[eclipse],
            ),
            patch.object(sky_events, "aurora_events", return_value=[]),
            patch.object(
                sky_events,
                "satellite_events",
                return_value=[satellite],
            ),
        ):
            report = collect_sky_events(
                night,
                latitude=39.7392,
                longitude=-104.9903,
                ephemeris=None,
                observer_vector=None,
                timescale=None,
                moon=SimpleNamespace(illumination=0.0, altitude=-90),
                coordinate_converter=lambda _ra, _dec: (0.0, 0.0),
                cache_path=Path(directory) / "cache.json",
                offline=True,
            )

        self.assertEqual(len(report.events), 8)
        self.assertEqual(
            [value.id for value in report.events[:3]],
            ["meteor:tonight", "satellite:tonight", "eclipse:tomorrow"],
        )
        self.assertEqual(
            len({value.id for value in report.events}),
            len(report.events),
        )
        self.assertIs(featured_event(report.events, night), meteor)

    def test_featured_event_requires_a_soon_high_priority_future_alert(self):
        night = observing_night()
        soon = event(
            "eclipse:soon",
            kind="eclipse",
            priority=94,
            peak=night.rendered_at + timedelta(hours=47),
        )
        low = event(
            "meteor:low",
            priority=89,
            peak=night.rendered_at + timedelta(hours=2),
        )
        late = event(
            "eclipse:late",
            kind="eclipse",
            priority=100,
            peak=night.rendered_at + timedelta(hours=49),
        )

        self.assertIs(featured_event((low, soon, late), night), soon)
        self.assertIsNone(featured_event((low, late), night))


@unittest.skipUnless(
    HAS_SKYFIELD and EPHEMERIS_PATH.is_file(),
    "Skyfield and the checked-in de421 ephemeris are required",
)
class LocalAstronomyEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from skyfield.api import load, load_file, wgs84

        cls.timescale = load.timescale(builtin=True)
        cls.ephemeris = load_file(str(EPHEMERIS_PATH))
        cls.wgs84 = wgs84

    @classmethod
    def tearDownClass(cls):
        cls.ephemeris.close()

    def test_known_2026_venus_jupiter_conjunction_is_locally_visible(self):
        zone = ZoneInfo("America/Denver")
        day = date(2026, 6, 4)
        rendered_at = datetime(2026, 6, 4, 18, tzinfo=zone)
        night = SimpleNamespace(
            rendered_at=rendered_at,
            observation_time=rendered_at.replace(hour=21),
            sunset=rendered_at.replace(hour=20, minute=25),
            sunrise=datetime(2026, 6, 5, 5, 30, tzinfo=zone),
            night_date=day,
        )
        observer = self.ephemeris["earth"] + self.wgs84.latlon(
            39.7392,
            -104.9903,
        )

        events = conjunction_events(
            night,
            self.ephemeris,
            observer,
            self.timescale,
        )

        conjunction = next(
            value for value in events if value.title == "Venus & Jupiter"
        )
        self.assertEqual(conjunction.peaks_at.date(), date(2026, 6, 9))
        self.assertGreater(conjunction.separation_degrees, 1.0)
        self.assertLess(conjunction.separation_degrees, 2.0)
        self.assertGreater(conjunction.altitude_degrees, 10.0)
        self.assertFalse(conjunction.is_tonight)

    def test_lunar_eclipse_is_emitted_only_where_moon_is_above_horizon(self):
        day = date(2026, 3, 2)

        def events_at(
            latitude: float,
            longitude: float,
            timezone_name: str,
        ) -> list[SkyEvent]:
            zone = ZoneInfo(timezone_name)
            rendered_at = datetime(2026, 3, 2, 18, tzinfo=zone)
            night = SimpleNamespace(
                rendered_at=rendered_at,
                observation_time=rendered_at.replace(hour=20),
                sunset=rendered_at.replace(hour=17, minute=45),
                sunrise=datetime(2026, 3, 3, 6, 30, tzinfo=zone),
                night_date=day,
            )
            observer = self.ephemeris["earth"] + self.wgs84.latlon(
                latitude,
                longitude,
            )
            # Keep this test focused on the lunar search; local solar-eclipse
            # geometry has separate, much denser five-minute sampling.
            with patch(
                "skyfield.almanac.find_discrete",
                return_value=((), ()),
            ):
                return eclipse_events(
                    night,
                    self.ephemeris,
                    observer,
                    self.timescale,
                )

        denver = events_at(39.7392, -104.9903, "America/Denver")
        london = events_at(51.5074, -0.1278, "Europe/London")

        self.assertEqual([value.title for value in denver], ["Total lunar eclipse"])
        self.assertGreater(denver[0].altitude_degrees, 0)
        self.assertEqual(denver[0].peaks_at.date(), date(2026, 3, 3))
        self.assertEqual(london, [])


if __name__ == "__main__":
    unittest.main()
