#!/usr/bin/env python3
"""Windows GUI wrapper for the existing tools/demo/run_all.py contract."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox, ttk


CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\ProjectSOMA.WindowsLauncher.v1"
FRONT_FILES = (
    "tools/serve_front.py",
    "web/view/login.html",
    "web/view/signup.html",
    "web/view/index.html",
    "web/view/auth.js",
    "web/view/measurement_runtime.mjs",
    "web/view/measurement_phase.mjs",
)

COLORS = {
    "canvas": "#F3F6F8",
    "surface": "#FFFFFF",
    "navy": "#14253D",
    "text": "#24364B",
    "muted": "#6B778C",
    "line": "#DDE4EA",
    "blue": "#3478F6",
    "blue_dark": "#245FCB",
    "blue_soft": "#EAF2FF",
    "blue_hover": "#DCE9FF",
    "control_disabled": "#F2F4F7",
    "text_disabled": "#9AA6B2",
    "green": "#22B573",
    "orange": "#B86B00",
    "orange_soft": "#FFF7E7",
    "danger": "#D34A4A",
    "danger_soft": "#FFF0F0",
    "log": "#F8FAFC",
}


class PreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class LauncherConfig:
    backend_root: Path
    python_path: Path
    front_root: Path
    mode: str
    use_mock: bool
    use_vision: bool
    use_nano: bool
    chair_port: str = ""
    nano_port: str = ""
    camera_index: int = 0

    @property
    def source_mode(self) -> str:
        return "all" if (self.use_mock or self.use_vision) else "chair-only"


def locate_backend_root(start: Path | None = None) -> Path:
    script_root = Path(__file__).resolve().parents[2]
    candidates = [start, script_root, Path.cwd()]
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates[0:0] = [executable_dir, executable_dir.parent]
    candidates.append(Path(r"C:\project-soma"))
    for candidate in candidates:
        if candidate and (candidate / "tools" / "demo" / "run_all.py").is_file():
            return candidate.resolve()
    raise PreflightError("Backend 경로에서 tools/demo/run_all.py를 찾지 못했습니다.")


def default_python(backend_root: Path) -> Path:
    for name in (".venv-clean", ".venv"):
        candidate = backend_root / name / "Scripts" / "python.exe"
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def validate_paths(config: LauncherConfig) -> None:
    if not config.python_path.is_file():
        raise PreflightError("선택한 Python 실행 파일이 없습니다.")
    if not (config.backend_root / "tools" / "demo" / "run_all.py").is_file():
        raise PreflightError("Backend 경로에 tools/demo/run_all.py가 없습니다.")
    missing = [name for name in FRONT_FILES if not (config.front_root / name).is_file()]
    if missing:
        raise PreflightError("Front 필수 파일이 없습니다: " + ", ".join(missing))
    if config.mode not in ("demo", "normal"):
        raise PreflightError("실행 모드는 DEMO 또는 NORMAL이어야 합니다.")
    if not config.use_mock:
        if not config.chair_port:
            raise PreflightError("실제 장치 실행에는 Chair COM 포트가 필요합니다.")
        if config.use_nano and not config.nano_port:
            raise PreflightError("Nano 사용 시 Nano COM 포트가 필요합니다.")
        if config.use_nano and config.chair_port.casefold() == config.nano_port.casefold():
            raise PreflightError("Chair와 Nano는 서로 다른 COM 포트를 선택해야 합니다.")
        if config.camera_index < 0:
            raise PreflightError("카메라 인덱스는 0 이상이어야 합니다.")


def build_run_all_command(
    config: LauncherConfig,
    *,
    check_only: bool = False,
    shutdown_file: Path | None = None,
) -> list[str]:
    command = [
        str(config.python_path),
        str(config.backend_root / "tools" / "demo" / "run_all.py"),
        "--front-root", str(config.front_root),
        "--mode", config.mode,
        "--source-mode", config.source_mode,
    ]
    if config.use_mock:
        command.append("--mock")
    else:
        command.extend(["--chair", "--chair-port", config.chair_port])
        if config.use_vision:
            command.extend(["--vision", "--camera-index", str(config.camera_index)])
        if config.use_nano:
            command.extend(["--nano", "--nano-port", config.nano_port])
    if check_only:
        command.append("--check-only")
    if shutdown_file is not None:
        command.extend(["--shutdown-file", str(shutdown_file)])
    return command


def _run_probe(python_path: Path, source: str, *args: str, timeout: int = 12) -> str:
    result = subprocess.run(
        [str(python_path), "-c", source, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        check=False,
    )
    if result.returncode:
        raise PreflightError(result.stderr.strip() or "장치 점검 명령이 실패했습니다.")
    return result.stdout.strip()


def validate_python(python_path: Path) -> None:
    output = _run_probe(
        python_path,
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
    )
    try:
        version = tuple(int(part) for part in output.split(".")[:2])
    except ValueError as error:
        raise PreflightError("Python 버전을 확인하지 못했습니다.") from error
    if version < (3, 10) or version > (3, 12):
        raise PreflightError(f"Python 3.10~3.12가 필요합니다. 현재 선택: {output}")


def list_serial_ports(python_path: Path) -> list[dict[str, str]]:
    source = (
        "import json; from serial.tools import list_ports; "
        "print(json.dumps([{'device': p.device, 'description': p.description} "
        "for p in list_ports.comports()]))"
    )
    try:
        return json.loads(_run_probe(python_path, source) or "[]")
    except (json.JSONDecodeError, PreflightError) as error:
        raise PreflightError("선택한 Python에서 COM 포트 목록을 읽지 못했습니다.") from error


def available_camera_indexes(python_path: Path, maximum: int = 6) -> list[int]:
    source = (
        "import cv2, json, sys; found=[]; "
        "[(lambda c, i: (found.append(i) if c.isOpened() else None, c.release()))"
        "(cv2.VideoCapture(i), i) for i in range(int(sys.argv[1]))]; "
        "print(json.dumps(found))"
    )
    try:
        return [int(value) for value in json.loads(
            _run_probe(python_path, source, str(maximum), timeout=20) or "[]"
        )]
    except (json.JSONDecodeError, PreflightError) as error:
        raise PreflightError("선택한 Python에서 카메라 목록을 읽지 못했습니다.") from error


def verify_devices(config: LauncherConfig) -> None:
    if config.use_mock:
        return
    ports = {item["device"].casefold() for item in list_serial_ports(config.python_path)}
    if config.chair_port.casefold() not in ports:
        raise PreflightError(f"Chair 포트 {config.chair_port}를 현재 장치 목록에서 찾지 못했습니다.")
    if config.use_nano and config.nano_port.casefold() not in ports:
        raise PreflightError(f"Nano 포트 {config.nano_port}를 현재 장치 목록에서 찾지 못했습니다.")
    if config.use_vision:
        cameras = available_camera_indexes(config.python_path)
        if config.camera_index not in cameras:
            raise PreflightError(f"카메라 {config.camera_index}을(를) 열 수 없습니다.")


def run_preflight(config: LauncherConfig, *, check_devices: bool = True) -> str:
    validate_paths(config)
    validate_python(config.python_path)
    if check_devices:
        verify_devices(config)
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        build_run_all_command(config, check_only=True),
        cwd=config.backend_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        check=False,
    )
    if result.returncode:
        raise PreflightError((result.stderr or result.stdout).strip() or "통합 사전 점검 실패")
    return result.stdout.strip()


class SingleInstance:
    def __init__(self) -> None:
        self.handle = None
        self.kernel32 = None
        if os.name == "nt":
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            self.kernel32.CreateMutexW.restype = ctypes.c_void_p
            self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            self.kernel32.CloseHandle.restype = ctypes.c_bool
            self.handle = self.kernel32.CreateMutexW(None, False, MUTEX_NAME)
            if not self.handle or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                if self.handle:
                    self.kernel32.CloseHandle(self.handle)
                    self.handle = None
                raise RuntimeError("SOMA GUI 실행기가 이미 실행 중입니다.")

    def close(self) -> None:
        if self.handle and self.kernel32:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


class SomaLauncherApp:
    def __init__(self, root: Tk, backend_root: Path) -> None:
        self.root = root
        self.backend_root = backend_root
        self.process: subprocess.Popen[str] | None = None
        self.shutdown_file: Path | None = None
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.browser_opened = False

        self.python_var = StringVar(value=str(default_python(backend_root)))
        self.front_var = StringVar(value=str(backend_root.with_name("project-soma-front")))
        self.mode_var = StringVar(value="demo")
        self.mock_var = BooleanVar(value=False)
        self.vision_var = BooleanVar(value=True)
        self.nano_var = BooleanVar(value=True)
        self.chair_var = StringVar(value=os.getenv("CHAIR_SERIAL_PORT", ""))
        self.nano_port_var = StringVar(value=os.getenv("FEEDBACK_SERIAL_PORT", ""))
        self.camera_var = StringVar(value=os.getenv("WEBCAM_INDEX", "0"))
        self.status_var = StringVar(value="설정을 확인한 뒤 사전 점검 또는 시작을 누르세요.")
        self.profile_summary_var = StringVar()
        self.mode_buttons: dict[str, tk.Button] = {}

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll_events)

    def _build_ui(self) -> None:
        self.root.title("SOMA Windows 실행기")
        self.root.geometry("920x790")
        self.root.minsize(840, 700)
        self.root.configure(background=COLORS["canvas"])
        self._configure_styles()

        frame = ttk.Frame(self.root, style="App.TFrame", padding=(28, 24, 28, 22))
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        header = ttk.Frame(frame, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="SOMA", style="Brand.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Windows 실행기",
            style="PageTitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(
            header,
            text="실행 환경과 장치를 확인하고 SOMA 측정을 시작하세요.",
            style="Subtitle.TLabel",
        ).grid(row=2, column=0, sticky="w", pady=(5, 0))
        badge = ttk.Label(header, text="  LOCAL LAUNCHER  ", style="Badge.TLabel")
        badge.grid(row=0, column=1, rowspan=2, sticky="ne", pady=(4, 0))

        content = ttk.Frame(frame, style="App.TFrame")
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=3, uniform="content")
        content.columnconfigure(1, weight=2, uniform="content")

        left = ttk.Frame(content, style="App.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        right = ttk.Frame(content, style="App.TFrame")
        right.grid(row=0, column=1, sticky="nsew", padx=(9, 0))

        paths = self._card(left, "실행 경로", "SOMA가 사용할 Python과 Frontend 위치")
        paths.pack(fill="x")
        self._path_row(paths, 1, "Python", self.python_var, self._browse_python)
        self._path_row(paths, 2, "Frontend", self.front_var, self._browse_front)

        profile = self._card(left, "실행 모드", "시연 환경에 맞는 정책 프로필을 선택하세요.")
        profile.pack(fill="x", pady=(14, 0))
        choices = ttk.Frame(profile, style="Card.TFrame")
        choices.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(7, 7))
        choices.columnconfigure((0, 1), weight=1)
        self.mode_buttons["demo"] = self._mode_card(
            choices, 0, "DEMO", "빠른 시연과 기능 확인", "demo"
        )
        self.mode_buttons["normal"] = self._mode_card(
            choices, 1, "NORMAL", "실사용 측정 정책", "normal"
        )
        self.mock_check = ttk.Checkbutton(
            profile,
            text="Mock 센서 사용  ·  실제 장치 없이 통합 흐름 실행",
            variable=self.mock_var,
            command=self._sync_controls,
            style="Switch.TCheckbutton",
        )
        self.mock_check.grid(row=2, column=0, columnspan=3, sticky="w")

        devices = self._card(left, "장치 설정", "실제 센서를 사용할 때 연결 장치를 지정하세요.")
        devices.pack(fill="x", pady=(14, 0))
        ttk.Label(devices, text="Chair COM", style="Field.TLabel").grid(row=1, column=0, sticky="w", pady=3)
        self.chair_combo = ttk.Combobox(
            devices, textvariable=self.chair_var, width=18, style="Control.TCombobox"
        )
        self.chair_combo.grid(row=1, column=1, sticky="ew", padx=(12, 8), pady=3)
        self.refresh_button = ttk.Button(
            devices, text="새로고침", command=self.refresh_devices, style="Secondary.TButton"
        )
        self.refresh_button.grid(row=1, column=2, sticky="e", pady=3)

        self.vision_check = ttk.Checkbutton(
            devices,
            text="Vision 사용",
            variable=self.vision_var,
            command=self._sync_controls,
            style="Switch.TCheckbutton",
        )
        self.vision_check.grid(row=2, column=0, sticky="w", pady=3)
        ttk.Label(devices, text="카메라", style="Field.TLabel").grid(row=2, column=1, sticky="e", pady=3)
        self.camera_combo = ttk.Combobox(
            devices, textvariable=self.camera_var, width=7, style="Control.TCombobox"
        )
        self.camera_combo.grid(row=2, column=2, sticky="ew", pady=3)

        self.nano_check = ttk.Checkbutton(
            devices,
            text="Feedback Nano 사용",
            variable=self.nano_var,
            command=self._sync_controls,
            style="Switch.TCheckbutton",
        )
        self.nano_check.grid(row=3, column=0, sticky="w", pady=3)
        ttk.Label(devices, text="Nano COM", style="Field.TLabel").grid(row=3, column=1, sticky="e", pady=3)
        self.nano_combo = ttk.Combobox(
            devices, textvariable=self.nano_port_var, width=12, style="Control.TCombobox"
        )
        self.nano_combo.grid(row=3, column=2, sticky="ew", pady=3)
        devices.columnconfigure(1, weight=1)

        control = self._card(right, "실행 제어", "설정 점검 후 SOMA를 시작합니다.")
        control.pack(fill="x")
        summary = tk.Label(
            control,
            textvariable=self.profile_summary_var,
            anchor="w",
            justify="left",
            bg=COLORS["blue_soft"],
            fg=COLORS["navy"],
            font=("Segoe UI", 10, "bold"),
            padx=14,
            pady=12,
        )
        summary.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(7, 10))
        self.check_button = ttk.Button(
            control, text="사전 점검", command=self.preflight, style="Secondary.TButton"
        )
        self.check_button.grid(row=2, column=0, sticky="ew", padx=(0, 6))
        self.start_button = ttk.Button(
            control, text="SOMA 시작", command=self.start, style="Primary.TButton"
        )
        self.start_button.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(6, 0))
        self.stop_button = ttk.Button(
            control,
            text="프로세스 종료",
            command=self.stop,
            state="disabled",
            style="Danger.TButton",
        )
        self.stop_button.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        control.columnconfigure((0, 1, 2), weight=1)

        status = self._card(right, "현재 상태", "실행기 상태와 다음 단계를 확인하세요.")
        status.pack(fill="x", pady=(14, 0))
        status_line = ttk.Frame(status, style="Status.TFrame", padding=(12, 11))
        status_line.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(7, 8))
        status_line.columnconfigure(1, weight=1)
        ttk.Label(status_line, text="●", style="StatusDot.TLabel").grid(row=0, column=0, sticky="n", padx=(0, 8))
        ttk.Label(
            status_line,
            textvariable=self.status_var,
            style="StatusText.TLabel",
            wraplength=270,
            justify="left",
        ).grid(row=0, column=1, sticky="ew")
        warning = tk.Label(
            status,
            text="안전 종료 안내\n브라우저에서 Stop으로 측정을 끝낸 뒤 프로세스를 종료하세요.",
            anchor="w",
            justify="left",
            wraplength=300,
            bg=COLORS["orange_soft"],
            fg=COLORS["orange"],
            font=("Segoe UI", 9),
            padx=12,
            pady=10,
        )
        warning.grid(row=2, column=0, columnspan=3, sticky="ew")

        log_card = self._card(frame, "실행 로그", "사전 점검과 통합 프로세스의 출력")
        log_card.grid(row=2, column=0, sticky="nsew", pady=(16, 0))
        log_card.rowconfigure(1, weight=1)
        log_card.columnconfigure(0, weight=1)
        log_wrap = ttk.Frame(log_card, style="Log.TFrame")
        log_wrap.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(7, 0))
        log_wrap.rowconfigure(0, weight=1)
        log_wrap.columnconfigure(0, weight=1)
        self.log = tk.Text(
            log_wrap,
            height=8,
            state="disabled",
            wrap="word",
            borderwidth=0,
            highlightthickness=0,
            background=COLORS["log"],
            foreground=COLORS["text"],
            insertbackground=COLORS["text"],
            selectbackground=COLORS["blue_soft"],
            font=("Consolas", 9),
            padx=12,
            pady=10,
        )
        scrollbar = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._sync_controls()

    def _checkbox_indicator(self, fill: str, border: str, *, checked: bool = False) -> tk.PhotoImage:
        image = tk.PhotoImage(master=self.root, width=22, height=16)
        image.put(COLORS["surface"], to=(0, 0, 22, 16))
        image.put(border, to=(0, 0, 16, 16))
        image.put(fill, to=(2, 2, 14, 14))
        if checked:
            for bounds in (
                (3, 7, 5, 10),
                (5, 9, 7, 12),
                (7, 8, 9, 10),
                (9, 6, 11, 8),
                (11, 4, 13, 6),
            ):
                image.put("#FFFFFF", to=bounds)
        return image

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self.root.option_add("*TCombobox*Listbox.background", COLORS["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", COLORS["navy"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLORS["blue_soft"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", COLORS["navy"])
        checkbox_off = self._checkbox_indicator(COLORS["surface"], COLORS["line"])
        checkbox_hover = self._checkbox_indicator(COLORS["blue_soft"], COLORS["blue"])
        checkbox_on = self._checkbox_indicator(COLORS["blue"], COLORS["blue"], checked=True)
        checkbox_disabled = self._checkbox_indicator(
            COLORS["control_disabled"], "#D9E0E7"
        )
        checkbox_disabled_on = self._checkbox_indicator(
            "#B9C9DF", "#B9C9DF", checked=True
        )
        self._checkbox_images = (
            checkbox_off,
            checkbox_hover,
            checkbox_on,
            checkbox_disabled,
            checkbox_disabled_on,
        )
        style.element_create(
            "Soma.Checkbutton.indicator",
            "image",
            checkbox_off,
            ("disabled selected", checkbox_disabled_on),
            ("disabled", checkbox_disabled),
            ("active selected", checkbox_on),
            ("active", checkbox_hover),
            ("selected", checkbox_on),
        )
        style.layout(
            "Switch.TCheckbutton",
            [
                (
                    "Checkbutton.padding",
                    {
                        "sticky": "nswe",
                        "children": [
                            ("Soma.Checkbutton.indicator", {"side": "left", "sticky": ""}),
                            (
                                "Checkbutton.focus",
                                {
                                    "side": "left",
                                    "sticky": "w",
                                    "children": [
                                        ("Checkbutton.label", {"sticky": "nswe"})
                                    ],
                                },
                            ),
                        ],
                    },
                )
            ],
        )
        style.configure("App.TFrame", background=COLORS["canvas"])
        style.configure("Card.TFrame", background=COLORS["surface"])
        style.configure("Log.TFrame", background=COLORS["log"])
        style.configure("Brand.TLabel", background=COLORS["canvas"], foreground=COLORS["blue"], font=("Segoe UI", 10, "bold"))
        style.configure("PageTitle.TLabel", background=COLORS["canvas"], foreground=COLORS["navy"], font=("Segoe UI", 22, "bold"))
        style.configure("Subtitle.TLabel", background=COLORS["canvas"], foreground=COLORS["muted"], font=("Segoe UI", 9))
        style.configure("Badge.TLabel", background=COLORS["blue_soft"], foreground=COLORS["blue_dark"], font=("Segoe UI", 8, "bold"), padding=(8, 5))
        style.configure("CardTitle.TLabel", background=COLORS["surface"], foreground=COLORS["navy"], font=("Segoe UI", 11, "bold"))
        style.configure("CardHint.TLabel", background=COLORS["surface"], foreground=COLORS["muted"], font=("Segoe UI", 8))
        style.configure("Field.TLabel", background=COLORS["surface"], foreground=COLORS["text"], font=("Segoe UI", 9))
        style.configure(
            "Switch.TCheckbutton",
            background=COLORS["surface"],
            foreground=COLORS["navy"],
            font=("Segoe UI", 9),
            padding=(0, 5),
            indicatorcolor=COLORS["surface"],
            indicatorrelief="flat",
            bordercolor=COLORS["line"],
            lightcolor=COLORS["line"],
            darkcolor=COLORS["line"],
            focuscolor=COLORS["blue"],
            focusthickness=1,
        )
        style.map(
            "Switch.TCheckbutton",
            background=[
                ("active", COLORS["surface"]),
                ("disabled", COLORS["surface"]),
            ],
            foreground=[("disabled", COLORS["text_disabled"])],
            indicatorcolor=[
                ("disabled selected", "#B9C9DF"),
                ("disabled", COLORS["control_disabled"]),
                ("selected", COLORS["blue"]),
                ("active", COLORS["blue_soft"]),
            ],
            bordercolor=[
                ("disabled", COLORS["line"]),
                ("focus", COLORS["blue"]),
                ("active", COLORS["blue"]),
            ],
            lightcolor=[("selected", COLORS["blue"]), ("focus", COLORS["blue"])],
            darkcolor=[("selected", COLORS["blue"]), ("focus", COLORS["blue"])],
        )
        style.configure("Primary.TButton", background=COLORS["blue"], foreground="#FFFFFF", font=("Segoe UI", 9, "bold"), borderwidth=0, padding=(12, 10))
        style.map("Primary.TButton", background=[("active", COLORS["blue_dark"]), ("disabled", "#AFC8F4")])
        style.configure("Secondary.TButton", background=COLORS["surface"], foreground=COLORS["blue_dark"], font=("Segoe UI", 9, "bold"), borderwidth=1, padding=(11, 9))
        style.map("Secondary.TButton", background=[("active", COLORS["blue_soft"]), ("disabled", "#EEF1F4")], foreground=[("disabled", "#A7B0BC")])
        style.configure("Danger.TButton", background=COLORS["danger_soft"], foreground=COLORS["danger"], font=("Segoe UI", 9, "bold"), borderwidth=0, padding=(12, 9))
        style.map("Danger.TButton", background=[("active", "#FFE2E2"), ("disabled", "#F3F4F5")], foreground=[("disabled", "#A7B0BC")])
        style.configure("Status.TFrame", background=COLORS["blue_soft"])
        style.configure("StatusDot.TLabel", background=COLORS["blue_soft"], foreground=COLORS["green"], font=("Segoe UI", 9))
        style.configure("StatusText.TLabel", background=COLORS["blue_soft"], foreground=COLORS["text"], font=("Segoe UI", 9))
        style.configure("TEntry", fieldbackground="#FBFCFD", bordercolor=COLORS["line"], padding=6)
        style.configure(
            "Control.TCombobox",
            background=COLORS["surface"],
            fieldbackground=COLORS["surface"],
            foreground=COLORS["navy"],
            arrowcolor=COLORS["blue_dark"],
            bordercolor=COLORS["line"],
            lightcolor=COLORS["line"],
            darkcolor=COLORS["line"],
            selectbackground=COLORS["blue_soft"],
            selectforeground=COLORS["navy"],
            insertcolor=COLORS["navy"],
            borderwidth=1,
            relief="flat",
            padding=(8, 6),
        )
        style.map(
            "Control.TCombobox",
            background=[
                ("disabled", COLORS["control_disabled"]),
                ("readonly", COLORS["surface"]),
            ],
            fieldbackground=[
                ("disabled", COLORS["control_disabled"]),
                ("readonly", COLORS["surface"]),
            ],
            foreground=[("disabled", COLORS["text_disabled"])],
            arrowcolor=[
                ("disabled", "#B5BEC8"),
                ("active", COLORS["blue"]),
                ("focus", COLORS["blue"]),
            ],
            bordercolor=[
                ("disabled", "#E3E8ED"),
                ("focus", COLORS["blue"]),
                ("active", COLORS["blue"]),
            ],
            lightcolor=[("focus", COLORS["blue"]), ("active", COLORS["blue"])],
            darkcolor=[("focus", COLORS["blue"]), ("active", COLORS["blue"])],
            selectbackground=[("readonly", COLORS["blue_soft"])],
            selectforeground=[("readonly", COLORS["navy"])],
        )

    def _card(self, parent, title: str, hint: str) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=(18, 11))
        ttk.Label(card, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(card, text=hint, style="CardHint.TLabel").grid(row=0, column=1, columnspan=2, sticky="e")
        card.columnconfigure(1, weight=1)
        return card

    def _mode_card(self, parent, column: int, title: str, description: str, value: str) -> tk.Button:
        button = tk.Button(
            parent,
            text=f"{title}\n{description}",
            command=lambda: self._select_mode(value),
            justify="left",
            anchor="w",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            borderwidth=0,
            highlightthickness=2,
            padx=13,
            pady=7,
            cursor="hand2",
            takefocus=True,
        )
        button.grid(row=0, column=column, sticky="ew", padx=(0, 6) if column == 0 else (6, 0))
        return button

    def _select_mode(self, value: str) -> None:
        self.mode_var.set(value)
        self._sync_controls()

    def _refresh_mode_cards(self) -> None:
        selected = self.mode_var.get()
        for value, button in self.mode_buttons.items():
            active = value == selected
            button.configure(
                background=COLORS["blue_soft"] if active else COLORS["surface"],
                foreground=COLORS["blue_dark"] if active else COLORS["navy"],
                activebackground=COLORS["blue_hover"],
                activeforeground=COLORS["blue_dark"] if active else COLORS["navy"],
                highlightbackground=COLORS["blue"] if active else COLORS["line"],
                highlightcolor=COLORS["blue"],
            )

    def _refresh_profile_summary(self) -> None:
        mode = self.mode_var.get().upper()
        source = "MOCK" if self.mock_var.get() else "실제 장치"
        extras = []
        if not self.mock_var.get() and self.vision_var.get():
            extras.append("Vision")
        if not self.mock_var.get() and self.nano_var.get():
            extras.append("Nano")
        detail = " · ".join(extras) if extras else ("센서 시뮬레이션" if self.mock_var.get() else "Chair only")
        self.profile_summary_var.set(f"{mode}  /  {source}\n{detail}")

    def _path_row(self, parent, row, label, variable, command) -> None:
        ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(12, 8), pady=4)
        ttk.Button(parent, text="찾기", command=command, style="Secondary.TButton").grid(row=row, column=2, pady=4)
        parent.columnconfigure(1, weight=1)

    def _browse_python(self) -> None:
        selected = filedialog.askopenfilename(filetypes=[("Python", "python.exe"), ("Executable", "*.exe")])
        if selected:
            self.python_var.set(selected)

    def _browse_front(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.front_var.get())
        if selected:
            self.front_var.set(selected)

    def _sync_controls(self) -> None:
        mock = self.mock_var.get()
        for widget in (self.chair_combo, self.vision_check, self.nano_check, self.refresh_button):
            widget.configure(state="disabled" if mock else "readonly" if widget is self.chair_combo else "normal")
        self.camera_combo.configure(state="readonly" if not mock and self.vision_var.get() else "disabled")
        self.nano_combo.configure(state="readonly" if not mock and self.nano_var.get() else "disabled")
        self._refresh_mode_cards()
        self._refresh_profile_summary()

    def _config(self) -> LauncherConfig:
        try:
            camera_index = int(self.camera_var.get())
        except ValueError as error:
            raise PreflightError("카메라 번호는 0 이상의 정수여야 합니다.") from error
        return LauncherConfig(
            backend_root=self.backend_root,
            python_path=Path(self.python_var.get()).expanduser(),
            front_root=Path(self.front_var.get()).expanduser(),
            mode=self.mode_var.get(),
            use_mock=self.mock_var.get(),
            use_vision=self.vision_var.get(),
            use_nano=self.nano_var.get(),
            chair_port=self.chair_var.get().strip(),
            nano_port=self.nano_port_var.get().strip(),
            camera_index=camera_index,
        )

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.check_button.configure(state=state)
        self.start_button.configure(state=state if self.process is None else "disabled")

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_devices(self) -> None:
        try:
            python_path = Path(self.python_var.get())
        except Exception as error:
            messagebox.showerror("장치 점검", str(error))
            return
        self._set_busy(True)
        self.status_var.set("COM 포트와 카메라를 확인하는 중입니다…")

        def worker() -> None:
            try:
                ports = list_serial_ports(python_path)
                cameras = available_camera_indexes(python_path)
                self.events.put(("devices", (ports, cameras)))
            except Exception as error:  # noqa: BLE001
                self.events.put(("error", error))

        threading.Thread(target=worker, daemon=True).start()

    def preflight(self) -> None:
        self._start_preflight(start_after=False)

    def start(self) -> None:
        self._start_preflight(start_after=True)

    def _start_preflight(self, *, start_after: bool) -> None:
        if self.process is not None:
            return
        try:
            config = self._config()
        except PreflightError as error:
            messagebox.showerror("설정 오류", str(error))
            return
        self._set_busy(True)
        self.status_var.set("실행 전 경로·설정·장치를 점검하는 중입니다…")

        def worker() -> None:
            try:
                output = run_preflight(config)
                self.events.put(("preflight", (config, output, start_after)))
            except Exception as error:  # noqa: BLE001
                self.events.put(("error", error))

        threading.Thread(target=worker, daemon=True).start()

    def _launch(self, config: LauncherConfig) -> None:
        self.shutdown_file = Path(tempfile.gettempdir()) / f"soma-launcher-{os.getpid()}-{uuid.uuid4().hex}.stop"
        self.shutdown_file.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        self.process = subprocess.Popen(
            build_run_all_command(config, shutdown_file=self.shutdown_file),
            cwd=config.backend_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.browser_opened = False
        self.stop_button.configure(state="normal")
        self.status_var.set(f"SOMA {config.mode.upper()} 모드를 시작하는 중입니다…")

        def reader() -> None:
            assert self.process is not None and self.process.stdout is not None
            for line in self.process.stdout:
                self.events.put(("log", line))
            code = self.process.wait()
            self.events.put(("exit", code))

        threading.Thread(target=reader, daemon=True).start()

    def stop(self) -> None:
        if self.process is None:
            return
        confirmed = messagebox.askyesno(
            "SOMA 프로세스 종료",
            "브라우저에서 Stop을 눌러 측정을 먼저 종료했습니까?\n\n"
            "‘예’를 누르면 Backend·Frontend·Chair·Vision·Nano 프로세스를 정리합니다. "
            "이 동작 자체는 인증된 Front Stop을 보내지 않습니다.",
        )
        if not confirmed:
            self.status_var.set("브라우저에서 Stop을 누른 뒤 프로세스를 종료하세요.")
            return
        self._request_shutdown()

    def _request_shutdown(self) -> None:
        if self.process is None or self.shutdown_file is None:
            return
        self.stop_button.configure(state="disabled")
        self.status_var.set("SOMA가 소유한 프로세스를 정리하는 중입니다…")
        self.shutdown_file.touch()
        process = self.process

        def watchdog() -> None:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        creationflags=CREATE_NO_WINDOW,
                        check=False,
                    )
                else:
                    process.terminate()

        threading.Thread(target=watchdog, daemon=True).start()

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                line = str(payload)
                self._append_log(line)
                if line.startswith("로그인:") and not self.browser_opened:
                    self.browser_opened = True
                    webbrowser.open(line.split("로그인:", 1)[1].strip())
                    self.status_var.set("SOMA가 실행 중입니다. 로그인 후 Front에서 Start를 누르세요.")
            elif kind == "devices":
                ports, cameras = payload
                port_names = [item["device"] for item in ports]
                self.chair_combo["values"] = port_names
                self.nano_combo["values"] = port_names
                self.camera_combo["values"] = [str(value) for value in cameras]
                self.status_var.set(f"COM {len(port_names)}개, 카메라 {len(cameras)}개를 찾았습니다.")
                self._set_busy(False)
            elif kind == "preflight":
                config, output, start_after = payload
                self._append_log(output)
                if start_after:
                    try:
                        self._launch(config)
                    except Exception as error:  # noqa: BLE001
                        self._handle_error(error)
                else:
                    self.status_var.set("사전 점검을 통과했습니다. 프로세스와 측정은 시작하지 않았습니다.")
                    self._set_busy(False)
            elif kind == "error":
                self._handle_error(payload)
            elif kind == "exit":
                code = int(payload)
                self._append_log(f"[실행기] 통합 프로세스 종료 (code {code})")
                self.status_var.set("SOMA가 종료되었습니다." if code == 0 else "SOMA가 오류로 종료되었습니다. 로그를 확인하세요.")
                self.process = None
                if self.shutdown_file:
                    self.shutdown_file.unlink(missing_ok=True)
                self.shutdown_file = None
                self.stop_button.configure(state="disabled")
                self._set_busy(False)
        self.root.after(100, self._poll_events)

    def _handle_error(self, error: object) -> None:
        message = str(error) or type(error).__name__
        self._append_log("[점검 실패] " + message)
        self.status_var.set("사전 점검에 실패했습니다. 표시된 항목을 확인하세요.")
        self._set_busy(False)
        messagebox.showerror("SOMA 실행 실패", message)

    def on_close(self) -> None:
        if self.process is not None:
            if not messagebox.askyesno(
                "실행 중",
                "SOMA가 실행 중입니다. 먼저 Front Stop을 눌렀는지 확인하세요.\n\n"
                "창을 닫고 소유 프로세스를 종료하시겠습니까?",
            ):
                return
            self._request_shutdown()

            def close_when_done() -> None:
                if self.process is None:
                    self.root.destroy()
                else:
                    self.root.after(200, close_when_done)

            close_when_done()
            return
        self.root.destroy()


def self_test() -> int:
    backend = locate_backend_root()
    python = default_python(backend)
    if not python.is_file() or not (backend / "tools" / "demo" / "run_all.py").is_file():
        return 1
    print(f"SOMA Launcher self-test OK: {backend}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    try:
        instance = SingleInstance()
        backend_root = locate_backend_root()
    except (RuntimeError, PreflightError) as error:
        root = Tk()
        root.withdraw()
        messagebox.showerror("SOMA Windows 실행기", str(error))
        root.destroy()
        return 1
    root = Tk()
    SomaLauncherApp(root, backend_root)
    try:
        root.mainloop()
    finally:
        instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
