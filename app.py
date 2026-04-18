from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow


APP_NAME = "CAN Scope"
APP_VERSION = "v00.00.05"


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    icon_path = Path(__file__).resolve().parent / "resources" / "app_icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow(app_name=APP_NAME, version=APP_VERSION)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
