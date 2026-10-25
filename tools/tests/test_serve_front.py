"""HTTP-level regression tests for the local Front development server."""

from __future__ import annotations

import http.client
import threading
from pathlib import Path

from tools.serve_front import create_server


FRONT_ROOT = Path(__file__).resolve().parents[2] / "web"


def _request(server, path):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.request("GET", path)
    response = connection.getresponse()
    content_type = response.getheader("Content-Type")
    body = response.read()
    connection.close()
    return response.status, content_type, body


def test_front_server_serves_pages_and_es_modules_with_browser_safe_mime():
    server = create_server(port=0, directory=FRONT_ROOT)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        login_status, login_content_type, login_body = _request(server, "/view/login.html")
        assert login_status == 200
        assert login_content_type.startswith("text/html")
        assert b'id="loginForm"' in login_body

        for module in (
            "auth_redirect.mjs",
            "measurement_runtime.mjs",
            "measurement_history.mjs",
        ):
            status, content_type, body = _request(server, f"/view/{module}")
            assert status == 200
            assert content_type.startswith("text/javascript")
            assert body

        js_status, js_content_type, _ = _request(server, "/view/auth.js")
        assert js_status == 200
        assert "javascript" in js_content_type
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
