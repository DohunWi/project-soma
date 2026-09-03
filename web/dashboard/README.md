# 대시보드

```bash
# 서버를 먼저 띄우고
python server/app.py

# 정적 서빙 (file:// 로 열면 Service Worker 가 등록되지 않습니다)
python -m http.server 5500 --directory web/dashboard
# → http://127.0.0.1:5500
```

`.env` 의 `CORS_ALLOWED_ORIGINS` 에 `http://127.0.0.1:5500` 을 넣어야 소켓이 붙습니다.

## 이전 시안과 달라진 점

`docs/reference/ui-prototype/` 의 시안은 실행되지 않았습니다.
raw WebSocket `:8000` 을 보는데 서버는 Socket.IO `:5000` 이고,
기대하던 필드(`neck_angle` 등)는 코드에 존재하지 않습니다.

여기서는 `docs/contracts/state.schema.json` 만 봅니다.

## 판정을 프론트에 넣지 마세요

점수·상태·이유는 전부 서버(`fusion/state.py`)가 정합니다.
이 화면에는 **문구와 색만** 있습니다.

이전 시안은 `POSTURE_MAP` 에 상태별 점수(GOOD 100 / BAD 30)를 하드코딩해서,
실시간 점수와 리포트 점수가 서로 다른 계산이 될 수 있었습니다.

## 알림의 한계

Service Worker + Notification API 를 씁니다.
**브라우저가 실행 중이어야 합니다** — 탭은 백그라운드여도 되고 안 보여도 되지만,
브라우저를 완전히 종료하면 뜨지 않습니다.

## 발표 전 할 것

CDN 두 개(`socket.io`, `chart.js`)를 `vendor/` 로 내려받고 `<script src>` 를 로컬 경로로 바꾸세요.
발표장 네트워크가 막히면 화면이 통째로 깨집니다. `web/dependencies.txt` 참조.
