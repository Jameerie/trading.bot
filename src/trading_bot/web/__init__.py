"""Web UI and JSON API.

``api`` holds the handlers as pure functions; ``server`` adds the HTTP layer.
Keeping them apart means the entire API can be tested without a socket.
"""

from .api import ApiError, ROUTES, dispatch
from .server import build_server, serve

__all__ = ["ApiError", "ROUTES", "dispatch", "build_server", "serve"]
