"""
vision/camera.py
────────────────
카메라 선택. **인덱스를 코드에 박지 않습니다.**

인덱스는 "몇 번째 장치" 라는 뜻일 뿐 어느 카메라인지 보장하지 않습니다.
macOS 의 연속성 카메라가 근처 iPhone 을 장치 목록에 끼워 넣고 보통 0번을
가져가므로, 같은 숫자가 어제와 다른 카메라를 가리킵니다.

세 도구(run.py · eval/record.py · eval/distance_check.py)가 각자 기본값 0 을
들고 있었고, .env 를 읽는 것은 run.py 뿐이었습니다. 그래서 .env 에
WEBCAM_INDEX 를 넣어도 녹화 도구는 여전히 iPhone 을 열었습니다.
선택 규칙을 여기 한 곳에 둡니다.

    idx = default_index()          # --cam → .env → 0
    cap = open_camera(idx)         # 실패하면 무엇을 하라고 알려주고 종료
    list_cams()                    # 쓸 수 있는 장치 훑어보기
"""
import os
import subprocess
import sys

MAX_CAM_PROBE = 6      # --list-cams 가 훑어볼 인덱스 범위


def default_index(explicit=None) -> int:
    """지정 → .env(WEBCAM_INDEX) → 0."""
    if explicit is not None:
        return int(explicit)
    return int(os.getenv("WEBCAM_INDEX", 0))


def parse_camera_names(profiler_output: str):
    """system_profiler 출력에서 장치 이름만 뽑습니다. 순수 함수 — 테스트됩니다."""
    names = []
    for line in profiler_output.splitlines():
        stripped = line.strip()
        if (stripped.endswith(":") and line.startswith("    ")
                and not line.startswith("      ")):
            name = stripped[:-1]
            if name and name != "Camera":
                names.append(name)
    return names


def camera_names():
    """
    macOS 의 카메라 이름 목록. **힌트로만 씁니다.**

    OpenCV 는 이름을 알려주지 않고 인덱스만 줍니다. 이름을 같이 보여주면
    "0번이 왜 iPhone 이지" 를 훨씬 빨리 알 수 있습니다. 다만 system_profiler 의
    순서가 AVFoundation 인덱스 순서와 같다는 보장은 없으므로 매칭하지 않습니다.
    """
    if sys.platform != "darwin":
        return []
    try:
        out = subprocess.run(["system_profiler", "SPCameraDataType"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_camera_names(out)


def list_cams(max_index: int = MAX_CAM_PROBE) -> None:
    """열리는 카메라를 훑어 인덱스·해상도를 출력합니다."""
    import cv2

    try:
        cv2.setLogLevel(0)          # 없는 인덱스를 열 때 나오는 경고를 줄입니다
    except AttributeError:
        pass

    for name in camera_names():
        print(f"  (이름) {name}")
    if sys.platform == "darwin":
        print("  ※ 이름 순서와 인덱스 순서는 다릅니다. 연속성 카메라(iPhone)가")
        print("     0번을 가져가는 경우가 많습니다.\n")

    found = 0
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        if not cap.isOpened():
            cap.release()
            continue
        ok, frame = cap.read()
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        if not ok or frame is None:
            print(f"  [{i}] 열렸지만 프레임이 오지 않습니다 (다른 앱이 쓰는 중일 수 있습니다)")
            continue
        found += 1
        print(f"  [{i}] {w}x{h}")

    if not found:
        print("  쓸 수 있는 카메라가 없습니다.")
        print("  macOS 라면 시스템 설정 → 개인정보 보호 및 보안 → 카메라에서")
        print("  터미널 앱을 허용하고 터미널을 다시 시작하세요.")
    else:
        print("\n어느 것이 원하는 카메라인지 --preview 로 확인한 뒤")
        print(".env 의 WEBCAM_INDEX 에 박아 두세요.")


def open_camera(index: int):
    """
    카메라를 엽니다. 실패하면 무엇을 하면 되는지 알려주고 종료합니다.

    "카메라를 열 수 없습니다" 만 보고는 권한 문제인지 인덱스 문제인지
    구분할 수 없습니다. 실제로 그 메시지 앞에서 한 번 막혔습니다.
    """
    import cv2

    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        sys.exit(f"카메라 {index} 를 열 수 없습니다.\n"
                 f"  목록 보기:  python vision/run.py --list-cams\n"
                 f"  다른 것 쓰기:  --cam <번호>  또는 .env 의 WEBCAM_INDEX\n"
                 f"  macOS 권한:  시스템 설정 → 개인정보 보호 및 보안 → 카메라")
    return cap
