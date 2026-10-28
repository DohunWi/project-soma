# R24-3 Normal 실물 통합 검증 기록

## 목적

R20~R24에서 구현한 Normal 기본 정책과 Chair, Vision, Front, Feedback Nano의 실제 연결을
최종 확인한 기록입니다. 이 문서는 검증 결과를 보존하며 threshold나 계산식을 새로
정의하지 않습니다.

## 검증 기준

- 일자: 2026-10-28
- 실행 profile: `normal`
- source mode: `all`
- Static 정책: `cumulative-v2`
- Blink 정책: `score-v1`
- posture tracker: `clock` (cumulative-v2에 의해 자동 선택)
- Front: Supabase 로그인 후 사용자가 Start/Stop
- Calibration: Backend `CALIBRATING → READY → MEASURING`
- Feedback: Backend integrated policy → Nano bridge → production firmware

## 실물 설정

- Chair: FSR 4채널과 Chair IR
- Vision: 실제 webcam, 개인화 OPEN EAR baseline
- Feedback Nano:
  - NeoPixel data: D13
  - strip pixel 수: 6
  - 실제 점등: 사람 기준 2·3번(zero-based 1·2)
  - 밝기: 96
  - vibration input: D9
  - vibration PWM: 200
  - WARNING: 180ms 1회
  - BREAK: 180ms 2회, 중간 OFF 140ms

`.env`의 legacy `LED_COUNT=32`는 이 Nano 설정에 사용되지 않습니다.

## 결과

사용자가 실제 Chair, Vision, Front, Backend, Nano를 연결해 다음 8개 항목이 모두 통과했음을
확인했습니다.

| 항목 | 결과 |
|---|---|
| Normal이 별도 정책 override 없이 `cumulative-v2 + score-v1`을 적용 | 통과 |
| Demo가 기존 `legacy + legacy` 정책을 유지 | 통과 |
| Chair IR과 Vision을 이용한 session Calibration 및 MEASURING 전환 | 통과 |
| Static/Blink/Distance를 포함한 Score, State, Reasons 전달 | 통과 |
| 확정 ABSENT와 재착석 lifecycle | 통과 |
| Stop 및 새 measurement에서 session-scoped 상태 초기화 | 통과 |
| Front 표시와 Backend 판단 결과 동기화 | 통과 |
| Nano LEVEL, WARNING/BREAK 진동 및 ABSENT/Stop OFF | 통과 |

## 함께 유지되는 기존 실물 근거

- R5: 0.5초 및 1~2초 순간 이석 안정화
- R6: BREAK → ABSENT → NORMAL 및 Nano LED 복귀
- R7: 수동 CAUTION/DANGER/BREAK 팝업과 Nano 연동
- R12: BREAK 최초 파란 LED/2회 진동 후 현재 Fusion state LED로 복귀
- R22: 10분 Normal 통합 로그에서 cumulative-v2 감점과 Fusion 처리
- R23: 수동 blink 12회와 시스템 13회 비교

## 제한

- 한 사용자·한 Chair·현재 webcam/Nano 설치에서 수행한 engineering validation입니다.
- 의료적 기준이나 모든 사용자·장치에 대한 보편 검증이 아닙니다.
- `VISION_YAW_MAX`, Phase C 거리 임계값과 개인 Blink Rate 임계값은 이번 검증에서 변경하지
  않았습니다.
- 원본 R18~R23 실험 JSONL/로그는 Git에 포함하지 않고 로컬 비공개 자료로 보존합니다.

## 재현 실행

```powershell
Set-Location C:\project-soma

& .\.venv-clean\Scripts\python.exe .\tools\demo\run_all.py `
  --front-root C:\project-soma-front `
  --mode normal `
  --source-mode all `
  --real `
  --chair-port COM3 `
  --nano `
  --nano-port COM4
```

COM 번호는 실행 전에 Windows 장치 목록에서 다시 확인합니다. 정상 종료는 Front Stop →
Nano OFF 확인 → 실행기 터미널 `Ctrl+C` 순서입니다.
