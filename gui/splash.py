"""
CAN Scope splash screen.

Displays the splash image while the application is initialising.
Overlays:
  - Version string (bottom-right of image, programmatically rendered)
  - Live loading status text (inside the frosted-glass panel area of the image)

The splash is shown before MainWindow is constructed so the user sees
something immediately even on slow machines where heavy imports take time.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore  import Qt, QTimer
from PySide6.QtGui   import (
    QColor, QFont, QFontMetrics, QPainter, QPixmap
)
from PySide6.QtWidgets import QSplashScreen, QApplication


# ── Layout constants (tuned to 1635 × 962 splash image) ──────────────────
# Status text box area on the image (the frosted-glass panel)
_BOX_X      = 80       # left edge of text box (px)
_BOX_Y      = 700      # top edge of text box (px)
_BOX_W      = 1475     # width of text box (px)
_BOX_H      = 120      # height of text box (px)

# Version overlay — bottom-right corner
_VER_MARGIN = 18       # pixels from right / bottom edges

# Status text appearance
_STATUS_FONT_SIZE = 22  # pt
_STATUS_COLOR     = QColor('#a0c8ff')  # light blue — matches splash palette
_VERSION_COLOR    = QColor('#6090b0')  # muted blue-grey


class CANScopeSplash(QSplashScreen):
    """
    Splash screen shown during application startup.

    Usage::

        splash = CANScopeSplash(version='v00.00.12')
        splash.show()
        app.processEvents()

        # ... heavy initialisation ...
        splash.set_status('Loading signal store...')
        app.processEvents()

        window.show()
        splash.finish(window)
    """

    def __init__(self, version: str = '') -> None:
        img_path = Path(__file__).resolve().parents[1] / 'resources' / 'splashscreen.png'
        if img_path.exists():
            base_pixmap = QPixmap(str(img_path))
        else:
            # Fallback: plain dark rectangle if image missing
            base_pixmap = QPixmap(1635, 962)
            base_pixmap.fill(QColor('#0d1b2a'))

        # Scale to 60% of screen height for reasonable display size
        screen = QApplication.primaryScreen()
        if screen:
            avail_h = screen.availableGeometry().height()
            target_h = int(avail_h * 0.60)
        else:
            target_h = 577   # 962 * 0.60

        self._scale = target_h / base_pixmap.height()
        scaled = base_pixmap.scaled(
            int(base_pixmap.width()  * self._scale),
            int(base_pixmap.height() * self._scale),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        super().__init__(scaled, Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        self._version     = version
        self._status_text = 'Starting...'

        # Re-render immediately so version appears before first processEvents
        self._render()

    # ── Public API ────────────────────────────────────────────────────────

    def set_status(self, message: str) -> None:
        """Update the loading status text shown in the frosted-glass panel."""
        self._status_text = message
        self._render()
        QApplication.processEvents()

    # ── Rendering ─────────────────────────────────────────────────────────

    def _render(self) -> None:
        """Composite status text + version onto the base pixmap and repaint."""
        pm = self.pixmap().copy()   # fresh copy each call (base pixmap is cached)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        s = self._scale

        # ── Status text inside the frosted-glass box ──────────────────────
        font = QFont('Consolas', int(_STATUS_FONT_SIZE * s))
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(_STATUS_COLOR)

        box = (
            int(_BOX_X  * s),
            int(_BOX_Y  * s),
            int(_BOX_W  * s),
            int(_BOX_H  * s),
        )
        painter.drawText(
            *box,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            f'  {self._status_text}'
        )

        # ── Version overlay — bottom-right corner ─────────────────────────
        if self._version:
            ver_font = QFont('Segoe UI', int(13 * s))
            ver_font.setWeight(QFont.Weight.Light)
            painter.setFont(ver_font)
            painter.setPen(_VERSION_COLOR)
            margin = int(_VER_MARGIN * s)
            pw, ph = pm.width(), pm.height()
            fm = QFontMetrics(ver_font)
            tw = fm.horizontalAdvance(self._version)
            painter.drawText(
                pw - tw - margin,
                ph - margin - fm.descent(),
                self._version,
            )

        painter.end()
        self.setPixmap(pm)
