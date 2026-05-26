#!/usr/bin/env python3
"""audio-to-claude GUI

Left pane  — live editable transcript + Insert button
Right pane — Claude Code running in an embedded PTY terminal
"""

import fcntl
import os
import re
import select
import signal
import struct
import subprocess
import sys
import termios
from pathlib import Path

import pty
import pyte
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QKeyEvent, QTextCharFormat, QTextCursor, QWheelEvent
from PyQt6.QtCore import QMimeData
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from dotenv import load_dotenv

from audio_capture import AudioCapture
from transcriber import Transcriber

# ---------------------------------------------------------------------------
# pyte named-colour → hex (VS Code terminal palette)
# ---------------------------------------------------------------------------
_PYTE_COLORS: dict[str, str] = {
    # Light-terminal palette (similar to macOS Terminal "Basic" theme)
    "default":       "#1a1a1a",
    "black":         "#000000",
    "red":           "#c0392b",
    "green":         "#27ae60",
    "brown":         "#d68910",
    "blue":          "#2471a3",
    "magenta":       "#8e44ad",
    "cyan":          "#148f77",
    "white":         "#555555",
    "brightblack":   "#777777",
    "brightred":     "#e74c3c",
    "brightgreen":   "#2ecc71",
    "brightyellow":  "#f1c40f",
    "brightblue":    "#3498db",
    "brightmagenta": "#9b59b6",
    "brightcyan":    "#1abc9c",
    "brightwhite":   "#1a1a1a",
}


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


# ---------------------------------------------------------------------------
# Background threads
# ---------------------------------------------------------------------------

class _PtyReader(QThread):
    data = pyqtSignal(bytes)   # raw PTY bytes — pyte handles decoding

    def __init__(self, master_fd: int) -> None:
        super().__init__()
        self._fd = master_fd
        self._running = True

    def run(self) -> None:
        while self._running:
            try:
                r, _, _ = select.select([self._fd], [], [], 0.05)
                if r:
                    chunk = os.read(self._fd, 4096)
                    if chunk:
                        self.data.emit(chunk)
            except (OSError, KeyboardInterrupt):
                break

    def stop(self) -> None:
        self._running = False


class _AudioWorker(QThread):
    transcript = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, device_name: str, chunk_seconds: int) -> None:
        super().__init__()
        self._device = device_name
        self._seconds = chunk_seconds
        self._running = True

    def run(self) -> None:
        previous_text = ""
        pre_roll = None
        while self._running:
            try:
                # Re-read settings from os.environ each iteration so that
                # changes saved via the Settings dialog take effect immediately.
                capture = AudioCapture(device_name=os.getenv("AUDIO_DEVICE_NAME", self._device))
                transcriber = Transcriber()
                path, pre_roll = capture.record_until_silence(
                    min_seconds=float(os.getenv("CHUNK_SECONDS", "2")),
                    max_seconds=float(os.getenv("MAX_CHUNK_SECONDS", "6")),
                    silence_duration=float(os.getenv("SILENCE_DURATION", "0.8")),
                    silence_threshold=float(os.getenv("SILENCE_THRESHOLD", "0.005")),
                    pre_roll=pre_roll,
                )
                try:
                    text = transcriber.transcribe(path, previous_text=previous_text)
                    if text:
                        previous_text = (previous_text + " " + text)[-512:]
                        self.transcript.emit(text)
                except Exception as exc:
                    self.error.emit(str(exc))
                finally:
                    path.unlink(missing_ok=True)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                if self._running:
                    self.error.emit(str(exc))

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Zoom transcript file watcher
# ---------------------------------------------------------------------------

