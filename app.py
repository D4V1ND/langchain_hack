from array import array
from html import escape
import math
import os
from pathlib import Path
import sys
import threading

from pynput import keyboard
from PySide6.QtCore import QBuffer, QIODevice, QObject, QPoint, QRect, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QTransform,
)
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

import brain


HOTKEY = "<ctrl>+<alt>+l"
PAD = 40
RATE = brain.LIVE_AUDIO_RATE  # the mic streams 24 kHz mono 16-bit, what live transcription expects
# Turn detection (calibration knobs): how the app decides you've finished speaking.
SILENCE_MS = 1100    # this much quiet after speech ends your question
MIN_SPEECH_MS = 300  # shorter sounds (a cough, a click) are ignored
MAX_TURN_MS = 30000  # a turn this long ends anyway, in case the room never gets quiet
PREROLL_MS = 300     # audio kept from just before you started, so the first word isn't clipped
TEMPLATE = Path(__file__).with_name("panel.html").read_text(encoding="utf-8")

# Panel chrome, matching panel.html: teal sky, cream paper, ink outlines, mustard and coral accents.
STYLE = """
#panel { background: #6cc3bc; }
#title { color: #26262b; font: bold 23px "Ink Free"; }
QPushButton { background: #f2b632; color: #26262b; border: 2px solid #26262b; border-bottom-width: 4px;
              border-radius: 12px; font-size: 15px; }
QPushButton:hover { background: #f5c45a; }
QPushButton:pressed { border-bottom-width: 2px; margin-top: 2px; }
QPushButton:disabled { background: #d8cfae; color: #6d6a60; }
QPushButton[recording="true"] { background: #e2733f; color: #f6f1e4; }
#close { background: #f6f1e4; font-weight: bold; }
QLineEdit { background: #f6f1e4; color: #26262b; border: 2px solid #26262b; border-bottom-width: 4px;
            border-radius: 14px; padding: 6px 12px; font: 14px "Segoe UI"; selection-background-color: #f2b632; }
QLineEdit:disabled { background: #e6dfcc; color: #6d6a60; }
"""


def make_images(shot: QImage, path: QPainterPath, logical_width: int) -> tuple[bytes, bytes]:
    k = shot.width() / logical_width
    marked = shot.copy()
    marked.setDevicePixelRatio(1)
    painter = QPainter(marked)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(k, k)
    painter.setPen(QPen(Qt.GlobalColor.red, 4))
    painter.drawPath(path)
    painter.end()

    bounds = path.boundingRect().adjusted(-PAD, -PAD, PAD, PAD)
    rect = QTransform.fromScale(k, k).mapRect(bounds).toRect().intersected(marked.rect())
    crop = marked.copy(rect)

    def png_bytes(image):
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        return bytes(buffer.data())

    return png_bytes(marked), png_bytes(crop)


def page(question: str, explanation, status: str, error: str) -> str:
    """The panel page: our escaped header, then the HTML the AI wrote for this answer."""
    header = "".join(
        f'<div class="{cls}">{escape(text)}</div>'
        for cls, text in (("q", question), ("status", status), ("error", error))
        if text
    )
    if explanation is not None:
        body = f'<div class="card">{explanation.html or "<p>" + escape(explanation.answer) + "</p>"}</div>'
    elif header:
        body = ""
    else:
        body = '<p class="hint">Ask out loud, or type your question below.</p>'
    return TEMPLATE.replace("<!--HEADER-->", header, 1).replace("<!--BODY-->", body, 1)


