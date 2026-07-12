from __future__ import annotations

import argparse
import html
import io
import socket
import threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps


PAGE = b"""<!doctype html><html><head><meta name=viewport content='width=device-width,initial-scale=1'>
<title>E-Ink Frame Upload</title><style>body{font:18px system-ui;max-width:34rem;margin:12vh auto;padding:1.5rem;background:#f2efe5;color:#1b211e}
main{background:white;padding:2rem;border-radius:18px;box-shadow:0 8px 30px #0002}input,button{font:inherit;margin:.7rem 0;width:100%}button{padding:.8rem;background:#1e3528;color:white;border:0;border-radius:8px}</style></head>
<body><main><h1>Send a photo to the frame</h1><p>Choose a PNG, JPEG, or WebP image. The simulator will preview and convert it.</p>
<form method=post enctype=multipart/form-data><input required type=file name=photo accept='image/png,image/jpeg,image/webp'><button>Upload photo</button></form></main></body></html>"""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, output: Path, max_bytes: int, callback: Callable[[Path], None] | None):
        super().__init__(address, handler)
        self.output = output
        self.max_bytes = max_bytes
        self.callback = callback


class UploadHandler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, _format, *_args):
        return

    def _reply(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._reply(200, PAGE)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > self.server.max_bytes:
            self._reply(413, b"<h1>Upload too large</h1>")
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            self._reply(415, b"<h1>Expected an image upload</h1>")
            return
        raw = self.rfile.read(length)
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
        )
        part = next((item for item in message.walk()
                     if item.get_content_disposition() == "form-data"
                     and item.get_param("name", header="content-disposition") == "photo"), None)
        if part is None:
            self._reply(400, b"<h1>No photo received</h1>")
            return
        try:
            payload = part.get_payload(decode=True)
            with Image.open(io.BytesIO(payload)) as opened:
                opened.verify()
            with Image.open(io.BytesIO(payload)) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
            self.server.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.server.output.with_suffix(".tmp")
            image.save(temporary, format="PNG")
            temporary.replace(self.server.output)
        except Exception as exc:
            body = f"<h1>Invalid image</h1><p>{html.escape(str(exc))}</p>".encode()
            self._reply(400, body)
            return
        if self.server.callback:
            self.server.callback(self.server.output)
        self._reply(201, b"<h1>Photo received</h1><p>You can return to the frame.</p>")


class UploadServer:
    def __init__(self, host: str, port: int, output: Path, max_bytes: int = 20 * 1024 * 1024,
                 callback: Callable[[Path], None] | None = None):
        self.httpd = _Server((host, port), UploadHandler, output, max_bytes, callback)
        self.thread: threading.Thread | None = None
        self.host = host

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        host = self.host
        if host in ("0.0.0.0", "::"):
            try:
                host = socket.gethostbyname(socket.gethostname())
            except OSError:
                host = "127.0.0.1"
        return f"http://{host}:{self.port}/"

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="photo-upload-server", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Host the E-Ink frame's LAN photo upload page")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", type=Path, default=Path("simulator_output/latest-upload.png"))
    args = parser.parse_args(argv)
    server = UploadServer(args.host, args.port, args.output)
    server.start()
    print(f"Upload page: {server.url} (Ctrl-C to stop)")
    try:
        server.thread.join()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