class _ZoomWatcher(QThread):
    """
    Polls ~/Documents/Zoom every second for Zoom's saved closed-caption file.
    File path pattern:
      ~/Documents/Zoom/<date> <title>/meeting_saved_closed_caption.txt
    Also auto-clicks Zoom's "Save Transcript" button via osascript every
    _SAVE_INTERVAL seconds so the user never has to do it manually.

    Requires one-time Accessibility permission:
      System Settings → Privacy & Security → Accessibility → add Terminal / the .app
    """
    transcript     = pyqtSignal(str)        # new formatted text block to append
    correction     = pyqtSignal(str, str)   # (old_text, new_text) in-place fix
    save_triggered = pyqtSignal(bool, str)  # (success, error_msg)

    _ZOOM_DIR      = Path.home() / "Documents" / "Zoom"
    _FILENAME      = "meeting_saved_closed_caption.txt"
    _SAVE_INTERVAL = 1           # seconds between automatic "Save Transcript" clicks
    # Zoom header line format: "[Speaker Name] HH:MM:SS"
    _HEADER_RE     = re.compile(r"^\[.+\]\s+\d{2}:\d{2}:\d{2}\s*$")

    # AppleScript that clicks Zoom's "Save transcript" button (identified by description,
    # since its name attribute is missing value). Checks the popped-out Transcript window
    # first, then falls back to searching all windows.
    _SAVE_SCRIPT = """\
tell application "System Events"
    if not (exists process "zoom.us") then return
    tell process "zoom.us"
        -- Primary: popped-out Transcript window (button is a direct child)
        try
            if exists window "Transcript" then
                repeat with elem in (every UI element of window "Transcript")
                    try
                        if (description of elem) is "Save transcript" then
                            click elem
                            return
                        end if
                    end try
                end repeat
            end if
        end try
        -- Fallback: scan all windows one level deep
        repeat with w in (every window)
            repeat with elem in (every UI element of w)
                try
                    if (description of elem) is "Save transcript" then
                        click elem
                        return
                    end if
                end try
            end repeat
        end repeat
    end tell
end tell
"""

    def __init__(self) -> None:
        super().__init__()
        self._running = True
        self._last_speaker: str = ""  # tracks speaker across poll cycles for grouping

    def run(self) -> None:
        import time
        current_file: Path | None = None
        last_entry_count: int = 0
        last_save_time: float = 0.0
        entries: list[tuple[str, str, str]] = []

        # index -> last observed text (for detecting Zoom ASR updates)
        entry_texts: dict[int, str] = {}
        # index -> text that was actually displayed (for building the correction)
        emitted_texts: dict[int, str] = {}

        while self._running:
            now = time.monotonic()

            # Auto-click "Save Transcript" in Zoom at the configured interval
            if now - last_save_time >= self._SAVE_INTERVAL:
                self._trigger_zoom_save()
                last_save_time = now

            # Read the file on every cycle (small text file; OS cache is free).
            try:
                txt_files = list(self._ZOOM_DIR.rglob(self._FILENAME))
                if txt_files:
                    newest = max(txt_files, key=lambda p: p.stat().st_mtime)

                    if newest != current_file:
                        current_file = newest
                        last_entry_count = 0
                        entry_texts = {}
                        emitted_texts = {}
                        entries = []
                        self._last_speaker = ""

                    if current_file is not None:
                        raw = current_file.read_text(encoding="utf-8", errors="replace")
                        entries = self._parse_entries(raw)
                        for i, (_, _, text) in enumerate(entries):
                            entry_texts[i] = text
            except Exception:
                pass

            # 1. Emit any brand-new entries immediately so the user can copy them now.
            try:
                n = len(entries)
                if n > last_entry_count:
                    new_entries = entries[last_entry_count:n]
                    formatted = self._format_entries(new_entries)
                    if formatted:
                        self.transcript.emit(formatted)
                    for idx, (_, _, text) in enumerate(new_entries, start=last_entry_count):
                        emitted_texts[idx] = text
                    last_entry_count = n
            except Exception:
                pass

            # 2. Check every already-displayed entry for ASR corrections.
            #    Zoom retroactively refines earlier captions; when the text changes
            #    we send (old_text, new_text) so the pane can update in-place.
            try:
                for i in range(last_entry_count):
                    current = entry_texts.get(i, "")
                    emitted = emitted_texts.get(i, "")
                    if current and emitted and current != emitted:
                        self.correction.emit(emitted, current)
                        emitted_texts[i] = current
            except Exception:
                pass

            self.msleep(1000)

    def _trigger_zoom_save(self) -> None:
        """Fire-and-forget osascript call to click Zoom's Save Transcript button."""
        try:
            result = subprocess.run(
                ["osascript", "-e", self._SAVE_SCRIPT],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                self.save_triggered.emit(True, "")
            else:
                err = (result.stderr or result.stdout or b"").decode(errors="replace").strip()
                self.save_triggered.emit(False, err or f"exit {result.returncode}")
        except Exception as exc:
            self.save_triggered.emit(False, str(exc))

    @classmethod
    def _parse_entries(cls, content: str) -> list[tuple[str, str, str]]:
        """
        Parse a Zoom closed-caption file into structured entries.
        Each entry is (speaker, timestamp, text) where text has wrapped
        continuation lines joined into a single string.

        File format:
            [Speaker Name] HH:MM:SS
            Text line one that may be
            wrapped onto a second line

            [Speaker Name] HH:MM:SS
            Next entry text
        """
        entries: list[tuple[str, str, str]] = []
        current_speaker = ""
        current_ts = ""
        pending: list[str] = []

        header_re = re.compile(r"^\[(.+)\]\s+(\d{2}:\d{2}:\d{2})\s*$")

        for raw in content.splitlines():
            line = raw.strip()
            m = header_re.match(line)
            if m:
                # Flush previous entry's accumulated text
                if pending:
                    text = " ".join(pending).strip()
                    if text:
                        entries.append((current_speaker, current_ts, text))
                    pending = []
                current_speaker = m.group(1)
                current_ts = m.group(2)
            elif line:
                pending.append(line)

        # Flush the final entry
        if pending:
            text = " ".join(pending).strip()
            if text:
                entries.append((current_speaker, current_ts, text))

        return entries

    def _format_entries(self, entries: list[tuple[str, str, str]]) -> str:
        """
        Format new caption entries for display.
        Shows [Speaker] header only when the speaker changes;
        separates consecutive text blocks with a blank line.
        """
        result: list[str] = []
        last_was_header = False

        for speaker, _ts, text in entries:
            if speaker != self._last_speaker:
                self._last_speaker = speaker
                if result:
                    result.append("")  # blank line before new speaker block
                result.append(f"[{speaker}]")
                last_was_header = True

            if result and not last_was_header:
                result.append("")  # blank line between same-speaker entries
            result.append(text)
            last_was_header = False

        return "\n".join(result)

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Smooth-scrolling text display for the terminal output
# ---------------------------------------------------------------------------

class _SmoothTextEdit(QTextEdit):
    """Read-only QTextEdit with trackpad scrolling and direct PTY key forwarding."""

    _KEY_MAP: dict = {
        Qt.Key.Key_Return:   b'\r',
        Qt.Key.Key_Enter:    b'\r',
        Qt.Key.Key_Backspace: b'\x7f',
        Qt.Key.Key_Tab:      b'\t',
        Qt.Key.Key_Escape:   b'\x1b',
        Qt.Key.Key_Up:       b'\x1b[A',
        Qt.Key.Key_Down:     b'\x1b[B',
        Qt.Key.Key_Right:    b'\x1b[C',
        Qt.Key.Key_Left:     b'\x1b[D',
        Qt.Key.Key_Home:     b'\x1b[H',
        Qt.Key.Key_End:      b'\x1b[F',
        Qt.Key.Key_PageUp:   b'\x1b[5~',
        Qt.Key.Key_PageDown: b'\x1b[6~',
        Qt.Key.Key_Delete:   b'\x1b[3~',
    }
    _CTRL_MAP: dict = {
        Qt.Key.Key_C: b'\x03',
        Qt.Key.Key_D: b'\x04',
        Qt.Key.Key_Z: b'\x1a',
        Qt.Key.Key_L: b'\x0c',
        Qt.Key.Key_A: b'\x01',
        Qt.Key.Key_E: b'\x05',
        Qt.Key.Key_U: b'\x15',
        Qt.Key.Key_K: b'\x0b',
        Qt.Key.Key_W: b'\x17',
        Qt.Key.Key_R: b'\x12',
        Qt.Key.Key_P: b'\x10',
        Qt.Key.Key_N: b'\x0e',
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # NOT read-only so the blinking cursor is visible
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)  # each PTY row = one visual line
        vsb = self.verticalScrollBar()
        if vsb is not None:
            vsb.setSingleStep(3)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._write_fn = None
        self.bracketed_paste: bool = False  # set by TerminalPane when PTY enables it

    def set_write_fn(self, fn) -> None:
        """Register a callable(bytes) that writes raw bytes to the PTY."""
        self._write_fn = fn

    def wheelEvent(self, e: QWheelEvent | None) -> None:
        if e is None:
            return
        pixel = e.pixelDelta()
        if not pixel.isNull():
            vsb = self.verticalScrollBar()
            if vsb is not None:
                vsb.setValue(vsb.value() - pixel.y())
        else:
            super().wheelEvent(e)

    def keyPressEvent(self, e: QKeyEvent | None) -> None:
        if e is None:
            return
        mods = e.modifiers()
        key  = e.key()
        # macOS: Qt maps ControlModifier → Command (⌘),  MetaModifier → Control (^)
        if mods & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_V:
                # Paste clipboard text directly into the PTY.
                # Wrap in bracketed-paste sequences if the running app requested it
                # (e.g. claude sends \x1b[?2004h to enable bracketed paste mode).
                cb = QApplication.clipboard()
                if cb is not None and self._write_fn is not None:
                    text = cb.text()
                    if text:
                        data = text.encode('utf-8', errors='replace')
                        if self.bracketed_paste:
                            data = b'\x1b[200~' + data + b'\x1b[201~'
                        self._write_fn(data)
            else:
                super().keyPressEvent(e)   # Cmd+C copy, Cmd+A select-all, etc.
            return
        if self._write_fn is None:
            return
        if mods & Qt.KeyboardModifier.MetaModifier:
            data = self._CTRL_MAP.get(key)
            if data is None:
                text = e.text()
                data = text.encode('utf-8') if text else None
            if data:
                self._write_fn(data)
        elif key in self._KEY_MAP:
            self._write_fn(self._KEY_MAP[key])
        else:
            text = e.text()
            if text:
                self._write_fn(text.encode('utf-8', errors='replace'))

    def insertFromMimeData(self, source: QMimeData | None) -> None:
        """Forward clipboard paste (Cmd+V / drag-drop) to PTY instead of inserting."""
        if self._write_fn and source is not None and source.hasText():
            self._write_fn(source.text().encode('utf-8', errors='replace'))


# ---------------------------------------------------------------------------
# Right pane — embedded PTY terminal running Claude Code
# ---------------------------------------------------------------------------

class TerminalPane(QWidget):
    _PADDING = 16  # left+right padding in stylesheet (8px each side)

    def __init__(self, repo_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._master_fd: int | None = None
        self._proc: subprocess.Popen | None = None
        self._reader: _PtyReader | None = None
        self._committed = 0
        self._bracketed_paste: bool = False
        self._setup_ui()
        self._start_claude(repo_path)

    # ------------------------------------------------------------------
    # Helpers to compute cols/rows from the current widget + font size
    # ------------------------------------------------------------------
    def _terminal_cols(self) -> int:
        from PyQt6.QtGui import QFontMetrics
        fm = QFontMetrics(self._output.font())
        vp = self._output.viewport()
        w = (vp.width() if vp is not None else self._output.width()) - self._PADDING
        return max(80, w // max(1, fm.horizontalAdvance('M')))

    def _terminal_rows(self) -> int:
        from PyQt6.QtGui import QFontMetrics
        fm = QFontMetrics(self._output.font())
        vp = self._output.viewport()
        h = (vp.height() if vp is not None else self._output.height()) - self._PADDING
        return max(24, h // max(1, fm.height()))

    def _resize_pty(self) -> None:
        """Resize pyte screen and notify the PTY kernel of the new dimensions."""
        cols = self._terminal_cols()
        rows = self._terminal_rows()
        if (cols, rows) == (self._screen.columns, self._screen.lines):
            return
        self._screen.resize(rows, cols)
        if self._master_fd is not None:
            _set_winsize(self._master_fd, rows, cols)
        if self._proc is not None and self._proc.pid:
            try:
                import signal as _signal
                os.kill(self._proc.pid, _signal.SIGWINCH)
            except OSError:
                pass

    def resizeEvent(self, a0) -> None:  # type: ignore[override]
        super().resizeEvent(a0)
        self._resize_pty()

    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        hdr = QLabel("  Terminal")
        hdr.setFixedHeight(28)
        hdr.setStyleSheet("background:#e8e8e8; color:#555; font-weight:bold; border-bottom:1px solid #d0d0d0;")
        lay.addWidget(hdr)

        self._output = _SmoothTextEdit()
        self._output.setFont(QFont("Monaco", 13))
        self._output.setStyleSheet(
            "QTextEdit{background:#ffffff;color:#1a1a1a;border:none;padding:8px;}"
        )
        lay.addWidget(self._output)
        # pyte screen — sized after the widget is laid out (resizeEvent fires later)
        self._screen = pyte.HistoryScreen(120, 40, history=5000)
        self._stream = pyte.ByteStream(self._screen)

    def _start_claude(self, repo_path: Path) -> None:
        cols = self._terminal_cols()
        rows = self._terminal_rows()
        self._screen.resize(rows, cols)
        master, slave = pty.openpty()
        _set_winsize(slave, rows, cols)
        shell = os.environ.get("SHELL", "/bin/zsh")
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "COLUMNS": str(cols),
            "LINES": str(rows),
        }
        self._proc = subprocess.Popen(
            [shell],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=str(repo_path),
            env=env,
            close_fds=True,
        )
        os.close(slave)
        self._master_fd = master

        self._reader = _PtyReader(master)
        self._reader.data.connect(self._on_data)
        self._reader.start()
        self._output.set_write_fn(self._write_bytes)

    def _on_data(self, raw: bytes) -> None:
        # Track bracketed paste mode: \x1b[?2004h = enable, \x1b[?2004l = disable
        if b'\x1b[?2004h' in raw:
            self._bracketed_paste = True
            self._output.bracketed_paste = True
        if b'\x1b[?2004l' in raw:
            self._bracketed_paste = False
            self._output.bracketed_paste = False
        self._stream.feed(raw)
        self._render()

    def _render(self) -> None:
        """Sync QTextEdit with the current pyte screen state."""
        screen = self._screen
        doc = self._output.document()
        if doc is None:
            return

        # Locate block where the live screen starts
        screen_block = doc.findBlockByNumber(self._committed)
        start_pos = (
            screen_block.position()
            if screen_block.isValid()
            else doc.characterCount() - 1
        )

        cur = QTextCursor(doc)
        cur.setPosition(start_pos)
        cur.movePosition(
            QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor
        )

        self._output.setUpdatesEnabled(False)
        try:
            cur.removeSelectedText()

            # Commit any newly scrolled-off history lines
            hist = list(screen.history.top)
            for i in range(self._committed, len(hist)):
                self._render_line(cur, hist[i], screen.columns)
                cur.insertText("\n")
            self._committed = len(hist)

            # Render the live 40-line screen
            for y in range(screen.lines):
                self._render_line(cur, screen.buffer[y], screen.columns)
                if y < screen.lines - 1:
                    cur.insertText("\n")
        finally:
            self._output.setUpdatesEnabled(True)

        self._output.setTextCursor(cur)
        self._output.ensureCursorVisible()

        # Only reposition the caret if the user has no active text selection
        # (preserving selection lets Cmd+C copy work without it being wiped)
        if not self._output.textCursor().hasSelection():
            cursor_row = self._committed + screen.cursor.y
            cursor_col = screen.cursor.x
            cb = doc.findBlockByNumber(cursor_row)  # type: ignore[union-attr]
            if cb.isValid():
                col = min(cursor_col, max(0, cb.length() - 1))
                qt_cur = QTextCursor(doc)  # type: ignore[arg-type]
                qt_cur.setPosition(cb.position() + col)
                self._output.setTextCursor(qt_cur)
            self._output.ensureCursorVisible()

    def _render_line(self, cur: QTextCursor, line: dict, ncols: int) -> None:
        """Insert one pyte Line (defaultdict col→Char) into the document."""
        chars = [line[x] for x in range(ncols)]

        # Trim trailing blank cells (default fg, default bg, space/empty)
        last = ncols - 1
        while last >= 0 and chars[last].data in (" ", "") \
                and chars[last].fg == "default" and chars[last].bg == "default":
            last -= 1
        if last < 0:
            return

        i = 0
        while i <= last:
            ch = chars[i]
            run = ch.data or " "
            j = i + 1
            while j <= last:
                nch = chars[j]
                if (nch.fg == ch.fg and nch.bg == ch.bg
                        and nch.bold == ch.bold and nch.italics == ch.italics
                        and nch.underscore == ch.underscore
                        and nch.reverse == ch.reverse):
                    run += nch.data or " "
                    j += 1
                else:
                    break
            cur.insertText(run, self._char_fmt(ch))
            i = j

    def _char_fmt(self, ch) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fg_key = ch.bg if ch.reverse else ch.fg
        bg_key = ch.fg if ch.reverse else ch.bg

        def _hex(key: str, fallback: str) -> str:
            return _PYTE_COLORS.get(key, key if isinstance(key, str) and key.startswith("#") else fallback)

        fmt.setForeground(QColor(_hex(fg_key, "#1a1a1a")))
        if bg_key and bg_key not in ("default", ""):
            fmt.setBackground(QColor(_hex(bg_key, "#ffffff")))
        if ch.bold:
            fmt.setFontWeight(700)
        if ch.italics:
            fmt.setFontItalic(True)
        if ch.underscore:
            fmt.setFontUnderline(True)
        return fmt

    # Public API ------------------------------------------------------------

    def _write_bytes(self, data: bytes) -> None:
        if self._master_fd is not None:
            os.write(self._master_fd, data)

    def write(self, text: str) -> None:
        """Write a string to the PTY master."""
        self._write_bytes(text.encode('utf-8', errors='replace'))

    def set_input(self, text: str) -> None:
        """Send text to the PTY so it appears in the terminal (user presses Enter to submit)."""
        self._write_bytes(text.encode('utf-8', errors='replace'))
        self._output.setFocus()

    def closeEvent(self, a0) -> None:
        try:
            if self._reader:
                self._reader.stop()
                self._reader.wait(500)
            if self._proc:
                self._proc.terminate()
        except Exception:
            pass
        super().closeEvent(a0)


# ---------------------------------------------------------------------------
# Left pane — live editable transcript
# ---------------------------------------------------------------------------

class TranscriptPane(QWidget):
    source_changed = pyqtSignal(str)  # "microphone" or "zoom"

    def __init__(self, terminal: TerminalPane, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._terminal = terminal
        self._last_append: float = 0.0
        self._setup_ui()

    def _setup_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Header bar
        hdr = QWidget()
        hdr.setFixedHeight(28)
        hdr.setStyleSheet("background:#e8e8e8; border-bottom:1px solid #d0d0d0;")
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(8, 0, 8, 0)

        lbl = QLabel("Live Transcript")
        lbl.setStyleSheet("color:#555; font-weight:bold;")
        hdr_lay.addWidget(lbl)
        hdr_lay.addStretch()

        # Source selector
        self._source_combo = QComboBox()
        self._source_combo.addItems(["Zoom", "Microphone"])
        # Disable the Microphone item (grayed out)
        from PyQt6.QtGui import QStandardItemModel
        combo_model = self._source_combo.model()
        if isinstance(combo_model, QStandardItemModel):
            mic_item = combo_model.item(1)
            if mic_item is not None:
                mic_item.setFlags(mic_item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        self._source_combo.setCurrentIndex(0)  # default to Zoom
        self._source_combo.setFixedHeight(22)
        self._source_combo.setToolTip("Choose transcription source")
        self._source_combo.setStyleSheet(
            "QComboBox{border:1px solid #bbb;border-radius:3px;padding:0 6px;"
            "background:#fff;color:#333;font-size:12px;}"
            "QComboBox::drop-down{width:18px;}"
        )
        self._source_combo.currentTextChanged.connect(
            lambda t: self.source_changed.emit(t.lower())
        )
        hdr_lay.addWidget(self._source_combo)

        self._btn = QPushButton("Insert")
        self._btn.setFixedHeight(22)
        self._btn.setToolTip("Insert selected text (or all text) into Claude Code input")
        self._btn.setStyleSheet(
            "QPushButton{background:#0078d4;color:#fff;border:none;"
            "padding:0 14px;border-radius:3px;font-weight:bold;}"
            "QPushButton:hover{background:#106ebe;}"
            "QPushButton:pressed{background:#005a9e;}"
        )
        self._btn.clicked.connect(self._on_insert)
        hdr_lay.addWidget(self._btn)

        self._save_btn = QPushButton("Save")
        self._save_btn.setFixedHeight(22)
        self._save_btn.setToolTip("Save transcript to a text file")
        self._save_btn.setStyleSheet(
            "QPushButton{background:#5a9e5a;color:#fff;border:none;"
            "padding:0 14px;border-radius:3px;font-weight:bold;}"
            "QPushButton:hover{background:#4a8a4a;}"
            "QPushButton:pressed{background:#3a7a3a;}"
        )
        self._save_btn.clicked.connect(self._on_save)
        hdr_lay.addWidget(self._save_btn)
        lay.addWidget(hdr)

        self._edit = QTextEdit()
        self._edit.setFont(QFont("Helvetica Neue", 14))
        self._edit.setStyleSheet(
            "QTextEdit{background:#ffffff;color:#1a1a1a;border:none;padding:12px;}"
        )
        self._edit.setPlaceholderText("Transcription will appear here in real-time…")
        lay.addWidget(self._edit)

    def append(self, text: str) -> None:
        import time
        now = time.monotonic()
        cur = self._edit.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        if not self._edit.toPlainText():
            sep = ""
        elif self._last_append and (now - self._last_append) >= float(os.getenv("PAUSE_THRESHOLD", "4.0")):
            sep = "\n\n"  # noticeable pause → new paragraph
        else:
            sep = " "
        cur.insertText(sep + text)
        self._last_append = now
        self._edit.setTextCursor(cur)
        self._edit.ensureCursorVisible()

    def set_content(self, text: str) -> None:
        """Replace the entire pane with raw text (used for Zoom file content)."""
        scrollbar = self._edit.verticalScrollBar()
        at_bottom = scrollbar is not None and scrollbar.value() >= scrollbar.maximum() - 4
        self._edit.setPlainText(text)
        if at_bottom and scrollbar is not None:
            scrollbar.setValue(scrollbar.maximum())

    def append_block(self, text: str) -> None:
        """Append a raw text block at the end without disturbing the active selection."""
        sel = self._edit.textCursor()
        had_selection = sel.hasSelection()
        anchor = sel.anchor()
        pos = sel.position()

        sb = self._edit.verticalScrollBar()
        at_bottom = sb is None or sb.value() >= sb.maximum() - 4

        end_cur = QTextCursor(self._edit.document())
        end_cur.movePosition(QTextCursor.MoveOperation.End)
        prefix = "\n\n" if self._edit.toPlainText() else ""
        end_cur.insertText(prefix + text)

        if had_selection:
            sel.setPosition(anchor, QTextCursor.MoveMode.MoveAnchor)
            sel.setPosition(pos, QTextCursor.MoveMode.KeepAnchor)
            self._edit.setTextCursor(sel)
        elif at_bottom and sb is not None:
            sb.setValue(sb.maximum())

    def correct(self, old_text: str, new_text: str) -> None:
        """Replace old_text with new_text in-place (Zoom ASR retroactive correction).

        Preserves the user's current selection and scroll position, adjusting
        offsets when the replacement happens before them.
        """
        doc = self._edit.document()
        if doc is None:
            return
        # Search backwards from the end: corrections always target recently-appended
        # entries, so the last occurrence in the document is the right one.
        from PyQt6.QtGui import QTextDocument
        cursor = doc.find(
            old_text,
            doc.characterCount(),
            QTextDocument.FindFlag.FindBackward,
        )
        if cursor.isNull():
            return

        # Snapshot user's cursor and scroll before touching the document
        sel = self._edit.textCursor()
        had_selection = sel.hasSelection()
        a, p = sel.anchor(), sel.position()
        sb = self._edit.verticalScrollBar()
        scroll_val = sb.value() if sb is not None else 0

        replace_start = min(cursor.anchor(), cursor.position())
        delta = len(new_text) - len(old_text)
        cursor.insertText(new_text)

        # Shift selection offsets that fall after the replaced region
        def _adj(n: int) -> int:
            return n + delta if n > replace_start else n

        if had_selection:
            sel.setPosition(_adj(a), QTextCursor.MoveMode.MoveAnchor)
            sel.setPosition(_adj(p), QTextCursor.MoveMode.KeepAnchor)
            self._edit.setTextCursor(sel)
        if sb is not None:
            sb.setValue(scroll_val)

    def _on_save(self) -> None:
        text = self._edit.toPlainText().strip()
        if not text:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Transcript", "transcript.txt", "Text Files (*.txt);;All Files (*)"
        )
        if path:
            Path(path).write_text(text, encoding="utf-8")

    def _on_insert(self) -> None:
        cur = self._edit.textCursor()
        text = cur.selectedText().strip() or self._edit.toPlainText().strip()
        if text:
            self._terminal.set_input(text)


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

_SETTINGS_FIELDS = [
    ("OPENAI_API_KEY",               "OpenAI API Key",                    True),
    ("AUDIO_DEVICE_NAME",            "Audio Device Name",                 False),
    ("CHUNK_SECONDS",                "Min Chunk Duration (seconds)",      False),
    ("MAX_CHUNK_SECONDS",            "Max Chunk Duration (seconds)",      False),
    ("SILENCE_DURATION",             "Silence Duration to Cut (seconds)", False),
    ("SILENCE_THRESHOLD",            "Silence RMS Threshold",             False),
    ("PAUSE_THRESHOLD",              "Transcript Pause Threshold (secs)", False),
    ("TRANSCRIPTION_PROVIDER",       "Transcription Provider",            False),
    ("TRANSCRIPTION_MODEL",          "Transcription Model",               False),
    ("TRANSCRIPTION_CONTEXT_CHARS",  "Transcription Context (chars)",     False),
]


class SettingsDialog(QDialog):
    def __init__(self, app_support: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._app_support = app_support
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(16)

        self._fields: dict[str, QLineEdit] = {}
        for env_var, label, is_password in _SETTINGS_FIELDS:
            edit = QLineEdit(os.getenv(env_var, ""))
            edit.setMinimumWidth(300)
            if is_password:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow(label + ":", edit)
            self._fields[env_var] = edit

        # Repo path row with Browse button
        repo_row = QHBoxLayout()
        self._repo_edit = QLineEdit(os.getenv("REPO_PATH", ""))
        repo_row.addWidget(self._repo_edit)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse_repo)
        repo_row.addWidget(browse_btn)
        form.addRow("Repo / Working Folder:", repo_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addSpacing(8)
        layout.addWidget(buttons)

    def _browse_repo(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select working folder", self._repo_edit.text() or str(Path.home())
        )
        if chosen:
            self._repo_edit.setText(chosen)

    def _save(self) -> None:
        env_file = self._app_support / ".env"
        # Read existing lines, update/add keys, write back
        existing: dict[str, str] = {}
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    existing[k.strip()] = v.strip()

        for env_var, _, _ in _SETTINGS_FIELDS:
            val = self._fields[env_var].text().strip()
            if val:
                existing[env_var] = val
                os.environ[env_var] = val
            elif env_var in existing:
                del existing[env_var]

        repo = self._repo_edit.text().strip()
        if repo:
            existing["REPO_PATH"] = repo
            os.environ["REPO_PATH"] = repo
        elif "REPO_PATH" in existing:
            del existing["REPO_PATH"]

        env_file.write_text(
            "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n"
        )
        self.accept()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class App(QMainWindow):
    def __init__(
        self,
        repo_path: Path,
        device_name: str,
        chunk_seconds: int,
        app_support: Path,
    ) -> None:
        super().__init__()
        self._app_support = app_support
        self.setWindowTitle("audio-to-claude")
        self.resize(1440, 860)
        self.setStyleSheet("QMainWindow{background:#f0f0f0;}")

        self._term = TerminalPane(repo_path=repo_path)
        self._trans = TranscriptPane(terminal=self._term)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._trans)
        splitter.addWidget(self._term)
        splitter.setSizes([480, 960])
        splitter.setHandleWidth(2)
        splitter.setStyleSheet("QSplitter::handle{background:#d0d0d0;}")
        self.setCentralWidget(splitter)

        # Gear button in the menu bar area
        toolbar: QToolBar = QToolBar("main", self)
        self.addToolBar(toolbar)
        toolbar.setMovable(False)
        toolbar.setStyleSheet(
            "QToolBar{background:#e8e8e8;border-bottom:1px solid #d0d0d0;spacing:4px;}"
        )
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        settings_btn = QPushButton("⚙  Settings")
        settings_btn.setFlat(True)
        settings_btn.setStyleSheet(
            "QPushButton{color:#555;font-size:13px;padding:2px 10px;border:none;}"
            "QPushButton:hover{color:#000;}"
        )
        settings_btn.clicked.connect(self._open_settings)
        toolbar.addWidget(settings_btn)

        self._worker = _AudioWorker(
            device_name=device_name,
            chunk_seconds=chunk_seconds,
        )
        self._worker.transcript.connect(self._trans.append)
        self._worker.error.connect(lambda e: print(f"[audio] {e}", flush=True))
        # Don't start the audio worker — Zoom is the default source

        self._zoom_watcher: _ZoomWatcher | None = None
        self._trans.source_changed.connect(self._on_source_changed)
        # Start in Zoom mode immediately
        self._on_source_changed("zoom")

        # Status bar — shows active configuration at a glance
        sb = self.statusBar()
        if sb is not None:
            sb.setStyleSheet(
                "QStatusBar{background:#e8e8e8;border-top:1px solid #d0d0d0;"
                "color:#555;font-size:12px;padding:0 8px;}"
            )
        self._refresh_status()

    def _status_text(self) -> str:
        device    = os.getenv("AUDIO_DEVICE_NAME", "—")
        chunk     = os.getenv("CHUNK_SECONDS", "—")
        max_chunk = os.getenv("MAX_CHUNK_SECONDS", "—")
        sil_dur   = os.getenv("SILENCE_DURATION", "—")
        provider  = os.getenv("TRANSCRIPTION_PROVIDER", "—")
        model     = os.getenv("TRANSCRIPTION_MODEL", "—")
        return (
            f"  Device: {device}   •   Chunk: {chunk}–{max_chunk}s   •   "
            f"Silence: {sil_dur}s   •   Provider: {provider}   •   Model: {model}"
        )

    def _refresh_status(self) -> None:
        sb = self.statusBar()
        if sb is not None:
            sb.showMessage(self._status_text())

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._app_support, parent=self)
        dlg.exec()
        self._refresh_status()  # update bar after any saves

    def _on_zoom_save_triggered(self, ok: bool, err: str) -> None:
        """Update status bar with the result of the latest Save Transcript attempt."""
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        if ok:
            status = "saved ✓"
        elif "-25211" in err or "not allowed assistive access" in err.lower():
            status = "⚠ Accessibility denied — add this app in System Settings → Privacy & Security → Accessibility"
        elif err:
            status = f"error: {err}"
        else:
            status = "no-op"
        sb = self.statusBar()
        if sb is not None:
            sb.showMessage(f"  Source: Zoom   •   Last save: {ts} ({status})")

    def _on_source_changed(self, source: str) -> None:
        """Switch between microphone and Zoom transcript sources."""
        if source == "zoom":
            # Stop the audio worker only if it was running
            if self._worker.isRunning():
                self._worker.stop()
            # Start Zoom watcher if not already running
            if self._zoom_watcher is None or not self._zoom_watcher.isRunning():
                self._zoom_watcher = _ZoomWatcher()
                self._zoom_watcher.transcript.connect(self._trans.append_block)
                self._zoom_watcher.correction.connect(self._trans.correct)
                self._zoom_watcher.save_triggered.connect(self._on_zoom_save_triggered)
                self._zoom_watcher.start()
            sb = self.statusBar()
            if sb is not None:
                sb.showMessage("  Source: Zoom   •   Waiting for first save…")
        else:  # microphone
            # Stop Zoom watcher
            if self._zoom_watcher is not None:
                self._zoom_watcher.stop()
                self._zoom_watcher.wait(1500)
                self._zoom_watcher = None
            # Restart audio worker (recreate so it picks up latest settings)
            self._worker.stop()
            self._worker.wait(2000)
            self._worker = _AudioWorker(
                device_name=os.getenv("AUDIO_DEVICE_NAME", "ZoomAudioDevice"),
                chunk_seconds=int(os.getenv("CHUNK_SECONDS", "3")),
            )
            self._worker.transcript.connect(self._trans.append)
            self._worker.error.connect(lambda e: print(f"[audio] {e}", flush=True))
            self._worker.start()
            self._refresh_status()

    def closeEvent(self, a0) -> None:
        try:
            self._worker.stop()
            self._worker.wait(2000)
        except Exception:
            pass
        try:
            if self._zoom_watcher is not None:
                self._zoom_watcher.stop()
                self._zoom_watcher.wait(1500)
        except Exception:
            pass
        super().closeEvent(a0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # When running as a frozen .app the cwd is unreliable.
    # Prefer ~/Library/Application Support/audio-to-claude/.env, then fall
    # back to the directory that contains this source file.
    _app_support = Path.home() / "Library" / "Application Support" / "audio-to-claude"
    _app_support.mkdir(parents=True, exist_ok=True)
    _env_candidates = [
        _app_support / ".env",
        Path(__file__).parent / ".env",
    ]
    for _env_path in _env_candidates:
        if _env_path.exists():
            load_dotenv(_env_path)
            break
    else:
        load_dotenv()  # last-resort default search

    repo_path_str = os.getenv("REPO_PATH", "").strip()
    device_name = os.getenv("AUDIO_DEVICE_NAME", "ZoomAudioDevice")
    chunk_seconds = int(os.getenv("CHUNK_SECONDS", "3"))

    qapp = QApplication(sys.argv)
    qapp.setStyle("Fusion")

    # Let Ctrl+C in the launch terminal kill the process.
    # Qt blocks Python's default SIGINT handler, so we restore it and use a
    # no-op QTimer to periodically yield back to Python so the signal fires.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    from PyQt6.QtCore import QTimer
    sigint_timer = QTimer()
    sigint_timer.setInterval(200)
    sigint_timer.timeout.connect(lambda: None)  # just wake Python
    sigint_timer.start()

    if not repo_path_str:
        # Ask the user to pick their working folder (first-run or missing config)
        chosen = QFileDialog.getExistingDirectory(
            None,
            "Select your working repo folder",
            str(Path.home()),
        )
        if not chosen:
            QMessageBox.critical(None, "AudioToClaude", "No folder selected. Exiting.")
            sys.exit(1)
        repo_path_str = chosen
        # Persist to app-support .env so the user isn't asked again
        _env_file = _app_support / ".env"
        with _env_file.open("a") as _f:
            _f.write(f"\nREPO_PATH={repo_path_str}\n")
        os.environ["REPO_PATH"] = repo_path_str

    repo_path = Path(repo_path_str).expanduser().resolve()

    win = App(repo_path=repo_path, device_name=device_name, chunk_seconds=chunk_seconds, app_support=_app_support)
    win.show()
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
