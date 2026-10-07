#!/usr/bin/env python3
"""
tools/demo/run_all.py
─────────────────────
시연용 일괄 실행. start_system.bat 을 대체합니다 — Windows 전용이 아닙니다.

    python tools/demo/run_all.py            # 서버 + mock  (하드웨어 없이)
    python tools/demo/run_all.py --real     # 서버 + Chair + Vision
    python tools/demo/run_all.py --real --chair-only --nano
    python tools/demo/run_all.py --no-web   # 대시보드 서버 제외

Feedback Policy는 Backend에 통합되어 있습니다. 이전 standalone policy와 ambient
LED driver는 production/default 실행 경로에 포함하지 않습니다.

Ctrl+C 로 전부 종료합니다.
"""
import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
procs = []
OPTIONAL_PROCESSES = frozenset({"vision", "nano"})


def load_project_env(path=ROOT / ".env"):
    """Load project settings once so every child inherits the same values."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return load_dotenv(path, override=False)


def process_specs(*, real, chair_only, nano, chair_raw_log=None):
    """Return production process commands without legacy feedback processes."""
    specs = [("server", [PY, "server/app.py"])]
    if real:
        chair = [PY, "chair/bridge/bridge.py"]
        if chair_raw_log:
            chair.extend(["--raw-log", chair_raw_log])
        specs.append(("chair", chair))
        if not chair_only:
            specs.append(("vision", [PY, "vision/run.py"]))
    else:
        mock = [
            PY,
            "tools/mock/stream.py",
            "--scenario",
            "fatigue",
            "--speed",
            "20",
        ]
        if chair_only:
            mock.extend(["--source", "chair"])
        specs.append(("mock", mock))
    if nano:
        specs.append(("nano", [PY, "feedback/nano/bridge.py"]))
    return specs


def spawn(name, args):
    print(f"  ▶ {name}")
    p = subprocess.Popen(args, cwd=ROOT)
    procs.append((name, p))
    return p


def find_core_exit(processes, handled_optional):
    """Return an exited core process, while isolating optional failures."""
    for name, process in processes:
        if name in handled_optional:
            continue
        if process.poll() is None:
            continue
        if name in OPTIONAL_PROCESSES:
            handled_optional.add(name)
            print(
                f"[{name}] 선택 구성요소 종료됨 (code {process.returncode}); "
                "나머지 시스템은 계속 실행합니다."
            )
            continue
        return name, process
    return None


def main():
    load_project_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="mock 대신 실제 의자·웹캠")
    ap.add_argument(
        "--chair-only",
        action="store_true",
        help="Vision 없이 Chair 경로만 실행",
    )
    ap.add_argument("--no-web", action="store_true")
    ap.add_argument(
        "--nano",
        action="store_true",
        help="optional Feedback Nano bridge 실행",
    )
    ap.add_argument(
        "--chair-raw-log",
        help="실제 Chair의 FL/FR/BL/BR/IR/t JSONL 저장 경로",
    )
    ap.add_argument("--web-port", type=int, default=5500)
    args = ap.parse_args()
    if args.chair_raw_log and not args.real:
        ap.error("--chair-raw-log는 --real과 함께 사용해야 합니다")

    print("Project Soma 시연 실행")
    specs = process_specs(
        real=args.real,
        chair_only=args.chair_only,
        nano=args.nano,
        chair_raw_log=args.chair_raw_log,
    )
    server_name, server_command = specs.pop(0)
    spawn(server_name, server_command)
    time.sleep(2.5)                      # 서버가 포트를 열 때까지
    for name, command in specs:
        spawn(name, command)

    if not args.no_web:
        spawn("dashboard", [PY, "-m", "http.server", str(args.web_port),
                            "--directory", "web/dashboard"])
        print(f"\n  대시보드: http://127.0.0.1:{args.web_port}")

    print("\nCtrl+C 로 종료합니다.\n")

    def shutdown(*_):
        print("\n종료 중…")
        for name, p in procs:
            if p.poll() is None:
                p.terminate()
        for name, p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    handled_optional = set()
    while True:
        exited = find_core_exit(procs, handled_optional)
        if exited is not None:
            name, p = exited
            print(f"[{name}] 종료됨 (code {p.returncode})")
            shutdown()
        time.sleep(1)


if __name__ == "__main__":
    main()
