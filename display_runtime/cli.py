from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Sequence

from display_simulator.models import FitMode, Orientation

from .config import ConfigError, load_runtime_config
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
    if getattr(args, "no_rgb", False):
        config = replace(config, write_rgb=False)
    return FrameRuntime(config)


def _print_artifact(artifact: RuntimeArtifact) -> None:
    state = "changed" if artifact.changed else "unchanged"
    action = "committed" if artifact.written else "retained"
    print(f"{artifact.mode}: {state}; {action} last-known-good frame")
    print(f"source: {artifact.source_name} ({artifact.provenance})")
    print(f"native: {artifact.width}x{artifact.height}")
    print(f"checksum: {artifact.checksum}")
    print(f"frame: {artifact.frame_path}")
    if artifact.rgb_path:
        print(f"rgb: {artifact.rgb_path}")
    print(f"manifest: {artifact.manifest_path}")
    print(
        f"timing: source {artifact.source_seconds:.3f}s; "
        f"conversion {artifact.conversion_seconds:.3f}s"
    )


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
                checksum = (manifest.get("pixel_checksum") or {}).get("value", "unknown")
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

    raise RuntimeError(f"unsupported command {args.command!r}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
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
