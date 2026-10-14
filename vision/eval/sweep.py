#!/usr/bin/env python3
"""Evaluate absolute and subject-relative EAR thresholds on guided recordings.

Guided frame labels describe the on-screen prompt, not physiological ground
truth. Blink events are therefore matched one-to-one to cue timestamps with an
asymmetric reaction window. OPEN baselines exclude cue-response windows and
label-transition margins before robust statistics are calculated.

This module is evaluation-only. It never changes production thresholds.
"""

import argparse
import glob
import json
import statistics
from dataclasses import dataclass
from pathlib import Path

ABSOLUTE_CLOSED = 0.21
ABSOLUTE_OPEN = 0.25
MIN_CLOSED_MS = 60
MAX_CLOSED_MS = 500

DEFAULT_MATCH_EARLY_SEC = 0.10
DEFAULT_MATCH_LATE_SEC = 1.20
DEFAULT_TRANSITION_MARGIN_SEC = 0.25
DEFAULT_TRIM_FRACTION = 0.10


@dataclass(frozen=True)
class Frame:
    t: float
    left_ear: float | None
    right_ear: float | None
    combined_ear: float | None
    label: str
    face_detected: bool
    frontal: bool | None


@dataclass(frozen=True)
class BlinkEvent:
    started_at: float
    ended_at: float
    duration_ms: float


@dataclass(frozen=True)
class Simulation:
    events: tuple[BlinkEvent, ...]
    final_candidate_started_at: float | None
    longest_candidate_ms: float
    overlong_candidates: int


@dataclass(frozen=True)
class Recording:
    path: Path
    meta: dict
    frames: tuple[Frame, ...]
    cues: tuple[float, ...]

    @property
    def name(self):
        return self.path.name

    @property
    def condition(self):
        return self.meta.get("condition", "unspecified")


@dataclass(frozen=True)
class Evaluation:
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    hold_false_events: int
    candidate_stuck_at_end: bool
    longest_candidate_ms: float
    overlong_candidates: int


@dataclass(frozen=True)
class SweepRow:
    source: str
    target: str
    baseline_method: str
    baseline: float
    closed_ratio: float
    open_ratio: float
    closed_threshold: float
    open_threshold: float
    result: Evaluation


def percentile(values, probability):
    """Linearly interpolated percentile using only the standard library."""
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def trimmed_mean(values, fraction=DEFAULT_TRIM_FRACTION):
    if not values:
        raise ValueError("trimmed_mean requires at least one value")
    if not 0 <= fraction < 0.5:
        raise ValueError("trim fraction must be in [0, 0.5)")
    ordered = sorted(values)
    trim_count = int(len(ordered) * fraction)
    kept = ordered[trim_count:len(ordered) - trim_count] if trim_count else ordered
    return statistics.fmean(kept)


def summarize(values):
    if not values:
        return None
    return {
        "n": len(values),
        "min": min(values),
        "p10": percentile(values, 0.10),
        "p25": percentile(values, 0.25),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "p75": percentile(values, 0.75),
        "p90": percentile(values, 0.90),
        "max": max(values),
    }


def load_recording(path):
    meta = {}
    frames = []
    cues = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
        row_type = row.get("type")
        if row_type == "meta":
            meta = row
        elif row_type == "cue":
            cues.append(float(row["t"]))
        elif row_type == "frame":
            combined = row.get("combined_ear", row.get("ear"))
            frames.append(Frame(
                t=float(row["t"]),
                left_ear=row.get("left_ear"),
                right_ear=row.get("right_ear"),
                combined_ear=combined,
                label=row.get("label", "UNLABELED"),
                face_detected=bool(row.get("face_detected", combined is not None)),
                frontal=row.get("frontal"),
            ))
    if not frames:
        raise ValueError(f"{path}: frame rows not found")
    return Recording(Path(path), meta, tuple(frames), tuple(cues))


def label_transition_times(frames):
    return tuple(
        frame.t
        for previous, frame in zip(frames, frames[1:])
        if previous.label != frame.label
    )


