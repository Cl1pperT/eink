from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from display_runtime.ee02 import EE02_PAYLOAD_BYTES, EE02_WIRE_FORMAT
from display_runtime.frame_server import FrameServer


TOKEN = "test-token-with-enough-entropy"


def write_committed_frame(
    output_directory: Path,
    *,
    mode: str = "test-pattern",
    byte: int = 0x00,
) -> tuple[bytes, str, Path]:
    payload = bytes((byte,)) * EE02_PAYLOAD_BYTES
    checksum = hashlib.sha256(payload).hexdigest()
    mode_directory = output_directory / mode
    frame_directory = mode_directory / "frames"
    frame_directory.mkdir(parents=True, exist_ok=True)
    frame_path = frame_directory / f"{checksum}.ee02"
    frame_path.write_bytes(payload)
    manifest = {
        "schema_version": 2,
        "format": "eink-frame-artifacts-v2",
        "mode": mode,
        "source": {"name": "test fixture", "provenance": "synthetic"},
        "rendered_for": "2026-07-12T00:00:00+00:00",
        "generated_at": "2026-07-12T00:00:01+00:00",
        "orientation": "landscape",
        "dimensions": {"width": 1600, "height": 1200},
        "palette": {"name": "spectra6-monitor-rgb-v1", "colors": []},
        "pixel_checksum": {"algorithm": "sha256-dimensions-rgb-v1", "value": "0" * 64},
        "wire": {
            "format": EE02_WIRE_FORMAT,
            "bits_per_pixel": 4,
            "buffer_dimensions": {"width": 1200, "height": 1600},
            "logical_dimensions": {"width": 1600, "height": 1200},
            "rotation": "clockwise",
            "seeed_sprite_rotation": 1,
            "pixel_order": "row-major",
            "nibble_order": "even-x-high-odd-x-low",
            "color_codes": {
                "black": "0xF",
                "white": "0x0",
                "yellow": "0xB",
                "red": "0x6",
                "blue": "0xD",
                "green": "0x2",
            },
            "bytes": EE02_PAYLOAD_BYTES,
            "sha256": checksum,
        },
        "files": {
            "ee02_4bpp": {
                "path": f"frames/{checksum}.ee02",
                "bytes": EE02_PAYLOAD_BYTES,
                "sha256": checksum,
            }
        },
        "timings": {"source_seconds": 0.0, "conversion_seconds": 0.0},
    }
    (mode_directory / "current.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload, checksum, frame_path


@contextmanager
def running_server(output_directory: Path):
    try:
        server = FrameServer(
            ("127.0.0.1", 0),
            output_directory=output_directory,
            auth_token=TOKEN,
            chunk_size=4096,
            log_requests=False,
        )
    except PermissionError as exc:
        raise unittest.SkipTest(f"loopback sockets unavailable: {exc}") from exc
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def server_url(server: FrameServer) -> str:
    base_url = getattr(server, "base_url", None)
    if base_url:
        return str(base_url).rstrip("/")
    host, port = server.server_address
    return f"http://{host}:{port}"


def request(
    server: FrameServer,
    path: str,
    *,
    token: str | None = TOKEN,
    method: str = "GET",
    headers: dict[str, str] | None = None,
):
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    return urlopen(
        Request(server_url(server) + path, headers=request_headers, method=method),
        timeout=5,
    )


class FrameServerTests(unittest.TestCase):
    def test_bearer_authentication_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_committed_frame(output)
            with running_server(output) as server:
                with request(server, "/v1/health", token=None) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read()), {"ok": True})

                for token in (None, "incorrect-token"):
                    with self.subTest(token=token):
                        with self.assertRaises(HTTPError) as caught:
                            request(server, "/v1/manifest/test-pattern", token=token)
                        self.assertEqual(caught.exception.code, 401)
                        self.assertTrue(
                            caught.exception.headers.get("WWW-Authenticate", "").startswith("Bearer")
                        )
                        self.assertEqual(json.loads(caught.exception.read()), {"error": "unauthorized"})

                with request(server, "/v1/manifest/test-pattern") as response:
                    self.assertEqual(response.status, 200)

    def test_manifest_get_head_and_conditional_get(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            _payload, checksum, _path = write_committed_frame(output)
            with running_server(output) as server:
                with request(server, "/v1/manifest/test-pattern") as response:
                    body = response.read()
                    etag = response.headers["ETag"]
                    self.assertEqual(response.status, 200)
                    self.assertTrue(etag)
                    self.assertEqual(response.headers.get_content_type(), "application/json")
                    self.assertEqual(int(response.headers["Content-Length"]), len(body))
                    self.assertIn("no-cache", response.headers["Cache-Control"])
                    manifest = json.loads(body)
                    self.assertEqual(manifest["mode"], "test-pattern")
                    self.assertEqual(manifest["wire"]["sha256"], checksum)

                with request(server, "/v1/manifest/test-pattern", method="HEAD") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["ETag"], etag)
                    self.assertEqual(response.read(), b"")

                with self.assertRaises(HTTPError) as caught:
                    request(
                        server,
                        "/v1/manifest/test-pattern",
                        headers={"If-None-Match": etag},
                    )
                self.assertEqual(caught.exception.code, 304)
                self.assertEqual(caught.exception.headers["ETag"], etag)
                self.assertEqual(caught.exception.read(), b"")

    def test_current_and_content_addressed_frame_support_etags_and_head(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            payload, checksum, _path = write_committed_frame(output, byte=0x6F)
            with running_server(output) as server:
                paths = (
                    "/v1/frame/test-pattern",
                    f"/v1/frame/test-pattern/{checksum}",
                )
                for path in paths:
                    with self.subTest(path=path):
                        with request(server, path) as response:
                            etag = response.headers["ETag"]
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.read(), payload)
                            self.assertEqual(int(response.headers["Content-Length"]), len(payload))
                            self.assertEqual(response.headers.get("X-Content-SHA256"), checksum)
                            self.assertEqual(response.headers.get("X-Frame-Format"), EE02_WIRE_FORMAT)
                            self.assertIn("no-cache", response.headers["Cache-Control"])

                        with request(server, path, method="HEAD") as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.headers["ETag"], etag)
                            self.assertEqual(response.read(), b"")

                        with self.assertRaises(HTTPError) as caught:
                            request(server, path, headers={"If-None-Match": f'"other", {etag}'})
                        self.assertEqual(caught.exception.code, 304)
                        self.assertEqual(caught.exception.headers["ETag"], etag)

    def test_missing_and_invalid_artifacts_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with running_server(output) as server:
                with self.assertRaises(HTTPError) as caught:
                    request(server, "/v1/frame/weather")
                self.assertEqual(caught.exception.code, 404)

            payload, checksum, frame_path = write_committed_frame(output)
            self.assertEqual(len(payload), EE02_PAYLOAD_BYTES)
            frame_path.write_bytes(b"truncated")
            with running_server(output) as server:
                for path in (
                    "/v1/manifest/test-pattern",
                    "/v1/frame/test-pattern",
                    f"/v1/frame/test-pattern/{checksum}",
                ):
                    with self.subTest(path=path):
                        with self.assertRaises(HTTPError) as caught:
                            request(server, path)
                        self.assertEqual(caught.exception.code, 503)

    def test_malformed_manifest_and_invalid_paths_are_not_served(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            mode_directory = output / "weather"
            mode_directory.mkdir(parents=True)
            (mode_directory / "current.json").write_text("not json", encoding="utf-8")
            with running_server(output) as server:
                for path in (
                    "/v1/manifest/weather",
                    "/v1/frame/weather",
                    "/v1/frame/automatic",
                    "/v1/frame/../weather",
                    "/v1/frame/test-pattern/not-a-sha",
                ):
                    with self.subTest(path=path):
                        with self.assertRaises(HTTPError) as caught:
                            request(server, path)
                        self.assertIn(caught.exception.code, (400, 404, 503))


if __name__ == "__main__":
    unittest.main()
