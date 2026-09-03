#!/usr/bin/env python3
"""
계약 스키마로 payload 를 검증합니다.

    python tools/mock/validate.py docs/contracts/sensor_data.schema.json sample.json
    python tools/mock/stream.py --stdout --duration 3 | python tools/mock/validate.py docs/contracts/sensor_data.schema.json -

스키마의 examples 도 함께 검사하므로, 계약을 고치면 이걸 먼저 돌려보세요.
"""
import json
import sys

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.exit("jsonschema 가 없습니다.  pip install jsonschema")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    schema = json.load(open(sys.argv[1], encoding="utf-8"))
    validator = Draft202012Validator(schema)

    bad = 0
    for i, ex in enumerate(schema.get("examples", [])):
        errs = list(validator.iter_errors(ex))
        if errs:
            bad += 1
            print(f"[examples[{i}]] 실패")
            for e in errs:
                print(f"   {list(e.path)}: {e.message}")
    if not bad:
        print(f"examples {len(schema.get('examples', []))}건 통과")

    if len(sys.argv) < 3:
        sys.exit(1 if bad else 0)

    src = sys.stdin if sys.argv[2] == "-" else open(sys.argv[2], encoding="utf-8")
    total = 0
    for line in src:
        line = line.strip()
        if not line:
            continue
        total += 1
        errs = list(validator.iter_errors(json.loads(line)))
        if errs:
            bad += 1
            print(f"[{total}행] 실패")
            for e in errs:
                print(f"   {list(e.path)}: {e.message}")

    print(f"{total}건 중 {total - bad}건 통과")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