def loudness(chunk: bytes) -> float:
    """RMS level of 16-bit PCM audio."""
    samples = array("h", chunk[: len(chunk) // 2 * 2])
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


class TurnDetector:
    """Finds where a spoken question starts and ends, from mic loudness alone.

    feed(chunk) returns (action, audio_to_send):
      "quiet"  nothing to send          "start"  you began talking (audio includes the pre-roll)
      "speech" keep sending             "end"    you paused long enough: send, then commit
      "blip"   too short to be a question (a cough or click): clear it
    """

    def __init__(self):
        self.floor = 200.0  # running estimate of the room's background loudness
        self.reset()

    def reset(self):
        self.speaking = False
        self.voiced_ms = self.quiet_ms = self.turn_ms = 0.0
        self.preroll = b""

    def feed(self, chunk: bytes) -> tuple[str, bytes]:
        ms = len(chunk) / 2 / RATE * 1000
        level = loudness(chunk)
        # ponytail: plain loudness, so a noisy room can cut in; OpenAI-side semantic turn detection if it matters.
        loud = level > self.floor * 2.5 + 300
        if not self.speaking:
            if not loud:
                self.floor = 0.95 * self.floor + 0.05 * level
                self.preroll = (self.preroll + chunk)[-int(RATE * 2 * PREROLL_MS / 1000):]
                return "quiet", b""
            self.speaking, self.voiced_ms, self.quiet_ms, self.turn_ms = True, ms, 0.0, ms
            audio, self.preroll = self.preroll + chunk, b""
            return "start", audio
        self.turn_ms += ms
        if loud:
            self.voiced_ms += ms
            self.quiet_ms = 0.0
        else:
            self.quiet_ms += ms
        if self.quiet_ms >= SILENCE_MS or self.turn_ms >= MAX_TURN_MS:
            self.speaking = False
            return ("end" if self.voiced_ms >= MIN_SPEECH_MS else "blip"), chunk
        return "speech", chunk


def pick_mic():
    """The mic whose name contains STUDYLENS_MIC (from .env), else Windows' default mic."""
    wanted = os.getenv("STUDYLENS_MIC", "").strip().lower()
    for device in QMediaDevices.audioInputs():
        if wanted and wanted in device.description().lower():
            return device
    return QMediaDevices.defaultAudioInput()


class Mic:
    """Streams the chosen microphone to on_chunk as 24 kHz mono 16-bit PCM (on the UI thread)."""

    def __init__(self, on_chunk):
        fmt = QAudioFormat()
        fmt.setSampleRate(RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self.source = QAudioSource(pick_mic(), fmt)
        self.io = self.source.start()
        self.io.readyRead.connect(lambda: on_chunk(self.io.readAll().data()))

    def stop(self):
        self.source.stop()


def round_corners(widget):
    """Windows 11: rounded corners and an ink-coloured edge for a frameless window."""
    if sys.platform != "win32":
        return
    import ctypes

    hwnd = int(widget.winId())
    for attribute, value in ((33, 2), (34, 0x002B2626)):  # corner preference = round; border colour 0x00BBGGRR
        v = ctypes.c_int(value)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(v), ctypes.sizeof(v))


class Header(QWidget):
    """The panel's title bar. Dragging it moves the frameless window."""

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window().windowHandle().startSystemMove()


class PanelPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, kind, is_main_frame):
        # The page is AI-written: never let a link navigate the panel away.
        return kind != QWebEnginePage.NavigationType.NavigationTypeLinkClicked


def panel_pos(sel: QRect, screen: QRect, w: int, h: int) -> QPoint:
    x = sel.right() + 16
    if x + w > screen.right() + 1:
        x = sel.left() - w - 16
    x = max(screen.left(), min(x, screen.right() - w + 1))
    y = max(screen.top(), min(sel.top(), screen.bottom() - h + 1))
    return QPoint(x, y)


class Overlay(QWidget):
    def __init__(self, screen, snapshot, finished):
        super().__init__(None, Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setGeometry(screen.geometry())
        # ponytail: Mixed-DPI multi-monitor setups are not handled.
        self.snapshot = snapshot
        self.finished = finished
        self.path = QPainterPath()
        self.selection = None
        self.drawing = False
        self.setCursor(Qt.CursorShape.CrossCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.snapshot)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(Qt.GlobalColor.red, 4))
        painter.drawPath(self.path)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.path = QPainterPath(event.position())
            self.drawing = True
            self.update()

    def mouseMoveEvent(self, event):
        if self.drawing:
            self.path.lineTo(event.position())
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.drawing:
            self.path.lineTo(event.position())
            self.drawing = False
            bounds = self.path.boundingRect()
            if not (bounds.width() < 10 and bounds.height() < 10):
                self.selection = self.path
            self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.finished(self.selection)
        super().closeEvent(event)


class Panel(QWidget):
    def __init__(self, controller, snapshot_png, crop_png, selection, screen):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.FramelessWindowHint)
        self.controller = controller
        self.snapshot_png = snapshot_png
        self.crop_png = crop_png
        self.session_id = None
        self.session_ended = False
        self.asking = False
        self.closed = False
        self.question = ""
        self.explanation = None
        self.setWindowTitle("StudyLens")
        self.setObjectName("panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(STYLE)
        self.resize(460, 600)
        self.move(panel_pos(selection, screen, self.width(), self.height()))
        round_corners(self)

        header = Header()
        title = QLabel("StudyLens")
        title.setObjectName("title")
        close = QPushButton("✕")
        close.setObjectName("close")
        close.setToolTip("Close (saves your notes)")
        close.setFixedSize(32, 32)
        close.clicked.connect(self.close)
        top = QHBoxLayout(header)
        top.setContentsMargins(6, 0, 0, 0)
        top.addWidget(title)
        top.addStretch()
        top.addWidget(close)

        self.view = QWebEngineView()
        self.view.setPage(PanelPage(self.view))
        self.view.page().setBackgroundColor(QColor("#6cc3bc"))  # no white flash between renders
        self.view.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask about your selection…")
        self.input.setMinimumHeight(42)
        self.input.returnPressed.connect(self.ask)
        self.input.textEdited.connect(self.typed)
        self.voice = None  # (Mic, LiveTranscriber, TurnDetector) while voice is on
        self.mic = QPushButton("🎤")
        self.mic.setFixedSize(48, 42)
        self.mic.clicked.connect(lambda: self.set_voice(self.voice is None))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.input, 1)
        row.addWidget(self.mic)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(6)
        layout.addWidget(header)
        layout.addWidget(self.view, 1)
        layout.addLayout(row)
        self.render()
        self.set_voice(True)  # listening from the start; the 🎤 button turns it off

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:  # no title bar, so Esc closes too
            self.close()
        else:
            super().keyPressEvent(event)

    def set_voice(self, on):
        if on == (self.voice is not None):
            return
        if on:
            call = self.controller.call.emit  # transcriber callbacks arrive on a background thread
            transcriber = brain.LiveTranscriber(
                lambda text: call(lambda: self.heard_partial(text)),
                lambda text: call(lambda: self.heard(text)),
                lambda exc: call(lambda: self.voice_failed(exc)),
            )
            self.voice = (Mic(self.listen), transcriber, TurnDetector())
        else:
            mic, transcriber, _ = self.voice
            mic.stop()
            transcriber.close()
            self.voice = None
        self.mic.setToolTip("Listening. Click to type instead" if on else "Click to talk")
        self.mic.setProperty("recording", on)
        self.mic.style().unpolish(self.mic)  # re-apply the [recording] style
        self.mic.style().polish(self.mic)
        self.input.setPlaceholderText("Listening… just ask, or type here" if on else "Type your question…")
        if not on:
            self.input.setFocus()

    def listen(self, chunk):
        if self.voice is None or self.asking:
            return  # paused while the tutor thinks
        _, transcriber, detector = self.voice
        action, audio = detector.feed(chunk)
        if audio:
            transcriber.append(audio)
        if action == "end":
            transcriber.commit()
        elif action == "blip":
            transcriber.clear()
            self.input.clear()

    def heard_partial(self, text):
        if self.voice is not None and not self.asking and not self.closed:
            self.input.setText(text.strip())  # live captions while you speak

    def heard(self, text):
        if self.voice is None or self.asking or self.closed:
            return
        if not text:
            self.input.clear()
            return
        self.input.setText(text)
        self.ask()  # you stopped talking: send it like pressing Enter

    def typed(self, _text):
        self.set_voice(False)  # typing means you'd rather type; keeps any caption so you can fix it

    def voice_failed(self, exc):
        if self.closed:
            return
        self.set_voice(False)
        self.render(error="Voice stopped (couldn't reach live transcription). You can type instead.")

    def render(self, status="", error=""):
        html = page(self.question, self.explanation, status, error)
        self.view.setHtml(html, QUrl("https://studylens.local/"))

    def ask(self):
        question = self.input.text().strip()
        if not question or self.asking:
            return
        self.question = question
        self.asking = True
        self.input.setEnabled(False)
        self.mic.setEnabled(False)
        self.render(status="Thinking…")

        def work():
            if self.session_id is None:
                self.session_id = brain.start_session(self.snapshot_png, self.crop_png)
            return brain.ask(self.session_id, question)

        self.controller.in_background(work, self.ask_done, self.ask_failed)

    def ask_done(self, explanation):
        self.asking = False
        if self.closed:
            self.end_session()
            return
        self.explanation = explanation
        self.input.clear()
        self.input.setEnabled(True)
        self.mic.setEnabled(True)
        self.input.setFocus()
        if self.voice is not None:
            self.voice[2].reset()  # listen afresh for the next question
        self.render()

    def ask_failed(self, exc):
        self.asking = False
        if self.closed:
            self.end_session()
            return
        self.input.setEnabled(True)
        self.mic.setEnabled(True)
        self.input.setFocus()
        if self.voice is not None:
            self.voice[2].reset()
        self.render(error="Couldn't reach the AI, try again.")

    def end_session(self):
        # Wait for an in-flight ask before saving its Transcript.
        if self.asking or self.session_id is None or self.session_ended:
            return
        self.session_ended = True
        sid = self.session_id
        self.controller.in_background(
            lambda: brain.end_session(sid),
            self.controller.notes_saved,
            self.controller.save_failed,
        )

    def closeEvent(self, event):
        self.closed = True
        self.set_voice(False)
        self.end_session()
        super().closeEvent(event)


