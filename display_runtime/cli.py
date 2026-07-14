from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Sequence

from display_simulator.models import FitMode, Orientation

from .config import ConfigError, load_runtime_config
from .ee02 import LandscapeRotation
from .esp_client import ESPClientError, SimulatedESPClient
from .frame_server import FrameServer
from .runtime import (
    CANONICAL_MODES,
    FrameRuntime,
    RuntimeArtifact,
    SourcePolicyError,
    canonical_mode,
    parse_render_time,
)


def _mode_argument(value: str) -> str:
    try:
        return canonical_mode(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _fit_argument(value: str) -> FitMode:
    aliases = {
        "crop": FitMode.CROP,
        "fit": FitMode.FIT,
        "stretch": FitMode.STRETCH,
    }
    try:
        return aliases[value.strip().lower()]
    except KeyError as exc:
        raise argparse.ArgumentTypeError("fit must be crop, fit, or stretch") from exc


def _port_argument(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eink-display",
        description="Headless Spectra 6 frame renderer for Raspberry Pi and service use.",
    )
    parser.add_argument("--config", type=Path, help="runtime TOML configuration")
    parser.add_argument("--debug", action="store_true", help="show a traceback for unexpected errors")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)

    render = commands.add_parser("render", help="render and atomically commit a frame")
    render.add_argument("mode", type=_mode_argument, choices=CANONICAL_MODES)
    render.add_argument("--at", "--when", dest="at", help="ISO-8601 render time; naive values use configured timezone")
    render.add_argument("--output-dir", type=Path, help="override the configured output directory")
    render.add_argument("--orientation", choices=("landscape", "portrait"), help="override output orientation")
    render.add_argument("--fit", type=_fit_argument, metavar="{crop,fit,stretch}", help="override fit mode")
    render.add_argument(
        "--landscape-rotation",
        choices=tuple(rotation.value for rotation in LandscapeRotation),
        help="override rotation into the EE02 1200x1600 backing buffer",
    )
    render.add_argument("--allow-demo", action="store_true", help="allow fixture/sample fallback for this manual render")
    render.add_argument("--no-rgb", action="store_true", help="do not retain the continuous-colour RGB sidecar")
    render.add_argument("--force", action="store_true", help="rewrite artifacts even when pixels are unchanged")
    render.add_argument("--json", action="store_true", help="emit a machine-readable result")

    status = commands.add_parser("status", help="show committed last-known-good frames")
    status.add_argument("mode", nargs="?", type=_mode_argument, help="optional concrete mode")
    status.add_argument("--output-dir", type=Path, help="override the configured output directory")
    status.add_argument("--json", action="store_true", help="emit machine-readable status")

    check = commands.add_parser("check", help="validate configuration and report source readiness")
    check.add_argument("--output-dir", type=Path, help="override the configured output directory")
    check.add_argument("--json", action="store_true", help="emit machine-readable checks")

    selected = commands.add_parser("mode", help="resolve the scheduled mode for a date and time")
    selected.add_argument("--at", "--when", dest="at", help="ISO-8601 time; defaults to now")
    selected.add_argument("--json", action="store_true", help="emit a machine-readable result")

    serve = commands.add_parser("serve", help="serve committed EE02 frames to an ESP32")
    serve.add_argument("--host", help="override the configured listen address")
    serve.add_argument("--port", type=_port_argument, help="override the configured port")
    serve.add_argument("--output-dir", type=Path, help="override the configured frame directory")
    serve.add_argument(
        "--token-file",
        type=Path,
        help="read the bearer token from a file instead of config or DISPLAY_RUNTIME_AUTH_TOKEN",
    )
    serve.add_argument("--quiet", action="store_true", help="disable HTTP access logging")

    esp_sync = commands.add_parser("esp-sync", help="pull and verify a frame with the simulated ESP client")
    esp_sync.add_argument("mode", type=_mode_argument, choices=CANONICAL_MODES)
    esp_sync.add_argument("--server-url", help="override the configured frame server URL")
    esp_sync.add_argument("--state-dir", type=Path, help="override the simulated ESP state directory")
    esp_sync.add_argument("--timeout", type=float, help="override the HTTP timeout in seconds")
    esp_sync.add_argument(
        "--token-file",
        type=Path,
        help="read the bearer token from a file instead of config or DISPLAY_RUNTIME_AUTH_TOKEN",
    )
    esp_sync.add_argument("--json", action="store_true", help="emit a machine-readable result")
    return parser


def _runtime_from_args(args) -> FrameRuntime:
    config = load_runtime_config(args.config)
    if getattr(args, "output_dir", None) is not None:
        output = args.output_dir.expanduser().resolve(strict=False)
        config = replace(config, output_directory=output)
    if getattr(args, "orientation", None):
        config = replace(config, orientation=Orientation(args.orientation))
    if getattr(args, "fit", None) is not None:
        config = replace(config, fit_mode=args.fit)
    if getattr(args, "landscape_rotation", None):
        config = replace(config, landscape_rotation=LandscapeRotation(args.landscape_rotation))
    if getattr(args, "no_rgb", False):
        config = replace(config, write_rgb=False)
    return FrameRuntime(config)


