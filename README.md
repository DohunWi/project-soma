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

SOMA Vision은 `opencv-contrib-python==5.0.0.93`과 `mediapipe==0.10.35`를 사용합니다.
OpenCV는 `opencv-contrib-python` 하나로 통일합니다. `opencv-python`,
`opencv-contrib-python`, 두 패키지의 headless 변형은 같은 환경에 함께 설치하지 마세요.
이미 여러 OpenCV 배포판이 설치된 환경에서 복구할 때는 깨끗한 가상환경을 새로 만들고
`pip install -r requirements.txt -r vision/requirements.txt`로 설치하는 것을 권장합니다.

> **`.env` 는 절대 커밋하지 않습니다.** `.gitignore` 에 들어 있지만 `git add -f` 로 강제하지 마세요.
> DB 는 `postgres` 슈퍼유저 대신 앱 전용 롤을 만들어 쓰세요.

### 실행

공식 로컬 시연 진입점은 Backend 작업본의 `tools/demo/run_all.py`입니다.
기본 실행은 Backend와 **실제 Front 작업본**의 MIME-safe 서버만 시작하며,
Mock/Chair/Vision/Nano 및 measurement Start는 자동 실행하지 않습니다.

#### Windows 더블클릭 GUI 실행기

PowerShell이나 VS Code 없이 실행하려면 저장소 루트의 `SOMA_Launcher.bat`를
더블클릭합니다. `.venv-clean`을 우선 사용하고, 없으면 `.venv`의 `pythonw.exe`를
사용합니다. GUI에서 다음 항목을 선택할 수 있습니다.

- DEMO/NORMAL 모드. 기존 `run_all.py`의 정책 기본값을 그대로 사용합니다.
- 실제 Chair/Nano COM 포트와 Vision 카메라 번호, 또는 하드웨어 없는 Mock.
- Python 실행 파일과 실제 Front 작업본 경로.

`장치 새로고침`은 선택한 Python 환경의 pyserial/OpenCV로 현재 COM 포트와 열 수 있는
카메라 번호를 확인합니다. `사전 점검`은 경로, Python 3.10~3.12, 필수 환경변수,
Front 파일, 5000/5500 포트를 검사하되 프로세스·장치·측정을 시작하지 않습니다.
`SOMA 시작`도 같은 점검을 통과한 뒤 기존 `tools/demo/run_all.py`만 호출하며, Front 준비가
끝나면 로그인 페이지를 기본 브라우저로 엽니다. 중복 GUI 실행은 Windows named mutex로
차단합니다.

안전 종료는 **Front Stop → Nano OFF 확인 → GUI의 `프로세스 종료`** 순서입니다.
`프로세스 종료`는 인증된 Front Stop 요청을 대신하지 않으며, GUI가 생성한 종료 신호를
`run_all.py`에 전달해 그 실행기가 소유한 자식 프로세스만 정리합니다. 응답하지 않는 경우에만
15초 뒤 해당 소유 프로세스 트리를 강제 종료합니다.

EXE는 선택 사항입니다. PyInstaller가 설치된 검증 환경에서만 아래 스크립트로 빌드합니다.
생성물은 `dist/SOMA_Launcher.exe`이며, 소스 실행과 별도로 `--self-test` 및 GUI 열기 검증 후
배포해야 합니다.

```powershell
& .\tools\launcher\build_exe.ps1
```

```powershell
Set-Location C:\project-soma

# 포트/환경변수/실제 Front 파일 점검만 수행 (프로세스와 장치 시작 없음)
& .\.venv-clean\Scripts\python.exe tools\demo\run_all.py `
  --front-root C:\project-soma-front --mode demo --source-mode chair-only --check-only

# 하드웨어 없는 소프트웨어 시연: Mock을 명시적으로 선택
& .\.venv-clean\Scripts\python.exe tools\demo\run_all.py `
  --front-root C:\project-soma-front --mode demo --source-mode chair-only --mock

# Chair + Vision Mock으로 all-source Calibration 검증
& .\.venv-clean\Scripts\python.exe tools\demo\run_all.py `
  --front-root C:\project-soma-front --mode demo --source-mode all --mock

