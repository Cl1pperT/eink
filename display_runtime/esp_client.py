from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, BinaryIO, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .ee02 import (
    EE02_BUFFER_HEIGHT,
    EE02_BUFFER_WIDTH,
    EE02_PAYLOAD_BYTES,
    EE02_WIRE_FORMAT,
)
from .runtime import SCHEMA_VERSION, canonical_mode


ESP_STATE_VERSION = 1
DEFAULT_CHUNK_SIZE = 64 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FRAME_CONTENT_TYPES = {
    "application/octet-stream",
    "application/vnd.seeed.ee02-4bpp",
}


class ESPClientError(RuntimeError):
    """A pull failed before a complete EE02 frame could be installed."""


class ESPProtocolError(ESPClientError):
    """The server returned a response that is unsafe for the display."""


@dataclass(frozen=True, slots=True)
class ESPPullResult:
    mode: str
    changed: bool
    status: str
    sha256: str
    bytes: int
    frame_path: Path
    manifest_etag: str
    frame_etag: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "changed": self.changed,
            "status": self.status,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "frame_path": str(self.frame_path),
            "manifest_etag": self.manifest_etag,
            "frame_etag": self.frame_etag,
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_frame(path: Path, sha256: str) -> bool:
    try:
        return path.stat().st_size == EE02_PAYLOAD_BYTES and _sha256_file(path) == sha256
    except OSError:
        return False


