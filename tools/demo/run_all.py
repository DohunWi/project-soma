#!/usr/bin/env python3
"""Authenticated local demo. Default: Backend + actual Front, without hardware.

Use --mock or --real/--chair/--vision explicitly. Login and Start remain manual.
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
BACKEND_PORT = 5000  # Existing Front's Backend URL.
STARTUP_TIMEOUT_SEC = 20
OPTIONAL_PROCESSES = frozenset({"chair", "vision", "nano"})


class LaunchError(RuntimeError):
    """Credential-safe startup failure."""


def load_project_env(path=ROOT / ".env"):
    from dotenv import load_dotenv
    return load_dotenv(path, override=False)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("demo", "normal"), default=None,
                        help="override SOMA_MODE (.env or demo)")
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--source-mode", choices=("all", "chair-only"), default=None)
    sources.add_argument("--chair-only", dest="source_mode", action="store_const",
                         const="chair-only", help="alias for --source-mode chair-only")
    producers = parser.add_mutually_exclusive_group()
    producers.add_argument("--mock", action="store_true", help="1 Hz / speed 1 mock")
    producers.add_argument("--real", action="store_true", help="Chair + Vision (chair-only: Chair)")
    parser.add_argument("--chair", action="store_true", help="optional Chair bridge")
    parser.add_argument("--vision", action="store_true", help="optional Vision producer")
    parser.add_argument("--nano", action="store_true", help="optional Feedback Nano bridge")
    parser.add_argument("--chair-port", help="override CHAIR_SERIAL_PORT")
    parser.add_argument("--nano-port", help="override FEEDBACK_SERIAL_PORT")
    parser.add_argument("--chair-raw-log", help="new raw Chair JSONL path")
    parser.add_argument("--mock-scenario",
                        choices=("normal", "fatigue", "imbalance", "static", "absent"),
                        default="normal")
    parser.add_argument("--front-root", type=Path, default=ROOT.with_name("project-soma-front"),
                        help="actual Front worktree root")
    parser.add_argument("--no-web", action="store_true", help="use separately managed Front")
    parser.add_argument("--web-port", type=int, default=5500)
    parser.add_argument("--check-only", action="store_true", help="validate; start nothing")
    return parser


def child_environment(args):
    """Local-launch overrides only; never write .env or change the caller's env."""
    env = os.environ.copy()
    env.update(SOMA_MODE=args.mode, SOMA_SOURCE_MODE=args.source_mode,
               SERVER_HOST="127.0.0.1", SERVER_PORT=str(BACKEND_PORT),
               CORS_ALLOWED_ORIGINS=f"http://127.0.0.1:{args.web_port}",
               PYTHONUNBUFFERED="1")
    return env


def process_specs(*, real=False, chair_only=False, nano=False, chair_raw_log=None,
                  mock=False, chair=False, vision=False, chair_port=None, nano_port=None,
                  mock_scenario="normal"):
    """Reuse production producers and integrated Backend feedback."""
    specs = [("server", [PY, "server/app.py", "--source-mode",
                         "chair-only" if chair_only else "all"])]
    if mock:
        specs.append(("mock", [PY, "tools/mock/stream.py", "--scenario", mock_scenario,
                               "--speed", "1", "--hz", "1", "--source",
                               "chair" if chair_only else "all"]))
    if real or chair:
        command = [PY, "chair/bridge/bridge.py"]
        if chair_port:
            command.extend(["--port", chair_port])
        if chair_raw_log:
            command.extend(["--raw-log", chair_raw_log])
        specs.append(("chair", command))
    if vision or (real and not chair_only):
        specs.append(("vision", [PY, "vision/run.py"]))
    if nano:
        command = [PY, "feedback/nano/bridge.py"]
        if nano_port:
            command.extend(["--port", nano_port])
        specs.append(("nano", command))
    return specs


def prepare(args):
    """Validate before any child, hardware or measurement starts."""
    args.mode = args.mode if args.mode is not None else os.getenv("SOMA_MODE", "demo")
    args.source_mode = (args.source_mode if args.source_mode is not None
                        else os.getenv("SOMA_SOURCE_MODE", "all"))
    if args.mode not in ("demo", "normal") or args.source_mode not in ("all", "chair-only"):
        raise LaunchError("SOMA_MODE 또는 SOMA_SOURCE_MODE 설정을 확인하세요.")
    if not 1 <= args.web_port <= 65535 or args.web_port == BACKEND_PORT:
        raise LaunchError("Front port는 1..65535이며 Backend 5000과 달라야 합니다.")
    if args.mock and (args.chair or args.vision):
        raise LaunchError("--mock과 실제 Chair/Vision을 함께 실행할 수 없습니다.")
    if args.source_mode == "chair-only" and args.vision:
        raise LaunchError("--vision은 --source-mode all에서 사용하세요.")
    has_chair = args.real or args.chair
    if (args.chair_port or args.chair_raw_log) and not has_chair:
        raise LaunchError("--chair-port/--chair-raw-log에는 --chair 또는 --real이 필요합니다.")
    if args.nano_port and not args.nano:
        raise LaunchError("--nano-port에는 --nano가 필요합니다.")
    chair_port = args.chair_port or os.getenv("CHAIR_SERIAL_PORT")
    nano_port = args.nano_port or os.getenv("FEEDBACK_SERIAL_PORT")
    if args.nano and not nano_port:
        raise LaunchError("--nano에는 --nano-port 또는 FEEDBACK_SERIAL_PORT가 필요합니다.")
    if has_chair and args.nano and (
        not chair_port or chair_port.casefold() == nano_port.casefold()
    ):
        raise LaunchError("Chair와 Nano를 함께 쓰면 서로 다른 명시적 serial port가 필요합니다.")
    missing = [name for name in ("SUPABASE_URL", "SOCKET_AUTH_TOKEN") if not os.getenv(name)]
    if not (os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_KEY")):
        missing.append("SUPABASE_PUBLISHABLE_KEY (or SUPABASE_KEY)")
    if missing:
        raise LaunchError("필수 환경변수 누락: " + ", ".join(missing))
    args.front_root = args.front_root.resolve()
    if not args.no_web:
        required = ("tools/serve_front.py", "web/view/login.html", "web/view/signup.html",
                    "web/view/index.html", "web/view/auth.js", "web/view/measurement_runtime.mjs",
                    "web/view/measurement_phase.mjs")
        if not all((args.front_root / name).is_file() for name in required):
            raise LaunchError("실제 Front 파일이 부족합니다. --front-root에 Front worktree를 지정하세요.")
    return process_specs(real=args.real, chair_only=args.source_mode == "chair-only",
                         nano=args.nano, chair_raw_log=args.chair_raw_log, mock=args.mock,
                         chair=args.chair, vision=args.vision, chair_port=chair_port,
                         nano_port=nano_port, mock_scenario=args.mock_scenario)


def check_port(port):
    """Never attach to or terminate an existing service."""
    with socket.socket() as probe:
        if sys.platform == "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise LaunchError(
                f"Port {port} 사용 불가: 기존 서버를 확인하고 해당 터미널에서 종료하세요."
            ) from None


