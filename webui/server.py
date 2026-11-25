"""The web UI server: a small web server in a background thread (REQ-15).

Standard library only (http.server), so no new dependency. It serves the page
from static/ and three API routes:

    GET  /api/state     latest pack result, live settings and run state, as JSON
    GET  /api/config    the settings form built from config.yaml, as JSON
    POST /api/command   one command as JSON, for example
                        {"command": "set_soc", "value": 50}; answers its id

It only ever calls the three functions of the handle the engine gives it
(read_state, send_command, read_config), never the engine or the pack (REQ-34).
It knows nothing about any interface: the form is whatever config.yaml holds.
"""

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- 1. Constants -------------------------------------------------------------

STATIC_FOLDER = Path(__file__).parent / "static"
# The only files the server sends. The URL is looked up in this table and
# never used as a file path, so nothing outside static/ can be read.
PAGES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
MAX_BODY_BYTES = 100_000  # a Save carries every changed setting


# --- 2. Data shapes -----------------------------------------------------------


@dataclass
class WebState:
    """The running server and its thread, kept between start() and stop()."""

    server: ThreadingHTTPServer
    thread: threading.Thread


state: WebState | None = None  # None while not started


# --- 3. Answering requests ----------------------------------------------------
# http.server calls do_GET or do_POST once per request, each in its own thread.


class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/api/state":
            self.send_json(200, self.server.handle.read_state())
        elif self.path == "/api/config":
            # config.yaml may be missing or broken: answer with the reason.
            try:
                self.send_json(200, self.server.handle.read_config())
            except ValueError as error:
                self.send_json(500, {"error": str(error)})
        elif self.path in PAGES:
            file_name, content_type = PAGES[self.path]
            self.send_body(200, (STATIC_FOLDER / file_name).read_bytes(), content_type)
        else:
            self.send_json(404, {"error": f"not found: {self.path}"})

    def do_POST(self) -> None:
        if self.path != "/api/command":
            self.send_json(404, {"error": f"not found: {self.path}"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY_BYTES:
            self.send_json(400, {"error": "send one command as JSON in the body"})
            return
        try:
            command = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self.send_json(400, {"error": "the body is not valid JSON"})
            return

        # The engine checks the command and raises CommandError, a ValueError.
        # ValueError is caught (not CommandError by name) because with
        # "python -m emulator.engine" the engine runs as __main__, so its
        # CommandError is a different class object than an imported one.
        try:
            command_id = self.server.handle.send_command(command)
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        # The result appears in /api/state "actions" under this id.
        self.send_json(200, {"ok": True, "id": command_id})

    def send_json(self, status: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_body(status, body, "application/json")

    def send_body(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # always the latest values
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        """No line per request: it would bury the engine's status lines."""


# --- 4. Starting and stopping -------------------------------------------------


def start(host: str, port: int, handle) -> int:
    """Start serving in a background thread. Returns the port really used."""
    global state
    if state is not None:
        raise RuntimeError("webui.start() called twice without stop()")

    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.daemon_threads = True  # a slow browser never keeps the process alive
    server.handle = handle  # the handlers reach it as self.server.handle
    thread = threading.Thread(target=server.serve_forever, name="webui", daemon=True)
    thread.start()
    state = WebState(server=server, thread=thread)
    return server.server_address[1]


def stop() -> None:
    """Stop serving, close the port and wait for the thread to end."""
    global state
    if state is None:
        return
    state.server.shutdown()  # makes serve_forever return
    state.server.server_close()
    state.thread.join()
    state = None