def _fsync_directory(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(value: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f".{path.name}.", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _header(response: Any, name: str) -> str:
    value = response.headers.get(name)
    return str(value).strip() if value is not None else ""


def _content_length(response: Any, *, label: str, expected: int) -> None:
    value = _header(response, "Content-Length")
    try:
        actual = int(value)
    except (TypeError, ValueError) as exc:
        raise ESPProtocolError(f"{label} response has no valid Content-Length") from exc
    if actual != expected:
        raise ESPProtocolError(f"{label} response length is {actual}; expected {expected}")


def _etag_matches_sha256(etag: str, sha256: str) -> bool:
    if not etag or etag.startswith("W/") or not (etag.startswith('"') and etag.endswith('"')):
        return False
    opaque = etag[1:-1]
    return opaque in (
        sha256,
        f"sha256:{sha256}",
        f"sha256-{sha256}",
        f"wire-{sha256}",
    )


def _validate_manifest(value: Any, mode: str) -> tuple[str, int]:
    if not isinstance(value, dict):
        raise ESPProtocolError("frame manifest is not a JSON object")
    if value.get("schema_version") != SCHEMA_VERSION or value.get("format") != "eink-frame-artifacts-v2":
        raise ESPProtocolError("frame manifest uses an unsupported schema")
    if value.get("mode") != mode:
        raise ESPProtocolError("frame manifest mode does not match the request")

    wire = value.get("wire")
    files = value.get("files")
    if not isinstance(wire, dict) or not isinstance(files, dict):
        raise ESPProtocolError("frame manifest is missing wire metadata")
    sha256 = wire.get("sha256")
    if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
        raise ESPProtocolError("frame manifest has an invalid wire checksum")
    if wire.get("format") != EE02_WIRE_FORMAT or wire.get("bits_per_pixel") != 4:
        raise ESPProtocolError("frame manifest has an unsupported wire format")
    if wire.get("bytes") != EE02_PAYLOAD_BYTES:
        raise ESPProtocolError("frame manifest has an invalid wire byte count")
    if wire.get("buffer_dimensions") != {
        "width": EE02_BUFFER_WIDTH,
        "height": EE02_BUFFER_HEIGHT,
    }:
        raise ESPProtocolError("frame manifest has invalid EE02 buffer dimensions")
    if wire.get("pixel_order") != "row-major" or wire.get("nibble_order") != "even-x-high-odd-x-low":
        raise ESPProtocolError("frame manifest has an unsupported byte layout")

    binary = files.get("ee02_4bpp")
    if not isinstance(binary, dict):
        raise ESPProtocolError("frame manifest has no EE02 artifact")
    if binary.get("sha256") != sha256 or binary.get("bytes") != EE02_PAYLOAD_BYTES:
        raise ESPProtocolError("EE02 artifact metadata disagrees with the wire metadata")
    if binary.get("path") != f"frames/{sha256}.ee02":
        raise ESPProtocolError("EE02 artifact is not the expected content-addressed file")
    return sha256, EE02_PAYLOAD_BYTES


class SimulatedESPClient:
    """Network client with the same verify-before-refresh behavior as the ESP32.

    Each verified payload is retained by digest. ``display.ee02`` represents the
    buffer most recently installed on the simulated display. Neither it nor the
    persistent ETags are changed until an entire response passes validation.
    """

    def __init__(
        self,
        server_url: str,
        token: str,
        state_directory: str | Path,
        *,
        timeout: float = 30.0,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        parsed = urlsplit(server_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("server_url must be an http(s) URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("server_url must not contain a query string or fragment")
        if not isinstance(token, str) or not token:
            raise ValueError("an authentication token is required")
        if timeout <= 0 or chunk_size <= 0:
            raise ValueError("timeout and chunk_size must be greater than zero")
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.state_directory = Path(state_directory).expanduser().resolve(strict=False)
        self.timeout = float(timeout)
        self.chunk_size = int(chunk_size)
        self.state_path = self.state_directory / "state.json"
        self.frames_directory = self.state_directory / "frames"
        self.frame_path = self.state_directory / "display.ee02"

    def _default_state(self) -> dict[str, Any]:
        return {"schema_version": ESP_STATE_VERSION, "modes": {}, "display": None}

    def _load_state(self) -> dict[str, Any]:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._default_state()
        except OSError as exc:
            raise ESPClientError(f"could not read ESP state: {exc}") from exc
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ESPClientError("ESP state file is corrupt; last-known-good frame was retained") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != ESP_STATE_VERSION
            or not isinstance(value.get("modes"), dict)
        ):
            raise ESPClientError("ESP state file has an unsupported schema; last-known-good frame was retained")
        return value

    def _request(self, path: str, *, etag: str = "") -> tuple[Any | None, bool]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept-Encoding": "identity",
            "User-Agent": "eink-display-esp-simulator/1",
        }
        if etag:
            headers["If-None-Match"] = etag
        request = Request(f"{self.server_url}{path}", headers=headers, method="GET")
        try:
            return urlopen(request, timeout=self.timeout), False
        except HTTPError as exc:
            if exc.code == 304:
                exc.close()
                return None, True
            exc.close()
            if exc.code == 401:
                raise ESPClientError("frame server rejected the authentication token") from exc
            if exc.code == 404:
                raise ESPClientError("the requested mode has no committed frame") from exc
            if exc.code == 503:
                raise ESPClientError("the frame server has no valid committed artifact") from exc
            raise ESPClientError(f"frame server returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ESPClientError(f"could not reach frame server: {exc}") from exc

    def _read_manifest(self, response: Any) -> tuple[dict[str, Any], str]:
        content_type = _header(response, "Content-Type").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ESPProtocolError("manifest response is not application/json")
        length_text = _header(response, "Content-Length")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ESPProtocolError("manifest response has no valid Content-Length") from exc
        if length < 2 or length > MAX_MANIFEST_BYTES:
            raise ESPProtocolError("manifest response has an unsafe Content-Length")
        payload = response.read(length + 1)
        if len(payload) != length:
            raise ESPProtocolError("manifest response was truncated or oversized")
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ESPProtocolError("manifest response is not valid JSON") from exc
        etag = _header(response, "ETag")
        if not etag:
            raise ESPProtocolError("manifest response has no ETag")
        return value, etag

    def _download_frame(self, mode: str, sha256: str, expected_bytes: int) -> tuple[Path, str]:
        response, not_modified = self._request(
            f"/v1/frame/{quote(mode, safe='')}/{sha256}"
        )
        if not_modified or response is None:
            raise ESPProtocolError("content-addressed frame unexpectedly returned not modified")
        temporary: Path | None = None
        try:
            content_type = _header(response, "Content-Type").split(";", 1)[0].strip().lower()
            if content_type not in _FRAME_CONTENT_TYPES:
                raise ESPProtocolError("frame response has an unsupported Content-Type")
            _content_length(response, label="frame", expected=expected_bytes)
            if _header(response, "X-Frame-Format") != EE02_WIRE_FORMAT:
                raise ESPProtocolError("frame response has an unsupported wire format")
            response_sha = _header(response, "X-Frame-SHA256") or _header(
                response, "X-Content-SHA256"
            )
            if response_sha != sha256:
                raise ESPProtocolError("frame response checksum header disagrees with its manifest")
            frame_etag = _header(response, "ETag")
            if not _etag_matches_sha256(frame_etag, sha256):
                raise ESPProtocolError("frame response ETag disagrees with its checksum")

            self.frames_directory.mkdir(parents=True, exist_ok=True)
            destination = self.frames_directory / f"{sha256}.ee02"
            digest = hashlib.sha256()
            received = 0
            with tempfile.NamedTemporaryFile(
                mode="w+b", prefix=f".{sha256}.", suffix=".tmp",
                dir=self.frames_directory, delete=False,
            ) as handle:
                temporary = Path(handle.name)
                while True:
                    block = response.read(min(self.chunk_size, expected_bytes - received + 1))
                    if not block:
                        break
                    received += len(block)
                    if received > expected_bytes:
                        raise ESPProtocolError("frame response exceeded the declared EE02 buffer size")
                    digest.update(block)
                    handle.write(block)
                if received != expected_bytes:
                    raise ESPProtocolError(f"frame response was truncated at {received} bytes")
                if digest.hexdigest() != sha256:
                    raise ESPProtocolError("frame response failed its SHA-256 verification")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
            _fsync_directory(self.frames_directory)
            return destination, frame_etag
        finally:
            response.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _activate(self, cached: Path, mode: str, sha256: str, state: Mapping[str, Any]) -> bool:
        display = state.get("display")
        already_active = (
            isinstance(display, dict)
            and display.get("sha256") == sha256
            and display.get("mode") == mode
            and _valid_frame(self.frame_path, sha256)
        )
        if already_active:
            return False

        self.state_directory.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b", prefix=".display.", suffix=".tmp",
                dir=self.state_directory, delete=False,
            ) as target:
                temporary = Path(target.name)
                with cached.open("rb") as source:
                    shutil.copyfileobj(source, target, length=self.chunk_size)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.frame_path)
            temporary = None
            _fsync_directory(self.state_directory)
            return True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def pull(self, requested_mode: str) -> ESPPullResult:
        mode = canonical_mode(requested_mode)
        if mode == "automatic":
            raise ValueError("the ESP client needs a concrete mode, not automatic")
        state = self._load_state()
        saved = state["modes"].get(mode)
        if not isinstance(saved, dict):
            saved = {}
        saved_sha = saved.get("sha256") if isinstance(saved.get("sha256"), str) else ""
        cached = self.frames_directory / f"{saved_sha}.ee02" if _SHA256.fullmatch(saved_sha) else None
        cache_valid = cached is not None and _valid_frame(cached, saved_sha)

        conditional_etag = saved.get("manifest_etag", "") if cache_valid else ""
        response, not_modified = self._request(
            f"/v1/manifest/{quote(mode, safe='')}", etag=conditional_etag
        )
        if not_modified:
            if not cache_valid or cached is None:
                response, not_modified = self._request(f"/v1/manifest/{quote(mode, safe='')}")
                if not_modified or response is None:
                    raise ESPProtocolError("unconditional manifest request returned not modified")
            else:
                changed = self._activate(cached, mode, saved_sha, state)
                next_state = dict(state)
                next_state["display"] = {
                    "mode": mode,
                    "sha256": saved_sha,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                _atomic_json(next_state, self.state_path)
                return ESPPullResult(
                    mode=mode,
                    changed=changed,
                    status="cache-activated" if changed else "not-modified",
                    sha256=saved_sha,
                    bytes=EE02_PAYLOAD_BYTES,
                    frame_path=self.frame_path,
                    manifest_etag=str(saved.get("manifest_etag", "")),
                    frame_etag=str(saved.get("frame_etag", "")),
                )

        if response is None:
            raise ESPProtocolError("manifest request returned no response")
        try:
            manifest, manifest_etag = self._read_manifest(response)
        finally:
            response.close()
        sha256, expected_bytes = _validate_manifest(manifest, mode)
        cached = self.frames_directory / f"{sha256}.ee02"
        if _valid_frame(cached, sha256):
            frame_etag = str(saved.get("frame_etag", "")) if saved_sha == sha256 else f'"sha256:{sha256}"'
            status = "verified-cache"
        else:
            cached, frame_etag = self._download_frame(mode, sha256, expected_bytes)
            status = "downloaded"

        changed = self._activate(cached, mode, sha256, state)
        next_state = dict(state)
        modes = dict(state["modes"])
        modes[mode] = {
            "manifest_etag": manifest_etag,
            "frame_etag": frame_etag,
            "sha256": sha256,
            "bytes": expected_bytes,
        }
        next_state["modes"] = modes
        next_state["display"] = {
            "mode": mode,
            "sha256": sha256,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_json(next_state, self.state_path)
        return ESPPullResult(
            mode=mode,
            changed=changed,
            status=status if changed else "not-modified",
            sha256=sha256,
            bytes=expected_bytes,
            frame_path=self.frame_path,
            manifest_etag=manifest_etag,
            frame_etag=frame_etag,
        )


__all__ = [
    "ESPClientError",
    "ESPPullResult",
    "ESPProtocolError",
    "SimulatedESPClient",
]
