#!/usr/bin/env python3
"""
feedback/ambient_led/driver.py
──────────────────────────────
모니터 상단 LED 유닛 드라이버.

**아두이노를 거치지 않습니다.** 노트북이 허브이고, LED 유닛은 별개의 USB 장치입니다.
의자 아두이노는 압력·ToF·진동만 담당합니다.
그래서 LED 전원(WS2812 32개면 최대 1.9A)과 의자→모니터 2m 배선 문제가 없습니다.

    서버 'feedback' (target=ambient_led)  →  이 프로세스  →  USB LED

부호화 (docs/contracts/feedback.schema.json):
    pos    켜진 위치   0=왼쪽 1=오른쪽   ← 좌우 균형
    width  켜진 폭     0~1              ← 전후 (앞으로 나올수록 좁아짐)
    sat    채도·밝기   0~1              ← 정적 유지·피로 누적
    pulse  강조할 축   none|pos|width|sat

백엔드는 .env 의 LED_BACKEND 로 고릅니다. 계약이 장치와 무관하므로
부품이 늦어져도 console 이나 openrgb 로 시연할 수 있습니다.

    python feedback/ambient_led/driver.py                    # .env 설정
    python feedback/ambient_led/driver.py --backend console  # 터미널에 그리기
    python feedback/ambient_led/driver.py --demo             # 서버 없이 애니메이션
"""
import argparse
import colorsys
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

HUE = 0.09          # 앰버. 빨강은 경보 의미가 강해 침입적입니다
MAX_V = 0.55        # 최대 밝기. 주변시 표시라 눈부시면 안 됩니다


# ── 부호화 → RGB 배열 ────────────────────────────────────────────────
def render(n: int, pos: float, width: float, sat: float, phase: float = 0.0,
           pulse: str = "none"):
    """
    pos/width/sat 을 n 개 LED 의 (r,g,b) 리스트로 바꿉니다.
    순수 함수 — 장치 없이 테스트됩니다.
    """
    pos   = min(max(pos, 0.0), 1.0)
    width = min(max(width, 0.0), 1.0)
    sat   = min(max(sat, 0.0), 1.0)

    # 맥동은 한 축에만. 셋이 동시에 흔들리면 무엇이 강조된 것인지 읽히지 않습니다
    k = (math.sin(phase * 2 * math.pi) + 1) / 2      # 0~1
    if pulse == "sat":
        sat = sat * (0.45 + 0.55 * k)
    elif pulse == "pos":
        pos = min(max(pos + (k - 0.5) * 0.12, 0.0), 1.0)
    elif pulse == "width":
        width = width * (0.7 + 0.3 * k)

    center = pos * (n - 1)
    half = max(width * n / 2.0, 0.6)      # 최소 한 칸은 켜지게

    out = []
    for i in range(n):
        d = abs(i - center) / half
        fall = max(0.0, 1.0 - d * d)      # 가운데가 밝고 가장자리로 갈수록 어두움
        v = MAX_V * fall
        r, g, b = colorsys.hsv_to_rgb(HUE, sat, v)
        out.append((int(r * 255), int(g * 255), int(b * 255)))
    return out


# ── 백엔드 ───────────────────────────────────────────────────────────
class ConsoleLED:
    """터미널에 그립니다. 장치 없이 부호화를 눈으로 확인할 때."""
    name = "console"

    def __init__(self, n): self.n = n

    def show(self, px):
        bar = "".join(f"\033[38;2;{r};{g};{b}m█\033[0m" for r, g, b in px)
        print(f"\r{bar}", end="", flush=True)

    def close(self):
        print()


class BlinkStickLED:
    """
    BlinkStick Flex/Strip — USB 어드레서블 LED.
    아두이노 코드가 필요 없고 USB 가 전원까지 줍니다.
        pip install blinkstick
    """
    name = "blinkstick"

    def __init__(self, n):
        from blinkstick import blinkstick
        self.dev = blinkstick.find_first()
        if self.dev is None:
            raise RuntimeError("BlinkStick 을 찾지 못했습니다")
        self.n = n

    def show(self, px):
        data = []
        for r, g, b in px:
            data += [g, r, b]           # BlinkStick 은 GRB 순서입니다
        self.dev.set_led_data(0, data)

    def close(self):
        self.dev.set_led_data(0, [0] * (self.n * 3))


