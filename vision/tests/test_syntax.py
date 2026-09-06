"""
vision/ 의 모든 파이썬 파일이 최소한 **문법적으로 성립하는지** 확인합니다.

run.py 와 eval/*.py 는 카메라와 mediapipe 가 필요해서 테스트가 import 하지
않습니다. 그래서 run.py 에 들어간 문법 오류(잘못된 위치의 elif)가 테스트
90개를 모두 통과한 채로 남아 있었고, 실행해서야 발견됐습니다.

컴파일은 카메라 없이도 됩니다. 최소한의 그물입니다.
"""
import py_compile
import sys
from pathlib import Path

import pytest

VISION = Path(__file__).resolve().parents[1]
FILES = sorted(p for p in VISION.rglob("*.py") if "__pycache__" not in str(p))


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(VISION)))
def test_문법이_성립한다(path):
    try:
        py_compile.compile(str(path), doraise=True, cfile=str(path) + "c.tmp")
    except py_compile.PyCompileError as e:
        pytest.fail(f"{path.name}: {e}")
    finally:
        tmp = Path(str(path) + "c.tmp")
        if tmp.exists():
            tmp.unlink()


def test_대상_파일이_실제로_있다():
    names = {p.name for p in FILES}
    for required in ("run.py", "calibrator.py", "record.py", "distance_check.py"):
        assert required in names, f"{required} 가 검사 대상에서 빠졌습니다"