def is_near_transition(t, transitions, margin_sec):
    return any(abs(t - transition) <= margin_sec for transition in transitions)


def is_in_cue_window(t, cues, early_sec, late_sec):
    return any(cue - early_sec <= t <= cue + late_sec for cue in cues)


def first_hold_start(recording):
    return next(
        (frame.t for frame in recording.frames if frame.label == "CLOSED_HOLD"),
        None,
    )


def usable_open_frames(
    recording,
    *,
    transition_margin_sec=DEFAULT_TRANSITION_MARGIN_SEC,
    match_early_sec=DEFAULT_MATCH_EARLY_SEC,
    match_late_sec=DEFAULT_MATCH_LATE_SEC,
):
    """OPEN prompt frames cleaned of reaction and phase-transition contamination."""
    transitions = label_transition_times(recording.frames)
    hold_start = first_hold_start(recording)
    return tuple(
        frame
        for frame in recording.frames
        if frame.label == "OPEN"
        # Post-hold OPEN is a recovery check, not an open-eye calibration phase.
        and (hold_start is None or frame.t < hold_start)
        and frame.face_detected
        and frame.frontal is not False
        and frame.combined_ear is not None
        and not is_near_transition(frame.t, transitions, transition_margin_sec)
        and not is_in_cue_window(
            frame.t,
            recording.cues,
            match_early_sec,
            match_late_sec,
        )
    )


def baseline_values(frames):
    values = [frame.combined_ear for frame in frames if frame.combined_ear is not None]
    if not values:
        raise ValueError("no valid OPEN frames remain for baseline")
    return {
        "median": statistics.median(values),
        "p75": percentile(values, 0.75),
        "trimmed_mean": trimmed_mean(values),
    }


def simulate(frames, closed_threshold, open_threshold):
    """Replay the production two-threshold state machine without changing it."""
    events = []
    closed_since = None
    longest_ms = 0.0
    overlong = 0
    last_t = None
    for frame in frames:
        last_t = frame.t
        ear = frame.combined_ear
        if ear is None:
            continue
        if closed_since is None:
            if ear < closed_threshold:
                closed_since = frame.t
        elif ear > open_threshold:
            duration_ms = (frame.t - closed_since) * 1000.0
            longest_ms = max(longest_ms, duration_ms)
            if MIN_CLOSED_MS <= duration_ms <= MAX_CLOSED_MS:
                events.append(BlinkEvent(closed_since, frame.t, duration_ms))
            elif duration_ms > MAX_CLOSED_MS:
                overlong += 1
            closed_since = None
    if closed_since is not None and last_t is not None:
        longest_ms = max(longest_ms, (last_t - closed_since) * 1000.0)
        if last_t - closed_since > MAX_CLOSED_MS / 1000.0:
            overlong += 1
    return Simulation(tuple(events), closed_since, longest_ms, overlong)


def match_events(cues, events, early_sec=DEFAULT_MATCH_EARLY_SEC,
                 late_sec=DEFAULT_MATCH_LATE_SEC):
    """One-to-one cue matching with a human-reaction-aware asymmetric window."""
    used = set()
    matched = 0
    for cue in sorted(cues):
        candidates = [
            (abs(event.ended_at - cue), index)
            for index, event in enumerate(events)
            if index not in used
            and cue - early_sec <= event.ended_at <= cue + late_sec
        ]
        if candidates:
            _, index = min(candidates)
            used.add(index)
            matched += 1
    return matched, len(events) - matched, len(cues) - matched


def label_intervals(frames, label):
    """Return observed contiguous intervals for one guided prompt label."""
    intervals = []
    start = previous = None
    for frame in frames:
        if frame.label == label:
            if start is None:
                start = frame.t
            previous = frame.t
        elif start is not None:
            intervals.append((start, previous))
            start = previous = None
    if start is not None:
        intervals.append((start, previous))
    return tuple(intervals)


def overlaps(start, end, intervals):
    return any(start <= interval_end and end >= interval_start
               for interval_start, interval_end in intervals)