# 실제 장치 예시: Windows에서 현재 COM 번호를 확인한 후 사용
& .\.venv-clean\Scripts\python.exe tools\demo\run_all.py `
  --front-root C:\project-soma-front --mode demo --source-mode all `
  --real --chair-port COM3 --nano --nano-port COM4

# 실제 Normal 통합 측정: cumulative-v2 + score-v1이 자동 적용됨
& .\.venv-clean\Scripts\python.exe tools\demo\run_all.py `
  --front-root C:\project-soma-front --mode normal --source-mode all `
  --real --chair-port COM3 --nano --nano-port COM4
```

로그인 `http://127.0.0.1:5500/view/login.html`, 회원가입 `/view/signup.html`,
대시보드 `/view/index.html`을 사용합니다. **기준 자세 준비 → 로그인 → Start →
CALIBRATING → READY → MEASURING** 순서이며 실행기가 JWT를 받거나 인증을 우회하지 않습니다.
현재 Front는 Start 직후 Backend Calibration을 요청하므로 Start 전에 평소 작업 자세로 앉으세요.
Calibration 중에는 일반 state/score/Feedback/저장을 시작하지 않습니다. 이미 저장 설정이
있는 경우 MEASURING 이후 실제 DB 쓰기가 발생할 수 있으므로 테스트 계정으로 시작하세요.

`--mode demo|normal`은 자식 프로세스의 `SOMA_MODE`를 선택합니다. 생략하면 기존 환경/.env,
없으면 demo입니다. `--source-mode all|chair-only`도 환경/.env를 따르고 없으면 all입니다.
`--chair-only`는 같은 옵션의 별칭입니다. Normal 시연은 위 명령의 `--mode normal`로 바꿉니다.
Normal의 정책 기본값은 `cumulative-v2`/`score-v1`, Demo는 `legacy`/`legacy`입니다.
필요하면 `--static-policy legacy --blink-policy legacy`로 Normal을 이전 정책으로 실행할 수
있습니다. 우선순위는 CLI → `SOMA_STATIC_POLICY`/`SOMA_BLINK_POLICY` → profile 기본값입니다.
Demo Calibration은 최소 5초/유효 표본 5개, Normal은 8초/8개입니다. all은 Chair IR과
Vision을 모두 요구하고 chair-only는 Vision을 요구하지 않습니다. 잘못된 IR/미착석/불안정,
센서 누락 시 자동으로 다른 source mode로 전환하지 않습니다.

- 기본값: Backend + Front만 시작. 센서가 없으면 로그인/UI/페이지 로딩까지 점검 가능하며
  Start 후 Calibration 완료에는 선택한 source mode의 유효 센서/Mock이 필요합니다.
- `--mock`: 기본 normal 시나리오, 1Hz, speed 1. `--mock-scenario static`이면 고정 압력으로
  기존 정적 유지 상태 전환을 시연할 수 있습니다. fatigue는 장시간 변화를 모사하므로 짧은
  상태 전환 테스트에 적합하지 않습니다. Mock과 실제 Chair/Vision 동시 실행은 거부합니다.
- `--chair`, `--vision`, `--nano`: 각각 명시적 선택. `--real`은 Chair+Vision 묶음이며
  chair-only에서는 Chair만 실행합니다. Nano는 별도로 `--nano`를 지정합니다.
- `--chair-port`/`--nano-port`: 환경의 `CHAIR_SERIAL_PORT`/`FEEDBACK_SERIAL_PORT`보다 우선.
  Nano에는 명시적 포트가 필요하며 Chair와 함께 사용하면 서로 다른 명시적 포트가 필요합니다.
- `--chair-raw-log logs\새파일.jsonl`: `--chair` 또는 `--real`과 함께 사용합니다.
- `--no-web`: 별도 관리 중인 실제 Front를 사용할 때만 지정합니다. 오래된 dashboard로
  fallback하지 않습니다. `--front-root` 기본값은 Backend 옆 `project-soma-front`입니다.

Front는 그 작업본의 `tools/serve_front.py`를 실행해 **web 디렉터리 전체**를 루트로
제공하며 `.mjs`는 `text/javascript`로 고정합니다. 일반 `python -m http.server`나
`C:\project-soma\web/dashboard`는 공식 인증 시연 경로가 아닙니다.

