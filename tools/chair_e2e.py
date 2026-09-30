#!/usr/bin/env python3
"""Authenticated development observer for physical Chair E2E runs.

The client uses a Supabase user access token from an environment variable,
joins the matching user Socket.IO room, starts a measurement, prints ``state``
and logical ``feedback`` events, and always attempts to stop the measurement.
The token is never accepted as a CLI value and is never printed.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN_ENV = "SUPABASE_ACCESS_TOKEN"


class ChairE2EError(RuntimeError):
    """A safe user-facing observer or measurement lifecycle failure."""


def post_measurement(base_url, endpoint, token, *, opener=urlopen):
    """POST one authenticated lifecycle request without logging credentials."""
    request = Request(
        f"{base_url.rstrip('/')}{endpoint}",
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with opener(request, timeout=10) as response:
            status = response.status
            body = response.read()
    except HTTPError as error:
        status = error.code
        body = error.read()
    except URLError as error:
        raise ChairE2EError(
            f"Backend lifecycle request failed: {type(error.reason).__name__}"
        ) from error

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChairE2EError(
            f"Backend returned a non-JSON response (HTTP {status})"
        ) from error
    if not 200 <= status < 300:
        code = payload.get("error", {}).get("code", "request_failed")
        raise ChairE2EError(f"Backend rejected {endpoint}: HTTP {status} ({code})")
    return payload


def register_handlers(client, write=print, counts=None):
    """Print only public event payloads; authentication data is never included."""
    event_counts = counts if counts is not None else {"state": 0, "feedback": 0}

    def print_event(name, payload):
        event_counts[name] += 1
        write(
            f"[{name}] {json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
        )

    client.on("state", lambda payload: print_event("state", payload))
    client.on("feedback", lambda payload: print_event("feedback", payload))


def wait_for_events(client, duration_sec, *, monotonic=time.monotonic,
                    sleeper=time.sleep):
    deadline = monotonic() + duration_sec if duration_sec > 0 else None
    while client.connected and (deadline is None or monotonic() < deadline):
        sleeper(0.1)


def run_observer(
    base_url,
    token,
    *,
    duration_sec=0,
    socket_client,
    post=post_measurement,
    wait=wait_for_events,
    write=print,
):
    """Run connect/start/observe/stop/disconnect with stop in the cleanup path."""
    event_counts = {"state": 0, "feedback": 0}
    register_handlers(socket_client, write, event_counts)
    connected = False
    started = False
    stop_error = None
    try:
        socket_client.connect(base_url, auth={"token": token})
        connected = True
        write(f"[observer] authenticated Socket.IO connected: {base_url}")
        started_payload = post(base_url, "/api/measurement/start", token)
        started = True
        write(
            "[measurement-start] "
            + json.dumps(started_payload, ensure_ascii=False, sort_keys=True)
        )
        wait(socket_client, duration_sec)
        if event_counts["state"] == 0:
            write(
                "[diagnostic] no state received; verify the Chair bridge is "
                "connected without --stdout"
            )
    except KeyboardInterrupt:
        write("[observer] stop requested")
    finally:
        if started:
            try:
                stopped_payload = post(base_url, "/api/measurement/stop", token)
                write(
                    "[measurement-stop] "
                    + json.dumps(stopped_payload, ensure_ascii=False, sort_keys=True)
                )
            except ChairE2EError as error:
                stop_error = error
                write(
                    "[diagnostic] measurement stop request failed; disconnecting "
                    "the authenticated observer so Backend can release the session"
                )
        if connected and socket_client.connected:
            socket_client.disconnect()
    if stop_error is not None:
        raise stop_error


def main():
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}",
    )
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help="Supabase access token이 들어 있는 환경변수 이름",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="관찰 시간(초). 0이면 Ctrl+C까지 실행",
    )
    args = parser.parse_args()
    if args.duration < 0:
        parser.error("--duration은 0 이상이어야 합니다")
    token = os.getenv(args.token_env)
    if not token:
        parser.error(f"환경변수 {args.token_env}에 Supabase access token이 필요합니다")

    try:
        import socketio
    except ImportError:
        sys.exit("python-socketio가 없습니다: pip install -r tools/requirements.txt")

    client = socketio.Client(reconnection=True, logger=False, engineio_logger=False)
    try:
        run_observer(
            args.url,
            token,
            duration_sec=args.duration,
            socket_client=client,
        )
    except ChairE2EError as error:
        sys.exit(f"[observer] {error}")
    except Exception as error:  # Socket.IO implementations expose varied errors.
        message = str(error).replace(token, "<redacted>")
        sys.exit(f"[observer] {type(error).__name__}: {message}")


if __name__ == "__main__":
    main()
