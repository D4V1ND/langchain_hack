from html import escape
from pathlib import Path
import sys
import threading

from pynput import keyboard
from PySide6.QtCore import QBuffer, QIODevice, QObject, QPoint, QRect, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import (
    QCursor,
    QDesktopServices,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QTransform,
)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QMenu,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

import brain


HOTKEY = "<ctrl>+<alt>+l"
PAD = 40
TEMPLATE = Path(__file__).with_name("panel.html").read_text(encoding="utf-8")


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
        f'<p class="{cls}">{escape(text)}</p>'
        for cls, text in (("muted", question), ("muted", status), ("error", error))
        if text
    )
    if explanation is None:
        body = "" if header else '<p class="muted">Type your question below.</p>'
    else:
        body = explanation.html or f"<p>{escape(explanation.answer)}</p>"
    return TEMPLATE.replace("<!--HEADER-->", header, 1).replace("<!--BODY-->", body, 1)


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
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
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
        self.resize(440, 560)
        self.move(panel_pos(selection, screen, self.width(), self.height()))

        self.view = QWebEngineView()
        self.view.setPage(PanelPage(self.view))
        self.view.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask about your selection…")
        self.input.returnPressed.connect(self.ask)
        layout = QVBoxLayout(self)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.input)
        self.render()

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
        self.input.setFocus()
        self.render()

    def ask_failed(self, exc):
        self.asking = False
        if self.closed:
            self.end_session()
            return
        self.input.setEnabled(True)
        self.input.setFocus()
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