def prf(tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall else 0.0
    )
    return precision, recall, f1


def evaluate(
    recording,
    closed_threshold,
    open_threshold,
    *,
    match_early_sec=DEFAULT_MATCH_EARLY_SEC,
    match_late_sec=DEFAULT_MATCH_LATE_SEC,
    transition_margin_sec=DEFAULT_TRANSITION_MARGIN_SEC,
):
    simulation = simulate(recording.frames, closed_threshold, open_threshold)
    tp, fp, fn = match_events(
        recording.cues,
        simulation.events,
        match_early_sec,
        match_late_sec,
    )
    precision, recall, f1 = prf(tp, fp, fn)
    hold_intervals = tuple(
        (start + transition_margin_sec, end - transition_margin_sec)
        for start, end in label_intervals(recording.frames, "CLOSED_HOLD")
        if end - start > 2 * transition_margin_sec
    )
    hold_false = sum(
        overlaps(event.started_at, event.ended_at, hold_intervals)
        for event in simulation.events
    )
    return Evaluation(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        hold_false_events=hold_false,
        candidate_stuck_at_end=simulation.final_candidate_started_at is not None,
        longest_candidate_ms=simulation.longest_candidate_ms,
        overlong_candidates=simulation.overlong_candidates,
    )


def float_range(start, stop, step):
    if step <= 0 or stop < start:
        raise ValueError("invalid sweep range")
    count = int(round((stop - start) / step))
    return tuple(round(start + index * step, 10) for index in range(count + 1))


def relative_sweep(
    source,
    target,
    *,
    closed_ratios,
    open_ratios,
    transition_margin_sec=DEFAULT_TRANSITION_MARGIN_SEC,
    match_early_sec=DEFAULT_MATCH_EARLY_SEC,
    match_late_sec=DEFAULT_MATCH_LATE_SEC,
):
    clean = usable_open_frames(
        source,
        transition_margin_sec=transition_margin_sec,
        match_early_sec=match_early_sec,
        match_late_sec=match_late_sec,
    )
    baselines = baseline_values(clean)
    rows = []
    for method, baseline in baselines.items():
        for closed_ratio in closed_ratios:
            for open_ratio in open_ratios:
                if open_ratio <= closed_ratio:
                    continue
                closed_threshold = baseline * closed_ratio
                open_threshold = baseline * open_ratio
                result = evaluate(
                    target,
                    closed_threshold,
                    open_threshold,
                    match_early_sec=match_early_sec,
                    match_late_sec=match_late_sec,
                    transition_margin_sec=transition_margin_sec,
                )
                rows.append(SweepRow(
                    source=source.condition,
                    target=target.condition,
                    baseline_method=method,
                    baseline=baseline,
                    closed_ratio=closed_ratio,
                    open_ratio=open_ratio,
                    closed_threshold=closed_threshold,
                    open_threshold=open_threshold,
                    result=result,
                ))
    return rows


def reaction_delays(recording, late_sec=DEFAULT_MATCH_LATE_SEC):
    """Estimate cue response from the local EAR minimum; it is descriptive only."""
    delays = []
    for cue in recording.cues:
        candidates = [
            frame for frame in recording.frames
            if cue <= frame.t <= cue + late_sec and frame.combined_ear is not None
        ]
        if candidates:
            minimum = min(candidates, key=lambda frame: frame.combined_ear)
            delays.append(minimum.t - cue)
    return delays


