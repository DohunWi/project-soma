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

generic COM 자동 선택은 사용하지 않습니다. Chair UNO와 Nano 포트를 명시적으로
구분해야 합니다.

## Firmware 확인 필요

저장소에는 실제 완성 배선의 pin 정보가 없습니다. 업로드 전에
`firmware/feedback_nano.ino` 상단의 다음 값을 실제 배선과 맞춰야 합니다.

- `PIN_LED_DATA`
- `PIN_VIBRATION`
- `LED_COUNT`

초기 firmware는 WS2812/NeoPixel 계열 LED를 전제로 하므로 실제 LED 종류도 확인해야
합니다. Arduino IDE Library Manager에서 `Adafruit NeoPixel` library가 필요합니다. 색상,
밝기, 점멸 주기와 진동 시간은 hardware E2E 후 조정합니다.
