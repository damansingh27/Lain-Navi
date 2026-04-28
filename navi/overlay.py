"""
Always-on-top transparent PyQt window showing `assets/navivis.gif` during active work.

`NAVIOverlay` exposes a small state machine (IDLE / LISTENING / PROCESSING / SPEAKING).
State changes are emitted via Qt signals so worker threads never touch widgets directly.
Watchdog timers reset stuck states after long operations (e.g. extended TTS).
"""

from enum import Enum
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QLabel, QWidget
from PyQt6.QtCore import Qt, QSize, pyqtSignal, QTimer
from PyQt6.QtGui import QMovie

GIF_SIZE = 200
_PROJECT_ROOT = Path(__file__).resolve().parent
GIF_PATH = _PROJECT_ROOT / "assets" / "navivis.gif"


class OverlayState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"


class NAVIOverlay(QWidget):
    _sig_set_state = pyqtSignal(str, str)
    _sig_force_idle = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self.label = QLabel(self)
        self.label.setFixedSize(GIF_SIZE, GIF_SIZE)
        self.label.setScaledContents(True)
        self.setFixedSize(GIF_SIZE, GIF_SIZE)

        self.movie = QMovie(str(GIF_PATH))
        self.movie.setScaledSize(QSize(GIF_SIZE, GIF_SIZE))
        self.label.setMovie(self.movie)
        if not self.movie.isValid():
            print(
                f"NAVI overlay: could not load GIF at {GIF_PATH}. "
                "Check that the file exists."
            )

        self.state = OverlayState.IDLE
        self.state_watchdog = QTimer(self)
        self.state_watchdog.setSingleShot(True)
        self.state_watchdog.timeout.connect(self._on_watchdog_timeout)
        self._watchdog_reason = ""
        # SPEAKING must survive long TTS (full emails); pipeline runs off the Qt thread.
        self.state_timeouts_ms = {
            OverlayState.IDLE: 0,
            OverlayState.LISTENING: 60_000,
            OverlayState.PROCESSING: 120_000,
            OverlayState.SPEAKING: 600_000,
        }

        screen = QApplication.primaryScreen().geometry()
        self.move(screen.width() - GIF_SIZE - 20, 20)

        self._sig_set_state.connect(self._apply_state)
        self._sig_force_idle.connect(self._do_force_idle)

        self._set_visual_hidden()

    def _set_visual_visible(self):
        self.movie.start()
        self.show()
        self.raise_()
        self.update()

    def _set_visual_hidden(self):
        self.movie.stop()
        self.hide()

    def _start_watchdog_for(self, state, reason):
        timeout = self.state_timeouts_ms.get(state, 0)
        self.state_watchdog.stop()
        if timeout > 0:
            self._watchdog_reason = reason
            self.state_watchdog.start(timeout)

    def _on_watchdog_timeout(self):
        print(
            f"Overlay watchdog timeout in state={self.state.value}. "
            f"Resetting to IDLE. reason={self._watchdog_reason}"
        )
        self._apply_state(OverlayState.IDLE.value, "watchdog_timeout")

    def _apply_state(self, new_state_value, reason):
        new_state = OverlayState(new_state_value)
        if new_state == self.state:
            self._start_watchdog_for(new_state, reason)
            return

        old_state = self.state
        self.state = new_state
        print(f"Overlay state: {old_state.value} -> {new_state.value} ({reason})")

        if new_state == OverlayState.IDLE:
            self.state_watchdog.stop()
            self._set_visual_hidden()
        else:
            self._set_visual_visible()
            self._start_watchdog_for(new_state, reason)

    def _do_force_idle(self, reason):
        self._apply_state(OverlayState.IDLE.value, reason)

    def set_state(self, state, reason=""):
        if isinstance(state, OverlayState):
            self._sig_set_state.emit(state.value, reason)
        else:
            self._sig_set_state.emit(str(state), reason)

    def force_idle(self, reason="forced_reset"):
        self._sig_force_idle.emit(reason)

    def set_listening(self, reason="listening"):
        self.set_state(OverlayState.LISTENING, reason)

    def set_processing(self, reason="processing"):
        self.set_state(OverlayState.PROCESSING, reason)

    def set_speaking(self, reason="speaking"):
        self.set_state(OverlayState.SPEAKING, reason)

    def set_idle(self, reason="idle"):
        self.set_state(OverlayState.IDLE, reason)

    def show_navi(self):
        self.set_listening("legacy_show")

    def hide_navi(self):
        self.set_idle("legacy_hide")
