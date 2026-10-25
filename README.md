# Project Soma

장시간 컴퓨터 사용자의 **피로도**를 의자·웹캠 센서로 측정하고,
비침습적으로 피드백하는 시스템.

---

## 담당 = 디렉터리

**본인 폴더에서만 작업합니다.** 남의 폴더를 고쳐야 하면 그 담당자에게 말하세요.

| 디렉터리 | 담당 | 내용 |
|---|---|---|
| `chair/` | 하드웨어 | 아두이노 펌웨어, 시리얼 브릿지, 압력·적외선 분석 |
| `vision/` | 웹캠 | 외부캠 캡처, 얼굴 거리, 눈 깜빡임, 정확도 분석 |
| `fusion/` | 분석 | 여러 소스를 합쳐 상태 결정 (순수 함수) |
| `feedback/` | 피드백 | 모니터 상단 LED, 의자 진동, 개입 정책 |
| `server/` | 백엔드 | Flask + Socket.IO 중계, API |
| `db/` | DB | 스키마, 마이그레이션, 시연 데이터 보고서 |
| `web/` | 프론트 | 대시보드, 팝업 알림 |
| `tools/` | 공용 | mock 스트림, 로그 재생, 시연 스크립트 |
| `docs/contracts/` | **전원** | 계층 간 계약. 변경 시 전원 리뷰 |

---

## 시작하기

```bash
git clone <repo>
cd project-soma
cp .env.example .env                 # 값을 채워 넣으세요

python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 공통 + 본인 폴더 것만 설치합니다
pip install -r requirements.txt -r <본인폴더>/requirements.txt
```

**Python 3.10 ~ 3.12** 를 씁니다. 3.13 은 mediapipe 가 아직 지원하지 않습니다.
프론트는 `web/dependencies.txt` 를 보세요 (npm 없이 정적 HTML).

> **`.env` 는 절대 커밋하지 않습니다.** `.gitignore` 에 들어 있지만 `git add -f` 로 강제하지 마세요.
> DB 는 `postgres` 슈퍼유저 대신 앱 전용 롤을 만들어 쓰세요.

### 실행

> 인증 Front는 반드시 이 Front 작업본의 루트에서 실행합니다. Backend 저장소의
> `C:\project-soma\web`은 별도 checkout의 정적 파일이므로 수동 Feedback API 연동을
> 검증할 때 서버 루트로 사용하지 마세요.

공식 일괄 실행은 **Backend 작업본**의 실행기를 사용합니다. 이 Front 작업본의 기존
`tools/serve_front.py`를 재사용하며 `.mjs` MIME과 Supabase 인증 흐름을 유지합니다.

```powershell
Set-Location C:\project-soma
& .\.venv-clean\Scripts\python.exe tools\demo\run_all.py `
  --front-root C:\project-soma-front --mode demo --source-mode chair-only --mock
```

Mock 없이 Backend/Front만 점검하려면 `--mock`을 생략합니다. all-source 소프트웨어 점검은
`--source-mode all --mock`, 실제 Chair+Vision은 `--source-mode all --real`, Nano는 명시적으로
`--nano --nano-port COM4`를 추가합니다(현재 포트 확인 필요). Chair/Nano 동시 실행은
`--chair-port COM3`처럼 서로 다른 포트를 지정하세요. Normal은 `--mode normal`입니다.

평소 작업 자세로 준비한 뒤 브라우저에서 로그인 → Start → Backend Calibration → READY →
MEASURING 순서로 진행합니다. 측정을 자동으로 시작하거나 인증을 우회하지 않습니다. 종료는 Front Stop →
Nano OFF 확인 → 실행기 터미널 Ctrl+C입니다. 포트/환경변수/실패 복구의 공식 절차는
`C:\project-soma\README.md`의 실행 절을 따릅니다. `--check-only`는 프로세스 없이 점검합니다.

낱개로 띄우려면:

```bash
python server/app.py                              # 백엔드      :5000
python tools/serve_front.py                       # 인증 Front  :5500
python chair/bridge/bridge.py                     # 의자
python vision/run.py --preview                    # 웹캠
```

Front 개발 서버는 `web`을 문서 루트로 제공하므로 다음 주소를 사용합니다.

- 로그인: `http://127.0.0.1:5500/view/login.html`
- 회원가입: `http://127.0.0.1:5500/view/signup.html`
- 대시보드: `http://127.0.0.1:5500/view/index.html`

일반 `python -m http.server`는 Windows 환경에 따라 `.mjs`를 `text/plain`으로 제공해
브라우저 ES module 로딩을 막을 수 있습니다. `tools/serve_front.py`는 `.mjs`를
`text/javascript`로 고정하며 기본적으로 외부에 노출하지 않고 `127.0.0.1:5500`에만
바인딩합니다. 서버는 실행 중인 PowerShell에서 `Ctrl+C`로 종료합니다.

웹캠은 mediapipe 모델 파일(3.7MB)을 씁니다. 없으면 처음 실행할 때 내려받아
`vision/models/` 에 캐시합니다. **발표 전에 미리 한 번 실행해 두세요** —
발표장 네트워크가 막히면 그 자리에서 받을 수 없습니다.

### 하드웨어 없이 개발하기

**의자는 1대이고 공용공간에 있습니다.** 기다리지 말고 mock 으로 개발하세요.

```bash
python tools/mock/stream.py                  # 가짜 센서 → 서버
python tools/mock/stream.py --stdout         # 서버조차 없이 jsonl
python chair/bridge/bridge.py --stdout       # 실제 의자, 서버 없이
python vision/run.py --stdout                # 실제 웹캠, 서버 없이
```

### 테스트

```bash
pytest fusion/     # 상태 판정. 하드웨어 없이 돌아갑니다
```

---

## 데이터 흐름

```
[의자]  압력4 + 적외선 ──┐
                          ├─→ [server] ──→ [fusion] ──→ 상태
[웹캠]  거리 + 깜빡임 ──┘        │                        │
                                  ├──→ [db]  저장 · 보고서 │
                                  ├──→ [web] 대시보드 · 팝업
                                  └──→ [feedback] LED · 진동  ← 역방향
```

계층 간에 오가는 것은 전부 `docs/contracts/` 에 정의돼 있습니다.
**계약을 먼저 보고, 그다음 코드를 쓰세요.**

---

## 이번 범위

**포함**: 의자(압력·적외선), 웹캠(거리·눈 깜빡임), 피드백(LED·진동·팝업), 대시보드, 보고서

**제외** — 아키텍처 슬라이드에는 있으나 이번 반복에서는 하지 않습니다.

| 항목 | 이유 |
|---|---|
| 키보드/마우스 입력 분석 | 범위 밖 결정 |
| 웹캠 자세 판정 (거북목·측만) | 눈 깜빡임 피로도 측정으로 대체 |
| Unity 3D 아바타 | 미러링할 자세 데이터가 없어짐. 담당도 미배정 |

---

## 알려진 제약

- **팝업 알림**은 브라우저가 실행 중이어야 뜹니다 (탭은 백그라운드여도 됨).
  브라우저를 완전히 종료하면 동작하지 않습니다.
- **웹캠은 외부캠**을 씁니다. 노트북 내장캠은 각도에 예민합니다.
- 대시보드의 CDN 의존(Tailwind·Chart.js·폰트)은 **발표 전 로컬 번들로** 내려야 합니다.
