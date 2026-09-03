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

```bash
python tools/demo/run_all.py          # 서버 + mock + 대시보드 (하드웨어 없이)
python tools/demo/run_all.py --real   # 서버 + 의자 + 웹캠 + 대시보드
```

낱개로 띄우려면:

```bash
python server/app.py                              # 백엔드      :5000
python -m http.server 5500 --directory web/dashboard   # 대시보드 :5500
python chair/bridge/bridge.py                     # 의자
python vision/run.py --preview                    # 웹캠
```

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