class Controller(QObject):
    call = Signal(object)

    def __init__(self, app):
        super().__init__(app)
        self.call.connect(lambda f: f(), Qt.ConnectionType.QueuedConnection)
        self.overlay = None
        self.panel = None
        self.selection_pending = False

        icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("StudyLens (Ctrl+Alt+L)")
        self.menu = QMenu()
        self.menu.addAction("Open Notebook").triggered.connect(self.open_notebook)
        self.menu.addAction("Quit").triggered.connect(app.quit)
        self.tray.setContextMenu(self.menu)
        self.tray.show()

        self.listener = keyboard.GlobalHotKeys({HOTKEY: lambda: self.call.emit(self.hotkey_pressed)})
        app.aboutToQuit.connect(self.listener.stop)
        self.listener.start()
        self.in_background(brain.consolidate_pending, self.notes_saved, self.save_failed)

    def in_background(self, work, done, fail):
        def run():
            try:
                result = work()
            except Exception as exc:
                self.call.emit(lambda exc=exc: fail(exc))
            else:
                self.call.emit(lambda: done(result))

        threading.Thread(target=run, daemon=True).start()

    def hotkey_pressed(self):
        if self.overlay is not None or self.selection_pending:
            return
        if self.panel is not None and not self.panel.closed:
            self.panel.close()
            self.panel = None
            self.selection_pending = True
            QTimer.singleShot(200, self.open_overlay)
        else:
            self.open_overlay()

    def open_overlay(self):
        self.selection_pending = False
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        snapshot = screen.grabWindow(0)
        self.overlay = Overlay(screen, snapshot, self.selection_finished)
        self.overlay.show()
        self.overlay.raise_()
        self.overlay.activateWindow()

    def selection_finished(self, path):
        overlay = self.overlay
        self.overlay = None
        if path is None:
            return
        snapshot_png, crop_png = make_images(overlay.snapshot.toImage(), path, overlay.width())
        selection = path.boundingRect().toRect().translated(overlay.geometry().topLeft())
        self.panel = Panel(self, snapshot_png, crop_png, selection, overlay.screen().availableGeometry())
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()
        self.panel.input.setFocus()

    def open_notebook(self):
        if brain.NOTEBOOK_PATH.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(brain.NOTEBOOK_PATH.resolve())))
        else:
            self.tray.showMessage("StudyLens", "No notes yet")

    def notes_saved(self, count):
        if count > 0:
            self.tray.showMessage("StudyLens", f"Saved {count} note{'s' if count != 1 else ''}")

    def save_failed(self, exc):
        self.tray.showMessage("StudyLens", "Couldn't save notes, will retry next start")


def main():
    app = QApplication(sys.argv)
    QApplication.setQuitOnLastWindowClosed(False)
    controller = Controller(app)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
