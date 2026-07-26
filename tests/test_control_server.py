from __future__ import annotations

import http.client
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from display_control.server import ControlServer
from display_control.settings import default_settings
from tests.test_control_settings import sample_catalog


class ControlServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog = sample_catalog(self.root)
        with patch("display_control.server.discover_catalog", return_value=self.catalog):
            try:
                self.server = ControlServer(
                    "127.0.0.1",
                    0,
                    settings_path=self.root / "settings.json",
                    photo_path=self.root / "latest.png",
                    access_token="phone-code",
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
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, _headers, body = self.request("GET", "/api/catalog")
        self.assertEqual(status, 200)
        catalog = json.loads(body)
        self.assertEqual(len(catalog["locations"]), 2)
        self.assertEqual(len(catalog["activities"]), 2)
        self.assertEqual(self.request("GET", "/../../etc/passwd")[0], 404)

    def test_mutations_require_token_and_invalid_state_is_not_saved(self):
        settings = default_settings(self.catalog)
        encoded = json.dumps(settings).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(encoded))}
        status, _response_headers, _body = self.request("PUT", "/api/settings", encoded, headers)
        self.assertEqual(status, 401)
        self.assertFalse(self.server.settings_path.exists())

        headers["X-EInk-Control-Token"] = "phone-code"
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

    def test_authenticated_photo_upload_is_normalized_and_visible(self):
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
            "X-EInk-Control-Token": "phone-code",
        }
        status, _response_headers, response_body = self.request(
            "POST", "/api/photo", body, headers
        )
        self.assertEqual(status, 201, response_body)
        with Image.open(self.server.photo_path) as saved:
            self.assertEqual(saved.mode, "RGB")
            self.assertEqual(saved.size, (17, 9))
        status, headers, payload = self.request("GET", "/api/photo")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(payload, self.server.photo_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