class OpenRGBLED:
    """
    OpenRGB — 이미 가지고 있는 RGB 키보드·스트립을 씁니다. 부품값 0.
    OpenRGB 앱을 켜고 SDK 서버를 활성화해야 합니다.
        pip install openrgb-python
    """
    name = "openrgb"

    def __init__(self, n, host="127.0.0.1", port=6742, device_index=0):
        from openrgb import OpenRGBClient
        self.client = OpenRGBClient(host, port, "soma")
        if not self.client.devices:
            raise RuntimeError("OpenRGB 에 장치가 없습니다")
        self.dev = self.client.devices[device_index]
        self.n = min(n, len(self.dev.leds))
        print(f"[led] OpenRGB: {self.dev.name} ({len(self.dev.leds)} LEDs)", file=sys.stderr)

    def show(self, px):
        from openrgb.utils import RGBColor
        colors = [RGBColor(*px[min(i, len(px) - 1)]) for i in range(len(self.dev.leds))]
        self.dev.set_colors(colors, fast=True)

    def close(self):
        from openrgb.utils import RGBColor
        self.dev.set_colors([RGBColor(0, 0, 0)] * len(self.dev.leds))


BACKENDS = {"console": ConsoleLED, "blinkstick": BlinkStickLED, "openrgb": OpenRGBLED}


def make_backend(name, n):
    cls = BACKENDS.get(name)
    if cls is None:
        sys.exit(f"알 수 없는 LED_BACKEND: {name}  (가능: {', '.join(BACKENDS)})")
    try:
        return cls(n)
    except Exception as e:                                   # noqa: BLE001
        print(f"[led] {name} 초기화 실패 ({e}) — console 로 대체합니다", file=sys.stderr)
        return ConsoleLED(n)


# ── 실행 ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=os.getenv("LED_BACKEND", "console"),
                    choices=list(BACKENDS))
    ap.add_argument("--count", type=int, default=int(os.getenv("LED_COUNT", 32)))
    ap.add_argument("--url", default=f"http://127.0.0.1:{os.getenv('SERVER_PORT', 5000)}")
    ap.add_argument("--fps", type=float, default=20.0)
    ap.add_argument("--demo", action="store_true", help="서버 없이 애니메이션만")
    args = ap.parse_args()

    led = make_backend(args.backend, args.count)
    print(f"[led] backend={led.name} count={args.count}", file=sys.stderr)

    # 최신 목표값. 소켓 콜백이 쓰고 렌더 루프가 읽습니다
    target = {"pos": 0.5, "width": 0.7, "sat": 1.0, "pulse": "none"}

    sio = None
    if not args.demo:
        try:
            import socketio
        except ImportError:
            sys.exit("python-socketio 가 없습니다.  pip install -r feedback/requirements.txt")
        sio = socketio.Client()

        @sio.on("feedback")
        def on_feedback(msg):
            if msg.get("target") != "ambient_led":
                return
            target.update(msg.get("action", {}))

        auth = os.getenv("SOCKET_AUTH_TOKEN")
        sio.connect(args.url, auth={"token": auth} if auth else None)
        print(f"[led] 서버 연결: {args.url}", file=sys.stderr)

    t0 = time.time()
    try:
        while True:
            now = time.time()
            if args.demo:                       # 좌우로 흐르며 채도가 빠지는 데모
                e = now - t0
                target["pos"] = (math.sin(e * 0.4) + 1) / 2
                target["width"] = 0.7 - 0.4 * min(e / 30, 1.0)
                target["sat"] = 1.0 - 0.8 * min(e / 30, 1.0)
                target["pulse"] = "sat" if e > 20 else "none"

            led.show(render(args.count, target["pos"], target["width"],
                            target["sat"], phase=(now * 0.5) % 1.0,
                            pulse=target.get("pulse", "none")))
            time.sleep(1.0 / args.fps)
    except KeyboardInterrupt:
        pass
    finally:
        led.close()
        if sio:
            sio.disconnect()
        print("\n[led] 종료", file=sys.stderr)


if __name__ == "__main__":
    main()
