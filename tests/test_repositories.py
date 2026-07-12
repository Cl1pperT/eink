import tempfile
import unittest
from pathlib import Path

from display_simulator.repositories import find_repository


class RepositoryDiscoveryTests(unittest.TestCase):
    def test_finds_checkout_below_integrations_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            checkout = parent / "integrations" / "inkystarmap"
            marker = checkout / "src" / "inkystarmap" / "inkystarmap.py"
            marker.parent.mkdir(parents=True)
            marker.write_text("# marker\n")
            self.assertEqual(
                find_repository(str(parent), "src/inkystarmap/inkystarmap.py", "UNUSED_TEST_REPO"),
                checkout.resolve(),
            )