### 환경변수와 로컬 네트워크

실행기는 Backend `.env`를 읽고 기존 터미널 변수를 우선합니다. 값이나 토큰을 출력하지
않으며 `.env`와 Front Auth 설정은 수정하지 않습니다. 필요한 설정:

| 변수 | 용도 |
|---|---|
| `SUPABASE_URL` | 기존 Front와 같은 Supabase 프로젝트 |
| `SUPABASE_PUBLISHABLE_KEY` 또는 기존 `SUPABASE_KEY` | Backend JWT 검증용 공개 키, service role 사용 금지 |
| `SOCKET_AUTH_TOKEN` | Chair/Vision/Mock/Nano와 Backend의 shared device 인증 |
| `DB_HOST`, `DB_USER`, `DB_PASSWORD` | 실제 state_logs 저장 시 필요, 미설정이어도 실시간 동작 가능 |
| `DB_PORT`, `DB_NAME`, `DB_SSLMODE` | 기존 DB 연결 설정 (기본값은 DBWriter 참조) |
| `CHAIR_SERIAL_PORT`, `FEEDBACK_SERIAL_PORT` | 선택한 실제 bridge의 서로 다른 포트 |
| `CHAIR_BAUD_RATE`, `FEEDBACK_BAUD_RATE` | 설치된 firmware와 동일 baud (기본 9600) |
| `WEBCAM_INDEX`, 선택적 `VISION_MODEL_PATH` | 실제 Vision 실행 시 카메라와 기존 모델 파일 |
| `SOMA_STATIC_POLICY`, `SOMA_BLINK_POLICY` | Normal을 legacy로 비교할 때만 쓰는 선택 override |
| `SOMA_POSTURE_TRACKING_MODE` | 자세 변화 진단용 선택 override (`off/shadow/clock`) |

Supabase Auth와 `state_logs` 저장은 같은 서비스 안에서도 별도 연결입니다.
`SUPABASE_URL`/publishable key는 Front 로그인과 Backend JWT 검증에 사용하고,
`DB_*`는 Backend의 PostgreSQL 앱 전용 role이 `state_logs`를 저장·조회할 때만 사용합니다.
로그인이 성공해도 DB 연결 실패 시 실시간 측정은 계속될 수 있으며, Front는 DB를 직접
조회하지 않고 인증된 Backend History/Report API만 사용합니다.

이 실행기로 생성한 자식에 한해 `SERVER_HOST=127.0.0.1`, `SERVER_PORT=5000`,
`CORS_ALLOWED_ORIGINS=http://127.0.0.1:5500`을 사용합니다. Front의 기존 Backend URL과
일치시키며 외부 네트워크에 노출하지 않습니다. `--web-port`를 바꾸면 CORS도 해당 포트로
맞추지만 Supabase Auth redirect 허용 목록은 운영자가 별도로 확인해야 하므로 5500을 권장합니다.
실행기에는 사용자 access/refresh token이 필요하지 않습니다. Supabase SDK 로그인/refresh는
기존 Front가 담당하며, Auth/CDN 접근에는 인터넷 연결이 필요합니다.

현재 노트북에서는 검증된 `.venv-clean`을 명시적으로 사용합니다. 기존 `.venv`는 보존하며
이 실행기는 환경 생성·패키지 설치·설정 수정을 수행하지 않습니다.

R24-3 실제 Chair/Vision/Nano 통합 검증 기록은
[`docs/validation/r24-3-normal-hardware.md`](docs/validation/r24-3-normal-hardware.md)에
보존합니다.

### 종료 및 실패 복구

1. 정상 종료는 **Front Stop → Nano LED/진동 OFF 확인 → 실행기 터미널 Ctrl+C** 순서입니다.
   Ctrl+C/자식 서버 오류/중간 시작 실패 시 실행기가 자신이 생성한 자식만 종료하고 회수합니다.
   프로세스 강제 종료만으로 Nano OFF나 DB queue flush가 보장되지는 않으므로 Stop을 먼저 합니다.