def print_summary(recording, transition_margin_sec, match_early_sec, match_late_sec):
    frames = recording.frames
    detected = sum(frame.face_detected for frame in frames)
    frontal = sum(frame.frontal is True for frame in frames)
    valid = sum(frame.combined_ear is not None for frame in frames)
    print(f"\n=== {recording.name} ({recording.condition}) ===")
    print(
        f"frames={len(frames)} face_detected={detected}/{len(frames)} "
        f"({detected / len(frames):.1%}) frontal={frontal}/{len(frames)} "
        f"({frontal / len(frames):.1%}) valid_ear={valid}/{len(frames)} "
        f"({valid / len(frames):.1%}) cues={len(recording.cues)}"
    )

    transitions = label_transition_times(frames)
    transition_count = sum(
        is_near_transition(frame.t, transitions, transition_margin_sec)
        for frame in frames
    )
    print(
        f"transition margin=±{transition_margin_sec:.2f}s: "
        f"{transition_count}/{len(frames)} frames ({transition_count / len(frames):.1%})"
    )
    print(
        "set /label/metric        n      min      p10      p25   median "
        "    mean      p75      p90      max"
    )
    for data_set in ("raw", "core"):
        for label in ("OPEN", "BLINK", "CLOSED_HOLD"):
            selected = [
                frame for frame in frames
                if frame.label == label
                and (
                    data_set == "raw"
                    or not is_near_transition(
                        frame.t,
                        transitions,
                        transition_margin_sec,
                    )
                )
            ]
            for metric in ("left_ear", "right_ear", "combined_ear"):
                stats = summarize([
                    getattr(frame, metric)
                    for frame in selected
                    if getattr(frame, metric) is not None
                ])
                if stats is None:
                    continue
                print(
                    f"{data_set:4}/{label[:6]:6}/{metric[:8]:8} {stats['n']:5d} "
                    f"{stats['min']:8.4f} {stats['p10']:8.4f} {stats['p25']:8.4f} "
                    f"{stats['median']:8.4f} {stats['mean']:8.4f} {stats['p75']:8.4f} "
                    f"{stats['p90']:8.4f} {stats['max']:8.4f}"
                )

    raw_open = [
        frame.combined_ear for frame in frames
        if frame.label == "OPEN" and frame.combined_ear is not None
    ]
    clean_open = usable_open_frames(
        recording,
        transition_margin_sec=transition_margin_sec,
        match_early_sec=match_early_sec,
        match_late_sec=match_late_sec,
    )
    cleaned_values = [frame.combined_ear for frame in clean_open]
    print(
        f"OPEN cleanup: raw={len(raw_open)} clean={len(cleaned_values)} "
        f"raw_median={statistics.median(raw_open):.4f} "
        f"clean_median={statistics.median(cleaned_values):.4f}"
    )
    for method, value in baseline_values(clean_open).items():
        print(f"  baseline {method:12}={value:.5f}")
    hold_start = first_hold_start(recording)
    recovery = [
        frame.combined_ear
        for frame in frames
        if hold_start is not None
        and frame.t > hold_start
        and frame.label == "OPEN"
        and frame.combined_ear is not None
        and not is_near_transition(frame.t, transitions, transition_margin_sec)
    ]
    if recovery:
        print(
            f"recovery OPEN prompt (not baseline): n={len(recovery)} "
            f"median={statistics.median(recovery):.4f} "
            f"p90={percentile(recovery, 0.90):.4f} max={max(recovery):.4f}"
        )
    delays = reaction_delays(recording, match_late_sec)
    print(
        f"cue→local-min delay: n={len(delays)} min={min(delays):.3f}s "
        f"median={statistics.median(delays):.3f}s max={max(delays):.3f}s"
    )


def print_absolute(recording, early_sec, late_sec, transition_margin_sec):
    result = evaluate(
        recording,
        ABSOLUTE_CLOSED,
        ABSOLUTE_OPEN,
        match_early_sec=early_sec,
        match_late_sec=late_sec,
        transition_margin_sec=transition_margin_sec,
    )
    print(
        f"{recording.condition:12} TP={result.tp:2d} FP={result.fp:2d} FN={result.fn:2d} "
        f"P={result.precision:.3f} R={result.recall:.3f} F1={result.f1:.3f} "
        f"hold_false={result.hold_false_events} "
        f"candidate_stuck_at_end={result.candidate_stuck_at_end} "
        f"longest_candidate={result.longest_candidate_ms:.0f}ms "
        f"overlong={result.overlong_candidates}"
    )


