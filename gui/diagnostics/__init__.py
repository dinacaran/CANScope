"""
Diagnostics GUI — separate window for fault detection + AI analysis.

Hidden by default. Activated via Ctrl+Shift+A; the only touch-point in
``gui/main_window.py`` is a single call to
:func:`gui.diagnostics.activation.install_shortcut`.

Set the env var ``CANSCOPE_DIAGNOSTICS=0`` to disable the shortcut entirely.
"""
