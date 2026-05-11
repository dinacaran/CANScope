"""
Hidden activation for the Diagnostics window.

This is the **only** module that ``gui/main_window.py`` imports from the
diagnostics package. Pull this single line and the feature is invisible.
"""
from __future__ import annotations

import os

from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QMessageBox


def install_shortcut(main_window) -> None:
    """
    Install the Ctrl+Shift+A shortcut on *main_window*.

    No menu entry, no toolbar button — the feature is intentionally hidden
    until tested. The shortcut is suppressed entirely if
    ``CANSCOPE_DIAGNOSTICS=0`` is set in the environment.
    """
    if os.environ.get("CANSCOPE_DIAGNOSTICS", "1") == "0":
        return

    sc = QShortcut(QKeySequence("Ctrl+Shift+A"), main_window)
    sc.activated.connect(lambda: _open_diagnostics(main_window))


def _open_diagnostics(main_window) -> None:
    """Open the Diagnostics window — lazy import to avoid startup cost."""
    if getattr(main_window, "store", None) is None:
        QMessageBox.information(
            main_window, "Diagnostics",
            "Load and decode a measurement file first, then press "
            "Ctrl+Shift+A to open Diagnostics."
        )
        return

    # Reuse a single window instance per main_window if already open
    existing = getattr(main_window, "_diagnostics_window", None)
    if existing is not None and existing.isVisible():
        existing.raise_()
        existing.activateWindow()
        return

    from gui.diagnostics.window import DiagnosticsWindow
    win = DiagnosticsWindow(main_window.store, parent=main_window)
    main_window._diagnostics_window = win
    win.show()
