#!/usr/bin/env python3
"""
chair/bridge/bridge.py
──────────────────────
Chair UNO → 서버 sensor_data 중계입니다.

    올려보냄:  시리얼 'D,fl,fr,bl,br,ir'  →  socket 'sensor_data'

Production 피드백은 Backend integrated policy → feedback_devices →
feedback/nano/bridge.py 경로를 사용합니다. Chair UNO 진동 수신은 이전 서버와의
호환이 꼭 필요할 때만 --legacy-vibration으로 활성화합니다.

사용법:
    python chair/bridge/bridge.py                 # .env 설정 사용
    python chair/bridge/bridge.py --port COM3
    python chair/bridge/bridge.py --port COM3 --raw-log logs/chair.jsonl
    python chair/bridge/bridge.py --list          # 포트 목록만 출력

이전 버전에서 고친 것:
  - 포트 하드코딩('COM3') 제거. .env → 자동탐색 → --port 순으로 결정합니다.
  - input() 으로 사용자 이름을 묻던 것 제거. 백그라운드·자동 실행이 불가능했습니다.
  - if py_serial.readable() 제거. pyserial 의 readable() 은 데이터 유무가 아니라
    스트림이 읽기 가능한 객체인지를 보므로 항상 True 입니다. 의미 없는 분기였습니다.
  - payload 에 t 와 source 를 넣습니다. docs/contracts/sensor_data.schema.json 참조.
    t 가 없으면 웹캠과의 시간축 정렬이 불가능합니다.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


def serial_modules():
    """Import the hardware-only dependency after CLI parsing."""
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        sys.exit("pyserial 이 없습니다.  pip install -r chair/requirements.txt")
    return serial, list_ports


def find_port(explicit=None, *, port_lister=None):
    """지정 → .env → 자동탐색 순."""
    if explicit:
        return explicit
    env = os.getenv("CHAIR_SERIAL_PORT")
    if env:
        return env

    if port_lister is None:
        _serial, list_ports = serial_modules()
        port_lister = list_ports.comports
    ports = list(port_lister())
    for p in ports:                      # 아두이노로 보이는 것 우선
        blob = f"{p.description} {p.manufacturer or ''}".lower()
        if any(k in blob for k in ("arduino", "ch340", "wch", "usb serial", "usbmodem")):
            print(f"[bridge] 포트 자동 선택: {p.device} ({p.description})", file=sys.stderr)
            return p.device
    if len(ports) == 1:
        print(f"[bridge] 포트 하나뿐이라 선택: {ports[0].device}", file=sys.stderr)
        return ports[0].device
    return None


def parse_line(line):
    """'D,900,700,1000,910,250' → ([900,700,1000,910], 250).  아니면 None."""
    if not line.startswith("D,"):
        return None                      # 부팅 메시지('# ready') 등은 무시
    parts = line[2:].split(",")
    if len(parts) < 5:
        return None
    try:
        n = [int(x.strip()) for x in parts[:5]]
    except ValueError:
        return None
    return n[:4], n[4]


def raw_log_row(t, pressure, ir):
    """Return an eval-only named raw sample without changing sensor_data."""
    return {
        "t": t,
        "fl": pressure[0],
        "fr": pressure[1],
        "bl": pressure[2],
        "br": pressure[3],
        "ir": ir,
    }


def emit_sensor_data(socket_client, event, *, debug=False, diagnostic=print):
    """Request one Socket.IO sensor emit and optionally report safe metadata."""
    socket_client.emit("sensor_data", event)
    if debug:
        diagnostic(
            "[bridge-debug] sensor_data emit requested "
            f"t={event['t']} device_id={event.get('device_id', '<none>')}"
        )


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=int(os.getenv("CHAIR_BAUD_RATE", 9600)))
    ap.add_argument("--url", default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}")
    ap.add_argument("--user", default=os.getenv("USER_NAME", "guest"))
    ap.add_argument("--device-id", default="smart_chair_01")
    ap.add_argument("--stdout", action="store_true", help="서버 없이 jsonl 출력")
    ap.add_argument(
        "--debug-events",
        action="store_true",
        help="Socket.IO sensor_data emit 요청을 stderr에 진단 출력",
    )
    ap.add_argument(
        "--raw-log",
        help="FL/FR/BL/BR/IR/t를 별도 개발용 JSONL 파일에 저장",
    )
    ap.add_argument(
        "--legacy-vibration",
        action="store_true",
        help="이전 feedback/Chair 진동 경로 활성화 (production 아님)",
    )
    ap.add_argument("--list", action="store_true", help="포트 목록만 출력하고 종료")
    args = ap.parse_args()
    serial, list_ports = serial_modules()

    if args.list:
        for p in list_ports.comports():
            print(f"{p.device}\t{p.description}")
        return

    port = find_port(args.port, port_lister=list_ports.comports)
    if not port:
        sys.exit("시리얼 포트를 찾지 못했습니다.\n"
                 "  python chair/bridge/bridge.py --list  로 확인 후\n"
                 "  .env 의 CHAIR_SERIAL_PORT 를 채우거나 --port 로 지정하세요.\n"
                 "  하드웨어가 없다면:  python tools/mock/stream.py")

    try:
        ser = serial.Serial(port=port, baudrate=args.baud, timeout=1)
    except serial.SerialException as e:
        sys.exit(f"포트를 열 수 없습니다 ({port}): {e}")
    print(f"[bridge] 아두이노 연결: {port} @ {args.baud}", file=sys.stderr)

    raw_log = None
    if args.raw_log:
        raw_log_path = Path(args.raw_log)
        raw_log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw_log = raw_log_path.open("x", encoding="utf-8", buffering=1)
        except FileExistsError:
            ser.close()
            ap.error(f"raw log 파일이 이미 존재합니다: {raw_log_path}")
        print(f"[bridge] raw log: {raw_log_path}", file=sys.stderr)

    emit = None
    if not args.stdout:
        try:
            import socketio
        except ImportError:
            sys.exit("python-socketio 가 없습니다.  pip install -r chair/requirements.txt")
        sio = socketio.Client()

        if args.legacy_vibration:
            @sio.on("feedback")
            def on_feedback(msg):
                """Legacy server → Chair vibration compatibility only."""
                if msg.get("target") != "chair_vibration":
                    return
                act = msg.get("action", {})
                code = {"off": 0, "short2": 1, "long1": 2}.get(
                    act.get("pattern"),
                    0,
                )
                level = int(act.get("intensity", 180))
                ser.write(f"V,{code},{level}\n".encode())
                print(
                    f"[bridge] legacy 진동 {act.get('pattern')} ({level})",
                    file=sys.stderr,
                )

        auth = os.getenv("SOCKET_AUTH_TOKEN")
        sio.connect(args.url, auth={"token": auth} if auth else None)
        emit = lambda ev: emit_sensor_data(
            sio,
            ev,
            debug=args.debug_events,
            diagnostic=lambda message: print(message, file=sys.stderr),
        )
        print(f"[bridge] 서버 연결: {args.url}", file=sys.stderr)
    else:
        print(
            "[bridge] --stdout mode: Socket.IO disabled; "
            "sensor_data is NOT sent to Backend",
            file=sys.stderr,
        )

    try:
        while True:
            raw = ser.readline().decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            parsed = parse_line(raw)
            if parsed is None:
                if raw.startswith("#"):
                    print(f"[arduino] {raw}", file=sys.stderr)
                continue
            pressure, ir = parsed

            sample_t = round(time.time(), 3)
            ev = {
                "v": 1,
                "t": sample_t,  # 수신 시각. 아두이노에 RTC 가 없습니다
                "source": "chair",
                "device_id": args.device_id,
                "user_name": args.user,
                "chair": {"pressure": pressure, "ir": [ir]},
            }
            if raw_log is not None:
                raw_log.write(json.dumps(
                    raw_log_row(sample_t, pressure, ir),
                    ensure_ascii=False,
                ) + "\n")
            if emit:
                emit(ev)
            else:
                print(json.dumps(ev, ensure_ascii=False), flush=True)

    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        if raw_log is not None:
            raw_log.close()
        ser.close()
        print("\n[bridge] 종료", file=sys.stderr)


if __name__ == "__main__":
    main()
