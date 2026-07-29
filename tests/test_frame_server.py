from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from datetime import datetime
from typing import Callable
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from display_runtime.ee02 import EE02_PAYLOAD_BYTES, EE02_WIRE_FORMAT
from display_runtime.frame_server import (
    FRAME_CONTENT_TYPE,
    FrameSelection,
    FrameServer,
    frame_etag,
)


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
def running_server(
    output_directory: Path,
    *,
    auth_token: str = TOKEN,
    active_mode_resolver: Callable[[], str] | None = None,
    active_state_resolver: Callable[[], FrameSelection] | None = None,
):
    try:
        server = FrameServer(
            ("127.0.0.1", 0),
            output_directory=output_directory,
            auth_token=auth_token,
            active_mode_resolver=active_mode_resolver,
            active_state_resolver=active_state_resolver,
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
    def test_dynamic_manifest_advertises_next_wake_without_mutating_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            payload, checksum, _ = write_committed_frame(
                output, mode="weather", byte=0x6F
            )
            committed_path = output / "weather" / "current.json"
            committed = committed_path.read_bytes()
            state = {
                "value": FrameSelection(
                    mode="weather",
                    evaluated_at=datetime.fromisoformat(
                        "2026-07-29T18:00:00+00:00"
                    ),
                    next_wake_at=datetime.fromisoformat(
                        "2026-07-30T03:13:34+00:00"
                    ),
                )
            }

            with running_server(
                output,
                active_state_resolver=lambda: state["value"],
            ) as server:
                with request(server, "/v1/manifest/active") as response:
                    manifest = json.loads(response.read())
                    first_etag = response.headers["ETag"]
                    self.assertEqual(manifest["mode"], "weather")
                    self.assertEqual(
                        manifest["next_wake_at"], "2026-07-30T03:13:34Z"
                    )
                    self.assertEqual(
                        response.headers["X-Next-Wake-Epoch"], "1785381214"
                    )
                    self.assertEqual(
                        response.headers["X-Server-Epoch"], "1785348000"
                    )

                with self.assertRaises(HTTPError) as caught:
                    request(
                        server,
                        "/v1/manifest/weather",
                        headers={"If-None-Match": first_etag},
                    )
                self.assertEqual(caught.exception.code, 304)
                self.assertEqual(
                    caught.exception.headers["X-Next-Wake-At"],
                    "2026-07-30T03:13:34Z",
                )

                with request(server, "/v1/frame/weather") as response:
                    self.assertEqual(response.read(), payload)
                    self.assertEqual(response.headers["X-Frame-SHA256"], checksum)
                    self.assertEqual(
                        response.headers["X-Next-Wake-At"],
                        "2026-07-30T03:13:34Z",
                    )

                state["value"] = FrameSelection(
                    mode="weather",
                    evaluated_at=datetime.fromisoformat(
                        "2026-07-30T03:13:34+00:00"
                    ),
                    next_wake_at=datetime.fromisoformat(
                        "2026-07-30T12:00:00+00:00"
                    ),
                )
                with request(server, "/v1/manifest/weather") as response:
                    self.assertNotEqual(response.headers["ETag"], first_etag)
                    self.assertEqual(
                        json.loads(response.read())["next_wake_at"],
                        "2026-07-30T12:00:00Z",
                    )

            self.assertEqual(committed_path.read_bytes(), committed)

    def test_active_channel_resolves_each_request_and_reports_concrete_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            weather, weather_sha, _ = write_committed_frame(
                output, mode="weather", byte=0x6F
            )
            birds, birds_sha, _ = write_committed_frame(
                output, mode="birds", byte=0xD2
            )
            selection = {"mode": "weather"}
            calls: list[str] = []

            def resolve() -> str:
                calls.append(selection["mode"])
                return selection["mode"]

            with running_server(output, active_mode_resolver=resolve) as server:
                with request(server, "/v1/frame/active") as response:
                    self.assertEqual(response.read(), weather)
                    self.assertEqual(response.headers["X-Frame-SHA256"], weather_sha)
                    self.assertEqual(response.headers["X-Resolved-Mode"], "weather")

                selection["mode"] = "birds"
                with request(server, "/v1/frame/active") as response:
                    self.assertEqual(response.read(), birds)
                    self.assertEqual(response.headers["X-Frame-SHA256"], birds_sha)
                    self.assertEqual(response.headers["X-Resolved-Mode"], "birds")

                with request(server, "/v1/manifest/active") as response:
                    manifest = json.loads(response.read())
                    self.assertEqual(manifest["mode"], "birds")
                    self.assertEqual(response.headers["X-Resolved-Mode"], "birds")

            self.assertEqual(calls, ["weather", "birds", "birds"])

    def test_active_channel_selection_and_artifact_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def broken_resolver() -> str:
                raise RuntimeError("bad control state")

            resolvers = (
                None,
                lambda: "automatic",
                lambda: "unknown",
                broken_resolver,
            )
            for resolver in resolvers:
                with self.subTest(resolver=resolver):
                    with running_server(
                        output, active_mode_resolver=resolver
                    ) as server:
                        with self.assertRaises(HTTPError) as caught:
                            request(server, "/v1/frame/active")
                        self.assertEqual(caught.exception.code, 503)
                        self.assertEqual(
                            json.loads(caught.exception.read()),
                            {"error": "artifact_unavailable"},
                        )

            with running_server(
                output, active_mode_resolver=lambda: "weather"
            ) as server:
                with self.assertRaises(HTTPError) as caught:
                    request(server, "/v1/frame/active")
                self.assertEqual(caught.exception.code, 404)

            write_committed_frame(output, mode="weather", byte=0x6F)
            (output / "weather" / "current.json").write_text(
                "not json", encoding="utf-8"
            )
            with running_server(
                output, active_mode_resolver=lambda: "weather"
            ) as server:
                with self.assertRaises(HTTPError) as caught:
                    request(server, "/v1/frame/active")
                self.assertEqual(caught.exception.code, 503)

    def test_empty_token_allows_read_only_lan_access(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_committed_frame(output)
            with running_server(output, auth_token="") as server:
                with request(
                    server, "/v1/frame/test-pattern", token=None
                ) as response:
                    self.assertEqual(response.status, 200)

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
                            self.assertEqual(response.headers.get_content_type(), FRAME_CONTENT_TYPE)
                            self.assertEqual(etag, frame_etag(checksum))
                            self.assertEqual(response.headers.get("X-Frame-SHA256"), checksum)
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
                        self.assertEqual(caught.exception.headers["X-Frame-SHA256"], checksum)
                        self.assertEqual(caught.exception.headers["X-Content-SHA256"], checksum)
                        self.assertEqual(caught.exception.headers["X-Frame-Format"], EE02_WIRE_FORMAT)

    def test_http_10_firmware_request_receives_complete_200_and_304_contract(self):
        import http.client

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            payload, checksum, _path = write_committed_frame(output, byte=0xD2)
            with running_server(output) as server:
                host, port = server.server_address

                def firmware_request(etag: str = ""):
                    connection = http.client.HTTPConnection(host, port, timeout=5)
                    connection._http_vsn = 10
                    connection._http_vsn_str = "HTTP/1.0"
                    headers = {
                        "Authorization": f"Bearer {TOKEN}",
                        "Accept": FRAME_CONTENT_TYPE,
                        "Accept-Encoding": "identity",
                    }
                    if etag:
                        headers["If-None-Match"] = etag
                    connection.request("GET", "/v1/frame/test-pattern", headers=headers)
                    response = connection.getresponse()
                    body = response.read()
                    response_headers = dict(response.getheaders())
                    connection.close()
                    return response.status, response_headers, body

                status, headers, body = firmware_request()
                etag = frame_etag(checksum)
                self.assertEqual(status, 200)
                self.assertEqual(body, payload)
                self.assertEqual(headers["ETag"], etag)
                self.assertEqual(headers["Content-Type"], FRAME_CONTENT_TYPE)
                self.assertEqual(int(headers["Content-Length"]), EE02_PAYLOAD_BYTES)
                self.assertEqual(headers["X-Frame-SHA256"], checksum)
                self.assertEqual(headers["X-Content-SHA256"], checksum)
                self.assertEqual(headers["X-Frame-Format"], EE02_WIRE_FORMAT)

                status, headers, body = firmware_request(etag)
                self.assertEqual(status, 304)
                self.assertEqual(body, b"")
                self.assertEqual(headers["ETag"], etag)
                self.assertEqual(headers["X-Frame-SHA256"], checksum)
                self.assertEqual(headers["X-Content-SHA256"], checksum)
                self.assertEqual(headers["X-Frame-Format"], EE02_WIRE_FORMAT)

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

    def test_payload_with_unsupported_color_nibbles_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            _payload, checksum, _path = write_committed_frame(output, byte=0x11)
            with running_server(output) as server:
                for path in (
                    "/v1/manifest/test-pattern",
                    "/v1/frame/test-pattern",
                    f"/v1/frame/test-pattern/{checksum}",
                ):
                    with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
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

    def test_symlinked_frame_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "served"
            _payload, _checksum, _frame_path = write_committed_frame(output)
            frames = output / "test-pattern" / "frames"
            outside = root / "outside-frames"
            frames.rename(outside)
            try:
                frames.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symbolic links unavailable: {exc}")
            with running_server(output) as server:
                for path in ("/v1/manifest/test-pattern", "/v1/frame/test-pattern"):
                    with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                        request(server, path)
                    self.assertEqual(caught.exception.code, 503)


if __name__ == "__main__":
    unittest.main()