def row_rank(row):
    result = row.result
    return (
        result.f1,
        -result.hold_false_events,
        -int(result.candidate_stuck_at_end),
        result.precision,
        result.recall,
        -result.overlong_candidates,
    )


def print_sweep_group(rows, top):
    rows = sorted(rows, key=row_rank, reverse=True)
    first = rows[0]
    print(
        f"\nsource={first.source} → target={first.target} "
        f"baseline={first.baseline_method} ({first.baseline:.5f})"
    )
    print(" c_ratio  o_ratio   closed     open  TP FP FN     P     R    F1 hold stuck")
    for row in rows[:top]:
        result = row.result
        print(
            f" {row.closed_ratio:7.3f} {row.open_ratio:7.3f} "
            f"{row.closed_threshold:8.4f} {row.open_threshold:8.4f} "
            f"{result.tp:3d} {result.fp:2d} {result.fn:2d} "
            f"{result.precision:5.3f} {result.recall:5.3f} {result.f1:5.3f} "
            f"{result.hold_false_events:4d} {str(result.candidate_stuck_at_end):>5}"
        )
    best_f1 = rows[0].result.f1
    best = [
        row for row in rows
        if abs(row.result.f1 - best_f1) < 1e-12
        and row.result.hold_false_events == 0
        and not row.result.candidate_stuck_at_end
    ]
    if not best:
        best = [row for row in rows if abs(row.result.f1 - best_f1) < 1e-12]
    print(
        f" best-F1 region ({len(best)} grid points): "
        f"closed_ratio={min(row.closed_ratio for row in best):.3f}.."
        f"{max(row.closed_ratio for row in best):.3f}, "
        f"open_ratio={min(row.open_ratio for row in best):.3f}.."
        f"{max(row.open_ratio for row in best):.3f}"
    )


def print_joint_regions(rows, expected_pairs):
    """Summarize ratios that remain strong across every source/target pairing."""
    print("\n=== Joint same/cross-condition candidate regions ===")
    for method in ("median", "p75", "trimmed_mean"):
        groups = {}
        for row in rows:
            if row.baseline_method == method:
                groups.setdefault((row.closed_ratio, row.open_ratio), []).append(row)
        complete = [group for group in groups.values() if len(group) == expected_pairs]
        if not complete:
            continue
        best_min_f1 = max(min(row.result.f1 for row in group) for group in complete)
        minimum_best = [
            group for group in complete
            if min(row.result.f1 for row in group) == best_min_f1
        ]
        best_mean_f1 = max(
            statistics.fmean(row.result.f1 for row in group)
            for group in minimum_best
        )
        selected = [
            group for group in minimum_best
            if abs(
                statistics.fmean(row.result.f1 for row in group) - best_mean_f1
            ) < 1e-12
        ]
        closed = [group[0].closed_ratio for group in selected]
        opened = [group[0].open_ratio for group in selected]
        hold_false = max(
            sum(row.result.hold_false_events for row in group)
            for group in selected
        )
        stuck = min(
            sum(row.result.candidate_stuck_at_end for row in group)
            for group in selected
        )
        print(
            f"{method:12} points={len(selected):3d} "
            f"closed_ratio={min(closed):.3f}..{max(closed):.3f} "
            f"open_ratio={min(opened):.3f}..{max(opened):.3f} "
            f"min_F1={best_min_f1:.3f} mean_F1={best_mean_f1:.3f} "
            f"hold_false_max={hold_false} stuck_pairings_min={stuck}/{expected_pairs}"
        )


