"""Serve out/ with the headers it ships (so the CSP is really in force)."""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT = Path(__file__).parent.parent / "out"


def headers() -> list[tuple[str, str]]:
    lines = (OUT / "_headers").read_text().split("\n/dash")[0].splitlines()  # the /* block
    pairs = (line.strip().split(": ", 1) for line in lines if line.startswith("  "))
    return [(k, v) for k, v in pairs if k != "Strict-Transport-Security"]


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        for k, v in headers():
            self.send_header(k, v)
        super().end_headers()

    def log_message(self, *args) -> None:
        pass


def start(port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(OUT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
