"""Authenticated HTTP access to committed EE02 frame artifacts.

The server deliberately does not render frames.  It only exposes a mode's
atomically committed ``current.json`` and the immutable, content-addressed
EE02 payloads referenced by those manifests.  This keeps HTTP requests out of
the comparatively expensive source/render pipeline and gives an ESP client a
small, deterministic pull protocol.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import stat
from threading import Thread
from typing import BinaryIO, Iterator, Mapping
from urllib.parse import urlsplit

from .ee02 import (
    EE02_BUFFER_HEIGHT,
    EE02_BUFFER_WIDTH,
    EE02_NAMED_COLOR_CODES,
    EE02_PAYLOAD_BYTES,
    EE02_WIRE_FORMAT,
)


CONCRETE_MODES = frozenset(
    ("weather", "birds", "star-map", "uploaded-photo", "test-pattern")
)
MANIFEST_FORMAT = "eink-frame-artifacts-v2"
MANIFEST_SCHEMA_VERSION = 2
FRAME_CONTENT_TYPE = "application/vnd.seeed.ee02-4bpp"
MAX_MANIFEST_BYTES = 1024 * 1024
DEFAULT_CHUNK_SIZE = 64 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_PATH_RE = re.compile(r"/v1/manifest/([^/]+)\Z")
_FRAME_PATH_RE = re.compile(r"/v1/frame/([^/]+)(?:/([^/]+))?\Z")


class FrameServerError(RuntimeError):
    """Base class for failures while finding or validating an artifact."""


class ArtifactNotFound(FrameServerError):
    """A valid mode has no committed artifact for the requested identity."""


class ArtifactUnavailable(FrameServerError):
    """A committed artifact is malformed, corrupt, or temporarily unreadable."""


def frame_etag(sha256: str) -> str:
    """Return the strong entity tag for one immutable wire payload."""

    if _SHA256_RE.fullmatch(sha256) is None:
        raise ValueError("frame SHA-256 must be 64 lowercase hexadecimal characters")
    return f'"sha256-{sha256}"'


def _manifest_etag(payload: bytes) -> str:
    # A manifest can be recommitted with different metadata while retaining the
    # same frame.  Its ETag therefore hashes its own representation; the frame's
    # wire-derived ETag is also returned in X-Frame-ETag.
    return f'"sha256-{hashlib.sha256(payload).hexdigest()}"'


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """Apply HTTP weak comparison to a simple If-None-Match field value.

    GET and HEAD use weak comparison, so ``W/\"tag\"`` matches ``\"tag\"``.
    Lists and the wildcard form are supported.  The ETags emitted here never
    contain commas, which makes the intentionally small parser sufficient.
    """

    if not if_none_match:
        return False

    def weak_value(value: str) -> str:
        stripped = value.strip()
        if stripped[:2].lower() == "w/":
            stripped = stripped[2:].lstrip()
        return stripped

    expected = weak_value(etag)
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate == "*" or weak_value(candidate) == expected:
            return True
    return False


def _is_exact_int(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ArtifactUnavailable(f"manifest {label} must be an object")
    return value


def _validate_manifest(value: object, mode: str) -> tuple[str, str]:
    """Validate the part of schema v2 that defines the bytes sent to hardware.

    Returns ``(wire_sha256, relative_wire_path)``.
    """

    manifest = _require_mapping(value, "root")
    if not _is_exact_int(manifest.get("schema_version"), MANIFEST_SCHEMA_VERSION):
        raise ArtifactUnavailable("manifest schema_version is not supported")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise ArtifactUnavailable("manifest format is not supported")
    if manifest.get("mode") != mode:
        raise ArtifactUnavailable("manifest mode does not match its directory")

    wire = _require_mapping(manifest.get("wire"), "wire")
    if wire.get("format") != EE02_WIRE_FORMAT:
        raise ArtifactUnavailable("manifest wire format is not supported")
    if not _is_exact_int(wire.get("bits_per_pixel"), 4):
        raise ArtifactUnavailable("manifest wire bits_per_pixel must be 4")
    if not _is_exact_int(wire.get("bytes"), EE02_PAYLOAD_BYTES):
        raise ArtifactUnavailable("manifest wire byte count is invalid")
    buffer_dimensions = _require_mapping(
        wire.get("buffer_dimensions"), "wire.buffer_dimensions"
    )
    if not _is_exact_int(buffer_dimensions.get("width"), EE02_BUFFER_WIDTH) or not _is_exact_int(
        buffer_dimensions.get("height"), EE02_BUFFER_HEIGHT
    ):
        raise ArtifactUnavailable("manifest wire buffer dimensions are invalid")
    if wire.get("pixel_order") != "row-major":
        raise ArtifactUnavailable("manifest wire pixel order is invalid")
    if wire.get("nibble_order") != "even-x-high-odd-x-low":
        raise ArtifactUnavailable("manifest wire nibble order is invalid")
    expected_color_codes = {
        name: f"0x{code:X}" for name, code in EE02_NAMED_COLOR_CODES.items()
    }
    if wire.get("color_codes") != expected_color_codes:
        raise ArtifactUnavailable("manifest wire color codes are invalid")

    logical_dimensions = _require_mapping(
        wire.get("logical_dimensions"), "wire.logical_dimensions"
    )
    logical_size = (
        logical_dimensions.get("width"),
        logical_dimensions.get("height"),
    )
    rotation = wire.get("rotation")
    sprite_rotation = wire.get("seeed_sprite_rotation")
    if logical_size == (EE02_BUFFER_HEIGHT, EE02_BUFFER_WIDTH):
        valid_rotation = (
            (rotation == "clockwise" and _is_exact_int(sprite_rotation, 1))
            or (
                rotation == "counter-clockwise"
                and _is_exact_int(sprite_rotation, 3)
            )
        )
    elif logical_size == (EE02_BUFFER_WIDTH, EE02_BUFFER_HEIGHT):
        valid_rotation = rotation == "none" and _is_exact_int(sprite_rotation, 0)
    else:
        valid_rotation = False
    if not valid_rotation:
        raise ArtifactUnavailable("manifest wire logical dimensions and rotation disagree")

    wire_sha = wire.get("sha256")
    if not isinstance(wire_sha, str) or _SHA256_RE.fullmatch(wire_sha) is None:
        raise ArtifactUnavailable("manifest wire SHA-256 is invalid")

    files = _require_mapping(manifest.get("files"), "files")
    binary = _require_mapping(files.get("ee02_4bpp"), "files.ee02_4bpp")
    if binary.get("sha256") != wire_sha:
        raise ArtifactUnavailable("manifest wire and file SHA-256 values disagree")
    if not _is_exact_int(binary.get("bytes"), EE02_PAYLOAD_BYTES):
        raise ArtifactUnavailable("manifest EE02 file byte count is invalid")
    relative_path = binary.get("path")
    expected_path = f"frames/{wire_sha}.ee02"
    if relative_path != expected_path:
        raise ArtifactUnavailable("manifest EE02 file path is not content-addressed")
    return wire_sha, expected_path


def _read_manifest(output_directory: Path, mode: str) -> tuple[bytes, str, Path]:
    mode_directory = _confined_mode_directory(output_directory, mode)
    manifest_path = mode_directory / "current.json"
    if not manifest_path.exists():
        raise ArtifactNotFound(f"no frame has been committed for {mode}")
    try:
        resolved = manifest_path.resolve(strict=True)
        resolved.relative_to(mode_directory)
        if manifest_path.is_symlink() or not resolved.is_file():
            raise ArtifactUnavailable("current manifest is not a regular file")
        size = resolved.stat().st_size
        if size <= 0 or size > MAX_MANIFEST_BYTES:
            raise ArtifactUnavailable("current manifest has an invalid size")
        payload = resolved.read_bytes()
    except ArtifactUnavailable:
        raise
    except (OSError, ValueError) as exc:
        raise ArtifactUnavailable("current manifest cannot be read safely") from exc
    if len(payload) != size:
        raise ArtifactUnavailable("current manifest changed while it was read")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ArtifactUnavailable("current manifest is not valid JSON") from exc
    wire_sha, relative_path = _validate_manifest(value, mode)
    return payload, wire_sha, mode_directory / relative_path


def _confined_mode_directory(output_directory: Path, mode: str) -> Path:
    if mode not in CONCRETE_MODES:
        raise ArtifactNotFound("unknown display mode")
    root = output_directory.resolve()
    candidate = root / mode
    try:
        # strict=False permits the ordinary not-yet-rendered case while still
        # resolving any existing symlink in the path.
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ArtifactUnavailable("mode directory is outside the frame cache") from exc
    if resolved != candidate:
        # A mode directory is wholly managed by the runtime.  Refusing symlinked
        # mode roots closes the remaining time-of-check/path-substitution gap.
        raise ArtifactUnavailable("mode directory may not be a symbolic link")
    return resolved


def _open_verified_frame(
    output_directory: Path,
    mode: str,
    sha256: str,
    *,
    manifest_path: Path | None = None,
) -> BinaryIO:
    if _SHA256_RE.fullmatch(sha256) is None:
        raise ArtifactNotFound("invalid frame identity")
    mode_directory = _confined_mode_directory(output_directory, mode)
    expected = mode_directory / "frames" / f"{sha256}.ee02"
    if manifest_path is not None and manifest_path != expected:
        raise ArtifactUnavailable("manifest frame path does not match its identity")
    if not expected.exists():
        raise ArtifactNotFound("frame payload was not found")

    handle: BinaryIO | None = None
    try:
        resolved = expected.resolve(strict=True)
        resolved.relative_to(mode_directory)
        if expected.is_symlink() or not resolved.is_file():
            raise ArtifactUnavailable("frame payload is not a regular file")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(resolved, flags)
        handle = os.fdopen(descriptor, "rb")
        file_stat = os.fstat(handle.fileno())
        if not stat.S_ISREG(file_stat.st_mode):
            raise ArtifactUnavailable("frame payload is not a regular file")
        if file_stat.st_size != EE02_PAYLOAD_BYTES:
            raise ArtifactUnavailable("frame payload has an invalid size")
        digest = hashlib.sha256()
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
        if digest.hexdigest() != sha256:
            raise ArtifactUnavailable("frame payload failed SHA-256 verification")
        handle.seek(0)
        return handle
    except ArtifactUnavailable:
        if handle is not None:
            handle.close()
        raise
    except OSError as exc:
        if handle is not None:
            handle.close()
        raise ArtifactUnavailable("frame payload cannot be read safely") from exc


class FrameServer(ThreadingHTTPServer):
    """Threading HTTP server for the frame pull protocol.

    ``server_address`` may use port zero, which is useful for tests and local
    simulations.  The selected address is then available through the standard
    ``server_address`` attribute.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        output_directory: str | Path,
        auth_token: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        log_requests: bool = True,
    ) -> None:
        token = str(auth_token)
        if not token:
            raise ValueError("auth_token must not be empty")
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        self.output_directory = Path(output_directory).expanduser().resolve()
        self.auth_token = token.encode("utf-8")
        self.chunk_size = chunk_size
        self.log_requests = bool(log_requests)
        super().__init__(server_address, FrameRequestHandler)

    def start_in_thread(self, *, name: str = "display-frame-server") -> Thread:
        """Start ``serve_forever`` in a daemon thread and return that thread."""

        thread = Thread(target=self.serve_forever, name=name, daemon=True)
        thread.start()
        return thread


