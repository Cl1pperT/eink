from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageCms

from display_control.demo import DemoOverrideStore
from display_control.server import AsyncRuntimeRenderer, ControlServer, _runtime_config_values
from display_control.settings import default_settings
from tests.test_control_settings import sample_catalog


class ControlServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog = sample_catalog(self.root)
        self.render_requests = []
        self.demo_now = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)
        self.demo_store = DemoOverrideStore(
            self.root / "settings.json",
            clock=lambda: self.demo_now,
        )
        self.bird_summary = {
            "provider": "birdweather",
            "source_label": "Nearby BirdWeather reports",
            "postal_code": "84601",
            "country": "us",
            "lookback_days": 7,
            "freshness": "fresh",
            "stale": False,
            "refreshing": False,
            "fetched_at": "2026-07-27T12:00:00+00:00",
            "age_seconds": 5,
            "species": [
                {
                    "scientific_name": "Poecile gambeli",
                    "common_name": "Mountain Chickadee",
                    "count": 12,
                    "slug": "poecile-gambeli",
                    "art_url": "/bird-art/poecile-gambeli.png",
                }
            ],
            "error": None,
            "disclaimer": "Regional reports, not local microphone detections.",
        }
        bird_cache = type(
            "StubBirdCache",
            (),
            {"get": lambda _self, _settings: dict(self.bird_summary)},
        )()
        with patch("display_control.server.discover_catalog", return_value=self.catalog):
            try:
                self.server = ControlServer(
                    "127.0.0.1",
                    0,
                    settings_path=self.root / "settings.json",
                    photo_path=self.root / "latest.png",
                    output_directory=self.root / "frames",
                    bird_cache=bird_cache,
                    demo_store=self.demo_store,
                    render_callback=lambda mode: self.render_requests.append(mode) or True,
                )
            except PermissionError:
                self.temporary.cleanup()
                self.skipTest("sandbox does not permit binding a loopback port")
        self.server.start()

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.stop()
        if hasattr(self, "temporary"):
            self.temporary.cleanup()

    def request(self, method: str, path: str, body: bytes | str | None = None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result

    def test_mobile_page_catalog_and_security_headers(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<meta name="viewport"', body)
        self.assertIn(b"Activities", body)
        self.assertIn(b"Nearby birds", body)
        self.assertIn(b'id="panel-stars"', body)
        self.assertIn(b'id="render-stars"', body)
        self.assertEqual(body.count(b"data-star-direction="), 4)
        self.assertIn(b'id="display-mode"', body)
        self.assertIn(b"Five-minute demo", body)
        self.assertEqual(body.count(b"data-demo-mode="), 4)
        self.assertIn(b"Press the physical button", body)
        self.assertIn(b"whole visible sky", body)
        self.assertIn(b"90 minutes after local sunset", body)
        self.assertIn(b"Date.now()<=sunriseTime", body)
        self.assertNotIn(b"X-EInk-Control-Token", body)
        self.assertNotIn(b"localStorage.setItem", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, _headers, body = self.request("GET", "/api/catalog")
        self.assertEqual(status, 200)
        catalog = json.loads(body)
        self.assertEqual(len(catalog["locations"]), 2)
        self.assertEqual(len(catalog["activities"]), 2)
        self.assertEqual(self.request("GET", "/../../etc/passwd")[0], 404)

    def test_bird_gallery_summary_local_art_and_safe_committed_preview(self):
        art = self.root / "avian" / "assets" / "illustrations" / "poecile-gambeli.png"
        art.parent.mkdir(parents=True)
        Image.new("RGBA", (12, 8), (20, 80, 50, 255)).save(art)
        mode = self.root / "frames" / "birds"
        frame_name = f"{'b' * 64}.rgb.png"
        preview = mode / "frames" / frame_name
        preview.parent.mkdir(parents=True)
        Image.new("RGB", (24, 18), "white").save(preview)
        preview_payload = preview.read_bytes()
        preview_digest = hashlib.sha256(preview_payload).hexdigest()
        (mode / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "format": "eink-frame-artifacts-v2",
                    "mode": "birds",
                    "generated_at": "2026-07-27T12:00:00+00:00",
                    "rendered_for": "2026-07-27T11:55:00+00:00",
                    "dimensions": {"width": 24, "height": 18},
                    "pixel_checksum": {
                        "algorithm": "sha256-dimensions-rgb-v1",
                        "value": "b" * 64,
                    },
                    "files": {
                        "rgb_png": {
                            "path": f"frames/{frame_name}",
                            "bytes": len(preview_payload),
                            "sha256": preview_digest,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        status, _headers, page = self.request("GET", "/birds")
        self.assertEqual(status, 200)
        self.assertIn(b"Nearby Birds", page)
        self.assertIn(b"not claim that a bird visited", page)
        status, _headers, body = self.request("GET", "/api/birds/summary")
        summary = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(summary["preview_available"])
        self.assertEqual(summary["source_label"], "Nearby BirdWeather reports")
        status, headers, body = self.request("GET", "/api/birds/preview")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["Cache-Control"], "private, no-cache")
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
        etag = headers["ETag"]
        status, _headers, body = self.request(
            "GET",
            "/api/birds/preview",
            headers={"If-None-Match": etag},
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        status, art_headers, _body = self.request(
            "GET", "/bird-art/poecile-gambeli.png"
        )
        self.assertEqual(status, 200)
        self.assertEqual(art_headers["Cache-Control"], "public, max-age=86400")
        self.assertEqual(self.request("GET", "/bird-art/..%2Fsecret.png")[0], 404)

        (mode / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "format": "eink-frame-artifacts-v2",
                    "mode": "birds",
                    "dimensions": {"width": 24, "height": 18},
                    "pixel_checksum": {
                        "algorithm": "sha256-dimensions-rgb-v1",
                        "value": "b" * 64,
                    },
                    "files": {
                        "rgb_png": {
                            "path": "../../etc/passwd",
                            "bytes": len(preview_payload),
                            "sha256": preview_digest,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.request("GET", "/api/birds/preview")[0], 404)

    def test_star_summary_and_preview_report_the_committed_cardinal_view(self):
        mode = self.root / "frames" / "star-map"
        pixel_identity = "c" * 64
        preview = mode / "frames" / f"{pixel_identity}.png"
        preview.parent.mkdir(parents=True)
        Image.new("RGB", (40, 30), (20, 35, 80)).save(preview)
        payload = preview.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        (mode / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "format": "eink-frame-artifacts-v2",
                    "mode": "star-map",
                    "generated_at": "2026-07-27T12:00:00+00:00",
                    "rendered_for": "2026-07-27T21:30:00-06:00",
                    "dimensions": {"width": 40, "height": 30},
                    "pixel_checksum": {
                        "algorithm": "sha256-dimensions-rgb-v1",
                        "value": pixel_identity,
                    },
                    "view": {
                        "direction_degrees": 90,
                        "direction_cardinal": "E",
                        "observation_time": "2026-07-27T22:18:00-06:00",
                        "sunrise_time": "2026-07-28T06:21:00-06:00",
                        "night_date": "2026-07-27",
                        "featured_constellation": "Cygnus",
                    },
                    "files": {
                        "eink_png": {
                            "path": f"frames/{pixel_identity}.png",
                            "bytes": len(payload),
                            "sha256": digest,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        status, _headers, body = self.request("GET", "/api/stars/summary")
        self.assertEqual(status, 200)
        summary = json.loads(body)
        self.assertTrue(summary["preview_available"])
        self.assertEqual(summary["preview_etag"], digest)
        self.assertEqual(summary["rendered_direction"], "E")
        self.assertEqual(summary["rendered_for"], "2026-07-27T21:30:00-06:00")
        self.assertEqual(summary["observation_time"], "2026-07-27T22:18:00-06:00")
        self.assertEqual(summary["sunrise_time"], "2026-07-28T06:21:00-06:00")
        self.assertEqual(summary["night_date"], "2026-07-27")
        self.assertEqual(summary["featured_constellation"], "Cygnus")

        status, headers, body = self.request("GET", "/api/stars/preview")
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertEqual(headers["ETag"], f'"{digest}"')
        self.assertEqual(headers["X-EInk-Generated-At"], "2026-07-27T12:00:00+00:00")
        status, _headers, body = self.request(
            "GET",
            "/api/stars/preview",
            headers={"If-None-Match": f'"{digest}"'},
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")

        manifest = json.loads((mode / "current.json").read_text(encoding="utf-8"))
        manifest["files"]["eink_png"]["sha256"] = "d" * 64
        (mode / "current.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.request("GET", "/api/stars/preview")[0], 404)

    def test_public_settings_mutation_validates_before_saving(self):
        settings = default_settings(self.catalog)
        encoded = json.dumps(settings).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(encoded))}
        status, _response_headers, body = self.request("PUT", "/api/settings", encoded, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), settings)
        before = self.server.settings_path.read_bytes()

        settings["enabled_locations"] = []
        encoded = json.dumps(settings).encode()
        headers["Content-Length"] = str(len(encoded))
        status, _response_headers, body = self.request("PUT", "/api/settings", encoded, headers)
        self.assertEqual(status, 422)
        self.assertIn("at least one", json.loads(body)["error"])
        self.assertEqual(self.server.settings_path.read_bytes(), before)

    def test_public_reset_requires_an_empty_json_object(self):
        settings = default_settings(self.catalog)
        settings["recommendation_count"] = 1
        self.server.httpd.store.save(settings)

        self.assertEqual(self.request("POST", "/api/settings/reset")[0], 415)

        invalid = b'{"unexpected":true}'
        invalid_headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(invalid)),
        }
        self.assertEqual(
            self.request("POST", "/api/settings/reset", invalid, invalid_headers)[0],
            422,
        )

        body = b"{}"
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        status, _response_headers, payload = self.request(
            "POST", "/api/settings/reset", body, headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), default_settings(self.catalog))

    def test_public_five_minute_demo_is_isolated_and_expires(self):
        initial_status, _headers, initial_body = self.request("GET", "/api/demo")
        self.assertEqual(initial_status, 200)
        self.assertFalse(json.loads(initial_body)["active"])

        settings = self.server.httpd.store.save(default_settings(self.catalog))
        settings_before = self.server.settings_path.read_bytes()
        body = json.dumps({"mode": "birds"}).encode()
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        for invalid in (
            {"mode": "automatic"},
            {"mode": "test-pattern"},
            {"mode": "birds", "duration_seconds": 3600},
        ):
            encoded = json.dumps(invalid).encode()
            invalid_headers = dict(headers, **{"Content-Length": str(len(encoded))})
            self.assertEqual(
                self.request("POST", "/api/demo", encoded, invalid_headers)[0],
                422,
            )
            self.assertFalse(self.demo_store.path.exists())

        status, _response_headers, payload = self.request(
            "POST", "/api/demo", body, headers
        )
        self.assertEqual(status, 200)
        active = json.loads(payload)
        self.assertTrue(active["active"])
        self.assertEqual(active["mode"], "birds")
        self.assertEqual(active["duration_seconds"], 300)
        self.assertEqual(active["remaining_seconds"], 300)
        self.assertEqual(self.server.settings_path.read_bytes(), settings_before)
        self.assertEqual(self.render_requests, [])

        demo_before = self.demo_store.path.read_bytes()
        encoded_settings = json.dumps(settings).encode()
        settings_headers = dict(
            headers,
            **{"Content-Length": str(len(encoded_settings))},
        )
        self.assertEqual(
            self.request(
                "PUT",
                "/api/settings",
                encoded_settings,
                settings_headers,
            )[0],
            200,
        )
        self.assertEqual(self.demo_store.path.read_bytes(), demo_before)

        self.demo_now += timedelta(seconds=299)
        active = json.loads(self.request("GET", "/api/demo")[2])
        self.assertTrue(active["active"])
        self.assertEqual(active["remaining_seconds"], 1)
        self.demo_now += timedelta(seconds=1)
        self.assertFalse(json.loads(self.request("GET", "/api/demo")[2])["active"])

        weather = json.dumps({"mode": "weather"}).encode()
        weather_headers = dict(headers, **{"Content-Length": str(len(weather))})
        self.assertEqual(
            self.request("POST", "/api/demo", weather, weather_headers)[0],
            200,
        )
        status, _response_headers, payload = self.request(
            "DELETE",
            "/api/demo",
        )
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(payload)["active"])
        self.assertFalse(self.demo_store.path.exists())

        image = json.dumps({"mode": "uploaded-photo"}).encode()
        image_headers = dict(headers, **{"Content-Length": str(len(image))})
        self.assertEqual(
            self.request("POST", "/api/demo", image, image_headers)[0],
            409,
        )

    def test_public_photo_upload_is_normalized_and_visible(self):
        source = io.BytesIO()
        Image.new("RGBA", (17, 9), (20, 40, 60, 100)).save(source, format="PNG")
        boundary = "control-panel-test-boundary"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="photo.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode() + source.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }
        status, _response_headers, response_body = self.request(
            "POST", "/api/photo", body, headers
        )
        self.assertEqual(status, 201, response_body)
        self.assertEqual(self.render_requests, ["uploaded-photo"])
        self.assertTrue(json.loads(response_body)["render_queued"])
        with Image.open(self.server.photo_path) as saved:
            self.assertEqual(saved.mode, "RGB")
            self.assertEqual(saved.size, (17, 9))
            self.assertEqual(saved.getpixel((0, 0)), (157, 163, 162))
            profile = ImageCms.ImageCmsProfile(io.BytesIO(saved.info["icc_profile"]))
            self.assertIn("sRGB", ImageCms.getProfileName(profile))
        status, headers, payload = self.request("GET", "/api/photo")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(payload, self.server.photo_path.read_bytes())

    def test_public_explicit_render_action_is_concrete(self):
        body = json.dumps({"mode": "birds"}).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        status, _headers, payload = self.request("POST", "/api/render", body, headers)
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(payload)["queued"])
        self.assertEqual(self.render_requests, ["birds"])

        invalid = json.dumps({"mode": "automatic"}).encode()
        headers["Content-Length"] = str(len(invalid))
        self.assertEqual(self.request("POST", "/api/render", invalid, headers)[0], 422)


class RuntimeConfigControlPathTests(unittest.TestCase):
    def test_runtime_config_exposes_weather_photo_and_output_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "runtime.toml"
            config.write_text(
                '[repositories]\navian_weather = "AvianVisitors"\n'
                '[sources]\nphoto = "uploads/latest.png"\n'
                '[output]\ndirectory = "frames"\n',
                encoding="utf-8",
            )
            weather, photo, output = _runtime_config_values(config)
            self.assertEqual(weather, (root / "AvianVisitors").resolve())
            self.assertEqual(photo, (root / "uploads" / "latest.png").resolve())
            self.assertEqual(output, (root / "frames").resolve())

    def test_async_runtime_renderer_uses_an_argument_vector_without_a_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "runtime.toml"
            config.write_text("", encoding="utf-8")
            finished = threading.Event()
            calls = []

            def fake_run(command, **kwargs):
                calls.append((command, kwargs))
                finished.set()
                return type("Completed", (), {"returncode": 0})()

            with patch("display_control.server.subprocess.run", side_effect=fake_run):
                lock = Path(directory) / ".render-scheduler.lock"
                renderer = AsyncRuntimeRenderer(
                    config,
                    command=["eink-display"],
                    lock_path=lock,
                )
                self.assertTrue(renderer.request("uploaded-photo"))
                self.assertTrue(finished.wait(1))

            command, kwargs = calls[0]
            self.assertEqual(
                command,
                [
                    "/usr/bin/flock",
                    "--wait",
                    "900",
                    str(lock.resolve()),
                    "eink-display",
                    "--config",
                    str(config.resolve()),
                    "render",
                    "uploaded-photo",
                    "--json",
                ],
            )
            self.assertNotIn("shell", kwargs)
            self.assertNotIn("stdout", kwargs)
            self.assertNotIn("stderr", kwargs)


if __name__ == "__main__":
    unittest.main()
