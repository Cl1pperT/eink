import http.client
import io
import tempfile
import unittest
from pathlib import Path
from threading import Event

from PIL import Image

from display_simulator.upload_server import UploadServer


class UploadServerTests(unittest.TestCase):
    def test_valid_image_upload_is_normalized_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.png"
            received = Event()
            try:
                server = UploadServer("127.0.0.1", 0, output, callback=lambda _path: received.set())
            except PermissionError:
                self.skipTest("sandbox does not permit binding a loopback port")
            server.start()
            try:
                data = io.BytesIO()
                Image.new("RGBA", (17, 9), (20, 40, 60, 120)).save(data, format="PNG")
                boundary = "simulator-test-boundary"
                body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"photo.png\"\r\n"
                        "Content-Type: image/png\r\n\r\n").encode() + data.getvalue() + f"\r\n--{boundary}--\r\n".encode()
                connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
                connection.request("POST", "/", body=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
                response = connection.getresponse(); response.read(); connection.close()
                self.assertEqual(response.status, 201)
                self.assertTrue(received.wait(1))
                with Image.open(output) as saved:
                    self.assertEqual(saved.mode, "RGB")
                    self.assertEqual(saved.size, (17, 9))
            finally:
                server.stop()
