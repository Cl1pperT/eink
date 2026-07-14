from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from display_runtime.ee02 import EE02_PAYLOAD_BYTES, EE02_WIRE_FORMAT
from display_runtime.config import load_runtime_config
from display_runtime.esp_client import ESPClientError, SimulatedESPClient
from display_runtime.frame_server import FrameServer
from display_runtime.runtime import FrameRuntime


TOKEN = "test-token-with-enough-entropy"


def commit_frame(output: Path, mode: str, byte: int) -> tuple[bytes, str, Path]:
    payload = bytes((byte,)) * EE02_PAYLOAD_BYTES
    sha256 = hashlib.sha256(payload).hexdigest()
    mode_directory = output / mode
    frames = mode_directory / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    frame_path = frames / f"{sha256}.ee02"
    frame_path.write_bytes(payload)
    manifest = {
        "schema_version": 2,
        "format": "eink-frame-artifacts-v2",
        "mode": mode,
        "source": {"name": "test fixture", "provenance": "synthetic"},
        "rendered_for": "2026-07-12T00:00:00+00:00",
        "generated_at": f"2026-07-12T00:00:{byte % 60:02d}+00:00",
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
            "sha256": sha256,
        },
        "files": {
            "ee02_4bpp": {
                "path": f"frames/{sha256}.ee02",
                "bytes": EE02_PAYLOAD_BYTES,
                "sha256": sha256,
            }
        },
        "timings": {"source_seconds": 0.0, "conversion_seconds": 0.0},
    }
    (mode_directory / "current.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload, sha256, frame_path


@contextmanager
def serving(output: Path):
    try:
        server = FrameServer(
            ("127.0.0.1", 0),
            output_directory=output,
            auth_token=TOKEN,
            chunk_size=8192,
            log_requests=False,
        )
    except PermissionError as exc:
        raise unittest.SkipTest(f"loopback sockets unavailable: {exc}") from exc
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class SimulatedESPClientTests(unittest.TestCase):
    def test_runtime_generated_manifest_and_payload_sync_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "runtime.toml"
            config_path.write_text("", encoding="utf-8")
            output = root / "served"
            runtime = FrameRuntime(
                replace(
                    load_runtime_config(config_path),
                    output_directory=output,
                    write_rgb=False,
                )
            )
            artifact = runtime.render("test-pattern")

            with serving(output) as url:
                pulled = SimulatedESPClient(url, TOKEN, root / "esp").pull(
                    "test-pattern"
                )

            self.assertTrue(pulled.changed)
            self.assertEqual(pulled.sha256, artifact.wire_checksum)
            self.assertEqual(pulled.frame_path.read_bytes(), artifact.wire_path.read_bytes())

    def test_first_pull_installs_verified_frame_and_second_uses_etag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "served"
            state = root / "esp"
            payload, sha256, _server_path = commit_frame(output, "weather", 0x0B)
            with serving(output) as url:
                client = SimulatedESPClient(url, TOKEN, state, chunk_size=4093)
                first = client.pull("weather")
                state_before = client.state_path.read_bytes()
                second = client.pull("weather")

            self.assertTrue(first.changed)
            self.assertEqual(first.status, "downloaded")
            self.assertEqual(first.sha256, sha256)
            self.assertEqual(first.bytes, EE02_PAYLOAD_BYTES)
            self.assertEqual(first.frame_path.read_bytes(), payload)
            self.assertFalse(second.changed)
            self.assertEqual(second.status, "not-modified")
            self.assertEqual(second.sha256, sha256)
            # An unchanged pull may update display bookkeeping, but its committed
            # frame and per-mode validators must stay stable.
            state_after = json.loads(client.state_path.read_text(encoding="utf-8"))
            state_first = json.loads(state_before)
            self.assertEqual(state_after["modes"], state_first["modes"])
            self.assertEqual(client.state_path.read_bytes(), state_before)

    def test_untrusted_persisted_etags_never_create_a_wildcard_304(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "served"
            commit_frame(output, "weather", 0x0B)
            with serving(output) as url:
                client = SimulatedESPClient(url, TOKEN, root / "esp")
                client.pull("weather")
                for invalid_etag in ("*", 'W/"sha256-' + "0" * 64 + '"', "bad", 42):
                    with self.subTest(etag=invalid_etag):
                        state = json.loads(client.state_path.read_text(encoding="utf-8"))
                        state["modes"]["weather"]["manifest_etag"] = invalid_etag
                        client.state_path.write_text(json.dumps(state), encoding="utf-8")
                        result = client.pull("weather")
                        self.assertFalse(result.changed)
                        self.assertEqual(result.status, "verified-cache")

    def test_new_manifest_downloads_and_atomically_replaces_display(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "served"
            old_payload, old_sha, _ = commit_frame(output, "birds", 0x00)
            with serving(output) as url:
                client = SimulatedESPClient(url, TOKEN, root / "esp")
                first = client.pull("birds")
                self.assertEqual(first.frame_path.read_bytes(), old_payload)
                new_payload, new_sha, _ = commit_frame(output, "birds", 0x6F)
                second = client.pull("birds")

            self.assertTrue(second.changed)
            self.assertEqual(second.status, "downloaded")
            self.assertNotEqual(old_sha, new_sha)
            self.assertEqual(second.sha256, new_sha)
            self.assertEqual(second.frame_path.read_bytes(), new_payload)

    def test_corrupt_local_display_and_cache_are_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "served"
            payload, sha256, _ = commit_frame(output, "star-map", 0xDD)
            with serving(output) as url:
                client = SimulatedESPClient(url, TOKEN, root / "esp")
                first = client.pull("star-map")
                first.frame_path.write_bytes(b"bad display")
                cached = client.frames_directory / f"{sha256}.ee02"
                cached.write_bytes(b"bad cache")
                recovered = client.pull("star-map")

            self.assertTrue(recovered.changed)
            self.assertEqual(recovered.status, "downloaded")
            self.assertEqual(recovered.frame_path.read_bytes(), payload)
            self.assertEqual(cached.read_bytes(), payload)

    def test_rebuilt_cache_does_not_claim_a_physical_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "served"
            payload, sha256, _ = commit_frame(output, "weather", 0xDD)
            with serving(output) as url:
                client = SimulatedESPClient(url, TOKEN, root / "esp")
                client.pull("weather")
                (client.frames_directory / f"{sha256}.ee02").write_bytes(b"bad cache")
                rebuilt = client.pull("weather")

            self.assertFalse(rebuilt.changed)
            self.assertEqual(rebuilt.status, "downloaded")
            self.assertEqual(rebuilt.frame_path.read_bytes(), payload)

    def test_corrupt_server_update_preserves_last_known_good_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "served"
            old_payload, old_sha, _ = commit_frame(output, "test-pattern", 0x22)
            with serving(output) as url:
                client = SimulatedESPClient(url, TOKEN, root / "esp")
                first = client.pull("test-pattern")
                state_before = client.state_path.read_bytes()
                _new_payload, _new_sha, corrupt_path = commit_frame(
                    output, "test-pattern", 0xBB
                )
                corrupt_path.write_bytes(b"incomplete")

                with self.assertRaises(ESPClientError):
                    client.pull("test-pattern")

            self.assertEqual(first.sha256, old_sha)
            self.assertEqual(first.frame_path.read_bytes(), old_payload)
            self.assertEqual(client.state_path.read_bytes(), state_before)
            self.assertFalse(list(client.state_directory.rglob("*.tmp")))

    def test_bad_token_and_missing_mode_do_not_create_a_display_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "served"
            commit_frame(output, "weather", 0x00)
            with serving(output) as url:
                bad = SimulatedESPClient(url, "wrong-token", root / "bad-esp")
                with self.assertRaisesRegex(ESPClientError, "authentication"):
                    bad.pull("weather")
                self.assertFalse(bad.frame_path.exists())

                missing = SimulatedESPClient(url, TOKEN, root / "missing-esp")
                with self.assertRaisesRegex(ESPClientError, "no committed frame"):
                    missing.pull("birds")
                self.assertFalse(missing.frame_path.exists())

    def test_switching_modes_reactivates_a_verified_cached_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "served"
            weather, weather_sha, _ = commit_frame(output, "weather", 0x00)
            birds, _birds_sha, _ = commit_frame(output, "birds", 0x66)
            with serving(output) as url:
                client = SimulatedESPClient(url, TOKEN, root / "esp")
                client.pull("weather")
                client.pull("birds")
                self.assertEqual(client.frame_path.read_bytes(), birds)
                returned = client.pull("weather")

            self.assertTrue(returned.changed)
            self.assertEqual(returned.status, "cache-activated")
            self.assertEqual(returned.sha256, weather_sha)
            self.assertEqual(returned.frame_path.read_bytes(), weather)

    def test_sync_lock_serializes_clients_sharing_one_display_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "served"
            _payload, sha256, _ = commit_frame(output, "weather", 0x66)
            with serving(output) as url:
                owner = SimulatedESPClient(url, TOKEN, root / "esp")
                waiter = SimulatedESPClient(url, TOKEN, root / "esp")
                started = threading.Event()
                finished = threading.Event()
                errors: list[BaseException] = []

                def pull_in_thread() -> None:
                    started.set()
                    try:
                        waiter.pull("weather")
                    except BaseException as exc:  # record failures for the test thread
                        errors.append(exc)
                    finally:
                        finished.set()

                with owner._sync_lock():
                    thread = threading.Thread(target=pull_in_thread)
                    thread.start()
                    self.assertTrue(started.wait(1))
                    time.sleep(0.1)
                    self.assertFalse(finished.is_set())
                thread.join(timeout=10)

            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            state = json.loads(owner.state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["display"]["sha256"], sha256)
            self.assertEqual(hashlib.sha256(owner.frame_path.read_bytes()).hexdigest(), sha256)


if __name__ == "__main__":
    unittest.main()
