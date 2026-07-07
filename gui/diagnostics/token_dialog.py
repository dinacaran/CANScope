"""
GitHub-token configuration dialog for the Diagnostics window.

Lets a testing engineer paste a fine-grained PAT, validate it with a tiny live
request, and save it to ``%USERPROFILE%\\.canscope\\copilot_token`` — no manual
file editing or environment-variable juggling.  The dialog is deliberately
security-conscious: it never logs the token, only ever shows a masked form, and
clears the input field when it closes.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QVBoxLayout,
)

from core.diagnostics.llm import GitHubModelsClient, LLMError
from core.diagnostics.llm.client import DEFAULT_MODEL
from core.diagnostics.llm import token_store
from gui.diagnostics.worker import run_worker

_TOKEN_URL = "https://github.com/settings/tokens?type=beta"


class _TokenTestWorker(QObject):
    """Validates a pasted token with the cheapest possible live request.

    Runs on a background QThread (via :func:`run_worker`).  Emits
    ``finished(ok, message)`` — ``ok`` True on a successful round-trip, else the
    raw error text for the dialog to translate into a friendly message.
    """

    finished = Signal(bool, str)   # (ok, message)

    #: Keep the validation round-trip short — this is a UI-blocking test,
    #: not an analysis call, so don't wait anywhere near DEFAULT_TIMEOUT_S.
    _TEST_TIMEOUT_S = 15

    def __init__(self, token: str, model: str) -> None:
        super().__init__()
        self._token = token
        self._model = model

    def run(self) -> None:
        try:
            # max_tokens=1 → the cheapest completion the API will bill for.
            client = GitHubModelsClient(
                token=self._token, model=self._model, max_tokens=1,
                timeout_s=self._TEST_TIMEOUT_S,
            )
            client.chat([{"role": "user", "content": "ping"}])
            self.finished.emit(True, "")
        except Exception as exc:                      # noqa: BLE001
            self.finished.emit(False, str(exc))


class TokenConfigDialog(QDialog):
    """Modal dialog to test + save a GitHub PAT for the LLM features.

    Parameters
    ----------
    model : str
        Model id used for the validation round-trip (defaults to the app default).
    on_saved : callable | None
        Called with no arguments after a token is successfully saved, so the
        caller can invalidate any cached client and pick up the new token
        immediately (no app restart).
    """

    def __init__(self, parent=None, *, model: str | None = None, on_saved=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configure GitHub Token")
        self.setModal(True)
        self.setMinimumWidth(480)

        self._model = model or DEFAULT_MODEL
        self._on_saved = on_saved
        self._test_thread = None
        self._test_worker = None
        self._busy = False
        self._closed = False

        self._build_ui()
        self._refresh_status()

    # ── UI construction ─────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        intro = QLabel(
            "CANScope's AI diagnostics use the GitHub Models API. Paste a "
            "fine-grained personal access token below.<br>"
            f'<a href="{_TOKEN_URL}">Generate a fine-grained PAT</a> — no '
            "scopes needed."
        )
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        intro.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(intro)

        # ── status line ──
        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("padding: 4px 0;")
        layout.addWidget(self._status_label)

        # ── token entry row ──
        entry_row = QHBoxLayout()
        self._token_edit = QLineEdit()
        self._token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._token_edit.setPlaceholderText("github_pat_…")
        self._token_edit.returnPressed.connect(self._on_test_and_save)
        entry_row.addWidget(self._token_edit, 1)

        self._toggle_btn = QPushButton("Show")
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setFixedWidth(60)
        self._toggle_btn.toggled.connect(self._on_toggle_echo)
        entry_row.addWidget(self._toggle_btn)
        layout.addLayout(entry_row)

        # ── button row ──
        btn_row = QHBoxLayout()
        self._save_btn = QPushButton("Test && Save")
        self._save_btn.setDefault(True)
        self._save_btn.clicked.connect(self._on_test_and_save)

        self._remove_btn = QPushButton("Remove token")
        self._remove_btn.clicked.connect(self._on_remove)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)

        btn_row.addWidget(self._save_btn)
        btn_row.addWidget(self._remove_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._cancel_btn)
        layout.addLayout(btn_row)

    # ── status ──────────────────────────────────────────────────────────

    def _refresh_status(self) -> None:
        """Show where the active token currently comes from (masked)."""
        token, source = token_store.resolve_token_source()
        if source in token_store.ENV_VARS:
            self._status_label.setText(
                f"⚠ Token from <b>{source}</b> environment variable is in use "
                f"({token_store.mask_token(token)}); the saved file will be "
                "ignored while that variable is set."
            )
            # Env wins → removing the file changes nothing; keep it available
            # but the message on Remove explains the precedence.
            self._remove_btn.setEnabled(token_store.load_token() is not None)
        elif source == "file":
            self._status_label.setText(
                f"✓ Using saved token {token_store.mask_token(token)} from "
                f"{token_store.token_file_path()}"
            )
            self._remove_btn.setEnabled(True)
        else:
            self._status_label.setText("Not configured — no token found.")
            self._remove_btn.setEnabled(False)

    # ── input handlers ──────────────────────────────────────────────────

    def _on_toggle_echo(self, shown: bool) -> None:
        self._token_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        )
        self._toggle_btn.setText("Hide" if shown else "Show")

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        for btn in (self._save_btn, self._remove_btn, self._cancel_btn,
                    self._toggle_btn):
            btn.setEnabled(not busy)
        self._token_edit.setEnabled(not busy)
        if busy and message:
            self._status_label.setText(message)
        if not busy:
            self._refresh_status()

    def _on_test_and_save(self) -> None:
        token = self._token_edit.text().strip()
        if not token:
            QMessageBox.warning(self, "No token", "Paste a token first.")
            return
        if any(ch.isspace() for ch in token):
            QMessageBox.warning(
                self, "Invalid token",
                "The token contains spaces or line breaks. Paste the value "
                "exactly as shown on GitHub.",
            )
            return
        if not token_store.looks_like_pat(token):
            proceed = QMessageBox.question(
                self, "Unrecognised token format",
                "This doesn't start with 'github_pat_' or 'ghp_'. Save and "
                "test it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if proceed != QMessageBox.StandardButton.Yes:
                return

        # Validate on a background thread; keep the pasted token local.
        self._set_busy(True, "Testing token…")
        worker = _TokenTestWorker(token, self._model)
        # Keep a strong reference so the worker survives until it's done —
        # run_worker's thread also holds one, but belt-and-braces here too.
        self._test_worker = worker
        # Bind the token to the success handler without stashing it on self.
        worker.finished.connect(
            lambda ok, msg, tok=token: self._on_test_finished(ok, msg, tok)
        )
        self._test_thread = run_worker(self, worker)
        self._test_thread.finished.connect(self._on_test_thread_finished)

    def _on_test_finished(self, ok: bool, message: str, token: str) -> None:
        if self._closed:
            return  # dialog closed mid-test; drop the result
        self._test_worker = None
        if not ok:
            self._set_busy(False)
            self._show_test_error(message)
            return
        # Success → persist and take effect immediately.
        try:
            token_store.save_token(token)
        except OSError as exc:
            self._set_busy(False)
            QMessageBox.critical(
                self, "Could not save token",
                f"The token is valid but writing the file failed:\n{exc}",
            )
            return
        self._set_busy(False)
        if callable(self._on_saved):
            self._on_saved()
        QMessageBox.information(
            self, "Token saved",
            "Token validated and saved. AI diagnostics are ready to use.",
        )
        self.accept()

    def _on_test_thread_finished(self) -> None:
        """Watchdog: guarantee the UI is never stuck disabled forever.

        Normally ``_on_test_finished`` already re-enabled the UI by the time
        the thread quits. If the worker died without emitting ``finished``
        (e.g. it raised outside the try/except, or was destroyed), this is
        the last line of defence.
        """
        self._test_thread = None
        self._test_worker = None
        if self._closed or not self._busy:
            return
        self._set_busy(False)
        self._show_test_error("")

    def _show_test_error(self, raw: str) -> None:
        """Translate a raw error into a friendly, token-free message."""
        lowered = raw.lower()
        if "401" in raw or "rejected" in lowered or "unauthor" in lowered:
            QMessageBox.warning(
                self, "Token rejected",
                "The token was rejected (HTTP 401). Generate a new "
                f"fine-grained PAT at:\n{_TOKEN_URL}",
            )
        else:
            QMessageBox.warning(
                self, "Network error",
                "Could not reach the GitHub Models API. Check your internet "
                "connection or proxy settings and try again.",
            )
        # Existing saved token is untouched — we only save on success.

    def _on_remove(self) -> None:
        _token, source = token_store.resolve_token_source()
        file_exists = token_store.load_token() is not None

        if source in token_store.ENV_VARS:
            note = (
                f"\n\nNote: the token currently in use comes from the "
                f"<b>{source}</b> environment variable, which takes priority "
                "and cannot be removed by this app. "
                + ("Removing the saved file will have no effect until that "
                   "variable is unset." if file_exists
                   else "There is no saved file to remove.")
            )
            if not file_exists:
                QMessageBox.information(
                    self, "Remove token", note.strip(),
                )
                return
        else:
            note = ""

        confirm = QMessageBox.question(
            self, "Remove token",
            "Delete the saved token file "
            f"({token_store.token_file_path()})?" + note,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        removed = token_store.remove_token()
        if removed and callable(self._on_saved):
            # The cached client may hold the now-deleted token.
            self._on_saved()
        self._refresh_status()

    # ── lifecycle ───────────────────────────────────────────────────────

    def _clear_input(self) -> None:
        self._token_edit.clear()
        if self._toggle_btn.isChecked():
            self._toggle_btn.setChecked(False)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # A test may still be running in the background (e.g. the user hit
        # Escape or the title-bar close button). Mark ourselves closed so
        # any late-arriving worker signal is dropped instead of touching
        # widgets after the caller has moved on — the worker thread is left
        # to finish on its own (bounded by _TokenTestWorker's timeout).
        self._closed = True
        self._clear_input()
        super().closeEvent(event)

    def done(self, result: int) -> None:  # noqa: N802 (Qt override)
        # Covers accept()/reject() as well as window-close.
        self._closed = True
        self._clear_input()
        super().done(result)