def _print_artifact(artifact: RuntimeArtifact) -> None:
    state = "changed" if artifact.changed else "unchanged"
    action = "committed" if artifact.written else "retained"
    print(f"{artifact.mode}: {state}; {action} last-known-good frame")
    print(f"source: {artifact.source_name} ({artifact.provenance})")
    print(f"native: {artifact.width}x{artifact.height}")
    print(f"pixel checksum: {artifact.checksum}")
    print(f"frame: {artifact.frame_path}")
    print(
        f"EE02: {artifact.wire_path} · {artifact.wire_path.stat().st_size} bytes · "
        f"{artifact.wire_rotation} · SHA-256 {artifact.wire_checksum}"
    )
    if artifact.rgb_path:
        print(f"rgb: {artifact.rgb_path}")
    print(f"manifest: {artifact.manifest_path}")
    print(
        f"timing: source {artifact.source_seconds:.3f}s; "
        f"conversion {artifact.conversion_seconds:.3f}s"
    )


def _authentication_token(args, runtime: FrameRuntime) -> str:
    token_file = getattr(args, "token_file", None)
    if token_file is not None:
        try:
            token = token_file.expanduser().read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise ConfigError(f"could not read authentication token file: {exc}") from exc
    else:
        token = runtime.config.server_auth_token
    if not token:
        raise ConfigError(
            "an authentication token is required; set DISPLAY_RUNTIME_AUTH_TOKEN, "
            "server.auth_token, or --token-file"
        )
    if "\r" in token or "\n" in token:
        raise ConfigError("the authentication token must be a single line")
    return token


def _run(args) -> int:
    runtime = _runtime_from_args(args)
    if args.command == "render":
        when = parse_render_time(args.at, runtime.config.timezone)
        artifact = runtime.render(
            args.mode,
            when=when,
            allow_demo=args.allow_demo,
            force=args.force,
        )
        if args.json:
            print(json.dumps(artifact.as_dict(), indent=2, sort_keys=True))
        else:
            _print_artifact(artifact)
        return 0

    if args.command == "status":
        status = runtime.status(args.mode)
        if args.json:
            print(json.dumps(status, indent=2, sort_keys=True))
        elif not status:
            print("No committed frames.")
        else:
            for mode, manifest in status.items():
                checksum = (manifest.get("wire") or {}).get("sha256", "unknown")
                source = (manifest.get("source") or {}).get("name", "unknown")
                dimensions = manifest.get("dimensions") or {}
                print(
                    f"{mode}: {dimensions.get('width', '?')}x{dimensions.get('height', '?')} "
                    f"{checksum[:16]}… · {source} · {manifest.get('generated_at', 'unknown')}"
                )
        return 0

    if args.command == "check":
        result = runtime.check()
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            config_label = result["config_path"] or "packaged defaults"
            print(f"configuration: {config_label}")
            print(f"output: {result['output_directory']}")
            print(f"headless: yes · orientation: {result['orientation']} · timezone: {result['timezone']}")
            ee02 = result["ee02"]
            print(
                f"EE02: {ee02['buffer_dimensions']['width']}x{ee02['buffer_dimensions']['height']} · "
                f"{ee02['bytes']} bytes · {ee02['landscape_rotation']}"
            )
            server = result["server"]
            auth = "configured" if server["authentication_configured"] else "missing"
            print(f"HTTP: {server['host']}:{server['port']} · authentication {auth}")
            for mode, check in result["modes"].items():
                marker = "ready" if check["ready"] else "not ready"
                print(f"{mode}: {marker} — {check['reason']}")
        return 0

    if args.command == "mode":
        when = parse_render_time(args.at, runtime.config.timezone)
        mode = runtime.resolve_mode("automatic", when)
        if args.json:
            print(json.dumps({"at": when.isoformat(), "mode": mode}, indent=2, sort_keys=True))
        else:
            print(f"{when.isoformat()}: {mode}")
        return 0

    if args.command == "serve":
        token = _authentication_token(args, runtime)
        host = args.host if args.host is not None else runtime.config.server_host
        port = args.port if args.port is not None else runtime.config.server_port
        server = FrameServer(
            (host, port),
            output_directory=runtime.config.output_directory,
            auth_token=token,
            chunk_size=runtime.config.server_chunk_size,
            max_connections=runtime.config.server_max_connections,
            request_timeout=runtime.config.server_request_timeout,
            log_requests=not args.quiet,
        )
        bound_host, bound_port = server.server_address[:2]
        print(f"frame server listening on http://{bound_host}:{bound_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    if args.command == "esp-sync":
        if args.mode == "automatic":
            raise ValueError("esp-sync needs a concrete mode, not automatic")
        token = _authentication_token(args, runtime)
        server_url = args.server_url or runtime.config.esp_server_url
        state_directory = (
            args.state_dir.expanduser().resolve(strict=False)
            if args.state_dir is not None
            else runtime.config.esp_state_directory
        )
        timeout = args.timeout if args.timeout is not None else runtime.config.esp_timeout
        result = SimulatedESPClient(
            server_url,
            token,
            state_directory,
            timeout=timeout,
            chunk_size=runtime.config.esp_chunk_size,
        ).pull(args.mode)
        if args.json:
            print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        else:
            action = "refresh" if result.changed else "skip refresh"
            print(f"{result.mode}: {action} · {result.status}")
            print(f"EE02: {result.frame_path} · {result.bytes} bytes · SHA-256 {result.sha256}")
        return 0

    raise RuntimeError(f"unsupported command {args.command!r}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except ESPClientError as exc:
        print(f"ESP sync failed: {exc}", file=sys.stderr)
        return 5
    except (SourcePolicyError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"render failed: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"runtime I/O failed: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        if args.debug:
            raise
        print(f"runtime failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
