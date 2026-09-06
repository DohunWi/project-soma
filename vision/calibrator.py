"""
vision/calibrator.py
────────────────────
개인 baseline — **그 사람의 평소값**을 기록합니다.

이전에는 여기서 거리의 스케일을 정했습니다. "지금 앉아 있는 이 자리가
60cm" 라고 가정하고 얼굴 폭을 재서 상수를 만들었습니다. 아무도 자로 재지
않으므로 거리 전체가 임의의 배율로 어긋났고, 같은 사람이 70cm 로도 77cm
로도 나왔습니다. fusion 의 근접 임계 45cm 는 그 위에서 판정했습니다.

**스케일은 이제 카메라가 정합니다** (vision/distance.py — 화각 + 홍채 11.7mm).
캘리브레이터는 스케일을 만들지 않고, 그 사람이 **평소 어느 거리에 앉는지**를
실제로 잽니다. 그래야 "평소보다 13cm 가까움" 이 성립합니다.
절대 임계(45cm)는 사람마다 의미가 달라지지만, 평소 대비 변화는 그렇지 않습니다.

    calib = Calibrator()
    calib.start()
    calib.add_sample({"distance_cm": 62.3, "blink_rate": 14.0})
    calib.baseline_distance_cm()      # 평소 거리
"""
import json
import threading
import time
from pathlib import Path
from typing import Optional

CALIB_DURATION = 3.0

# 실패한 캘리브레이션을 다시 시도하기까지의 간격.
# 얼굴이 보이지 않아 실패하는 것이 정상 경로입니다 — 사람이 아직 자리에
# 앉지 않았을 뿐입니다. 실패를 최종 상태로 두면 그 실행 내내 평소값이 없습니다.
CALIB_RETRY_SEC = 5.0

_METRIC_KEYS = ("distance_cm", "blink_rate")
_BASELINE_FILE = Path(__file__).parent / "baseline.json"


class Calibrator:
    """스레드 안전. 캡처 스레드가 add_sample() 을 매 프레임 호출합니다."""

    def __init__(self, baseline_path: Optional[Path] = None):
        self._lock = threading.Lock()
        self._path = baseline_path or _BASELINE_FILE

        self._calibrating = False
        self._done = False
        self._start: Optional[float] = None
        self._samples: list = []
        self._baseline: dict = {}
        self._attempts = 0
        self._last_end: Optional[float] = None
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

    # 계약에 실어 보낼 값들. "평소보다 N 만큼" 을 만들려면 평소값이 함께 가야 합니다.
    def baseline_distance_cm(self):
        with self._lock:
            return self._baseline.get("distance_cm") if self._done else None

    def baseline_blink_rate(self):
        with self._lock:
            if not self._done:
                return None
            rate = self._baseline.get("blink_rate")
            # 캘리브레이션은 3초라 창(60초)이 거의 비어 있습니다. 그 값을 평소
            # 깜빡임이라고 부르면 항상 0 에 가깝습니다. 0 이면 없는 것으로 봅니다.
            return rate if rate else None

    # ── 시작 ─────────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            self._calibrating = True
            self._done = False
            self._start = time.time()
            self._samples = []
        print(f"[calib] 시작 — 평소 자세로 {CALIB_DURATION:.0f}초간 앉아주세요 "
              f"(자로 잴 필요 없습니다. 평소값을 기록할 뿐입니다)")

    def recalibrate(self) -> None:
        self.start()

    # ── 샘플 ─────────────────────────────────────────────────────────
    def add_sample(self, metrics: dict) -> None:
        save = False
        with self._lock:
            if not self._calibrating or self._start is None:
                return
            sample = {k: float(metrics[k]) for k in _METRIC_KEYS
                      if metrics.get(k) is not None}
            # 거리가 없는 프레임은 세지 않습니다. 이전에는 빈 dict 도 샘플로
            # 쌓여서, 쓸 수 있는 값이 하나도 없어도 캘리브레이션이 "완료" 로
            # 끝났습니다. 그렇게 저장된 baseline 은 쓸 수 없는 값이라
            # 거리 판정이 영원히 조용히 실패했습니다.
            if sample.get("distance_cm"):
                self._samples.append(sample)
            if time.time() - self._start >= CALIB_DURATION:
                self._finalize_locked()
                save = self._done
        if save:                      # 락 밖에서 파일 I/O — 캡처 스레드를 막지 않습니다
            self._save()

    def tick(self, now: Optional[float] = None) -> None:
        """
        창이 끝났으면 마감합니다. **매 프레임 부르세요.**

        add_sample() 안에서만 마감을 판단하면, 얼굴이 한 번도 안 보일 때
        창이 영원히 끝나지 않습니다. add_sample() 은 정면 프레임에서만
        불리기 때문입니다. 그러면 is_calibrating() 이 True 로 굳어
        재시도도, 실패 보고도 일어나지 않습니다.
        """
        save = False
        with self._lock:
            if not self._calibrating or self._start is None:
                return
            now = time.time() if now is None else now
            if now - self._start < CALIB_DURATION:
                return
            self._finalize_locked()
            save = self._done
        if save:
            self._save()

    def should_retry(self, now: Optional[float] = None) -> bool:
        """
        다시 시도할 때가 됐는가.

        3초 창에 정면 프레임이 한 장도 없으면 캘리브레이션이 실패하는데,
        이전에는 그것으로 끝이었습니다. 실패 후 아무도 다시 시작하지 않아
        그 실행 내내 거리값이 None 이었습니다. 실패는 보통 사람이 아직
        자리에 앉지 않았다는 뜻이므로, 얼굴이 보일 때 다시 시도합니다.
        """
        now = time.time() if now is None else now
        with self._lock:
            if self._done or self._calibrating:
                return False
            if self._last_end is None:
                return True
            return now - self._last_end >= CALIB_RETRY_SEC

    def attempts(self) -> int:
        with self._lock:
            return self._attempts

    def _finalize_locked(self) -> None:
        self._attempts += 1
        self._last_end = time.time()
        if not self._samples:
            print(f"[calib] 샘플 없음 — 실패 ({self._attempts}회째). "
                  f"{CALIB_RETRY_SEC:.0f}초 뒤 얼굴이 보이면 다시 시도합니다")
            self._calibrating = False
            return
        b = {}
        for k in _METRIC_KEYS:
            vals = [s[k] for s in self._samples if k in s]
            b[k] = sum(vals) / len(vals) if vals else 0.0
        if not b.get("distance_cm"):
            print(f"[calib] 거리를 재지 못했습니다 — 실패 ({self._attempts}회째)")
            self._calibrating = False
            return
        self._baseline = b
        self._calibrating = False
        self._done = True
        print(f"[calib] 완료 ({len(self._samples)} 샘플)  "
              f"평소 거리 {b['distance_cm']:.1f}cm")

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
            print(f"[calib] baseline 로드: 평소 거리 {data['distance_cm']:.1f}cm")
        except (OSError, json.JSONDecodeError) as e:
            print(f"[calib] 로드 실패: {e}")