2. 5000/5500이 사용 중이면 새 프로세스를 만들기 전에 중단합니다. 기존 프로세스를 자동 종료하지
   않습니다. 아래 명령으로 확인하고 원래 서버 터미널에서 Ctrl+C 후 다시 실행하세요.

   ```powershell
   Get-NetTCPConnection -State Listen -LocalPort 5000,5500 |
     Select-Object LocalAddress,LocalPort,OwningProcess
   ```

3. 필수 환경변수나 Front 파일이 없으면 해당 이름/범주만 표시하고 중단합니다. 경로와 기존
   설정을 확인한 뒤 `--check-only`부터 재시도하세요. `.mjs` MIME 실패 시 실제 Front의
   `tools/serve_front.py`인지 확인하고 일반 HTTP 서버를 재사용하지 마세요.
4. Chair/Vision/Nano 종료는 선택 구성요소 오류로 알리고 Backend/Front는 유지합니다.
   필요한 센서가 없어 Calibration이 실패하면 **Stop**, 포트/모델/카메라를 확인하고 해당
   bridge를 별도 터미널에서 재실행하거나 실행기를 종료 후 재시작한 뒤 새 Start를 수행하세요.
   Mock 종료나 Backend/Front 종료는 전체 실행을 실패로 종료합니다.
5. Calibration timeout 시 Front가 표시하는 실패 사유와 유효 IR/Vision 표본을 확인하세요.
   all에서 Vision이 없다고 chair-only로 자동 변경하지 않습니다. mode/source 선택을 바꾸려면
   Stop 후 실행기를 종료하고 새 옵션으로 시작합니다. Backend 재시작은 OFF에서 시작합니다.
6. 로그인 401은 기존 Front 재로그인 흐름을 사용합니다. DB 오류는 Backend 저장 실패 로그와
   History 오류 화면으로 확인하며 실제 장치·인증·DB 실패를 Mock 성공으로 간주하지 않습니다.

Feedback Policy는 Backend에 통합되어 있습니다. Production 출력은
`Backend → feedback_devices → feedback/nano/bridge.py`이며, 이전
`feedback/policy/policy.py`, `feedback/ambient_led/driver.py`, Chair UNO 진동 경로는
기본 실행에 포함되지 않습니다.

낱개로 띄우려면:

```bash
python server/app.py                              # 백엔드      :5000
C:\project-soma\.venv-clean\Scripts\python.exe C:\project-soma-front\tools\serve_front.py  # Front :5500
python chair/bridge/bridge.py                     # 의자
python vision/run.py --preview                    # 웹캠
```

R20-4 누적 static burden은 Normal의 기본 Static 정책입니다. Normal에서 별도 옵션이
없으면 `cumulative-v2`와 posture tracker `clock`이 적용됩니다. 이전 정책과 비교해야 할
때만 명시적으로 legacy를 선택합니다.

```bash
python server/app.py --static-policy legacy
```

이때 `.env` 또는 실행 환경의 `SOMA_MODE=normal`이 필요합니다. 현재 프로젝트 `.env`가
`SOMA_MODE=demo`라면 단독 `server/app.py` 실행은 Demo가 되므로, 공식 실행기의
`--mode normal`을 사용하는 것이 안전합니다.
비교 진단은 인증된 `cross_validation_observation.static_burden`에서만 볼 수 있고 public
state/DB schema는 바뀌지 않습니다. 기존 로그의 A/B/C 오프라인 비교는 Backend나 장치를
실행하지 않고 `python tools/static_policy_compare.py <log.jsonl>`로 수행합니다.

R21-3 Blink Score `score-v1`도 Normal의 기본 Blink 정책입니다.
개인화 OPEN EAR baseline이 실제 Vision payload에 있을 때만 5분 grace 뒤 감점을 누적하고,
fallback `0.21/0.25`, stale/missing Vision, `detect_rate=0`에서는 timer와 감점을 동결합니다.

```bash
python server/app.py --blink-policy legacy
```

Normal의 두 legacy 정책을 동시에 비교할 때는 다음처럼 명시합니다.

```bash
python server/app.py --static-policy legacy --blink-policy legacy
```