def synth_recording():
    frames = []
    cues = []
    for index in range(600):
        t = index * 0.1
        cycle = index % 40
        if cycle == 4 and index > 0:
            cues.append(t)
        closed = cycle in (8, 9)
        frames.append(Frame(
            t,
            None,
            None,
            0.10 if closed else 0.30,
            "BLINK" if 4 <= cycle <= 11 else "OPEN",
            True,
            True,
        ))
    return Recording(
        Path("synthetic.jsonl"),
        {"condition": "synthetic"},
        tuple(frames),
        tuple(cues),
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="guided EAR recordings의 absolute/relative threshold 평가"
    )
    parser.add_argument("paths", nargs="*", help="JSONL paths or glob patterns")
    parser.add_argument("--top", type=int, default=3, help="조합별 상위 결과 수")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--match-early-sec", type=float, default=DEFAULT_MATCH_EARLY_SEC)
    parser.add_argument("--match-late-sec", type=float, default=DEFAULT_MATCH_LATE_SEC)
    parser.add_argument(
        "--transition-margin-sec", type=float, default=DEFAULT_TRANSITION_MARGIN_SEC
    )
    parser.add_argument("--closed-ratio-min", type=float, default=0.10)
    parser.add_argument("--closed-ratio-max", type=float, default=0.95)
    parser.add_argument("--open-ratio-min", type=float, default=0.20)
    parser.add_argument("--open-ratio-max", type=float, default=1.10)
    parser.add_argument("--ratio-step", type=float, default=0.025)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.match_early_sec < 0 or args.match_late_sec <= 0:
        raise SystemExit("matching window must be non-negative/positive")
    if args.transition_margin_sec < 0:
        raise SystemExit("transition margin must be non-negative")

    if args.self_test:
        recordings = [synth_recording()]
    else:
        files = sorted({path for pattern in args.paths for path in glob.glob(pattern)})
        if not files:
            raise SystemExit(
                "recording이 없습니다. 먼저: python vision/eval/record.py "
                "--guided --subject S01 --condition light-off"
            )
        try:
            recordings = [load_recording(path) for path in files]
        except ValueError as error:
            raise SystemExit(str(error)) from error

    for recording in recordings:
        if not recording.cues:
            raise SystemExit(f"{recording.name}: cue가 없습니다")
        print_summary(
            recording,
            args.transition_margin_sec,
            args.match_early_sec,
            args.match_late_sec,
        )

    print(
        f"\n=== Existing absolute thresholds ({ABSOLUTE_CLOSED:.2f}/{ABSOLUTE_OPEN:.2f}) ==="
    )
    print(
        f"matching window: cue-{args.match_early_sec:.2f}s .. "
        f"cue+{args.match_late_sec:.2f}s; blink duration: "
        f"{MIN_CLOSED_MS}..{MAX_CLOSED_MS}ms"
    )
    for recording in recordings:
        print_absolute(
            recording,
            args.match_early_sec,
            args.match_late_sec,
            args.transition_margin_sec,
        )

    closed_ratios = float_range(
        args.closed_ratio_min,
        args.closed_ratio_max,
        args.ratio_step,
    )
    open_ratios = float_range(
        args.open_ratio_min,
        args.open_ratio_max,
        args.ratio_step,
    )
    print("\n=== Relative threshold sweep (evaluation only) ===")
    print(
        f"closed_ratio={closed_ratios[0]:.3f}..{closed_ratios[-1]:.3f}, "
        f"open_ratio={open_ratios[0]:.3f}..{open_ratios[-1]:.3f}, "
        f"step={args.ratio_step:.3f}"
    )
    all_rows = []
    for source in recordings:
        for target in recordings:
            rows = relative_sweep(
                source,
                target,
                closed_ratios=closed_ratios,
                open_ratios=open_ratios,
                transition_margin_sec=args.transition_margin_sec,
                match_early_sec=args.match_early_sec,
                match_late_sec=args.match_late_sec,
            )
            all_rows.extend(rows)
            for method in ("median", "p75", "trimmed_mean"):
                print_sweep_group(
                    [row for row in rows if row.baseline_method == method],
                    args.top,
                )
    print_joint_regions(all_rows, len(recordings) ** 2)

    print(
        "\n주의: guided cue 기반 eval 결과이며 production threshold를 변경하지 않습니다. "
        "한 사용자·두 조건 결과를 universal threshold로 해석하지 마세요."
    )


if __name__ == "__main__":
    main()
