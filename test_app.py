import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.dont_write_bytecode = True

from PySide6.QtGui import QColor, QImage, QPainterPath
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

import math
from array import array
from app import RATE, SILENCE_MS, TurnDetector, make_images, page, panel_pos
from brain import Explanation


app = QApplication([])
shot = QImage(400, 200, QImage.Format.Format_RGB32)
shot.fill(QColor("white"))
shot.setDevicePixelRatio(2)
path = QPainterPath()
path.moveTo(20, 20)
path.lineTo(60, 20)
path.lineTo(60, 50)
snapshot_png, crop_png = make_images(shot, path, 200)
marked = QImage.fromData(snapshot_png, "PNG")
crop = QImage.fromData(crop_png, "PNG")
assert (marked.width(), marked.height()) == (400, 200)
assert marked.pixelColor(80, 40) == QColor("red")
assert (crop.width(), crop.height()) == (200, 180)

screen = QRect(0, 0, 800, 600)
left = QRect(20, 30, 40, 40)
right = QRect(750, 80, 30, 30)
assert panel_pos(left, screen, 200, 180).x() == left.right() + 16
assert panel_pos(right, screen, 200, 180).x() == right.left() - 200 - 16
for screen in (screen, QRect(-1280, -120, 1280, 720)):
    for selection in (
        QRect(screen.topLeft(), left.size()),
        QRect(screen.right() - 5, screen.bottom() - 5, 20, 20),
        QRect(screen.left() - 30, screen.top() - 30, 20, 20),
    ):
        pos = panel_pos(selection, screen, 200, 180)
        assert screen.contains(QRect(pos.x(), pos.y(), 200, 180))

html = page("<b>q</b>", Explanation(answer="a", html="<svg id='d'></svg>"), "", "")
assert "&lt;b&gt;q&lt;/b&gt;" in html and "<svg id='d'></svg>" in html
assert "default-src 'none'" in html
assert "<p>a &lt; b</p>" in page("", Explanation(answer="a < b"), "", "")
assert "type your question below" in page("", None, "", "")

# Turn detection: quiet room -> speech -> pause ends the turn; a click is a "blip", not a question.
def tone(amp, ms=50):
    return array("h", (int(amp * math.sin(i / 8)) for i in range(RATE * ms // 1000))).tobytes()

pause = math.ceil(SILENCE_MS / 50)
d = TurnDetector()
assert {d.feed(tone(30))[0] for _ in range(20)} == {"quiet"}
action, audio = d.feed(tone(6000))
assert action == "start" and len(audio) > len(tone(6000))  # includes the pre-roll
assert {d.feed(tone(6000))[0] for _ in range(10)} == {"speech"}
after = [d.feed(tone(30))[0] for _ in range(pause)]
assert after[-1] == "end" and set(after[:-1]) == {"speech"}
assert d.feed(tone(6000))[0] == "start"
assert [d.feed(tone(30))[0] for _ in range(pause)][-1] == "blip"

print("ok")
