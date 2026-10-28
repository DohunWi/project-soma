#!/usr/bin/env python3
"""Read-only authenticated observer for live Phase C diagnostics.

This tool opts into ``cross_validation_observation`` but never calls the
measurement start/stop APIs. It does not submit sensor data or modify Fusion.
"""

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.chair_e2e import connect_authenticated_socket  # noqa: E402


DEFAULT_TOKEN_ENV = "SUPABASE_ACCESS_TOKEN"


def _display(value):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def format_observation(observation):
    """Format one Chair-authoritative Phase C observation without credentials."""
    evidence = observation.get("distance_evidence") or {}
    temporal = observation.get("distance_temporal") or {}
    state = observation.get("state") or {}
    fields = (
        ("vision", observation.get("vision_availability")),
        ("class", evidence.get("classification")),
        ("face_delta_cm", evidence.get("face_approach_delta_cm")),
        ("ir_delta_mm", evidence.get("backrest_departure_delta_mm")),
        ("candidate", temporal.get("candidate_classification")),
        ("sustained", temporal.get("sustained_classification")),
        ("accumulated_sec", temporal.get("accumulated_sec")),
        ("active", temporal.get("active")),
        ("recovery_sec", temporal.get("recovery_sec")),
        ("score", state.get("score")),
    )
    return "[phase-c-observer] " + " ".join(
        f"{name}={_display(value)}" for name, value in fields
    )


def register_handlers(client, *, write=print):
    client.on(
        "cross_validation_observation",
        lambda observation: write(format_observation(observation)),
    )


def run_observer(base_url, token, *, socket_client, wait=None, write=print):
    """Connect and wait without starting, stopping, or mutating a measurement."""
    register_handlers(socket_client, write=write)
    connect_authenticated_socket(
        socket_client,
        base_url,
        token,
        observe_sensor_data=True,
    )
    write(
        "[phase-c-observer] connected; read-only observation active "
        "(Ctrl+C to disconnect)"
    )
    write(
        "[phase-c-observer] keep the existing Front socket connected until "
        "this observer exits"
    )
    try:
        (wait or socket_client.wait)()
    except KeyboardInterrupt:
        write("[phase-c-observer] interrupted")
    finally:
        if socket_client.connected:
            socket_client.disconnect()


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Observe live Phase C evidence without starting or stopping the "
            "current measurement"
        )
    )
    parser.add_argument(
        "--url",
        default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}",
    )
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help="Supabase access token이 들어 있는 환경변수 이름",
    )
    return parser


def main(argv=None):
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    token = os.getenv(args.token_env)
    if not token:
        parser.error(f"환경변수 {args.token_env}에 Supabase access token이 필요합니다")

    try:
        import socketio
    except ImportError:
        parser.error("python-socketio가 설치되어 있지 않습니다")

    client = socketio.Client(
        reconnection=True,
        logger=False,
        engineio_logger=False,
    )
    try:
        run_observer(args.url, token, socket_client=client)
    except Exception as error:  # noqa: BLE001
        print(
            f"[phase-c-observer] connection failed ({type(error).__name__})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