class FrameRequestHandler(BaseHTTPRequestHandler):
    """Request handler used by :class:`FrameServer`."""

    protocol_version = "HTTP/1.1"
    server_version = "DisplayFrameServer/1"

    @property
    def frame_server(self) -> FrameServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: object) -> None:
        if self.frame_server.log_requests:
            super().log_message(format, *args)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle(send_body=False)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def _handle(self, *, send_body: bool) -> None:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._send_json_error(404, "not_found", send_body=send_body)
            return
        path = parsed.path
        if path == "/v1/health":
            payload = b'{"ok":true}\n'
            self._send_bytes(
                200,
                payload,
                "application/json; charset=utf-8",
                send_body=send_body,
                cache_control="no-store",
            )
            return

        manifest_match = _MANIFEST_PATH_RE.fullmatch(path)
        frame_match = _FRAME_PATH_RE.fullmatch(path)
        if manifest_match is None and frame_match is None:
            self._send_json_error(404, "not_found", send_body=send_body)
            return
        if not self._is_authorized():
            self._send_json_error(
                401,
                "unauthorized",
                send_body=send_body,
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return

        try:
            if manifest_match is not None:
                mode = manifest_match.group(1)
                self._serve_manifest(mode, send_body=send_body)
            else:
                assert frame_match is not None
                mode, requested_sha = frame_match.groups()
                self._serve_frame(mode, requested_sha, send_body=send_body)
        except ArtifactNotFound:
            self._send_json_error(404, "not_found", send_body=send_body)
        except ArtifactUnavailable:
            self._send_json_error(503, "artifact_unavailable", send_body=send_body)

    def _is_authorized(self) -> bool:
        provided = self.headers.get("Authorization", "").encode("utf-8")
        expected = b"Bearer " + self.frame_server.auth_token
        return hmac.compare_digest(provided, expected)

    def _serve_manifest(self, mode: str, *, send_body: bool) -> None:
        if mode not in CONCRETE_MODES:
            raise ArtifactNotFound("unknown display mode")
        payload, wire_sha, frame_path = _read_manifest(
            self.frame_server.output_directory, mode
        )
        # Do not advertise a manifest whose hardware payload is missing or
        # corrupt.  Validation uses the same open file description later used
        # by frame requests, avoiding partial or last-known-bad responses.
        try:
            with _open_verified_frame(
                self.frame_server.output_directory,
                mode,
                wire_sha,
                manifest_path=frame_path,
            ):
                pass
        except ArtifactNotFound as exc:
            raise ArtifactUnavailable(
                "current manifest references a missing frame payload"
            ) from exc
        etag = _manifest_etag(payload)
        frame_tag = frame_etag(wire_sha)
        common_headers = {
            "ETag": etag,
            "X-Frame-ETag": frame_tag,
            "X-Content-SHA256": wire_sha,
            "X-Frame-SHA256": wire_sha,
            "X-Frame-Format": EE02_WIRE_FORMAT,
        }
        if etag_matches(self.headers.get("If-None-Match"), etag):
            self._send_not_modified(common_headers)
            return
        self._send_bytes(
            200,
            payload,
            "application/json; charset=utf-8",
            send_body=send_body,
            extra_headers=common_headers,
        )

    def _serve_frame(
        self, mode: str, requested_sha: str | None, *, send_body: bool
    ) -> None:
        if mode not in CONCRETE_MODES:
            raise ArtifactNotFound("unknown display mode")
        manifest_path: Path | None = None
        if requested_sha is None:
            _payload, wire_sha, manifest_path = _read_manifest(
                self.frame_server.output_directory, mode
            )
        else:
            if _SHA256_RE.fullmatch(requested_sha) is None:
                raise ArtifactNotFound("invalid frame identity")
            wire_sha = requested_sha

        try:
            handle = _open_verified_frame(
                self.frame_server.output_directory,
                mode,
                wire_sha,
                manifest_path=manifest_path,
            )
        except ArtifactNotFound as exc:
            if requested_sha is None:
                raise ArtifactUnavailable(
                    "current manifest references a missing frame payload"
                ) from exc
            raise
        etag = frame_etag(wire_sha)
        common_headers = {
            "ETag": etag,
            "X-Content-SHA256": wire_sha,
            "X-Frame-SHA256": wire_sha,
            "X-Frame-Format": EE02_WIRE_FORMAT,
        }
        if etag_matches(self.headers.get("If-None-Match"), etag):
            handle.close()
            self._send_not_modified(common_headers)
            return

        self.send_response(200)
        self.send_header("Content-Type", FRAME_CONTENT_TYPE)
        self.send_header("Content-Length", str(EE02_PAYLOAD_BYTES))
        self.send_header("Cache-Control", "no-cache")
        for name, value in common_headers.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            if send_body:
                while True:
                    chunk = handle.read(self.frame_server.chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            handle.close()

    def _send_not_modified(self, headers: Mapping[str, str]) -> None:
        self.send_response(304)
        self.send_header("Cache-Control", "no-cache")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()

    def _method_not_allowed(self) -> None:
        payload = b'{"error":"method_not_allowed"}\n'
        self._send_bytes(
            405,
            payload,
            "application/json; charset=utf-8",
            send_body=True,
            cache_control="no-store",
            extra_headers={"Allow": "GET, HEAD"},
        )

    def _send_bytes(
        self,
        status: int,
        payload: bytes,
        content_type: str,
        *,
        send_body: bool,
        cache_control: str = "no-cache",
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if send_body:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_json_error(
        self,
        status: int,
        code: str,
        *,
        send_body: bool,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        payload = (json.dumps({"error": code}, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        self._send_bytes(
            status,
            payload,
            "application/json; charset=utf-8",
            send_body=send_body,
            cache_control="no-store",
            extra_headers=extra_headers,
        )


@contextmanager
def running_frame_server(
    output_directory: str | Path,
    auth_token: str,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    log_requests: bool = False,
) -> Iterator[FrameServer]:
    """Run a frame server in a background thread for a bounded scope."""

    server = FrameServer(
        (host, port),
        output_directory=output_directory,
        auth_token=auth_token,
        chunk_size=chunk_size,
        log_requests=log_requests,
    )
    thread = server.start_in_thread()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


__all__ = [
    "ArtifactNotFound",
    "ArtifactUnavailable",
    "CONCRETE_MODES",
    "DEFAULT_CHUNK_SIZE",
    "FRAME_CONTENT_TYPE",
    "FrameRequestHandler",
    "FrameServer",
    "FrameServerError",
    "etag_matches",
    "frame_etag",
    "running_frame_server",
]
