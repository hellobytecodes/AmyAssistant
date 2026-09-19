"""
glow_orb.py
-----------
A pulsing, glowing orb widget -- Amy's "face". It idles with a slow gentle
breathing animation, and reacts sharply to live mic/speaker audio levels so
it visually feels alive while listening or speaking.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QColor, QPainter, QRadialGradient
from PyQt6.QtWidgets import QWidget

import config


class GlowOrb(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self._t = 0.0
        self._level = 0.0          # current audio level, 0..1
        self._display_level = 0.0  # smoothed for rendering
        self._state = "idle"       # idle | listening | speaking | thinking

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)  # ~60fps

    def set_state(self, state: str) -> None:
        self._state = state

    @pyqtSlot(float)
    def set_level(self, level: float) -> None:
        self._level = max(0.0, min(1.0, level))

    def _tick(self) -> None:
        self._t += 0.016
        # smooth toward the real level so it doesn't flicker
        self._display_level += (self._level - self._display_level) * 0.25
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        base_r = min(w, h) * 0.28

        breathing = 1.0 + 0.04 * math.sin(self._t * 1.3)
        reactive = 1.0 + 0.55 * self._display_level
        radius = base_r * breathing * reactive

        colors = {
            "idle": (config.ACCENT_COLOR, config.ACCENT_COLOR_2),
            "listening": ("#00E5FF", "#7C4DFF"),
            "speaking": ("#7C4DFF", "#FF4DFF"),
            "thinking": ("#FFD54D", "#7C4DFF"),
        }
        c1_hex, c2_hex = colors.get(self._state, colors["idle"])
        c1, c2 = QColor(c1_hex), QColor(c2_hex)

        # outer soft glow
        glow = QRadialGradient(QPointF(cx, cy), radius * 2.2)
        glow_color = QColor(c1)
        glow_color.setAlpha(70)
        glow.setColorAt(0.0, glow_color)
        transparent = QColor(c1)
        transparent.setAlpha(0)
        glow.setColorAt(1.0, transparent)
        painter.setBrush(glow)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(cx, cy), radius * 2.2, radius * 2.2)

        # core orb
        core = QRadialGradient(QPointF(cx - radius * 0.3, cy - radius * 0.3), radius * 1.4)
        core.setColorAt(0.0, c1.lighter(140))
        core.setColorAt(0.55, c1)
        core.setColorAt(1.0, c2)
        painter.setBrush(core)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # thin rotating ring for extra "AI" feel
        painter.setPen(Qt.PenStyle.NoPen)
        ring_color = QColor(c2)
        ring_color.setAlpha(120)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = painter.pen()
        painter.save()
        painter.translate(cx, cy)
        painter.rotate((self._t * 40) % 360)
        from PyQt6.QtGui import QPen

        ring_pen = QPen(ring_color, 2)
        painter.setPen(ring_pen)
        ring_r = radius * 1.35
        painter.drawArc(QRectF(-ring_r, -ring_r, ring_r * 2, ring_r * 2), 0, 120 * 16)
        painter.drawArc(QRectF(-ring_r, -ring_r, ring_r * 2, ring_r * 2), 180 * 16, 120 * 16)
        painter.restore()
