import unittest
from threading import Event
from time import sleep

from display_simulator.controller import RenderController


class FakePipeline:
    def render(self, source, context, settings, fit_mode):
        sleep(0.04)
        return source

    def accept(self, result):
        return result


class ControllerTests(unittest.TestCase):
    def test_stale_background_results_are_identifiable(self):
        controller = RenderController(FakePipeline())
        completed = []
        done = Event()

        def callback(token, future):
            completed.append((token, future.result(), controller.is_current(token)))
            if len(completed) == 2:
                done.set()

        try:
            controller.submit("old", None, None, None, callback)
            controller.submit("new", None, None, None, callback)
            self.assertTrue(done.wait(2))
            self.assertEqual(completed, [(1, "old", False), (2, "new", True)])
        finally:
            controller.close()