def spawn(name, command, processes, env, cwd=ROOT):
    print(f"  ▶ {name}", flush=True)
    process = subprocess.Popen(command, cwd=cwd, env=env)
    processes.append((name, process))
    return process


def wait_http(process, url, *, mime=None, marker=None):
    """Bounded readiness check, including Front ES-module MIME."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LaunchError("서버가 준비 전에 종료되었습니다. 위 프로세스 로그를 확인하세요.")
        try:
            with opener.open(url, timeout=1) as response:
                body = response.read()
                if mime and response.headers.get_content_type() != mime:
                    raise LaunchError("Front .mjs MIME이 text/javascript가 아닙니다.")
                if marker and marker not in body:
                    raise LaunchError("예상한 인증 Front 로그인 페이지가 아닙니다.")
                return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.1)
    raise LaunchError("서버 준비 시간이 초과되었습니다. 포트와 의존성 오류를 확인하세요.")


def find_core_exit(processes, handled_optional):
    for name, process in processes:
        if name in handled_optional or process.poll() is None:
            continue
        if name in OPTIONAL_PROCESSES:
            handled_optional.add(name)
            print(f"[{name}] 선택 구성요소 종료됨 (code {process.returncode}); 나머지 시스템은 계속 실행합니다.")
            continue
        return name, process
    return None


def stop_processes(processes):
    """Reap only children started here, including partial startup failures."""
    for _name, process in reversed(processes):
        if process.poll() is None:
            if sys.platform == "win32":
                # Windows venv python.exe is a redirector: killing it alone leaves
                # the real interpreter/server alive. Target only our owned tree.
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, check=False,
                )
                if result.returncode and process.poll() is None:
                    process.terminate()
            else:
                process.terminate()
    for _name, process in reversed(processes):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main(argv=None):
    args = build_parser().parse_args(argv)
    processes = []
    previous_handlers = {}

    def interrupt(*_):
        raise KeyboardInterrupt

    try:
        load_project_env()
        specs = prepare(args)
        check_port(BACKEND_PORT)
        if not args.no_web:
            check_port(args.web_port)
        print(f"SOMA local demo: mode={args.mode}, source-mode={args.source_mode}")
        print("구성: " + ", ".join(name for name, _ in specs)
              + ("" if args.no_web else ", front"))
        print("로그인 → Start → CALIBRATING → READY → MEASURING. 자동 Start는 하지 않습니다.")
        if args.check_only:
            print("사전 점검 통과. 프로세스/장치/측정은 시작하지 않았습니다.")
            return 0
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, interrupt)
        env = child_environment(args)
        name, command = specs[0]
        server = spawn(name, command, processes, env)
        wait_http(server, f"http://127.0.0.1:{BACKEND_PORT}/api/health")
        if not args.no_web:
            front = spawn("front", [PY, str(args.front_root / "tools/serve_front.py"),
                                   "--host", "127.0.0.1", "--port", str(args.web_port)],
                          processes, env, cwd=args.front_root)
            front_url = f"http://127.0.0.1:{args.web_port}"
            wait_http(front, front_url + "/view/login.html", marker=b'id="loginForm"')
            wait_http(front, front_url + "/view/measurement_runtime.mjs", mime="text/javascript")
            print(f"로그인: {front_url}/view/login.html", flush=True)
        for name, command in specs[1:]:
            spawn(name, command, processes, env)
        print("종료: Front에서 Stop → Nano OFF 확인 → 이 터미널에서 Ctrl+C", flush=True)
        handled_optional = set()
        while True:
            exited = find_core_exit(processes, handled_optional)
            if exited is not None:
                raise LaunchError(f"[{exited[0]}] 종료됨 (code {exited[1].returncode}).")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n시연 프로세스 종료 중…")
        return 0
    except (LaunchError, OSError) as error:
        # OSError may include environment-derived paths; never echo its full contents.
        print(str(error) if isinstance(error, LaunchError)
              else "프로세스 실행 실패: 경로/권한을 확인하세요.", file=sys.stderr)
        return 1
    finally:
        stop_processes(processes)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
