from __future__ import annotations

import os
import platform
import queue
import subprocess
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps

from .config import DEFAULTS, load_config
from .controller import RenderController
from .models import ConversionSettings, FitMode, Orientation, RenderContext, RenderResult
from .pipeline import normalize_source, unsupported_colors
from .preferences import load_preferences, save_preferences
from .schedule import ScheduleConfig, mode_for_time, parse_clock
from .sources import BirdsSource, StarMapSource, TestPatternSource, UploadedPhotoSource, WeatherSource
from .upload_server import UploadServer
from .widgets.controls import section
from .widgets.display_preview import DisplayPreview
from .widgets.status_panel import StatusPanel


MODES = ("Automatic", "Weather", "Birds", "Star Map", "Uploaded Photo", "Test Pattern")
BUTTON_ACTIONS = ("Weather", "Birds", "Star Map", "Uploaded Photo", "Automatic", "Refresh current frame")
WAKE_REASONS = ("Scheduled timer", "Button 1", "Button 2", "Button 3", "Manual upload")


class SimulatorApp:
    def __init__(self, root: tk.Tk, config_path: Path | None = None):
        self.root = root
        self.config = load_config(config_path)
        self.user_preferences = load_preferences()
        self.controller = RenderController()
        self.results: queue.Queue[tuple[int, object]] = queue.Queue()
        self.uploads: queue.Queue[Path] = queue.Queue()
        self.upload_server: UploadServer | None = None
        self.result: RenderResult | None = None
        self.active_token = 0
        self.busy = False
        self.wake_reason = tk.StringVar(value="Scheduled timer")
        self.auto_job: str | None = None
        self._build_variables()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(75, self._poll_results)
        self._update_schedule_label()

    def _build_variables(self) -> None:
        c = self.config
        preferences = self.user_preferences
        saved_repositories = preferences.get("repositories", {}) if isinstance(preferences.get("repositories"), dict) else {}
        saved_sources = preferences.get("sources", {}) if isinstance(preferences.get("sources"), dict) else {}
        repositories = c["repositories"]
        shared_repo = str(
            saved_repositories.get("avian_weather")
            or repositories.get("avian_weather")
            or repositories.get("weather")
            or repositories.get("avian")
            or ""
        )
        self.mode = tk.StringVar(value="Automatic")
        self.orientation = tk.StringVar(value=c["display"]["orientation"])
        now = datetime.now()
        self.date = tk.StringVar(value=now.strftime("%Y-%m-%d"))
        self.minutes = tk.IntVar(value=now.hour*60 + now.minute)
        self.use_current = tk.BooleanVar(value=False)
        self.auto_render = tk.BooleanVar(value=False)
        self.active_schedule = tk.StringVar()
        self.location = tk.StringVar(value=str(preferences.get("location") or c["location"]["name"]))
        self.avian_weather_repo = tk.StringVar(value=shared_repo)
        self.inkystarmap_repo = tk.StringVar(value=str(saved_repositories.get("inkystarmap") or repositories["inkystarmap"]))
        self.latitude = tk.DoubleVar(value=c["coordinates"]["latitude"])
        self.longitude = tk.DoubleVar(value=c["coordinates"]["longitude"])
        self.direction = tk.IntVar(value=c["coordinates"]["direction"])
        self.timezone = tk.StringVar(value=c["coordinates"]["timezone"])
        self.demo_weather = tk.BooleanVar(value=True)
        self.weather_style = tk.StringVar(value="woodblock")
        self.weather_scene_source = tk.StringVar(value="auto")
        self.weather_caption = tk.BooleanVar(value=False)
        self.bird_source = tk.StringVar(value=str(saved_sources.get("bird") or c["sources"]["bird"]))
        # The primary bird view is the real AvianVisitors page. The generated
        # collage is an explicit offline fallback, not the launch default.
        self.demo_birds = tk.BooleanVar(value=False)
        self.starmap_source = tk.StringVar(value=str(saved_sources.get("starmap") or c["sources"]["starmap"]))
        self.dark_starmap = tk.BooleanVar(value=True)
        self.use_inkystarmap = tk.BooleanVar(value=True)
        self.photo_path = tk.StringVar(value=str(saved_sources.get("photo") or ""))
        self.fit_mode = tk.StringVar(value=FitMode.CROP.value)
        self.rotation = tk.IntVar(value=0)
        self.caption = tk.StringVar()
        self.dither = tk.BooleanVar(value=c["conversion"]["dithering"])
        self.dither_method = tk.StringVar(value=c["conversion"]["method"])
        self.saturation = tk.DoubleVar(value=c["conversion"]["saturation"])
        self.blue_bias = tk.DoubleVar(value=c["conversion"]["blue_bias"])
        self.physical = tk.BooleanVar(value=c["display"]["physical_treatment"])
        self.button_maps = [tk.StringVar(value=c["buttons"][f"button{i}"]) for i in range(1, 4)]
        self.status = tk.StringVar(value="Ready")
        self.upload_status = tk.StringVar(value="Upload page stopped")

    def _build_ui(self) -> None:
        width, height = self.config["window"]["width"], self.config["window"]["height"]
        self.root.title("Spectra 6 Frame Simulator")
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(960, 650)
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=3)
        outer.columnconfigure(1, weight=0, minsize=390)
        outer.rowconfigure(0, weight=1)
        left = ttk.Frame(outer)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.preview = DisplayPreview(left)
        self.preview.grid(row=0, column=0, sticky="nsew")
        buttons = ttk.LabelFrame(left, text="Physical button simulation", padding=7)
        buttons.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for i in range(3):
            buttons.columnconfigure(i, weight=1)
            ttk.Button(buttons, text=f"Button {i+1}", command=lambda n=i: self._press_button(n)).grid(row=0, column=i, sticky="ew", padx=3)
            ttk.Combobox(buttons, textvariable=self.button_maps[i], values=BUTTON_ACTIONS, state="readonly", width=18).grid(row=1, column=i, sticky="ew", padx=3, pady=(4, 0))
        right_host = ttk.Frame(outer, width=400)
        right_host.grid(row=0, column=1, sticky="nsew")
        canvas = tk.Canvas(right_host, highlightthickness=0, width=390)
        scrollbar = ttk.Scrollbar(right_host, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        controls = ttk.Frame(canvas, padding=(2, 0, 5, 0))
        window = canvas.create_window((0, 0), window=controls, anchor="nw")
        controls.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        self._display_controls(controls)
        self._time_controls(controls)
        self._repository_controls(controls)
        self._source_controls(controls)
        self._conversion_controls(controls)
        self._action_controls(controls)

    def _display_controls(self, parent) -> None:
        box = section(parent, "Display mode")
        box.pack(fill="x", pady=(0, 7))
        ttk.Combobox(box, textvariable=self.mode, values=MODES, state="readonly").grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(box, text="Orientation").grid(row=1, column=0, sticky="w", pady=(6, 0))
        orientation = ttk.Combobox(box, textvariable=self.orientation, values=("landscape", "portrait"), state="readonly", width=14)
        orientation.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        orientation.bind("<<ComboboxSelected>>", lambda _e: self._orientation_changed())

    def _time_controls(self, parent) -> None:
        box = section(parent, "Simulated time")
        box.pack(fill="x", pady=7)
        ttk.Label(box, text="Date (YYYY-MM-DD)").grid(row=0, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.date).grid(row=0, column=1, sticky="ew")
        ttk.Checkbutton(box, text="Use current Mac time", variable=self.use_current, command=self._current_time_changed).grid(row=1, column=0, columnspan=2, sticky="w")
        scale = ttk.Scale(box, from_=0, to=1439, variable=self.minutes, command=self._time_changed)
        scale.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        self.clock_label = ttk.Label(box)
        self.clock_label.grid(row=3, column=0, sticky="w")
        ttk.Label(box, textvariable=self.active_schedule).grid(row=3, column=1, sticky="e")
        ttk.Checkbutton(box, text="Auto-render on mode change", variable=self.auto_render).grid(row=4, column=0, columnspan=2, sticky="w")

    def _source_controls(self, parent) -> None:
        weather = section(parent, "Location and weather")
        weather.pack(fill="x", pady=7)
        ttk.Label(weather, text="Location").grid(row=0, column=0, sticky="w")
        ttk.Entry(weather, textvariable=self.location).grid(row=0, column=1, sticky="ew")
        ttk.Checkbutton(weather, text="Fixture/demo weather (offline)", variable=self.demo_weather).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Combobox(weather, textvariable=self.weather_style, values=("woodblock", "ink_wash"), state="readonly").grid(row=2, column=0, sticky="ew")
        ttk.Combobox(weather, textvariable=self.weather_scene_source, values=("auto", "generated", "procedural"), state="readonly").grid(row=2, column=1, sticky="ew")
        ttk.Checkbutton(weather, text="Forecast caption", variable=self.weather_caption).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Button(weather, text="Generate Weather", command=lambda: self._generate_as("Weather")).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        birds = section(parent, "Birds")
        birds.pack(fill="x", pady=7)
        ttk.Entry(birds, textvariable=self.bird_source).grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Button(birds, text="Choose PNG", command=self._choose_bird).grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Button(birds, text="Refresh Birds", command=self._refresh_birds).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Checkbutton(birds, text="Use local demo species in the original AvianVisitors viewer", variable=self.demo_birds).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Button(birds, text="Demo Birds", command=self._demo_birds).grid(row=3, column=0, columnspan=2, sticky="ew")
        stars = section(parent, "Star map")
        stars.pack(fill="x", pady=7)
        ttk.Entry(stars, textvariable=self.starmap_source).grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Button(stars, text="Choose rendered PNG", command=self._choose_starmap).grid(row=1, column=0, sticky="ew")
        ttk.Checkbutton(stars, text="Dark background", variable=self.dark_starmap).grid(row=1, column=1, sticky="w")
        ttk.Checkbutton(stars, text="Use inkystarmap/Starplot when available", variable=self.use_inkystarmap).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(stars, text="Lat / lon / direction").grid(row=3, column=0, sticky="w")
        coordinates = ttk.Frame(stars)
        coordinates.grid(row=3, column=1, sticky="ew")
        ttk.Entry(coordinates, textvariable=self.latitude, width=8).pack(side="left")
        ttk.Entry(coordinates, textvariable=self.longitude, width=9).pack(side="left", padx=2)
        ttk.Spinbox(coordinates, from_=0, to=359, textvariable=self.direction, width=4).pack(side="left")
        ttk.Label(stars, text="Timezone").grid(row=4, column=0, sticky="w")
        ttk.Entry(stars, textvariable=self.timezone).grid(row=4, column=1, sticky="ew")
        photo = section(parent, "Uploaded photo")
        photo.pack(fill="x", pady=7)
        ttk.Entry(photo, textvariable=self.photo_path).grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Button(photo, text="Choose Image", command=self._choose_photo).grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Combobox(photo, textvariable=self.fit_mode, values=tuple(item.value for item in FitMode), state="readonly").grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(photo, text="Rotate").grid(row=2, column=0, sticky="w")
        ttk.Combobox(photo, textvariable=self.rotation, values=(0, 90, 180, 270), state="readonly").grid(row=2, column=1, sticky="ew")
        ttk.Label(photo, text="Caption").grid(row=3, column=0, sticky="w")
        ttk.Entry(photo, textvariable=self.caption).grid(row=3, column=1, sticky="ew")
        ttk.Button(photo, text="Convert to E-Ink", command=lambda: self._generate_as("Uploaded Photo")).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.upload_button = ttk.Button(photo, text="Start LAN Upload Page", command=self._toggle_upload_server)
        self.upload_button.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        ttk.Label(photo, textvariable=self.upload_status, wraplength=340).grid(row=6, column=0, columnspan=2, sticky="w")

    def _repository_controls(self, parent) -> None:
        box = section(parent, "Project repositories")
        box.pack(fill="x", pady=7)
        for row, (label, variable, command) in enumerate((("AvianVisitors + Weather", self.avian_weather_repo, lambda: self._choose_repo(self.avian_weather_repo)),
                                                          ("inkystarmap", self.inkystarmap_repo, lambda: self._choose_repo(self.inkystarmap_repo)))):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky="w")
            ttk.Entry(box, textvariable=variable).grid(row=row, column=1, sticky="ew")
            ttk.Button(box, text="…", width=3, command=command).grid(row=row, column=2)

    def _conversion_controls(self, parent) -> None:
        box = section(parent, "E-ink conversion")
        box.pack(fill="x", pady=7)
        ttk.Checkbutton(box, text="Dithering", variable=self.dither).grid(row=0, column=0, sticky="w")
        ttk.Combobox(box, textvariable=self.dither_method, values=("floyd-steinberg", "none"), state="readonly", width=18).grid(row=0, column=1, sticky="ew")
        ttk.Label(box, text="Saturation").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(box, from_=0, to=1, increment=.05, textvariable=self.saturation).grid(row=1, column=1, sticky="ew")
        ttk.Label(box, text="Blue bias").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(box, from_=0, to=1, increment=.05, textvariable=self.blue_bias).grid(row=2, column=1, sticky="ew")
        ttk.Checkbutton(box, text="Physical appearance preview", variable=self.physical, command=self._refresh_preview_treatment).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Button(box, text="Reset Defaults", command=self._reset_defaults).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(5, 0))

    def _action_controls(self, parent) -> None:
        box = section(parent, "Actions")
        box.pack(fill="x", pady=7)
        self.generate_button = ttk.Button(box, text="Generate / Refresh Preview", command=self.generate)
        self.generate_button.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(box, text="Wake reason").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Combobox(box, textvariable=self.wake_reason, values=WAKE_REASONS, state="readonly").grid(row=1, column=1, sticky="ew", pady=(4, 0))
        ttk.Button(box, text="Save Native E-Ink PNG", command=self._save_eink).grid(row=2, column=0, sticky="ew", pady=4)
        ttk.Button(box, text="Save RGB Source PNG", command=self._save_rgb).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(box, text="Copy diagnostic summary", command=self._copy_summary).grid(row=3, column=0, sticky="ew")
        ttk.Button(box, text="Open output folder", command=self._open_output).grid(row=3, column=1, sticky="ew")
        self.progress = ttk.Progressbar(box, mode="indeterminate")
        self.progress.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(7, 2))
        ttk.Label(box, textvariable=self.status, wraplength=355).grid(row=5, column=0, columnspan=2, sticky="w")
        self.diagnostics = StatusPanel(parent)
        self.diagnostics.pack(fill="x", pady=7)

    def _when(self) -> datetime:
        if self.use_current.get():
            return datetime.now()
        date = datetime.strptime(self.date.get().strip(), "%Y-%m-%d")
        return date.replace(hour=self.minutes.get()//60, minute=self.minutes.get()%60)

    def _schedule(self) -> ScheduleConfig:
        values = self.config["schedule"]
        return ScheduleConfig(parse_clock(values["weather_start"]), parse_clock(values["birds_start"]), parse_clock(values["star_start"]))

    def _selected_mode(self) -> str:
        return mode_for_time(self._when(), self._schedule()) if self.mode.get() == "Automatic" else self.mode.get()

    def _source(self, mode: str):
        return {"Weather": WeatherSource, "Birds": BirdsSource, "Star Map": StarMapSource,
                "Uploaded Photo": UploadedPhotoSource, "Test Pattern": TestPatternSource}[mode]()

    def _context(self) -> RenderContext:
        settings = f"dither={self.dither.get()} · saturation={self.saturation.get():.2f} · blue bias={self.blue_bias.get():.2f}"
        return RenderContext(
            orientation=Orientation(self.orientation.get()), when=self._when(), location=self.location.get().strip(),
            offline=self.demo_weather.get(), options={"bird_source": self.bird_source.get(), "demo_birds": self.demo_birds.get(),
                "avian_repo": self.avian_weather_repo.get(), "weather_repo": self.avian_weather_repo.get(), "inkystarmap_repo": self.inkystarmap_repo.get(),
                "starmap_source": self.starmap_source.get(), "dark_starmap": self.dark_starmap.get(),
                "use_inkystarmap": self.use_inkystarmap.get(), "latitude": self.latitude.get(), "longitude": self.longitude.get(), "direction": self.direction.get(), "timezone": self.timezone.get(),
                "weather_style": self.weather_style.get(), "weather_scene_source": self.weather_scene_source.get(), "weather_caption": self.weather_caption.get(),
                "photo_path": self.photo_path.get(), "rotation": self.rotation.get(), "caption": self.caption.get(),
                "settings_label": settings})

    def generate(self) -> None:
        if self.busy:
            self.controller.invalidate()
        try:
            mode, context = self._selected_mode(), self._context()
        except Exception as exc:
            messagebox.showerror("Invalid simulator settings", str(exc))
            return
        settings = ConversionSettings(self.dither.get(), self.dither_method.get(), self.saturation.get(), self.blue_bias.get())
        fit = FitMode(self.fit_mode.get())
        self._save_preferences()
        self.busy = True
        self.generate_button.configure(state="disabled")
        self.progress.start(12)
        self.status.set(f"Rendering {mode}…")
        self.active_token = self.controller.submit(self._source(mode), context, settings, fit, lambda token, future: self.results.put((token, future)))

    def _poll_results(self) -> None:
        self._poll_uploads()
        try:
            while True:
                token, future = self.results.get_nowait()
                if not self.controller.is_current(token):
                    continue
                self.busy = False
                self.progress.stop()
                self.generate_button.configure(state="normal")
                try:
                    self.result = self.controller.accept(future.result())
                except Exception as exc:
                    self.status.set(f"Render failed: {exc}")
                    messagebox.showerror("Could not render frame", str(exc))
                    continue
                self.preview.set_image(self.result.eink_image, self.physical.get())
                self.status.set(f"Rendered {self.result.source_name}")
                self.diagnostics.set(self._summary())
        except queue.Empty:
            pass
        self.root.after(75, self._poll_results)

    def _summary(self) -> str:
        if not self.result:
            return "No frame generated"
        r = self.result
        refresh = "Would refresh panel" if r.changed else "Frame unchanged — would skip refresh"
        return (f"Source: {r.source_name} · {r.eink_image.width}×{r.eink_image.height}\n"
                f"Source {r.source_seconds:.3f}s · conversion {r.conversion_seconds:.3f}s\n"
                f"Checksum: {r.checksum[:16]}…\n{refresh} · packed transfer ~{r.eink_image.width*r.eink_image.height/2/1024:.0f} KiB\n"
                f"Wake reason: {self.wake_reason.get()}")

    def _time_changed(self, _value=None) -> None:
        previous = self.active_schedule.get()
        self._update_schedule_label()
        if self.auto_render.get() and previous and previous != self.active_schedule.get():
            if self.auto_job:
                self.root.after_cancel(self.auto_job)
            self.auto_job = self.root.after(350, self.generate)

    def _update_schedule_label(self) -> None:
        try:
            when = self._when()
            self.clock_label.configure(text=when.strftime("%I:%M %p").lstrip("0"))
            self.active_schedule.set(mode_for_time(when, self._schedule()))
        except (ValueError, TypeError):
            self.active_schedule.set("Invalid time")

    def _current_time_changed(self) -> None:
        if self.use_current.get():
            now = datetime.now()
            self.date.set(now.strftime("%Y-%m-%d")); self.minutes.set(now.hour*60+now.minute)
        self._update_schedule_label()

    def _orientation_changed(self) -> None:
        self.result = None
        self.preview.set_image(None)
        self.status.set("Orientation changed; generate a new native frame")

    def _generate_as(self, mode: str) -> None:
        self.mode.set(mode); self.generate()

    def _refresh_birds(self) -> None:
        self.demo_birds.set(False); self._generate_as("Birds")

    def _demo_birds(self) -> None:
        self.demo_birds.set(True); self._generate_as("Birds")

    def _choose_path(self, variable: tk.StringVar, title: str) -> str:
        path = filedialog.askopenfilename(title=title, filetypes=(("Images", "*.png *.jpg *.jpeg *.webp"), ("All files", "*.*")))
        if path:
            variable.set(path)
            self._save_preferences()
        return path

    def _choose_repo(self, variable: tk.StringVar) -> None:
        path = filedialog.askdirectory(title="Choose repository checkout")
        if path:
            variable.set(path)
            self._save_preferences()

    def _choose_bird(self) -> None: self._choose_path(self.bird_source, "Choose bird frame")
    def _choose_starmap(self) -> None: self._choose_path(self.starmap_source, "Choose star-map frame")

    def _choose_photo(self) -> None:
        path = self._choose_path(self.photo_path, "Choose a photo")
        if path:
            try:
                self.wake_reason.set("Manual upload")
                with Image.open(path) as opened:
                    preview = normalize_source(ImageOps.exif_transpose(opened), Orientation(self.orientation.get()).dimensions, FitMode(self.fit_mode.get()))
                self.preview.set_image(preview, False)
                self.status.set("Showing RGB source preview; click Convert to E-Ink")
            except Exception as exc:
                messagebox.showerror("Could not open image", str(exc))

    def _press_button(self, index: int) -> None:
        action = self.button_maps[index].get()
        self.wake_reason.set(f"Button {index+1}")
        if action != "Refresh current frame": self.mode.set(action)
        self.generate()

    def _toggle_upload_server(self) -> None:
        if self.upload_server:
            self.upload_server.stop(); self.upload_server = None
            self.upload_status.set("Upload page stopped"); self.upload_button.configure(text="Start LAN Upload Page")
            return
        cfg = self.config["upload"]
        output = Path(str(cfg["file"])).expanduser()
        if not output.is_absolute(): output = Path.cwd() / output
        try:
            self.upload_server = UploadServer(str(cfg["host"]), int(cfg["port"]), output,
                                              int(cfg["max_megabytes"]) * 1024 * 1024, self.uploads.put)
            self.upload_server.start()
        except OSError as exc:
            self.upload_server = None; messagebox.showerror("Could not start upload page", str(exc)); return
        self.upload_status.set(f"Listening at {self.upload_server.url}")
        self.upload_button.configure(text="Stop LAN Upload Page")

    def _poll_uploads(self) -> None:
        latest = None
        try:
            while True: latest = self.uploads.get_nowait()
        except queue.Empty:
            pass
        if latest:
            self.photo_path.set(str(latest)); self.wake_reason.set("Manual upload"); self.mode.set("Uploaded Photo")
            self._save_preferences()
            self.status.set(f"Received upload: {latest.name}"); self.generate()

    def _refresh_preview_treatment(self) -> None:
        if self.result: self.preview.set_image(self.result.eink_image, self.physical.get())

    def _reset_defaults(self) -> None:
        c = DEFAULTS
        self.orientation.set(c["display"]["orientation"]); self.location.set(c["location"]["name"])
        self.avian_weather_repo.set(c["repositories"]["avian_weather"])
        self.inkystarmap_repo.set(c["repositories"]["inkystarmap"])
        self.latitude.set(c["coordinates"]["latitude"]); self.longitude.set(c["coordinates"]["longitude"])
        self.direction.set(c["coordinates"]["direction"]); self.timezone.set(c["coordinates"]["timezone"])
        self.bird_source.set(c["sources"]["bird"]); self.starmap_source.set(c["sources"]["starmap"])
        self.dither.set(c["conversion"]["dithering"]); self.dither_method.set(c["conversion"]["method"])
        self.saturation.set(c["conversion"]["saturation"]); self.blue_bias.set(c["conversion"]["blue_bias"])
        self.physical.set(c["display"]["physical_treatment"]); self.fit_mode.set(FitMode.CROP.value)
        for i, var in enumerate(self.button_maps, 1): var.set(c["buttons"][f"button{i}"])
        self.status.set("Defaults restored")
        self._save_preferences()

    def _save_preferences(self) -> None:
        data = {
            "location": self.location.get().strip(),
            "repositories": {
                "avian_weather": self.avian_weather_repo.get().strip(),
                "inkystarmap": self.inkystarmap_repo.get().strip(),
            },
            "sources": {
                "bird": self.bird_source.get().strip(),
                "starmap": self.starmap_source.get().strip(),
                "photo": self.photo_path.get().strip(),
            },
        }
        try:
            save_preferences(data)
            self.user_preferences = data
        except OSError as exc:
            if hasattr(self, "status"):
                self.status.set(f"Could not save preferences: {exc}")

    def _save(self, image: Image.Image | None, default: str, validate: bool = False) -> None:
        if image is None:
            messagebox.showwarning("Nothing to save", "Generate a frame first."); return
        if validate:
            bad = unsupported_colors(image)
            if bad:
                messagebox.showerror("Invalid e-ink output", f"Found {len(bad)} unsupported RGB colors."); return
            expected = Orientation(self.orientation.get()).dimensions
            if image.size != expected:
                messagebox.showerror("Invalid e-ink output", f"Expected {expected[0]}×{expected[1]}, got {image.width}×{image.height}."); return
        output = self._output_dir(); output.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(initialdir=output, initialfile=default, defaultextension=".png", filetypes=(("PNG", "*.png"),))
        if path:
            image.save(path, format="PNG"); self.status.set(f"Saved {path}")

    def _save_eink(self) -> None: self._save(self.result.eink_image if self.result else None, "spectra6-native.png", True)
    def _save_rgb(self) -> None: self._save(self.result.rgb_image if self.result else None, "rgb-source.png")

    def _copy_summary(self) -> None:
        self.root.clipboard_clear(); self.root.clipboard_append(self._summary()); self.status.set("Diagnostic summary copied")

    def _output_dir(self) -> Path:
        path = Path(self.config["output"]["directory"]).expanduser()
        return path if path.is_absolute() else Path.cwd()/path

    def _open_output(self) -> None:
        path = self._output_dir(); path.mkdir(parents=True, exist_ok=True)
        try:
            if platform.system() == "Darwin": subprocess.Popen(["open", str(path)])
            elif os.name == "nt": os.startfile(path)  # type: ignore[attr-defined]
            else: subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc: messagebox.showerror("Could not open folder", str(exc))

    def close(self) -> None:
        self._save_preferences()
        if self.upload_server: self.upload_server.stop()
        self.controller.close(); self.root.destroy()
