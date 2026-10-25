"""Serve the SOMA Front locally with an explicit JavaScript MIME for .mjs."""

from __future__ import annotations

import argparse
import functools
import http.server
from pathlib import Path


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5500
FRONT_ROOT = Path(__file__).resolve().parents[1] / "web"


class FrontRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Static handler that serves ES modules with a browser-safe MIME type."""

    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".mjs": "text/javascript",
    }


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    directory: Path | str = FRONT_ROOT,
) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(FrontRequestHandler, directory=str(directory))
    return http.server.ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the SOMA Front development files.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    with create_server(args.host, args.port) as server:
        print(f"SOMA Front: http://{args.host}:{server.server_port}/view/login.html")
        print("Press Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nSOMA Front stopped.")


if __name__ == "__main__":
    main()
