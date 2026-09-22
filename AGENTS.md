# AGENTS.md — Project Soma

## 1. Project Overview

Project Soma는 장시간 컴퓨터 사용자의 피로도를
의자 센서와 웹캠으로 측정하고 비침습적인 방식으로
사용자에게 피드백하는 시스템이다.

프로젝트의 전체 설계와 범위에 대한 최상위 기준은
`docs/architecture.md`이다.

설계와 코드가 충돌하는 경우 임의로 판단하지 말고
`docs/architecture.md`를 우선 확인한다.


## 2. Read Before Coding

코드를 수정하기 전에 다음 문서를 확인한다.

1. `docs/architecture.md`
2. `docs/contracts/`
3. `CONVENTIONS.md`
4. `CODEOWNERS`

특히 모듈 간 데이터 형식을 수정할 경우
반드시 `docs/contracts/`의 schema를 먼저 확인한다.


## 3. Repository Structure

- `chair/`
  - Arduino firmware
  - pressure / distance sensors
  - serial bridge

- `vision/`
  - webcam processing
  - blink detection
  - face distance measurement

- `fusion/`
  - chair + vision 데이터 융합
  - 상태 판정 로직

- `server/`
  - Flask + Socket.IO backend
  - sensor data routing
  - fusion 호출
  - DB / web 연동

- `feedback/`
  - LED
  - vibration
  - intervention policy

- `db/`
  - PostgreSQL / Supabase
  - migrations
  - reports

- `web/`
  - dashboard
  - popup notification

- `tools/`
  - mock sensor streams
  - demo scripts
  - replay / validation tools


## 4. Architecture Rules

데이터 흐름은 기본적으로 다음 구조를 따른다.

chair ─┐
       ├─> server ─> fusion ─> state
vision ┘       │
               ├─> db
               ├─> web
               └─> feedback

센서 데이터는 가능한 한 source별로 독립적으로 수집한다.

영상 자체는 backend로 전송하거나 저장하지 않는다.
vision에서는 분석된 수치만 전송한다.

상태 판정 로직은 가능한 한 `fusion/`에 유지한다.

`fusion/`은 hardware, socket, DB에 직접 의존하지 않는
순수한 분석 계층으로 유지한다.


## 5. Contracts

계층 간 데이터 형식은 `docs/contracts/`가 기준이다.

주요 계약:

- `sensor_data.schema.json`
- `state.schema.json`
- `feedback.schema.json`
- `report.schema.json`

계약에 영향을 주는 코드를 수정할 경우
관련 producer와 consumer를 함께 확인한다.

예:

vision -> server
chair -> server
server -> fusion
fusion -> server
server -> web
server -> feedback


## 6. Ownership

`CODEOWNERS`를 존중한다.

다른 담당자의 디렉터리를 수정해야 하는 경우
필요한 변경 범위를 먼저 확인한다.

특히 여러 모듈에 걸친 변경은
한 모듈만 수정하고 끝내지 말고 영향 범위를 분석한다.


## 7. Development Environment

Python:

3.10 ~ 3.12

Python 3.13은 현재 사용하지 않는다.

환경 변수는 `.env`에서 관리한다.

`.env` 및 실제 credentials를 repository에 commit하지 않는다.


## 8. Testing

가능하면 실제 hardware 없이 테스트 가능한 구조를 유지한다.

전체 mock demo:

python tools/demo/run_all.py

실제 hardware:

python tools/demo/run_all.py --real

Fusion tests:

pytest fusion/

개별 mock stream:

python tools/mock/stream.py


## 9. Coding Guidelines

기존 코드 스타일과 구조를 우선 유지한다.

불필요한 대규모 refactoring을 하지 않는다.

요청받은 문제와 직접 관련되지 않은 코드는
가능하면 수정하지 않는다.

새 dependency를 추가하기 전에
기존 dependency로 해결 가능한지 확인한다.

magic number를 새로 추가할 경우
그 값의 근거 또는 조정 가능성을 고려한다.


## 10. Before Making Changes

코드를 수정하기 전에:

1. 관련 파일을 읽는다.
2. 데이터 흐름을 확인한다.
3. 관련 contract를 확인한다.
4. 호출하는 코드와 호출되는 코드를 확인한다.
5. 변경 영향 범위를 설명한다.
6. 필요한 파일만 수정한다.


## 11. After Making Changes

수정 후:

1. 변경된 파일을 요약한다.
2. 변경 이유를 설명한다.
3. 실행 가능한 테스트가 있으면 실행한다.
4. 테스트 결과를 보고한다.
5. 테스트하지 못한 부분은 명확하게 밝힌다.
6. 다른 모듈에 미칠 수 있는 영향을 설명한다.


## 12. Safety / Security

실제 credentials, API keys, database passwords를
코드나 문서에 작성하지 않는다.

`.env.example`에는 placeholder만 사용한다.

외부 입력은 신뢰하지 않는다.

Socket.IO 및 API 입력은 가능한 경우
정의된 schema를 통해 검증한다.


## 13. Project Principle

Project Soma의 목적은 특정 자세를
"올바른 자세"라고 강제하는 것이 아니다.

핵심 목표는 사용자가 장시간 작업 중
자신의 피로 누적 상태를 인지하도록 돕는 것이다.

의료적 치료 또는 질병 예방 효과를
코드, UI, 문서에서 임의로 주장하지 않는다.
