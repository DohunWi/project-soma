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
import os
import threading
import time
from pathlib import Path
from typing import Optional

CALIB_DURATION = 3.0

# 실패한 캘리브레이션을 다시 시도하기까지의 간격.
# 얼굴이 보이지 않아 실패하는 것이 정상 경로입니다 — 사람이 아직 자리에
# 앉지 않았을 뿐입니다. 실패를 최종 상태로 두면 그 실행 내내 평소값이 없습니다.
CALIB_RETRY_SEC = 5.0

_METRIC_KEYS = ("distance_cm",)

# ── 평소 깜빡임 ──────────────────────────────────────────────────────────────
# 거리와 달리 3초로는 잴 수 없습니다. 분당 빈도라서 최소 수십 초를 봐야 하고,
# BlinkCounter 는 관측 10초 미만이면 값을 내지 않습니다. 그래서 이전 구현의
# blink_rate baseline 은 **항상 0 이었고 한 번도 전송된 적이 없습니다.**
#
# 세션 초반 5분을 평소 구간으로 잡습니다. 계속 따라가게 만들면 안 됩니다 —
# 피로로 깜빡임이 줄면 baseline 도 같이 내려가 "줄었다" 가 영원히 성립하지
# 않습니다. 초반에 한 번 잡고 얼려서 세션 내내, 그리고 다음 날에도 씁니다.
# 시연·검증에서 5분을 기다릴 수 없을 때만 .env 로 줄입니다.
# 짧게 잡으면 그만큼 평소값의 표본이 적어집니다.
BLINK_BASELINE_SEC = float(os.getenv("VISION_BLINK_BASELINE_SEC", 300.0))
BLINK_MIN_SAMPLES = 60          # 2Hz 전송 기준 30초 분량

# ── 평소 거리 ────────────────────────────────────────────────────────────────
# 3초 캘리브레이션은 그 3초의 자세를 그대로 평소값으로 삼습니다. 실측에서
# 화면 쪽으로 기울어 있던 순간이 잡혀 평소 거리가 35.4cm 로 저장됐고, 실제로
# 앉는 거리는 42.6cm 였습니다. 35.4cm 는 fusion 의 근접 임계 45cm 보다
# 가까워서 "평소가 이미 위험 거리" 가 됩니다.
#
# 3초 값은 즉시 쓸 수 있으므로 남겨 두고(provisional), 세션 초반 5분의
# 중앙값으로 덮어씁니다. 깜빡임과 같은 방식입니다.
DIST_BASELINE_SEC = float(os.getenv("VISION_DIST_BASELINE_SEC", 300.0))
DIST_MIN_SAMPLES = 60
_BASELINE_FILE = Path(__file__).parent / "baseline.json"


