from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

APP_NAME    = "CAN Scope"
APP_VERSION = "v00.00.37"


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    # Resolve resource path for both dev and frozen EXE
    _res_root = (
        Path(sys._MEIPASS) if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
        else Path(__file__).resolve().parent
    )
    # Prefer PNG (full RGBA, sharp on HiDPI); fall back to ICO
    for _icon_name in ('CANScope_ICON.png', 'app_icon.ico'):
        _icon_path = _res_root / 'resources' / _icon_name
        if _icon_path.exists():
            app.setWindowIcon(QIcon(str(_icon_path)))
            break

    # ── Show splash immediately — before any heavy imports ────────────────
    # gui.splash only imports PySide6 (already loaded) + pathlib.
    # All heavy modules (cantools, python-can, asammdf, pyqtgraph) are
    # imported lazily when MainWindow / PlotPanel are first constructed.
    from gui.splash import CANScopeSplash
    splash = CANScopeSplash(version=APP_VERSION)
    splash.show()
    app.processEvents()

    splash.set_status('Loading UI components...')
    from gui.main_window import MainWindow   # ← heavy imports happen here

    splash.set_status('Building main window...')
    window = MainWindow(app_name=APP_NAME, version=APP_VERSION, splash=splash)

    window.show()
    splash.finish(window)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
