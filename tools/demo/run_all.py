#!/usr/bin/env python3
"""
tools/demo/run_all.py
─────────────────────
시연용 일괄 실행. start_system.bat 을 대체합니다 — Windows 전용이 아닙니다.

    python tools/demo/run_all.py            # 서버 + mock  (하드웨어 없이)
    python tools/demo/run_all.py --real     # 서버 + 의자 + 웹캠
    python tools/demo/run_all.py --real --nano  # Feedback Nano도 실행
    python tools/demo/run_all.py --no-web   # 대시보드 서버 제외

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
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="mock 대신 실제 의자·웹캠")
    ap.add_argument("--no-web", action="store_true")
    ap.add_argument(
        "--nano",
        action="store_true",
        help="optional Feedback Nano bridge 실행",
    )
    ap.add_argument("--web-port", type=int, default=5500)
    args = ap.parse_args()

    print("Project Soma 시연 실행")
    spawn("server", [PY, "server/app.py"])
    time.sleep(2.5)                      # 서버가 포트를 열 때까지

    if args.real:
        spawn("chair",  [PY, "chair/bridge/bridge.py"])
        spawn("vision", [PY, "vision/run.py"])
    else:
        spawn("mock", [PY, "tools/mock/stream.py", "--scenario", "fatigue", "--speed", "20"])

    spawn("policy", [PY, "feedback/policy/policy.py"])
    spawn("led",    [PY, "feedback/ambient_led/driver.py"])
    if args.nano:
        spawn("nano", [PY, "feedback/nano/bridge.py"])

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
