from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Callable

from .models import ConversionSettings, FitMode, RenderContext, RenderResult
from .pipeline import ImagePipeline


class RenderController:
    """One-worker renderer with monotonically increasing stale-result tokens."""

    def __init__(self, pipeline: ImagePipeline | None = None) -> None:
        self.pipeline = pipeline or ImagePipeline()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="display-render")
        self._generation = 0
        self._lock = Lock()

    def submit(self, source, context: RenderContext, settings: ConversionSettings, fit_mode: FitMode,
               callback: Callable[[int, Future[RenderResult]], None]) -> int:
        with self._lock:
            self._generation += 1
            token = self._generation
        future = self.executor.submit(self.pipeline.render, source, context, settings, fit_mode)
        future.add_done_callback(lambda done: callback(token, done))
        return token

    def is_current(self, token: int) -> bool:
        with self._lock:
            return token == self._generation

    def accept(self, result: RenderResult) -> RenderResult:
        return self.pipeline.accept(result)

    def invalidate(self) -> None:
        with self._lock:
            self._generation += 1

    def close(self) -> None:
        self.invalidate()
        self.executor.shutdown(wait=False, cancel_futures=True)
