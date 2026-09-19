"""
main_window.py
--------------
Amy's frameless, translucent "glass" window: a glowing orb up top, a live
transcript in the middle, a status line, and a mic/power toggle. Drag
anywhere on the background to move the window; there's no OS titlebar.

Threading rule (this used to be broken): Qt widgets may only be touched from
the GUI thread. The wake-word listener and the Gemini session run on their own
threads, so everything they want to show goes through Qt signals, and the
confirmation dialog for risky actions is marshalled onto the GUI thread.
"""

from __future__ import annotations

import asyncio
import html
import sys
import threading

from PyQt6.QtCore import Qt, QPoint, QTimer, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import config
from core.gemini_live_client import AmyLiveSession
from core.wake_word import WakeWordListener
from system import system_control as sysctl

STYLE_SHEET = f"""
QWidget#glass {{
    background-color: {config.BG_COLOR};
    border-radius: 24px;
    border: 1px solid rgba(255,255,255,40);
}}
QLabel#title {{
    color: white;
    font-size: 20px;
    font-weight: 600;
}}
QLabel#status {{
    color: rgba(255,255,255,150);
    font-size: 12px;
}}
QTextEdit#transcript {{
    background-color: rgba(255,255,255,10);
    border-radius: 14px;
    border: none;
    color: rgba(255,255,255,220);
    font-size: 13px;
    padding: 10px;
}}
QPushButton {{
    background-color: {config.ACCENT_COLOR};
    color: white;
    border-radius: 20px;
    font-size: 14px;
    font-weight: 600;
    padding: 10px;
}}
QPushButton:hover {{ background-color: {config.ACCENT_COLOR_2}; color: #10121a; }}
QPushButton#closeBtn {{
    background-color: transparent;
    color: rgba(255,255,255,140);
    font-size: 16px;
    border-radius: 14px;
    padding: 0px;
}}
QPushButton#closeBtn:hover {{ background-color: rgba(255,80,80,120); color: white; }}
"""


def _has_rtl(text: str) -> bool:
    """True if the text contains Persian/Arabic/Hebrew letters."""
    return any("\u0590" <= ch <= "\u08ff" for ch in text)