부분 얼굴 검출에는 새 임계값을 적용하지 않습니다. 인증된 opt-in
`cross_validation_observation.blink_policy`에서 품질과 fallback/calibration 상태를 확인할 수
있으며 public state, DB schema, Front 계약은 추가되지 않습니다. Demo는 두 정책 모두
legacy로 고정하며 Normal 전용 조합을 거부합니다.

웹캠은 mediapipe 모델 파일(3.7MB)을 씁니다. 없으면 처음 실행할 때 내려받아
`vision/models/` 에 캐시합니다. **발표 전에 미리 한 번 실행해 두세요** —
발표장 네트워크가 막히면 그 자리에서 받을 수 없습니다.

### Chair-only Calibration

기본 Backend source mode는 `all`입니다. `run_all.py --chair-only`가 Backend에
`--source-mode chair-only`를 전달하므로 Vision 프로세스 없이 준비를 완료할 수 있습니다.
단독 Backend는 `python server/app.py --source-mode chair-only`로 실행하거나
`SOMA_SOURCE_MODE=chair-only`를 지정합니다(CLI 우선, 빈 값/잘못된 값은 오류).
Vision 누락에 따른 자동 전환은 없습니다. Demo는 5초와 유효 Chair IR 5개, Normal은
8초와 8개를 요구하며 IR이 -1이거나 미착석/불안정하면 여전히 준비가 완료되지 않습니다.
Chair-only의 얼굴 baseline은 없으며 IR만으로 Phase C 거리 감점을 만들지 않습니다.

실행기는 실제 Front를 기본 제공하며 별도로 관리할 때만 `--no-web`을 사용합니다.
Mock 준비 검증은 wall-clock 기준이므로 실행기는 `--speed 1 --hz 1`을 사용합니다.

### 하드웨어 없이 개발하기

**의자는 1대이고 공용공간에 있습니다.** 기다리지 말고 mock 으로 개발하세요.

```bash
python tools/mock/stream.py                  # 가짜 센서 → 서버
python tools/mock/stream.py --stdout         # 서버조차 없이 jsonl
python chair/bridge/bridge.py --stdout       # 실제 의자, 서버 없이
python vision/run.py --stdout                # 실제 웹캠, 서버 없이
```

`chair/bridge/bridge.py --stdout`는 센서 payload 확인 전용입니다. 이 모드에서는
Socket.IO 연결과 `sensor_data` 전송이 비활성화되므로 Chair E2E에 사용하면 안 됩니다.

### 실제 Chair E2E observer와 raw log

Front를 사용하지 않는 개발용 observer는 Supabase 사용자 access token을 환경변수로만
받습니다. Token은 출력하지 않으며 Socket.IO 연결 → measurement start → `state`와
`feedback` 출력 → measurement stop을 수행합니다.

```powershell
$env:SUPABASE_ACCESS_TOKEN = '<actual Supabase access token>'
python tools/chair_e2e.py
```

실물 Chair 값을 Backend로 보내면서 raw 채널도 함께 남기려면 다음처럼 실행합니다.
파일은 새 파일이어야 하며 `logs/`는 Git에서 제외됩니다.

```powershell
python tools/demo/run_all.py --real --chair-only --nano --no-web `
  --chair-raw-log logs/chair_raw_20260930.jsonl

# Chair bridge만 단독 실행할 때
python chair/bridge/bridge.py --port COM3 --debug-events `
  --raw-log logs/chair_raw_20260930.jsonl
```

각 JSONL row는 `t`, `fl`, `fr`, `bl`, `br`, `ir`를 가집니다. Backend 없이
contract payload를 확인하려면 기존 `--stdout`을 사용할 수 있습니다.

### 테스트

```bash
pytest fusion/     # 상태 판정. 하드웨어 없이 돌아갑니다
```

---

## 데이터 흐름

```
[Chair UNO] 압력4 + ToF ──┐
                          ├─→ [server] ──→ [fusion] ──→ 상태
[웹캠]  거리 + 깜빡임 ──┘        │                        │
                                  ├──→ [db]  저장 · 보고서 │
                                  ├──→ [web] 대시보드 · 팝업
                                  └──→ [Feedback Nano] LED · 진동
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
