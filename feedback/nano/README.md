# Feedback Nano

`bridge.py`는 Backend의 `feedback_device` event를 받아 Nano에 다음 ASCII line을
전송합니다.

```text
LEVEL,NORMAL
LEVEL,NOTICE
LEVEL,WARNING
LEVEL,BREAK
LEVEL,OFF
ALERT,WARNING
ALERT,BREAK
```

Nano는 부팅 후 `READY`를 보내며 bridge는 마지막 `LEVEL`만 다시 보냅니다. `ALERT`는
재연결 후 재생하지 않습니다.

Production BREAK는 최초 3초 이상 파란 `LEVEL,BREAK`와 `ALERT,BREAK` 1회로 표시합니다.
Front의 logical BREAK 권고가 계속 유지되더라도 이후 Nano steady LED는 Backend의 device-only
projection에 따라 현재 Fusion state의 `NORMAL`(초록), `NOTICE`(노랑), `WARNING`(빨강)으로
복귀합니다. 이 후속 LEVEL은 `transition=false`이므로 진동을 다시 만들지 않습니다.
`ABSENT`, measurement Stop, 마지막 owner disconnect의 `LEVEL,OFF`는 즉시 우선합니다.

## 실행

`.env`에 실제 Nano 포트와 shared device token을 지정합니다.

```text
FEEDBACK_SERIAL_PORT=COM4
FEEDBACK_BAUD_RATE=9600
SOCKET_AUTH_TOKEN=...
```

```bash
python feedback/nano/bridge.py
```

실제 이벤트와 Serial 전달 경로를 진단할 때만 `--debug-events`를 추가합니다.
일반 실행 로그는 그대로 유지하며, 이 옵션은 수신 이벤트명·level·transition, 전송할
Serial command, 동일 LEVEL 여부와 연결/재연결 상태만 출력하고 token은 출력하지 않습니다.

```powershell
.\.venv-clean\Scripts\python.exe feedback\nano\bridge.py --port COM4 --debug-events
```

generic COM 자동 선택은 사용하지 않습니다. Chair UNO와 Nano 포트를 명시적으로
구분해야 합니다.

## 검증된 physical wiring

2026-10-03 physical validation에서 다음 배선을 확인했습니다.

- Arduino Nano: `COM4`
- NeoPixel data: `D13`
- NeoPixel count: `6`
- Vibration IN: `D9`
- Vibration VCC/GND: `5V` / `GND`

Firmware는 WS2812/NeoPixel 계열 LED와 Arduino IDE의 `Adafruit NeoPixel` library를
사용합니다. 색상, 밝기, 점멸 주기와 진동 시간은 별도의 hardware UX 검증 대상으로
유지합니다.

Arduino IDE에서 바로 열 수 있는 production sketch는
`firmware/feedback_nano/feedback_nano.ino`입니다.

## Physical / E2E validation 결과

2026-10-03 실제 Nano에서 production firmware와 bridge를 검증했습니다.

- Serial smoke test에서 `NORMAL` 초록/무진동, `NOTICE` 노랑/무진동,
  `WARNING` 빨강/1회 진동, `BREAK` 파랑/2회 진동, `OFF` 전체 OFF를 확인했습니다.
- Backend + Front + mock Chair + Feedback bridge E2E에서 주의 → 경고 → 휴식 권고에
  맞춰 Nano 출력이 순서대로 전환됨을 확인했습니다.
- Front에서 measurement stop 시 LED와 vibration이 모두 OFF됨을 확인했습니다.

## Backend 없는 production serial smoke test

먼저 production firmware를 Nano에 업로드합니다. Arduino Serial Monitor를 사용했다면
반드시 닫아서 `COM4`를 해제한 뒤, repository root의 PowerShell에서 다음을 실행합니다.
이 절차는 production bridge의 decision mapper와 serial transport를 그대로 사용하지만
Socket.IO/Backend 경로는 실행하지 않습니다.

```powershell
@'
import time
import serial

from feedback.nano.bridge import NanoSerialTransport, commands_for_decision

transport = NanoSerialTransport("COM4", serial_factory=serial.Serial)
sequence = (
    ({"level": "NORMAL", "transition": False}, 2.0),
    ({"level": "NOTICE", "transition": False}, 2.0),
    ({"level": "WARNING", "transition": True}, 2.0),
    ({"level": "BREAK", "transition": True}, 2.0),
    ({"level": "NORMAL", "reason": "ABSENT", "transition": True}, 1.0),
)

try:
    transport.poll()
    time.sleep(2.0)  # serial open으로 reset된 Nano의 READY를 기다립니다.
    transport.poll()
    if not transport.ready:
        raise RuntimeError("Nano READY를 받지 못했습니다. COM4와 9600 baud를 확인하세요.")
    for decision, hold_sec in sequence:
        commands = commands_for_decision(decision)
        print("send:", ", ".join(commands))
        transport.send(commands)
        time.sleep(hold_sec)
finally:
    transport.close()  # best-effort LEVEL,OFF 후 COM4를 해제합니다.
'@ | .\.venv\Scripts\python.exe -
```

예상 순서는 `NORMAL → NOTICE → WARNING(+1회 진동) → BREAK(+2회 진동) → OFF`입니다.
실행 중 문제가 생기면 `Ctrl+C`로 중단하며, `finally`에서 OFF 전송과 port close를
시도합니다. Firmware 자체만 분리 확인할 때는 9600 baud, newline 설정의 Serial
Monitor에서 위 ASCII command를 한 줄씩 보낼 수 있습니다.
