from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from PIL import Image, ImageDraw

from ..models import RenderContext
from ..repositories import find_repository
from .drawing import font


class BirdsSource:
    name = "Birds"

    def __init__(self) -> None:
        self.name = "Birds"

    @staticmethod
    def _layout_arguments(context: RenderContext) -> list[str]:
        if context.orientation.value != "landscape":
            return []
        # AvianVisitors' frontend normally uses xBias=2.1/yBias=1.0 for a
        # landscape viewport. frame/shoot.py's portrait-oriented defaults
        # override that with a tall 1.0/1.2 cluster and reserve only 52vh for
        # birds. Restore the native wide packing profile and give it the panel.
        return [
            "--mat", "0.015",
            "--collage-vh", "76",
            "--cluster-xbias", "2.1",
            "--cluster-ybias", "1.15",
            "--count-exp", "0.65",
            "--cluster-pad", "2",
            "--packing-budget", "0.78",
        ]

    def render(self, context: RenderContext) -> Image.Image:
        provider = str(context.options.get("bird_provider", "")).strip().casefold()
        if provider == "birdweather" and not context.options.get("demo_birds", False):
            repository = find_repository(
                str(context.options.get("avian_repo", "")),
                "frame/birdweather.py",
                "AVIANVISITORS_REPO",
            )
            if repository is None:
                raise RuntimeError(
                    "BirdWeather rendering needs an AvianVisitors checkout with frame/birdweather.py"
                )
            return self._capture_birdweather(repository, context)
        value = str(context.options.get("bird_source", "")).strip()
        if value and not context.options.get("demo_birds", False):
            path = Path(value).expanduser()
            if path.is_file():
                with Image.open(path) as image:
                    return image.convert("RGB")
            if value.startswith(("http://", "https://")):
                repository = find_repository(str(context.options.get("avian_repo", "")), "frame/shoot.py", "AVIANVISITORS_REPO")
                if repository:
                    try:
                        return self._capture_avian(repository, value, context)
                    except RuntimeError:
                        if (
                            value.rstrip("/") == "http://birdnet.local"
                            and context.options.get("allow_demo_fallback", True)
                        ):
                            return self._capture_avian_demo(repository, context, live_unavailable=True)
                        raise
                return self._capture_page(value, context)
            raise FileNotFoundError(f"Bird frame not found: {path}")
        repository = find_repository(str(context.options.get("avian_repo", "")), "avian/assets/illustrations", "AVIANVISITORS_REPO")
        return self._demo(context, repository)

    def _capture_avian(self, repository: Path, url: str, context: RenderContext) -> Image.Image:
        """Invoke the real viewer at the panel aspect ratio.

        AvianVisitors' own ``frame/shoot.py`` loads the original frontend and
        applies its frame CSS. Supplying a landscape CSS viewport makes that
        same responsive page reflow horizontally; no simulator collage layout
        or image rotation is involved.
        """
        with tempfile.TemporaryDirectory(prefix="avian-simulator-") as directory:
            output = Path(directory) / "frame.png"
            # shoot.py uses device_scale_factor=2, so these become precisely
            # 1600x1200 landscape (or 1200x1600 portrait) output pixels.
            width, height = context.width // 2, context.height // 2
            days = int(context.options.get("bird_lookback_days", 7))
            title = str(context.options.get("bird_title", "Avian Visitors"))
            subtitle = str(context.options.get("bird_subtitle", "Nearby This Week"))
            command = [str(context.options.get("avian_python") or sys.executable), str(repository / "frame" / "shoot.py"),
                       "--url", url, "--out", str(output), "--width", str(width), "--height", str(height), "--dsf", "2",
                       "--window-hours", str(days * 24), "--title", title, "--subtitle", subtitle]
            command.extend(self._layout_arguments(context))
            try:
                completed = subprocess.run(command, cwd=repository, capture_output=True, text=True, timeout=75)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"AvianVisitors page capture timed out after {exc.timeout} seconds") from exc
            if completed.returncode or not output.is_file():
                detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
                raise RuntimeError(f"AvianVisitors frame capture failed: {detail}")
            with Image.open(output) as image:
                captured = image.convert("RGB")
            if captured.size != context.orientation.dimensions:
                raise RuntimeError(
                    f"AvianVisitors returned {captured.width}x{captured.height}; "
                    f"expected {context.width}x{context.height}"
                )
            self.name = "Birds · AvianVisitors horizontal viewer"
            return captured

    def _capture_birdweather(self, repository: Path, context: RenderContext) -> Image.Image:
        """Render honest regional BirdWeather data through AvianVisitors."""
        helper = Path(__file__).resolve().parents[1] / "avian_capture.py"
        postal_code = str(context.options.get("bird_postal_code", "84601")).strip()
        country = str(context.options.get("bird_country", "us")).strip().casefold()
        days = int(context.options.get("bird_lookback_days", 7))
        title = str(context.options.get("bird_title", "Avian Visitors"))
        subtitle = str(context.options.get("bird_subtitle", "Nearby This Week"))
        with tempfile.TemporaryDirectory(prefix="avian-birdweather-simulator-") as directory:
            output = Path(directory) / "frame.png"
            command = [
                str(context.options.get("avian_python") or sys.executable),
                str(helper),
                "--repo",
                str(repository),
                "--out",
                str(output),
                "--width",
                str(context.width // 2),
                "--height",
                str(context.height // 2),
                "--postal-code",
                postal_code,
                "--country",
                country,
                "--lookback-days",
                str(days),
                "--title",
                title,
                "--subtitle",
                subtitle,
            ]
            command.extend(self._layout_arguments(context))
            try:
                completed = subprocess.run(
                    command,
                    cwd=repository,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"Nearby BirdWeather render timed out after {exc.timeout} seconds"
                ) from exc
            if completed.returncode or not output.is_file():
                detail = (
                    completed.stderr.strip()
                    or completed.stdout.strip()
                    or f"exit {completed.returncode}"
                )
                raise RuntimeError(f"Nearby BirdWeather render failed: {detail}")
            with Image.open(output) as image:
                captured = image.convert("RGB")
        if captured.size != context.orientation.dimensions:
            raise RuntimeError(
                f"BirdWeather returned {captured.width}x{captured.height}; "
                f"expected {context.width}x{context.height}"
            )
        self.name = "Birds · nearby BirdWeather reports"
        return captured

    def _capture_avian_demo(self, repository: Path, context: RenderContext, live_unavailable: bool = False) -> Image.Image:
        """Render fixture species through AvianVisitors' actual local frontend."""
        helper = Path(__file__).resolve().parents[1] / "avian_capture.py"
        with tempfile.TemporaryDirectory(prefix="avian-local-simulator-") as directory:
            output = Path(directory) / "frame.png"
            command = [str(context.options.get("avian_python") or sys.executable), str(helper),
                       "--repo", str(repository), "--out", str(output),
                       "--width", str(context.width // 2), "--height", str(context.height // 2),
                       "--window-hours", str(int(context.options.get("bird_lookback_days", 7)) * 24),
                       "--title", str(context.options.get("bird_title", "Avian Visitors")),
                       "--subtitle", str(context.options.get("bird_subtitle", "Nearby This Week"))]
            command.extend(self._layout_arguments(context))
            try:
                completed = subprocess.run(command, cwd=repository, capture_output=True, text=True, timeout=75)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"Local AvianVisitors demo timed out after {exc.timeout} seconds") from exc
            if completed.returncode or not output.is_file():
                detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
                raise RuntimeError(f"Local AvianVisitors demo failed: {detail}")
            with Image.open(output) as image:
                captured = image.convert("RGB")
        if captured.size != context.orientation.dimensions:
            raise RuntimeError(
                f"Local AvianVisitors demo returned {captured.width}x{captured.height}; "
                f"expected {context.width}x{context.height}"
            )
        suffix = " · birdnet.local unavailable" if live_unavailable else ""
        self.name = f"Birds · original AvianVisitors local demo{suffix}"
        return captured

    def _capture_page(self, url: str, context: RenderContext) -> Image.Image:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Page capture needs optional Playwright; load a PNG or use Demo Birds.") from exc
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": context.width, "height": context.height})
            page.goto(url, wait_until="networkidle", timeout=30000)
            data = page.screenshot(type="png")
            browser.close()
        import io
        self.name = "Birds · webpage capture"
        return Image.open(io.BytesIO(data)).convert("RGB")

    def _demo(self, context: RenderContext, repository: Path | None = None) -> Image.Image:
        if repository:
            try:
                return self._capture_avian_demo(repository, context)
            except RuntimeError:
                # A launch without Playwright still has a no-browser fallback.
                if not context.options.get("allow_demo_fallback", True):
                    raise
        self.name = "Birds · synthetic offline fallback"
        w, h = context.width, context.height
        image = Image.new("RGB", (w, h), "#f5efdc")
        draw = ImageDraw.Draw(image)
        title = font(max(38, w // 25), bold=True)
        label = font(max(18, w // 55))
        draw.text((w*.05, h*.035), "Avian Visitors", font=title, fill="#17251d")
        names = (("Mountain Bluebird", "#3f76b5"), ("Western Tanager", "#e3bd2c"), ("House Finch", "#b94336"), ("Green-tailed Towhee", "#3b764b"))
        illustrations = []
        if repository:
            illustrations = sorted((repository / "avian" / "assets" / "illustrations").glob("*.png"))[:4]
        margin, gap = int(w*.05), int(w*.025)
        card_w = (w - margin*2 - gap) // 2
        card_h = int(h*.37)
        for i, (name, color) in enumerate(names):
            x = margin + (i % 2) * (card_w + gap)
            y = int(h*.14) + (i // 2) * (card_h + gap)
            draw.rounded_rectangle((x, y, x+card_w, y+card_h), 24, fill="white", outline="#26342b", width=4)
            cx, cy = x + card_w//2, y + int(card_h*.43)
            if i < len(illustrations):
                with Image.open(illustrations[i]) as opened:
                    bird = opened.convert("RGBA")
                bird.thumbnail((int(card_w*.68), int(card_h*.68)), Image.Resampling.LANCZOS)
                image.paste(bird, (cx-bird.width//2, cy-bird.height//2), bird)
            else:
                draw.ellipse((cx-card_w*.19, cy-card_h*.23, cx+card_w*.20, cy+card_h*.20), fill=color, outline="#1c271f", width=5)
                draw.ellipse((cx+card_w*.10, cy-card_h*.30, cx+card_w*.27, cy-card_h*.10), fill=color, outline="#1c271f", width=4)
                draw.polygon(((cx+card_w*.26, cy-card_h*.22), (cx+card_w*.36, cy-card_h*.18), (cx+card_w*.26, cy-card_h*.14)), fill="#e2bd35")
                draw.line((cx-card_w*.02, cy+card_h*.18, cx-card_w*.05, cy+card_h*.28), fill="#171c18", width=4)
            draw.text((x+20, y+card_h-50), name, font=label, fill="#1c271f")
        return image