def _friendly_error(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    low = text.lower()
    if any(k in low for k in ("api key", "api_key", "permission_denied", "unauthenticated", "401", "403")):
        return "Gemini rejected the API key. Check GEMINI_API_KEY in config.py."
    if "404" in low or "not_found" in low or "not found" in low:
        return "Live model not found. Update GEMINI_LIVE_MODEL in config.py."
    if "1007" in low or "content_type_audio" in low:
        return "Gemini rejected the audio it was sent (error 1007). Say 'Amy' and try again."
    if "429" in low or "quota" in low or "resource_exhausted" in low:
        return "Gemini quota / rate limit reached. Try again in a minute."
    if "portaudio" in low or "invalid device" in low or "no default" in low or "device unavailable" in low:
        return f"Microphone/speaker problem: {text[:160]}"
    if any(k in low for k in ("getaddrinfo", "name resolution", "timed out", "connection", "network")):
        return "Can't reach Gemini. Check your internet connection."
    return text[:200]


class AmyWindow(QWidget):
    status_changed = pyqtSignal(str)
    mic_level_changed = pyqtSignal(float)
    speaker_level_changed = pyqtSignal(float)
    transcript_line = pyqtSignal(str, str)
    orb_state_changed = pyqtSignal(str)
    wake_detected = pyqtSignal(str)          # emitted from the wake-word thread
    session_finished = pyqtSignal()          # emitted from the session thread
    confirm_requested = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        self._drag_pos: QPoint | None = None
        self._session: AmyLiveSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session_thread: threading.Thread | None = None
        self._active = False
        self._stop_requested = False
        self._mic_level = 0.0
        self._speaker_level = 0.0
        self._pending_confirms: list[dict] = []

        self._build_ui()
        self._wire_signals()
        if config.CONFIRM_METHOD == "dialog":          # popup instead of asking out loud
            sysctl.set_confirmation_hook(self._confirm_dialog)

        self.wake_listener = WakeWordListener(
            on_wake=lambda remainder: self.wake_detected.emit(remainder),
            on_status=lambda s: self.status_changed.emit(s),
        )
        self.wake_listener.start()

    # -- UI construction -------------------------------------------------
    def _build_ui(self) -> None:
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(config.WINDOW_WIDTH, config.WINDOW_HEIGHT)
        self.setStyleSheet(STYLE_SHEET)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        glass = QWidget(objectName="glass")
        outer.addWidget(glass)
        layout = QVBoxLayout(glass)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        title = QLabel(config.ASSISTANT_NAME, objectName="title")
        close_btn = QPushButton("✕", objectName="closeBtn")
        close_btn.setFixedSize(28, 28)
        close_btn.clicked.connect(self.close)
        top_row.addWidget(title)
        top_row.addStretch()
        top_row.addWidget(close_btn)
        layout.addLayout(top_row)

        from gui.glow_orb import GlowOrb

        self.orb = GlowOrb()
        layout.addWidget(self.orb, stretch=2)

        self.status_label = QLabel("Say 'Amy' to start...", objectName="status")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)  # long error messages used to be cut off
        layout.addWidget(self.status_label)

        self.transcript = QTextEdit(objectName="transcript", readOnly=True)
        self.transcript.setFixedHeight(180)
        layout.addWidget(self.transcript)

        self.toggle_btn = QPushButton("🎙  Talk to Amy")
        self.toggle_btn.clicked.connect(self._toggle_session)
        layout.addWidget(self.toggle_btn)

    def _wire_signals(self) -> None:
        self.status_changed.connect(self.status_label.setText)
        self.mic_level_changed.connect(self._on_mic_level)
        self.speaker_level_changed.connect(self._on_speaker_level)
        self.transcript_line.connect(self._append_transcript)
        self.orb_state_changed.connect(self.orb.set_state)
        self.wake_detected.connect(self._on_wake_word)
        self.session_finished.connect(self._on_session_finished)
        self.confirm_requested.connect(self._on_confirm_requested)

    # -- orb level: the louder of mic and speaker (they used to overwrite each other -> flicker)
    def _on_mic_level(self, level: float) -> None:
        self._mic_level = level
        self.orb.set_level(max(self._mic_level, self._speaker_level))

    def _on_speaker_level(self, level: float) -> None:
        self._speaker_level = level
        self.orb.set_level(max(self._mic_level, self._speaker_level))

    # -- window dragging (frameless window needs manual drag support) ----
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None

    # -- confirmation dialog used by system_control for risky actions ----
    def _confirm_dialog(self, description: str) -> bool:
        """Called by system_control from a worker thread. Shows the dialog on the GUI
        thread and blocks this worker (not the GUI) until the user answers."""
        holder = {"event": threading.Event(), "result": False}
        if threading.current_thread() is threading.main_thread():
            self._on_confirm_requested(description, holder)
            return bool(holder["result"])
        self._pending_confirms.append(holder)
        self.confirm_requested.emit(description, holder)
        holder["event"].wait(timeout=90)
        try:
            self._pending_confirms.remove(holder)
        except ValueError:
            pass
        return bool(holder["result"])

    def _on_confirm_requested(self, description: str, holder: dict) -> None:
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle(f"{config.ASSISTANT_NAME} wants to do this")
            box.setText(description)
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)   # Enter = safe answer
            box.setEscapeButton(QMessageBox.StandardButton.No)
            box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            box.setStyleSheet("QMessageBox { background-color: #12121c; } "
                              "QLabel { color: white; font-size: 13px; }")
            QTimer.singleShot(60_000, box.reject)                  # no answer = no
            holder["result"] = box.exec() == QMessageBox.StandardButton.Yes
        finally:
            holder["event"].set()

    # -- transcript --------------------------------------------------------
    def _append_transcript(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        safe = html.escape(text).replace("\n", "<br>")   # model text must never be parsed as HTML
        rtl = _has_rtl(text)
        if role == "system":
            content = f'<span style="color:#FFB74D">{safe}</span>'
        else:
            if role == "amy":
                speaker = config.ASSISTANT_NAME
            else:
                speaker = "شما" if rtl else "You"
            content = f"<b>{speaker}:</b> {safe}"
        direction = "rtl" if rtl else "ltr"                        # Persian lines read right-to-left
        self.transcript.append(f'<p dir="{direction}" style="margin:0 0 6px 0">{content}</p>')

    # -- wake word / session lifecycle --------------------------------------
    def _on_wake_word(self, remainder: str) -> None:
        if not self._active:
            self._start_session(remainder)

    def _toggle_session(self) -> None:
        if self._active:
            self._stop_session()
        else:
            self._start_session()

    def _start_session(self, initial_text: str = "") -> None:
        if self._active:
            return
        self._active = True
        self._stop_requested = False
        self.toggle_btn.setText("⏹  Stop")
        self.toggle_btn.setEnabled(True)
        self.orb_state_changed.emit("listening")
        self.status_changed.emit("Connecting to Gemini...")

        self._session_thread = threading.Thread(
            target=self._run_session_thread, args=(initial_text,), daemon=True, name="amy-session"
        )
        self._session_thread.start()

    def _run_session_thread(self, initial_text: str) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self.wake_listener.pause(wait=True)   # free the microphone for the conversation
            sysctl.reset_session_approvals()
            if self._stop_requested:
                return
            session = AmyLiveSession(
                on_status=lambda s: self.status_changed.emit(s),
                on_mic_level=lambda lvl: self.mic_level_changed.emit(lvl),
                on_speaker_level=lambda lvl: self.speaker_level_changed.emit(lvl),
                on_transcript=lambda role, text: self.transcript_line.emit(role, text),
                on_state=lambda state: self.orb_state_changed.emit(state),
                initial_text=initial_text,
            )
            self._session = session
            if self._stop_requested:
                session.request_stop()
            loop.run_until_complete(session.run())
        except Exception as exc:  # noqa: BLE001
            message = _friendly_error(exc)
            self.status_changed.emit(f"Error: {message}")
            self.transcript_line.emit("system", f"⚠ {message}")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            loop.close()
            sysctl.reset_session_approvals()
            self._session = None
            self._loop = None
            self.session_finished.emit()          # UI clean-up happens on the GUI thread

    def _stop_session(self) -> None:
        if not self._active:
            return
        self._stop_requested = True
        session = self._session
        if session is not None:
            session.request_stop()                # thread-safe
        self.toggle_btn.setEnabled(False)         # no restart until the old session has fully closed
        self.status_changed.emit("Stopping...")

    def _on_session_finished(self) -> None:
        self._active = False
        self._stop_requested = False
        self._mic_level = 0.0
        self._speaker_level = 0.0
        self.orb.set_level(0.0)
        self.orb_state_changed.emit("idle")
        self.toggle_btn.setText("🎙  Talk to Amy")
        self.toggle_btn.setEnabled(True)
        self.status_changed.emit("Say 'Amy' to start...")
        self.wake_listener.resume()

    def closeEvent(self, event) -> None:
        self.wake_listener.stop()
        for holder in list(self._pending_confirms):   # unblock any worker waiting on a dialog
            holder["event"].set()
        self._stop_session()
        thread = self._session_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.5)
        super().closeEvent(event)


def launch() -> None:
    app = QApplication(sys.argv)
    window = AmyWindow()
    window.show()
    app.exec()
