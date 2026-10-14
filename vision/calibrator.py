"""
vision/calibrator.py
────────────────────
개인 baseline 캘리브레이션.

이전 레포의 calibrator.py 를 포팅했습니다. 구조(스레드 안전, 락 밖 파일 I/O,
시작 시 자동 로드)는 그대로 두고, 지표만 새 범위에 맞췄습니다.

  이전: head_lateral_tilt / neck_compression / head_pitch / face_width / shoulder_tilt
  지금: face_width_px / open_ear_baseline / blink_rate와 관측용 얼굴 위치/roll

거리는 핀홀 근사로 구합니다.  face_width_px × distance = 상수
캘리브레이션 시점의 거리를 알면 이후 거리를 계산할 수 있습니다.
기본값 60cm 는 가정이며, 정확도가 필요하면 자로 재서 --calib-cm 으로 넘기세요.
OPEN EAR은 유효 정면 프레임의 중앙값으로 만들며 blink-rate baseline과 구분합니다.
"""
import json
import math
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

CALIB_DURATION = 3.0
MIN_VALID_SAMPLES = 20
DEFAULT_CALIB_CM = 60.0
_AVERAGE_METRIC_KEYS = ("face_width_px", "blink_rate")
_LATERAL_MEDIAN_KEYS = (
    "neutral_face_center_x_ratio",
    "neutral_face_width_ratio",
    "neutral_head_roll_deg",
)
_BASELINE_FILE = Path(__file__).parent / "baseline.json"


def _calib_log(message: str) -> None:
    """Keep calibration diagnostics out of the JSONL stdout contract."""
    print(message, file=sys.stderr, flush=True)


def calibration_sample(
    *,
    face_detected: bool,
    frontal: bool,
    face_width_px: object,
    left_ear: object,
    right_ear: object,
    combined_ear: object,
    blink_rate: object = None,
    face_center_x_ratio: object = None,
    face_width_ratio: object = None,
    head_roll_deg: object = None,
) -> Optional[dict]:
    """Build one valid frontal OPEN-baseline sample, or reject the frame."""
    if not face_detected or not frontal:
        return None
    try:
        values = tuple(
            float(value)
            for value in (face_width_px, left_ear, right_ear, combined_ear)
        )
    except (TypeError, ValueError):
        return None
    width, left, right, combined = values
    if not all(math.isfinite(value) for value in values):
        return None
    if width <= 1e-6 or min(left, right, combined) <= 0:
        return None
    sample = {
        "face_width_px": width,
        "blink_rate": blink_rate,
        "open_ear": combined,
    }
    optional = {
        "neutral_face_center_x_ratio": face_center_x_ratio,
        "neutral_face_width_ratio": face_width_ratio,
        "neutral_head_roll_deg": head_roll_deg,
    }
    parsed = {}
    for key, value in optional.items():
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        parsed[key] = value
    if (
        "neutral_face_width_ratio" in parsed
        and parsed["neutral_face_width_ratio"] <= 1e-6
    ):
        return None
    sample.update(parsed)
    return sample


