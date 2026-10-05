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

## 메인 Front 인증/측정 화면 (`web/view/index.html`)

위 `web/dashboard`는 별도 간이 화면입니다. 인증과 Phase C 측정 lifecycle은
`web/view/index.html`에 연결되어 있습니다. 최신 Backend (3d40f922 또는 호환 버전)가 필요합니다.

```powershell
python -m http.server 5500 --bind 127.0.0.1 --directory C:\project-soma-front\web
# http://127.0.0.1:5500/view/index.html
node --test web/tests/*.test.mjs
```

Backend의 `CORS_ALLOWED_ORIGINS`에 위 Front origin을 허용하고 기존 Supabase Auth 설정을
사용합니다. Backend URL은 기본 `http://<현재 페이지 hostname>:5000`이며,
배포 환경에서는 페이지 모듈 실행 전에 `window.SOMA_BACKEND_URL`로 지정할 수 있습니다.
HTTPS 페이지에서는 HTTPS Backend가 필요합니다. `file://`로 열지 마세요.

- 로그인 복원 후 사용자 access token으로 소켓 연결합니다. 페이지 로드 시 자동 Start는 없습니다.
- REST Start 응답은 session identity만 확정합니다. `measurement_phase`가 실제 phase 기준입니다.
- `CALIBRATING`: 정상 작업 자세 안내, 점수 `—`, 이전 현재-session 차트/cache 삭제, Stop 가능.
- `READY`: 클릭/대기 타이머 없이 `MEASURING`으로 자동 전환되는 이벤트를 기다립니다.
- `MEASURING`: 첫 `state` 전에는 데이터 수신 대기, 이후 기존 점수/카드를 표시합니다.
- `CALIBRATING + FAILED`: 센서 연결 확인 안내. 다시 준비는 **성공한 Stop → 새 Start**입니다.
- Start/Stop 요청은 직렬화합니다. 시작 요청 중 Stop을 눌러도 뒤늦은 응답이 표시를 재개하지 않습니다.
- 토큰 갱신은 REST 및 다음 소켓 handshake에 반영하며 건강한 소켓을 끊지 않습니다.
- 연결 종료/사용자 변경은 표시를 비웁니다. 재연결 시 Backend phase snapshot이 있으면 동기화하고,
  마지막 owner 종료로 세션이 정리됐으면 OFF로 남아 Start를 다시 눌러야 합니다.
- Feedback은 현재 MEASURING session의 논리적 level/reason/transition을 기존 toast에 표시합니다.

공개 `state`에는 `session_id`가 없습니다. phase gating, 연결 generation, cache 삭제,
Socket.IO lifecycle 순서로 stale state를 완화하지만 payload 자체의 완전한 session 검증은 아닙니다.
페이지 종료와 진행 중 REST 요청의 서버 처리 순서도 실제 네트워크에서 확인해야 합니다.
기존 일간 리포트/fatigue_logs/History 조회 구조는 이번 작업에서 변경하지 않았습니다.

브라우저 수동 검증: 로그인 → 준비 중 Stop → 다시 Start → READY/MEASURING → state/feedback,
센서 중단으로 calibration timeout → 다시 준비, 로그인 만료/409/네트워크 장애,
같은 사용자 두 탭/마지막 탭 refresh, token refresh 시 불필요한 disconnect 없는지 확인하세요.
Backend mock을 쓸 때 calibration은 wall-clock 기준이므로 `--speed 1`과 Chair + Vision을 사용하세요.
브라우저 웹캠 미리보기 자체는 Backend Vision producer가 아닙니다.
