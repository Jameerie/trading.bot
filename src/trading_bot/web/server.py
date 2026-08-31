"""HTTP server for the web UI and JSON API.

Built on ``http.server`` so the package keeps its promise of running anywhere
Python 3.11 runs, with nothing to install. That trade is deliberate: a phone on
the same network can reach a laptop running this with no build step, no node,
and no container.

Security posture, since this serves over a network:

* **Binds to 127.0.0.1 by default.** Reaching it from another device is an
  explicit choice (``--host 0.0.0.0``), not the default.
* **Optional bearer token** via ``TRADING_BOT_TOKEN``. Binding to a public
  interface without one prints a warning, because the alternative is a user
  quietly exposing their setup.
* **No credentials are ever served.** The settings endpoint reports whether a
  data-provider key exists, never its value.
* **Static paths are resolved and confined** to the bundled asset directory.
* **Request bodies are capped**, so a malformed or hostile client cannot make
  the server allocate without limit.

The server still cannot place a trade, because nothing in this package can.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..config import Config
from . import api

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 256 * 1024
TOKEN_ENV = "TRADING_BOT_TOKEN"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}


def local_ip() -> str:
    """Best guess at this machine's LAN address, for the printed URL.

    Uses a UDP socket to a public address purely to ask the OS which interface
    it would route through; no packet is actually sent.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class Handler(BaseHTTPRequestHandler):
    """Routes API calls and serves the bundled UI."""

    server_version = "trading.bot"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # Injected by build_server.
    config: Config
    token: str | None

    # ---------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args) -> None:
        """One tidy line per request instead of the stdlib's noisier default."""
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  {stamp}  {self.address_string()}  {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The UI is same-origin and uses no inline event handlers, so a tight
        # policy costs nothing and blocks injected script outright.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _authorised(self) -> bool:
        """Constant-time bearer check. Open when no token is configured."""
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return secrets.compare_digest(header[7:], self.token)
        # Allows opening the UI from a link on a phone, where setting a header by
        # hand is not practical. The token still gates every request.
        query = parse_qs(urlparse(self.path).query)
        supplied = (query.get("token") or [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, self.token)

    def _read_body(self) -> dict:
        """Parse a JSON or form-encoded body, refusing oversized ones."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise api.ApiError("invalid Content-Length header")
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise api.ApiError(
                f"request body of {length} bytes exceeds the {MAX_BODY_BYTES}-byte limit", 413
            )

        raw = self.rfile.read(length)
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type == "application/x-www-form-urlencoded":
            return {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise api.ApiError(f"body is not valid JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise api.ApiError("body must be a JSON object")
        return parsed

    # ----------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if not self._authorised():
            self._json(401, {"error": "unauthorized: supply the access token"})
            return

        if path.startswith("/api/"):
            self._handle_api(path, method, parsed.query)
            return
        if method != "GET":
            self._json(405, {"error": f"{method} not allowed on {path}"})
            return
        self._serve_static(path)

    def _handle_api(self, path: str, method: str, query: str) -> None:
        params = {k: v[0] for k, v in parse_qs(query).items()}
        if method == "POST":
            try:
                params.update(self._read_body())
            except api.ApiError as exc:
                self._json(exc.status, {"error": exc.message})
                return
        status, payload = api.dispatch(path, method, params, self.config)
        self._json(status, payload)

    def _serve_static(self, path: str) -> None:
        """Serve the bundled UI, confined to the static directory."""
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        candidate = (STATIC_DIR / relative).resolve()

        # Confinement check: a resolved path outside STATIC_DIR is a traversal
        # attempt and gets the same 404 as anything else missing.
        if not candidate.is_relative_to(STATIC_DIR) or not candidate.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return

        body = candidate.read_bytes()
        content_type = CONTENT_TYPES.get(candidate.suffix, "application/octet-stream")
        # The shell must revalidate so a redeploy is picked up; the service
        # worker handles offline caching deliberately rather than by accident.
        cache = "no-cache" if candidate.suffix in (".html", ".webmanifest", ".js") else "max-age=3600"
        self._send(200, body, content_type, {"Cache-Control": cache})


def build_server(
    config: Config, host: str = "127.0.0.1", port: int = 8787, token: str | None = None
) -> ThreadingHTTPServer:
    """Create the server with its configuration bound to the handler class."""
    attrs = {"config": config, "token": token or os.environ.get(TOKEN_ENV) or None}
    handler = type("BoundHandler", (Handler,), attrs)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(
    config: Config,
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str | None = None,
    open_browser: bool = False,
) -> None:
    """Run the server until interrupted."""
    server = build_server(config, host, port, token)
    active_token = getattr(server.RequestHandlerClass, "token", None)
    suffix = f"?token={active_token}" if active_token else ""

    print("=" * 70)
    print("  trading.bot  -  advisor running")
    print("=" * 70)
    print(f"  On this machine   http://127.0.0.1:{port}/{suffix}")
    if host not in ("127.0.0.1", "localhost"):
        print(f"  On your network   http://{local_ip()}:{port}/{suffix}")
        print("                    (open this on your phone, same wifi)")
    print()
    if active_token:
        print("  Access token is set - requests without it are refused.")
    elif host not in ("127.0.0.1", "localhost"):
        print("  WARNING: bound to a public interface with no access token.")
        print(f"  Anyone who can reach this port can read your setups. Set {TOKEN_ENV}")
        print("  or bind to 127.0.0.1 and use a tunnel instead.")
    print()
    print("  This tool advises. It cannot place a trade.")
    print("  Ctrl-C to stop.")
    print("=" * 70)

    if open_browser:
        import webbrowser

        threading.Timer(
            0.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}/{suffix}")
        ).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
        server.server_close()