class Calibrator:
    """스레드 안전. 캡처 스레드가 add_sample() 을 매 프레임 호출합니다."""

    def __init__(self, baseline_path: Optional[Path] = None, focal_px=None):
        self._lock = threading.Lock()
        self._path = baseline_path or _BASELINE_FILE
        # 평소 거리는 이 초점거리로 잰 값입니다. 초점거리가 바뀌면(카메라 교체,
        # 자로 다시 잼) 저장된 평소 거리는 다른 자로 잰 숫자가 됩니다.
        self._focal_px = round(float(focal_px), 1) if focal_px else None

        self._calibrating = False
        self._done = False
        self._start: Optional[float] = None
        self._samples: list = []
        self._baseline: dict = {}
        self._attempts = 0
        self._last_end: Optional[float] = None
        self._blink_samples: list = []
        self._blink_start: Optional[float] = None
        self._dist_samples: list = []
        self._dist_start: Optional[float] = None
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
        """
        세션 초반 5분의 중앙값 (add_blink_sample 참조).

        거리 캘리브레이션(_done)과 **무관합니다.** 둘은 재는 방식도 걸리는
        시간도 다릅니다. 예전에는 여기에 _done 게이트가 있어서, 거리 쪽이
        끝나지 않으면 다 모은 평소 깜빡임까지 없는 값이 됐습니다.
        """
        with self._lock:
            return self._baseline.get("blink_rate") or None

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

    def bind_focal(self, focal_px) -> None:
        """
        이 평소값이 어떤 초점거리로 잰 것인지 알려줍니다.

        카메라가 열린 뒤에야 초점거리가 정해지므로 생성 시점에는 모릅니다.
        저장된 값이 다른 초점거리로 잰 것이면 여기서 버립니다.
        """
        focal = round(float(focal_px), 1) if focal_px else None
        with self._lock:
            self._focal_px = focal
            saved = self._baseline.get("focal_px")
            stale = self._done and focal and (not saved or abs(saved - focal) > 1.0)
            if stale:
                self._baseline, self._done = {}, False
        if stale:
            how = f"f_px={saved:.0f} 로 잰 것" if saved else "초점거리를 모르는 값"
            print(f"[calib] 저장된 평소값은 {how}입니다 (지금 {focal:.0f}) — 다시 잽니다")

    # ── 평소 거리 다듬기 ─────────────────────────────────────────────
    def add_distance_sample(self, cm, now: float) -> None:
        """
        평소 거리 표본. 3초 값을 세션 초반 5분의 중앙값으로 덮어씁니다.

        3초 캘리브레이션은 하필 그때의 자세를 평소라고 부릅니다. 몸을 기울인
        순간이 잡히면 평소 거리가 실제보다 7cm 가까워지고, 그 baseline 위에서
        "평소보다 가까움" 을 판정하게 됩니다.
        """
        save = False
        with self._lock:
            if cm is None or not self._done:
                return
            if self._baseline.get("distance_refined"):
                return
            if self._dist_start is None:
                self._dist_start = now
            self._dist_samples.append(float(cm))

            elapsed = now - self._dist_start
            if elapsed >= DIST_BASELINE_SEC and len(self._dist_samples) >= DIST_MIN_SAMPLES:
                vals = sorted(self._dist_samples)
                median = round(vals[len(vals) // 2], 1)
                before = self._baseline.get("distance_cm")
                self._baseline["distance_cm"] = median
                self._baseline["distance_refined"] = True
                self._baseline["distance_samples"] = len(vals)
                print(f"[calib] 평소 거리 확정 {median:.1f}cm "
                      f"(3초 값 {before:.1f}cm → {len(vals)}표본 {elapsed / 60:.1f}분)")
                save = True
        if save:
            self._save()

    def distance_baseline_is_provisional(self) -> bool:
        """아직 3초 스냅샷이면 True. 대시보드가 '측정 중' 을 표시할 수 있습니다."""
        with self._lock:
            return self._done and not self._baseline.get("distance_refined")

    # ── 평소 깜빡임 ──────────────────────────────────────────────────
    def needs_blink_baseline(self) -> bool:
        with self._lock:
            return not self._baseline.get("blink_rate")

    def blink_baseline_progress(self) -> float:
        with self._lock:
            if self._blink_start is None:
                return 0.0
            return min(len(self._blink_samples) / BLINK_MIN_SAMPLES, 1.0)

    def add_blink_sample(self, rate, now: float) -> None:
        """
        평소 깜빡임 표본. **얼굴이 보일 때만** 넣으세요.

        자리를 비우면 빈도가 0 으로 떨어지는데 그것을 평소값에 섞으면
        기준이 통째로 내려갑니다.
        """
        save = False
        with self._lock:
            if rate is None or self._baseline.get("blink_rate"):
                return
            if self._blink_start is None:
                self._blink_start = now
                print(f"[calib] 평소 깜빡임 측정 시작 "
                      f"({BLINK_BASELINE_SEC / 60:.0f}분)")
            self._blink_samples.append(float(rate))

            elapsed = now - self._blink_start
            enough = len(self._blink_samples) >= BLINK_MIN_SAMPLES
            if elapsed >= BLINK_BASELINE_SEC and enough:
                vals = sorted(self._blink_samples)
                median = vals[len(vals) // 2]
                self._baseline["blink_rate"] = round(median, 1)
                self._baseline["blink_samples"] = len(vals)
                self._baseline["blink_measured_sec"] = round(elapsed, 1)
                print(f"[calib] 평소 깜빡임 {median:.1f}회/분 "
                      f"({len(vals)}표본, {elapsed / 60:.1f}분)")
                save = True
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
        if self._focal_px:
            b["focal_px"] = self._focal_px
        # 3초 값입니다. 세션 초반 5분의 중앙값으로 덮어쓸 때까지 임시입니다.
        b["distance_refined"] = False
        # 평소 깜빡임은 5분짜리라 거리 재측정에 딸려 버려지면 안 됩니다.
        if self._baseline.get("blink_rate"):
            b["blink_rate"] = self._baseline["blink_rate"]
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
                # 평소 깜빡임은 5분을 들여 잰 값입니다. 거리 캘리브레이션이
                # 없다고 같이 버리면 그 5분을 다시 치릅니다.
                if data.get("blink_rate"):
                    self._baseline = {"blink_rate": data["blink_rate"]}
                    print(f"[calib] 평소 깜빡임 {data['blink_rate']:.1f}회/분 은 살립니다")
                return
            saved_focal = data.get("focal_px")
            if self._focal_px and not saved_focal:
                # 초점거리를 모르는 평소값은 옛 스케일(얼굴 폭 기준)로 잰 것입니다.
                print("[calib] 저장된 평소값에 초점거리가 없습니다 — 재캘리브레이션합니다")
                return
            if self._focal_px and saved_focal and abs(saved_focal - self._focal_px) > 1.0:
                # 다른 자로 잰 숫자입니다. 이어 쓰면 "평소보다 N cm" 가 통째로
                # 어긋나는데, 값이 그럴듯해서 틀린 줄 모릅니다.
                print(f"[calib] 저장된 평소값은 f_px={saved_focal:.0f} 로 잰 것입니다 "
                      f"(지금 {self._focal_px:.0f}) — 재캘리브레이션합니다")
                return
            self._baseline = data
            self._done = True
            print(f"[calib] baseline 로드: 평소 거리 {data['distance_cm']:.1f}cm")
        except (OSError, json.JSONDecodeError) as e:
            print(f"[calib] 로드 실패: {e}")