class Calibrator:
    """스레드 안전. 캡처 스레드가 add_sample() 을 매 프레임 호출합니다."""

    def __init__(
        self,
        baseline_path: Optional[Path] = None,
        calib_distance_cm: float = DEFAULT_CALIB_CM,
        *,
        clock: Callable[[], float] = time.time,
        duration_sec: float = CALIB_DURATION,
        min_valid_samples: int = MIN_VALID_SAMPLES,
    ):
        self._lock = threading.Lock()
        self._path = baseline_path or _BASELINE_FILE
        self._calib_cm = calib_distance_cm
        self._clock = clock
        self._duration_sec = float(duration_sec)
        self._min_valid_samples = int(min_valid_samples)
        if self._duration_sec <= 0:
            raise ValueError("duration_sec must be positive")
        if self._min_valid_samples < 2:
            raise ValueError("min_valid_samples must be at least 2")

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
            return min((self._clock() - self._start) / self._duration_sec, 1.0)

    def get_baseline(self) -> dict:
        with self._lock:
            return dict(self._baseline)

    # 계약에 실어 보낼 값들. "평소보다 N 만큼" 을 만들려면 평소값이 함께 가야 합니다.
    # 절대 거리 45cm 임계는 카메라·개인마다 다르게 나오므로, 받는 쪽이
    # baseline 대비로 판정할 수 있게 열어 둡니다 (vision/eval/README.md 참조).
    def baseline_distance_cm(self):
        with self._lock:
            if not self._done:
                return None
            return self._baseline.get("calib_distance_cm")

    def baseline_blink_rate(self):
        with self._lock:
            if not self._done:
                return None
            rate = self._baseline.get("blink_rate")
            # 캘리브레이션은 3초라 창(60초)이 거의 비어 있습니다. 그 값을 평소
            # 깜빡임이라고 부르면 항상 0 에 가깝습니다. 0 이면 없는 것으로 봅니다.
            return rate if rate else None

    def open_ear_baseline(self):
        """Return a usable OPEN EAR baseline, or None for legacy/invalid files."""
        with self._lock:
            value = self._baseline.get("open_ear_baseline")
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    def lateral_baseline(self):
        """Return personalized neutral image geometry, or None for legacy files."""
        with self._lock:
            values = {
                key: self._baseline.get(key)
                for key in _LATERAL_MEDIAN_KEYS
            }
        try:
            parsed = {key: float(value) for key, value in values.items()}
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(value) for value in parsed.values()):
            return None
        if parsed["neutral_face_width_ratio"] <= 1e-6:
            return None
        return parsed

    # ── 시작 ─────────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            self._calibrating = True
            self._done = False
            # Detector/camera startup must not consume the calibration window.
            # The first complete, valid sample starts the timer in add_sample().
            self._start = None
            self._samples = []
        _calib_log(
            f"[calib] 준비 — 정면을 보고 눈을 뜬 상태를 유지해주세요. "
            f"첫 유효 프레임부터 {self._duration_sec:.0f}초간 측정합니다 "
            f"(기준 거리 {self._calib_cm:.0f}cm 가정)"
        )

    def recalibrate(self) -> None:
        self.start()

    # ── 샘플 ─────────────────────────────────────────────────────────
    def add_sample(self, metrics: dict, *, now: Optional[float] = None) -> None:
        save = False
        with self._lock:
            if not self._calibrating:
                return
            sample = self._valid_sample(metrics)
            if sample is None:
                return
            timestamp = self._clock() if now is None else float(now)
            if self._start is None:
                self._start = timestamp
                _calib_log("[calib] 첫 유효 정면 EAR 프레임 확인 — 측정 시작")
            self._samples.append(sample)
            if timestamp - self._start >= self._duration_sec:
                save = self._finalize_locked()
        if save:                      # 락 밖에서 파일 I/O — 캡처 스레드를 막지 않습니다
            self._save()

    def tick(self, *, now: Optional[float] = None) -> None:
        """Finish at the deadline even when the current frame is invalid."""
        save = False
        with self._lock:
            if not self._calibrating or self._start is None:
                return
            timestamp = self._clock() if now is None else float(now)
            if timestamp - self._start >= self._duration_sec:
                save = self._finalize_locked()
        if save:
            self._save()

    @staticmethod
    def _valid_sample(metrics: dict) -> Optional[dict]:
        sample = {}
        for key in (*_AVERAGE_METRIC_KEYS, "open_ear", *_LATERAL_MEDIAN_KEYS):
            value = metrics.get(key)
            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                sample[key] = value
        if sample.get("face_width_px", 0.0) <= 1e-6:
            return None
        if sample.get("open_ear", 0.0) <= 0:
            return None
        return sample

    def _finalize_locked(self) -> bool:
        sample_count = len(self._samples)
        if sample_count < self._min_valid_samples:
            _calib_log(
                f"[calib] 유효 샘플 부족 ({sample_count}/{self._min_valid_samples}) "
                "— 새 baseline을 저장하지 않습니다"
            )
            self._calibrating = False
            self._done = bool(self._baseline)
            return False
        b = {}
        for k in _AVERAGE_METRIC_KEYS:
            vals = [s[k] for s in self._samples if k in s]
            b[k] = sum(vals) / len(vals) if vals else 0.0
        ear_values = [s["open_ear"] for s in self._samples]
        b["open_ear_baseline"] = statistics.median(ear_values)
        b["open_ear_sample_count"] = sample_count
        for key in _LATERAL_MEDIAN_KEYS:
            values = [sample[key] for sample in self._samples if key in sample]
            if len(values) == sample_count:
                b[key] = statistics.median(values)
        b["calib_distance_cm"] = self._calib_cm
        self._baseline = b
        self._calibrating = False
        self._done = True
        _calib_log(
            f"[calib] 완료 ({sample_count} 샘플)  "
            f"face_width={b['face_width_px']:.1f}px  "
            f"open_ear={b['open_ear_baseline']:.4f}  "
            f"blink_rate={b['blink_rate']:.1f}/분"
        )
        return True

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
            _calib_log(f"[calib] 저장: {self._path}")
        except OSError as e:
            _calib_log(f"[calib] 저장 실패: {e}")

    def _load(self) -> None:
        if not self._path.exists():
            _calib_log("[calib] 저장된 baseline 없음 — 캘리브레이션이 필요합니다")
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            missing = [k for k in _AVERAGE_METRIC_KEYS if k not in data]
            if missing:
                _calib_log(f"[calib] 키 누락 {missing} — 재캘리브레이션 필요")
                return
            self._baseline = data
            self._done = True
            _calib_log(
                f"[calib] baseline 로드: face_width={data['face_width_px']:.1f}px "
                f"@ {data.get('calib_distance_cm', DEFAULT_CALIB_CM):.0f}cm "
                f"open_ear={data.get('open_ear_baseline', 'legacy/fallback')}"
            )
        except (OSError, json.JSONDecodeError) as e:
            _calib_log(f"[calib] 로드 실패: {e}")
