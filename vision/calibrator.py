"""
vision/calibrator.py
────────────────────
개인 baseline 캘리브레이션.

이전 레포의 calibrator.py 를 포팅했습니다. 구조(스레드 안전, 락 밖 파일 I/O,
시작 시 자동 로드)는 그대로 두고, 지표만 새 범위에 맞췄습니다.

  이전: head_lateral_tilt / neck_compression / head_pitch / face_width / shoulder_tilt
  지금: face_width_px / blink_rate     ← 자세 지표는 범위 밖

거리는 핀홀 근사로 구합니다.  face_width_px × distance = 상수
캘리브레이션 시점의 거리를 알면 이후 거리를 계산할 수 있습니다.
기본값 60cm 는 가정이며, 정확도가 필요하면 자로 재서 --calib-cm 으로 넘기세요.
"""
import json
import threading
import time
from pathlib import Path
from typing import Optional

CALIB_DURATION = 3.0
DEFAULT_CALIB_CM = 60.0
_METRIC_KEYS = ("face_width_px", "blink_rate")
_BASELINE_FILE = Path(__file__).parent / "baseline.json"


class Calibrator:
    """스레드 안전. 캡처 스레드가 add_sample() 을 매 프레임 호출합니다."""

    def __init__(self, baseline_path: Optional[Path] = None,
                 calib_distance_cm: float = DEFAULT_CALIB_CM):
        self._lock = threading.Lock()
        self._path = baseline_path or _BASELINE_FILE
        self._calib_cm = calib_distance_cm

        self._calibrating = False
        self._done = False
        self._start: Optional[float] = None
        self._samples: list = []
        self._baseline: dict = {}
        self._load()

    # ── 조회 ─────────────────────────────────────────────────────────
    def is_calibrating(self) -> bool:
        with self._lock:
            return self._calibrating

    def is_done(self) -> bool:
        with self._lock:
            return self._done

    def progress(self) -> float:
        with self._lock:
            if not self._calibrating or self._start is None:
                return 0.0
            return min((time.time() - self._start) / CALIB_DURATION, 1.0)

    def get_baseline(self) -> dict:
        with self._lock:
            return dict(self._baseline)

    # ── 시작 ─────────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            self._calibrating = True
            self._done = False
            self._start = time.time()
            self._samples = []
        print(f"[calib] 시작 — 평소 자세로 {CALIB_DURATION:.0f}초간 앉아주세요 "
              f"(기준 거리 {self._calib_cm:.0f}cm 가정)")

    def recalibrate(self) -> None:
        self.start()

    # ── 샘플 ─────────────────────────────────────────────────────────
    def add_sample(self, metrics: dict) -> None:
        save = False
        with self._lock:
            if not self._calibrating or self._start is None:
                return
            self._samples.append(
                {k: float(metrics[k]) for k in _METRIC_KEYS if metrics.get(k) is not None}
            )
            if time.time() - self._start >= CALIB_DURATION:
                self._finalize_locked()
                save = self._done
        if save:                      # 락 밖에서 파일 I/O — 캡처 스레드를 막지 않습니다
            self._save()

    def _finalize_locked(self) -> None:
        if not self._samples:
            print("[calib] 샘플 없음 — 실패. 얼굴이 화면에 있는지 확인하세요")
            self._calibrating = False
            return
        b = {}
        for k in _METRIC_KEYS:
            vals = [s[k] for s in self._samples if k in s]
            b[k] = sum(vals) / len(vals) if vals else 0.0
        b["calib_distance_cm"] = self._calib_cm
        self._baseline = b
        self._calibrating = False
        self._done = True
        print(f"[calib] 완료 ({len(self._samples)} 샘플)  "
              f"face_width={b['face_width_px']:.1f}px  blink_rate={b['blink_rate']:.1f}/분")

    # ── 거리 환산 ────────────────────────────────────────────────────
    def distance_cm(self, face_width_px: float) -> Optional[float]:
        """핀홀 근사.  px × cm = 상수."""
        with self._lock:
            if not self._done or not self._baseline:
                return None
            base_px = self._baseline.get("face_width_px", 0.0)
            base_cm = self._baseline.get("calib_distance_cm", DEFAULT_CALIB_CM)
        if face_width_px <= 1e-6 or base_px <= 1e-6:
            return None
        return round(base_px * base_cm / face_width_px, 1)

    # ── 파일 ─────────────────────────────────────────────────────────
    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._baseline, indent=2), encoding="utf-8")
            print(f"[calib] 저장: {self._path}")
        except OSError as e:
            print(f"[calib] 저장 실패: {e}")

    def _load(self) -> None:
        if not self._path.exists():
            print("[calib] 저장된 baseline 없음 — 캘리브레이션이 필요합니다")
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            missing = [k for k in _METRIC_KEYS if k not in data]
            if missing:
                print(f"[calib] 키 누락 {missing} — 재캘리브레이션 필요")
                return
            self._baseline = data
            self._done = True
            print(f"[calib] baseline 로드: face_width={data['face_width_px']:.1f}px "
                  f"@ {data.get('calib_distance_cm', DEFAULT_CALIB_CM):.0f}cm")
        except (OSError, json.JSONDecodeError) as e:
            print(f"[calib] 로드 실패: {e}")
