"""
Small PyQt launcher: start/stop Ollama, Outlook, and `navi.py` after `runtime_bootstrap` preflight.

`run_preflight_checks()` runs bootstrap inside `navi-env` with cwd set to the repo root
so CUDA/Ollama/asset checks match a real NAVI start.
"""

import sys
import os
import json
import subprocess
import time
from pathlib import Path
import psutil
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QLabel
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

WORKSPACE_ROOT = Path(__file__).resolve().parent
STATE_FILE = WORKSPACE_ROOT / "assets" / "launcher_state.json"
NAVI_SCRIPT = WORKSPACE_ROOT / "navi.py"
PYTHON = WORKSPACE_ROOT / "navi-env" / "Scripts" / "python.exe"
OUTLOOK_PATHS = [
    os.path.expandvars(r"%ProgramFiles%\Microsoft Office\root\Office16\OUTLOOK.EXE"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft Office\root\Office16\OUTLOOK.EXE"),
]
OLLAMA_STARTUP_WAIT_SECONDS = 3.0
NAVI_BOOT_WAIT_SECONDS = 2.0


def _is_running(name_fragment):
    """Check if a process whose cmdline contains name_fragment is alive."""
    for proc in psutil.process_iter(['cmdline', 'name']):
        try:
            cmdline = proc.info.get('cmdline') or []
            if any(name_fragment.lower() in c.lower() for c in cmdline):
                return True
            if name_fragment.lower() in (proc.info.get('name') or '').lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def load_state():
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f).get("running", False)
    return False


def save_state(running):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w") as f:
        json.dump({"running": running}, f)


def start_ollama():
    if _is_running("ollama"):
        return
    subprocess.Popen(
        ["ollama", "serve"],
        creationflags=subprocess.CREATE_NO_WINDOW
    )


def start_outlook():
    if _is_running("outlook"):
        return
    for path in OUTLOOK_PATHS:
        if os.path.isfile(path):
            subprocess.Popen([path], creationflags=subprocess.CREATE_NO_WINDOW)
            return
    print("Outlook not found — skipping.")


def run_preflight_checks():
    """
    Runs runtime_bootstrap in the project venv and returns:
    (ok: bool, message: str)
    """
    if not PYTHON.exists():
        return False, f"Python interpreter missing: {PYTHON}"
    if not NAVI_SCRIPT.exists():
        return False, f"NAVI entrypoint missing: {NAVI_SCRIPT}"

    model_name = os.getenv("OLLAMA_MODEL", "dolphin3")
    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    cmd = [
        str(PYTHON),
        "-c",
        (
            "import json; "
            "from runtime_bootstrap import run_startup_checks; "
            f"r=run_startup_checks(model_name={model_name!r}, ollama_base_url={ollama_base_url!r}, require_cuda=True); "
            "print(json.dumps(r.to_dict()))"
        ),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(WORKSPACE_ROOT),
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception as e:
        return False, f"Preflight execution failed: {e}"

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        return False, f"Preflight process failed: {stderr or 'unknown error'}"

    payload = (proc.stdout or "").strip().splitlines()
    if not payload:
        return False, "Preflight returned no diagnostics."
    try:
        report = json.loads(payload[-1])
    except json.JSONDecodeError:
        return False, "Preflight output was not valid JSON."

    if report.get("ok"):
        return True, "Bootstrap + model/service checks passed."

    critical_failures = []
    for diag in report.get("diagnostics", []):
        if diag.get("critical") and diag.get("status") == "fail":
            critical_failures.append(f"{diag.get('name')}: {diag.get('message')}")
    failure_msg = "; ".join(critical_failures) if critical_failures else "Critical startup checks failed."
    return False, failure_msg


def start_navi():
    if _is_running("navi.py"):
        return None
    return subprocess.Popen(
        [str(PYTHON), str(NAVI_SCRIPT)],
        creationflags=subprocess.CREATE_NO_WINDOW,
        cwd=str(WORKSPACE_ROOT),
    )


def stop_navi():
    targets = ['navi.py', 'ollama', 'outlook']
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = proc.info.get('cmdline') or []
            name = (proc.info.get('name') or '').lower()
            cmdline_lower = ' '.join(cmdline).lower()
            for target in targets:
                if target in cmdline_lower or target in name:
                    proc.terminate()
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


class LauncherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.running = load_state()
        self.navi_process = None
        self.last_error = ""
        self.setWindowTitle("NAVI")
        self.setFixedSize(360, 170)
        self.setStyleSheet("background-color: #0A0A0F;")

        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(16)

        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setFont(QFont("Courier New", 11))

        self.detail_label = QLabel()
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setWordWrap(True)
        self.detail_label.setFont(QFont("Courier New", 8))
        self.detail_label.setStyleSheet("color: #8B7FA7;")

        self.toggle_btn = QPushButton()
        self.toggle_btn.setFixedSize(220, 40)
        self.toggle_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        self.toggle_btn.clicked.connect(self.toggle)

        layout.addWidget(self.status_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.toggle_btn)
        self.setLayout(layout)

        if self.running and not _is_running("navi.py"):
            self.running = False
            save_state(False)

        self.update_ui()

        if self.running:
            self.running = self._startup_sequence()
            save_state(self.running)
            self.update_ui()

    def update_ui(self):
        if self.running:
            self.status_label.setText("● NAVI ONLINE")
            self.status_label.setStyleSheet("color: #39D353;")
            self.detail_label.setText("All startup checks passed.")
            self.toggle_btn.setText("SHUT DOWN")
            self.toggle_btn.setStyleSheet(
                "background-color: #3D1A6E; color: #E0D7F5; border: 1px solid #7B2FBE;")
        else:
            self.status_label.setText("○ NAVI OFFLINE")
            self.status_label.setStyleSheet("color: #A090C0;")
            self.detail_label.setText(self.last_error or "Idle.")
            self.toggle_btn.setText("START UP")
            self.toggle_btn.setStyleSheet(
                "background-color: #12101A; color: #E0D7F5; border: 1px solid #3D2D6E;")

    def _startup_sequence(self):
        self.last_error = ""
        start_ollama()
        start_outlook()
        time.sleep(OLLAMA_STARTUP_WAIT_SECONDS)

        ok, message = run_preflight_checks()
        if not ok:
            self.last_error = message
            return False

        self.navi_process = start_navi()
        time.sleep(NAVI_BOOT_WAIT_SECONDS)
        if not _is_running("navi.py"):
            self.last_error = "NAVI process failed to stay running after preflight."
            return False
        return True

    def toggle(self):
        if self.running:
            stop_navi()
            self.running = False
            self.last_error = ""
        else:
            self.running = self._startup_sequence()
        save_state(self.running)
        self.update_ui()

    def closeEvent(self, event):
        save_state(self.running)
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = LauncherWindow()
    window.show()
    sys.exit(app.exec())