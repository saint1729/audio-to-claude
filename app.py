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

import base64
import pty
from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFont, QTextCursor
# QtWebEngineWidgets MUST be imported before QApplication is instantiated
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel
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
    QSpinBox,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from dotenv import load_dotenv

from audio_capture import AudioCapture
from transcriber import Transcriber

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
    speaker_changed = pyqtSignal(str)       # fired whenever active speaker changes

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
        self._current_file: Path | None = None

    def run(self) -> None:
        import time
        current_file: Path | None = None
        last_entry_count: int = 0
        last_save_time: float = 0.0
        last_emitted_speaker: str = ""
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
                        self._current_file = newest
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
                    # Fire speaker_changed if the last new entry has a different speaker
                    last_new_speaker = new_entries[-1][0] if new_entries else ""
                    if last_new_speaker and last_new_speaker != last_emitted_speaker:
                        self.speaker_changed.emit(last_new_speaker)
                        last_emitted_speaker = last_new_speaker
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

    def get_all_formatted(self) -> str:
        """Return the full formatted transcript for the current Zoom file."""
        if self._current_file is None:
            return ""
        try:
            raw = self._current_file.read_text(encoding="utf-8", errors="replace")
            entries = self._parse_entries(raw)
            saved_speaker = self._last_speaker
            self._last_speaker = ""
            result = self._format_entries(entries)
            self._last_speaker = saved_speaker
            return result
        except Exception:
            return ""

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# xterm.js HTML — loaded into QWebEngineView for a proper terminal experience
# ---------------------------------------------------------------------------

_TERMINAL_HTML = """\
<!DOCTYPE html><html>
<head><meta charset="utf-8"/><style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { width: 100%; height: 100%; background: #ffffff; overflow: hidden; }
#terminal { width: 100%; height: 100%; }
/* Force light scrollbar regardless of OS dark/light mode */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: #f0f0f0; }
::-webkit-scrollbar-thumb { background: #b0b0b0; border-radius: 5px; border: 2px solid #f0f0f0; }
::-webkit-scrollbar-thumb:hover { background: #888888; }
</style>
<link rel="stylesheet" href="xterm.css"/>
<script src="xterm.js"></script>
<script src="xterm-addon-fit.js"></script>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
</head>
<body>
<div id="terminal"></div>
<script>
var term = new Terminal({
  fontFamily: 'Monaco, Menlo, "Courier New", monospace',
  fontSize: 13,
  theme: {
    background: '#ffffff', foreground: '#1a1a1a',
    cursor: '#1a1a1a',     cursorAccent: '#ffffff',
    selectionBackground: '#b4d5fe',
    black:       '#000000', red:           '#c0392b',
    green:       '#27ae60', yellow:        '#d68910',
    blue:        '#2471a3', magenta:       '#8e44ad',
    cyan:        '#148f77', white:         '#555555',
    brightBlack: '#777777', brightRed:     '#e74c3c',
    brightGreen: '#2ecc71', brightYellow:  '#f1c40f',
    brightBlue:  '#3498db', brightMagenta: '#9b59b6',
    brightCyan:  '#1abc9c', brightWhite:   '#1a1a1a',
  },
  scrollback: 5000,
  cursorBlink: true,
  convertEol: false,
});
var fitAddon = new FitAddon.FitAddon();
term.loadAddon(fitAddon);
term.open(document.getElementById('terminal'));
fitAddon.fit();

new QWebChannel(qt.webChannelTransport, function(channel) {
  var bridge = channel.objects.bridge;

  // PTY output → xterm: Python signals base64 bytes → decode → write to terminal
  bridge.dataFromPty.connect(function(b64) {
    var binary = atob(b64);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    term.write(bytes);
  });

  // Keyboard / paste → PTY: UTF-8 encode, base64, call Python slot
  term.onData(function(data) {
    var bytes = new TextEncoder().encode(data);
    var binary = String.fromCharCode.apply(null, Array.from(bytes));
    bridge.sendTopty(btoa(binary));
  });

  // Report size once channel is ready, then track resizes via ResizeObserver
  fitAddon.fit();
  bridge.resize(term.cols, term.rows);
  var resizeTimer = null;
  new ResizeObserver(function() {
    // Debounce: only fit+notify after 150 ms of no further resize events.
    // This prevents xterm.js from reflowing mid-drag and garbling the output.
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function() {
      fitAddon.fit();
      bridge.resize(term.cols, term.rows);
    }, 150);
  }).observe(document.getElementById('terminal'));
});
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# Bridge: connects Python PTY I/O to xterm.js via QWebChannel
# ---------------------------------------------------------------------------

class _PtyBridge(QObject):
    """Exposed to JS via QWebChannel; ferries bytes between the PTY and xterm.js."""
    dataFromPty = pyqtSignal(str)   # base64-encoded PTY output → JS term.write()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._master_fd: int | None = None
        self._child_pid: int | None = None

    def set_master_fd(self, fd: int) -> None:
        self._master_fd = fd

    def set_child_pid(self, pid: int) -> None:
        self._child_pid = pid

    @pyqtSlot(str)
    def sendTopty(self, b64: str) -> None:
        """Called from JS: keyboard / paste input → PTY master fd."""
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, base64.b64decode(b64))
            except Exception:
                pass

    @pyqtSlot(int, int)
    def resize(self, cols: int, rows: int) -> None:
        """Called from JS after its own debounce — apply immediately so xterm.js
        and the PTY transition to the new size atomically (no second delay).
        Cap at 100 cols: rich (used by claude CLI) switches to two-column panel
        layout on wide terminals, producing confusing side-by-side output."""
        cols = min(cols, 100)
        if self._master_fd is not None:
            _set_winsize(self._master_fd, rows, cols)
        if self._child_pid is not None:
            try:
                import signal as _signal
                os.kill(self._child_pid, _signal.SIGWINCH)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Right pane — embedded xterm.js terminal (QWebEngineView)
# ---------------------------------------------------------------------------

class TerminalPane(QWidget):
    """Right pane: a proper xterm.js terminal (QWebEngineView) backed by a PTY."""

    _ASSETS = Path(__file__).parent / "assets"

    def __init__(self, repo_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._master_fd: int | None = None
        self._proc: subprocess.Popen | None = None
        self._reader: _PtyReader | None = None
        self._bridge = _PtyBridge()
        self._setup_ui()
        self._start_claude(repo_path)

    def _setup_ui(self) -> None:
        from PyQt6.QtCore import QUrl

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        hdr = QLabel("  Terminal")
        hdr.setFixedHeight(28)
        hdr.setStyleSheet(
            "background:#e8e8e8; color:#555; font-weight:bold;"
            " border-bottom:1px solid #d0d0d0;"
        )
        lay.addWidget(hdr)

        self._webview = QWebEngineView()
        lay.addWidget(self._webview)

        channel = QWebChannel(self._webview.page())
        channel.registerObject("bridge", self._bridge)
        page = self._webview.page()
        if page is not None:
            page.setWebChannel(channel)

        # Load xterm.js from the bundled assets/ directory (works offline)
        base_url = QUrl.fromLocalFile(str(self._ASSETS) + "/")
        self._webview.setHtml(_TERMINAL_HTML, base_url)

    def _start_claude(self, repo_path: Path) -> None:
        master, slave = pty.openpty()
        _set_winsize(slave, 24, 100)
        shell = os.environ.get("SHELL", "/bin/zsh")
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "COLUMNS": "100",
            "LINES": "24",
        }
        self._proc = subprocess.Popen(
            [shell, "-l"],  # login shell: sources ~/.zprofile so PATH is complete
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=str(repo_path),
            env=env,
            close_fds=True,
        )
        os.close(slave)
        self._master_fd = master
        self._bridge.set_master_fd(master)
        self._bridge.set_child_pid(self._proc.pid)
        self._reader = _PtyReader(master)
        self._reader.data.connect(self._on_pty_data)
        self._reader.start()

    def _on_pty_data(self, raw: bytes) -> None:
        self._bridge.dataFromPty.emit(base64.b64encode(raw).decode('ascii'))

    # Public API ------------------------------------------------------------

    def write(self, text: str) -> None:
        if self._master_fd is not None:
            os.write(self._master_fd, text.encode('utf-8', errors='replace'))

    def set_input(self, text: str, grab_focus: bool = False) -> None:
        """Send text to the PTY so it appears in the terminal input line."""
        if self._master_fd is not None:
            os.write(self._master_fd, text.encode('utf-8', errors='replace'))
        if grab_focus:
            self._webview.setFocus()

    def send_and_submit(self, text: str) -> None:
        """Send text to the PTY then press Enter after a short delay so the
        TUI app (Claude Code) has time to register the full input first."""
        if self._master_fd is not None:
            os.write(self._master_fd, text.encode('utf-8', errors='replace'))
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(150, self._send_enter)
        self._webview.setFocus()

    def _send_enter(self) -> None:
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, b'\r')
            except OSError:
                pass

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
    load_all_clicked = pyqtSignal()

    def __init__(self, terminal: TerminalPane, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._terminal = terminal
        self._last_append: float = 0.0
        self._setup_ui()

    def _setup_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        _hdr_bg = "background:#e8e8e8; border-bottom:1px solid #d0d0d0;"
        _act_bg = "background:#f4f4f4; border-bottom:1px solid #d0d0d0;"
        _btn_style = (
            "QPushButton{{background:{bg};color:#fff;border:none;"
            "padding:0 4px;border-radius:3px;font-size:11px;font-weight:bold;}}"
            "QPushButton:hover{{background:{hov};}}"
            "QPushButton:pressed{{background:{pr};}}"
        )

        def _btn(label, tip, bg, hov, pr):
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setMinimumWidth(0)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setToolTip(tip)
            b.setStyleSheet(_btn_style.format(bg=bg, hov=hov, pr=pr))
            return b

        # ── Row 1: title + source combo ──────────────────────────────────────
        hdr = QWidget()
        hdr.setFixedHeight(28)
        hdr.setStyleSheet(_hdr_bg)
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(6, 0, 6, 0)
        hdr_lay.setSpacing(4)
        lbl = QLabel("Transcript")
        lbl.setStyleSheet("color:#555; font-weight:bold; font-size:12px;")
        hdr_lay.addWidget(lbl)
        hdr_lay.addStretch()

        self._source_combo = QComboBox()
        self._source_combo.addItems(["Zoom", "Microphone"])
        from PyQt6.QtGui import QStandardItemModel
        combo_model = self._source_combo.model()
        if isinstance(combo_model, QStandardItemModel):
            mic_item = combo_model.item(1)
            if mic_item is not None:
                mic_item.setFlags(mic_item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        self._source_combo.setCurrentIndex(0)
        self._source_combo.setFixedHeight(22)
        self._source_combo.setMinimumWidth(0)
        self._source_combo.setToolTip("Choose transcription source")
        self._source_combo.setStyleSheet(
            "QComboBox{border:1px solid #bbb;border-radius:3px;padding:0 4px;"
            "background:#fff;color:#333;font-size:11px;}"
            "QComboBox::drop-down{width:14px;}"
        )
        self._source_combo.currentTextChanged.connect(
            lambda t: self.source_changed.emit(t.lower())
        )
        hdr_lay.addWidget(self._source_combo)
        lay.addWidget(hdr)

        # ── Row 2: Interviewee name ───────────────────────────────────────────
        row_ee = QWidget()
        row_ee.setFixedHeight(28)
        row_ee.setStyleSheet(_act_bg)
        r_ee = QHBoxLayout(row_ee)
        r_ee.setContentsMargins(4, 3, 4, 3)
        r_ee.setSpacing(4)
        ee_name_lbl = QLabel("Interviewee:")
        ee_name_lbl.setStyleSheet("color:#555; font-size:11px;")
        ee_name_lbl.setFixedWidth(70)
        r_ee.addWidget(ee_name_lbl)
        self._ee_name_edit = QLineEdit(os.getenv("INTERVIEWEE_SPEAKER_NAME", "Interviewee"))
        self._ee_name_edit.setFixedHeight(22)
        self._ee_name_edit.setPlaceholderText("Zoom display name…")
        self._ee_name_edit.setStyleSheet(
            "QLineEdit{border:1px solid #bbb;border-radius:3px;padding:0 4px;"
            "background:#fff;color:#333;font-size:11px;}"
            "QLineEdit:focus{border:1px solid #0078d4;}"
        )
        self._ee_name_edit.setToolTip("Zoom display name of the Interviewee (for volume ducking)")
        self._ee_name_edit.textChanged.connect(
            lambda v: os.environ.__setitem__("INTERVIEWEE_SPEAKER_NAME", v.strip())
        )
        r_ee.addWidget(self._ee_name_edit)
        lay.addWidget(row_ee)

        # ── Row 3: Insert + Save ─────────────────────────────────────────────
        row2 = QWidget()
        row2.setFixedHeight(28)
        row2.setStyleSheet(_act_bg)
        r2 = QHBoxLayout(row2)
        r2.setContentsMargins(4, 3, 4, 3)
        r2.setSpacing(3)
        self._btn = _btn("Insert", "Insert selected/all text into Claude input",
                         "#0078d4", "#106ebe", "#005a9e")
        self._btn.clicked.connect(self._on_insert)
        r2.addWidget(self._btn)
        self._save_btn = _btn("Save", "Save transcript to a file",
                              "#5a9e5a", "#4a8a4a", "#3a7a3a")
        self._save_btn.clicked.connect(self._on_save)
        r2.addWidget(self._save_btn)
        lay.addWidget(row2)

        # ── Row 3: Ask + Clear + Load ────────────────────────────────────────
        row3 = QWidget()
        row3.setFixedHeight(28)
        row3.setStyleSheet(_act_bg)
        r3 = QHBoxLayout(row3)
        r3.setContentsMargins(4, 3, 4, 3)
        r3.setSpacing(3)
        ask_btn = _btn("Ask", "Send transcript to Claude and submit immediately",
                       "#0078d4", "#106ebe", "#005a9e")
        ask_btn.clicked.connect(self._on_insert_all_and_ask)
        r3.addWidget(ask_btn)
        clear_btn = _btn("Clear", "Clear transcript pane",
                         "#c0392b", "#a93226", "#922b21")
        clear_btn.clicked.connect(self._on_clear_all)
        r3.addWidget(clear_btn)
        load_btn = _btn("Load", "Load full Zoom transcript",
                        "#5a9e5a", "#4a8a4a", "#3a7a3a")
        load_btn.clicked.connect(self.load_all_clicked)
        r3.addWidget(load_btn)
        lay.addWidget(row3)

        # ── Row N: Zoom output device picker (full-width) ────────────────────
        row4 = QWidget()
        row4.setFixedHeight(28)
        row4.setStyleSheet(_act_bg)
        r4 = QHBoxLayout(row4)
        r4.setContentsMargins(4, 3, 4, 3)
        r4.setSpacing(4)
        dev_lbl = QLabel("Out:")
        dev_lbl.setStyleSheet("color:#555; font-size:11px;")
        dev_lbl.setFixedWidth(26)
        r4.addWidget(dev_lbl)
        self._dev_combo = QComboBox()
        self._dev_combo.setMinimumWidth(0)
        self._dev_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._dev_combo.setFixedHeight(22)
        self._dev_combo.setEditable(False)
        self._dev_combo.setToolTip("Zoom output device whose volume is adjusted")
        self._dev_combo.setStyleSheet(
            "QComboBox{border:1px solid #bbb;border-radius:3px;padding:0 4px;"
            "background:#fff;color:#333;font-size:11px;}"
            "QComboBox::drop-down{width:14px;}"
        )
        _out_devices = _list_output_device_names()
        self._dev_combo.addItem("(none)")
        for dn in _out_devices:
            self._dev_combo.addItem(dn)
        cur_dev = os.getenv("ZOOM_OUTPUT_DEVICE", "")
        _dev_idx = self._dev_combo.findText(cur_dev)
        if _dev_idx >= 0:
            self._dev_combo.setCurrentIndex(_dev_idx)
        elif _out_devices:
            # default to first real device
            self._dev_combo.setCurrentIndex(1)
            os.environ["ZOOM_OUTPUT_DEVICE"] = _out_devices[0]
        self._dev_combo.currentTextChanged.connect(self._on_dev_changed)
        r4.addWidget(self._dev_combo)
        lay.addWidget(row4)

        # ── Row N+1: Interviewee volume ───────────────────────────────────────
        row_vol = QWidget()
        row_vol.setFixedHeight(28)
        row_vol.setStyleSheet(_act_bg)
        r_vol = QHBoxLayout(row_vol)
        r_vol.setContentsMargins(4, 3, 4, 3)
        r_vol.setSpacing(4)
        vol_lbl = QLabel("Interviewee Vol:")
        vol_lbl.setStyleSheet("color:#555; font-size:11px;")
        vol_lbl.setFixedWidth(90)
        r_vol.addWidget(vol_lbl)
        self._ee_vol_spin = QSpinBox()
        self._ee_vol_spin.setRange(0, 100)
        self._ee_vol_spin.setSuffix("%")
        self._ee_vol_spin.setValue(int(os.getenv("INTERVIEWEE_VOLUME", "5")))
        self._ee_vol_spin.setFixedHeight(22)
        self._ee_vol_spin.setFixedWidth(60)
        self._ee_vol_spin.setToolTip("Volume % when Interviewee is speaking")
        self._ee_vol_spin.setStyleSheet(
            "QSpinBox{border:1px solid #bbb;border-radius:3px;padding:0 2px;"
            "background:#fff;color:#333;font-size:11px;}"
        )
        self._ee_vol_spin.valueChanged.connect(
            lambda v: os.environ.__setitem__("INTERVIEWEE_VOLUME", str(v))
        )
        r_vol.addWidget(self._ee_vol_spin)
        r_vol.addStretch()
        lay.addWidget(row_vol)

        # ── Text area ────────────────────────────────────────────────────────
        self._edit = QTextEdit()
        self._edit.setFont(QFont("Helvetica Neue", 14))
        self._edit.setStyleSheet(
            "QTextEdit{background:#ffffff;color:#1a1a1a;border:none;padding:12px;}"
            "QScrollBar:vertical{background:#f0f0f0;width:10px;border-radius:5px;}"
            "QScrollBar::handle:vertical{background:#b0b0b0;border-radius:5px;min-height:20px;}"
            "QScrollBar::handle:vertical:hover{background:#888888;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0px;}"
            "QScrollBar:horizontal{background:#f0f0f0;height:10px;border-radius:5px;}"
            "QScrollBar::handle:horizontal{background:#b0b0b0;border-radius:5px;min-width:20px;}"
            "QScrollBar::handle:horizontal:hover{background:#888888;}"
            "QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0px;}"
        )
        self._edit.setPlaceholderText("Transcription will appear here in real-time…")
        lay.addWidget(self._edit)

        # ── Question input row ───────────────────────────────────────────────
        q_wrap = QWidget()
        q_wrap.setStyleSheet("background:#f4f4f4; border-top:1px solid #d0d0d0;")
        q_lay = QHBoxLayout(q_wrap)
        q_lay.setContentsMargins(4, 4, 4, 4)
        q_lay.setSpacing(4)
        self._question_input = QLineEdit()
        self._question_input.setPlaceholderText("Question + Enter…")
        self._question_input.setFixedHeight(26)
        self._question_input.setStyleSheet(
            "QLineEdit{background:#fff;color:#1a1a1a;border:1px solid #bbb;"
            "border-radius:4px;padding:0 6px;font-size:12px;}"
            "QLineEdit:focus{border:1px solid #0078d4;}"
        )
        self._question_input.returnPressed.connect(self._on_send_to_claude)
        q_lay.addWidget(self._question_input)
        send_btn = _btn("Send", "Send transcript + question to Claude",
                        "#0078d4", "#106ebe", "#005a9e")
        send_btn.setFixedHeight(26)
        send_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        send_btn.setFixedWidth(44)
        send_btn.clicked.connect(self._on_send_to_claude)
        q_lay.addWidget(send_btn)
        lay.addWidget(q_wrap)

    def _on_dev_changed(self, name: str) -> None:
        os.environ["ZOOM_OUTPUT_DEVICE"] = "" if name == "(none)" else name.strip()

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

        doc_text = self._edit.toPlainText()
        has_content = bool(doc_text.strip())

        # Strip the trailing blank padding left by the previous append so the
        # gap only ever appears once at the very end, not between entries.
        trailing = len(doc_text) - len(doc_text.rstrip('\n'))
        if trailing > 0:
            end_cur.movePosition(
                QTextCursor.MoveOperation.Left,
                QTextCursor.MoveMode.KeepAnchor,
                trailing,
            )
            end_cur.removeSelectedText()

        prefix = "\n\n" if has_content else ""
        # 8 blank lines at the end so the latest entry sits comfortably above
        # the bottom edge without having to scroll all the way down.
        end_cur.insertText(prefix + text + "\n" * 16)

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

    def _on_insert_all_and_ask(self) -> None:
        """Send entire transcript to Claude terminal and submit immediately."""
        import re as _re
        transcript = self._edit.toPlainText().strip()
        if not transcript:
            return
        normalized = _re.sub(r"\s+", " ", transcript).strip()
        self._terminal.send_and_submit(normalized)
        self._edit.clear()
        self._last_append = 0.0

    def _on_clear_all(self) -> None:
        """Clear the transcript pane. The Zoom watcher keeps its position so
        only new entries (after the next Zoom save) will appear."""
        self._edit.clear()
        self._last_append = 0.0

    def _on_send_to_claude(self) -> None:
        """Combine transcript + question, send to Claude terminal, then reset transcript."""
        import re as _re
        transcript = self._edit.toPlainText().strip()
        question = self._question_input.text().strip()

        if not transcript and not question:
            return

        # Normalize transcript: collapse multiple whitespace/newlines into a
        # single space so the whole thing lands on one terminal input line.
        normalized = _re.sub(r"\s+", " ", transcript).strip()

        if normalized and question:
            message = f"{normalized} {question}"
        elif normalized:
            message = normalized
        else:
            message = question

        self._terminal.send_and_submit(message)

        # Clear transcript so the next capture starts fresh
        self._edit.clear()
        self._last_append = 0.0

        # Clear the question field and return focus to the terminal
        self._question_input.clear()

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
            self._terminal.set_input(text, grab_focus=True)


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


def _list_output_device_names() -> list[str]:
    """Return names of all output-capable audio devices via sounddevice."""
    try:
        import sounddevice as sd
        seen: set[str] = set()
        names: list[str] = []
        for device in sd.query_devices():
            if device.get("max_output_channels", 0) > 0:
                name = str(device.get("name", "")).strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
        return names
    except Exception:
        return []


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
        self.resize(900, 860)
        self.setFixedWidth(900)
        self.setStyleSheet("QMainWindow{background:#f0f0f0;}")

        self._term = TerminalPane(repo_path=repo_path)
        self._trans = TranscriptPane(terminal=self._term)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._term)
        splitter.addWidget(self._trans)
        splitter.setSizes([765, 135])
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
        self._restore_vol: int | None = None  # volume to restore when interviewee stops speaking
        self._trans.source_changed.connect(self._on_source_changed)
        self._trans.load_all_clicked.connect(self._on_load_all)
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

    def _on_load_all(self) -> None:
        """Load the full Zoom transcript into the pane."""
        if self._zoom_watcher is not None:
            content = self._zoom_watcher.get_all_formatted()
            if content:
                self._trans.set_content(content)

    def _status_text(self) -> str:
        device       = os.getenv("AUDIO_DEVICE_NAME", "—")
        chunk        = os.getenv("CHUNK_SECONDS", "—")
        max_chunk    = os.getenv("MAX_CHUNK_SECONDS", "—")
        sil_dur      = os.getenv("SILENCE_DURATION", "—")
        provider     = os.getenv("TRANSCRIPTION_PROVIDER", "—")
        model        = os.getenv("TRANSCRIPTION_MODEL", "—")
        cur_vol      = self._get_system_volume()
        return (
            f"  Vol: {cur_vol}%   •   "
            f"Device: {device}   •   Chunk: {chunk}\u2013{max_chunk}s   •   "
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

    @staticmethod
    def _coreaudio_device_id(device_name: str) -> "int | None":
        """Return the CoreAudio AudioObjectID for the first output device whose
        name contains `device_name` (case-insensitive), or None if not found."""
        import ctypes
        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

        UInt32 = ctypes.c_uint32
        c_void_p = ctypes.c_void_p

        class _Addr(ctypes.Structure):
            _fields_ = [("sel", UInt32), ("scope", UInt32), ("elem", UInt32)]

        kSysObj            = 1
        kDevices           = 0x64657623  # 'dev#'
        kScopeGlobal       = 0x676c6f62  # 'glob'
        kScopeOutput       = 0x6f757470  # 'outp'
        kElemMain          = 0
        kDevNameCF         = 0x6c6e616d  # 'lnam'
        kCFStringEncodingUTF8 = 0x08000100

        addr = _Addr(kDevices, kScopeGlobal, kElemMain)
        data_size = UInt32(0)
        ca.AudioObjectGetPropertyDataSize(UInt32(kSysObj), ctypes.byref(addr),
                                          UInt32(0), None, ctypes.byref(data_size))
        n = data_size.value // ctypes.sizeof(UInt32)
        ids = (UInt32 * n)()
        ca.AudioObjectGetPropertyData(UInt32(kSysObj), ctypes.byref(addr),
                                      UInt32(0), None, ctypes.byref(data_size), ids)

        for dev_id in ids:
            name_addr = _Addr(kDevNameCF, kScopeGlobal, kElemMain)
            cf_str = c_void_p(0)
            str_size = UInt32(ctypes.sizeof(c_void_p))
            ca.AudioObjectGetPropertyData(UInt32(dev_id), ctypes.byref(name_addr),
                                          UInt32(0), None, ctypes.byref(str_size),
                                          ctypes.byref(cf_str))
            if not cf_str.value:
                continue
            buf_len = cf.CFStringGetMaximumSizeForEncoding(cf_str, kCFStringEncodingUTF8) + 1
            buf = ctypes.create_string_buffer(buf_len)
            cf.CFStringGetCString(cf_str, buf, buf_len, kCFStringEncodingUTF8)
            cf.CFRelease(cf_str)
            name = buf.value.decode("utf-8", errors="replace")
            if device_name.lower() in name.lower():
                return int(dev_id)
        return None

    @staticmethod
    def _get_device_volume(device_name: str) -> str:
        """Read the output volume (0-100) of a named CoreAudio device, or '—'."""
        try:
            import ctypes
            ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
            UInt32 = ctypes.c_uint32
            Float32 = ctypes.c_float

            class _Addr(ctypes.Structure):
                _fields_ = [("sel", UInt32), ("scope", UInt32), ("elem", UInt32)]

            kVolScalar = 0x766f6c6d  # 'volm'
            kScopeOutput = 0x6f757470
            kElemMain = 0

            dev_id = App._coreaudio_device_id(device_name)
            if dev_id is None:
                return "—"

            vol_addr = _Addr(kVolScalar, kScopeOutput, kElemMain)
            vol = Float32(0.0)
            size = UInt32(ctypes.sizeof(Float32))
            ret = ca.AudioObjectGetPropertyData(UInt32(dev_id), ctypes.byref(vol_addr),
                                                UInt32(0), None, ctypes.byref(size),
                                                ctypes.byref(vol))
            if ret != 0:
                # Try channel 1 if master not available
                vol_addr = _Addr(kVolScalar, kScopeOutput, 1)
                ca.AudioObjectGetPropertyData(UInt32(dev_id), ctypes.byref(vol_addr),
                                              UInt32(0), None, ctypes.byref(size),
                                              ctypes.byref(vol))
            return str(round(vol.value * 100))
        except Exception:
            return "—"

    @staticmethod
    def _set_device_volume(device_name: str, volume_pct: int) -> None:
        """Set the output volume (0-100) of a named CoreAudio device."""
        try:
            import ctypes
            ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
            UInt32 = ctypes.c_uint32
            Float32 = ctypes.c_float

            class _Addr(ctypes.Structure):
                _fields_ = [("sel", UInt32), ("scope", UInt32), ("elem", UInt32)]

            kVolScalar   = 0x766f6c6d  # 'volm'
            kScopeOutput = 0x6f757470  # 'outp'

            dev_id = App._coreaudio_device_id(device_name)
            if dev_id is None:
                print(f"[volume] device '{device_name}' not found", flush=True)
                return

            scalar = Float32(max(0.0, min(1.0, volume_pct / 100.0)))
            size   = UInt32(ctypes.sizeof(Float32))

            # Try master (element 0) first, then channels 1 & 2
            for elem in (0, 1, 2):
                vol_addr = _Addr(kVolScalar, kScopeOutput, elem)
                settable = ctypes.c_bool(False)
                ca.AudioObjectIsPropertySettable(UInt32(dev_id), ctypes.byref(vol_addr),
                                                 ctypes.byref(settable))
                if settable.value:
                    ca.AudioObjectSetPropertyData(UInt32(dev_id), ctypes.byref(vol_addr),
                                                  UInt32(0), None, size,
                                                  ctypes.byref(scalar))
        except Exception as exc:
            print(f"[volume] CoreAudio error: {exc}", flush=True)

    def _get_system_volume(self) -> str:
        """Return current volume for status bar display."""
        try:
            return str(self._read_volume())
        except Exception:
            return "—"

    def _set_volume(self, vol_pct: int) -> None:
        """Set volume via CoreAudio (if device configured) or osascript fallback."""
        device = os.getenv("ZOOM_OUTPUT_DEVICE", "").strip()
        if device:
            self._set_device_volume(device, vol_pct)
        else:
            try:
                subprocess.Popen(
                    ["osascript", "-e", f"set volume output volume {vol_pct}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                print(f"[volume] osascript: {exc}", flush=True)

    def _read_volume(self) -> int:
        """Read current volume via CoreAudio (if device configured) or osascript fallback."""
        device = os.getenv("ZOOM_OUTPUT_DEVICE", "").strip()
        if device:
            v = self._get_device_volume(device)
            if v != "—":
                try:
                    return int(v)
                except ValueError:
                    pass
        # osascript fallback
        try:
            result = subprocess.run(
                ["osascript", "-e", "output volume of (get volume settings)"],
                capture_output=True, text=True, timeout=2,
            )
            return int(result.stdout.strip())
        except Exception:
            return 100

    def _on_speaker_changed_volume(self, speaker: str) -> None:
        """Adjust volume based on who is speaking."""
        interviewee = os.getenv("INTERVIEWEE_SPEAKER_NAME", "Interviewee").strip()
        if speaker.strip().lower() == interviewee.lower():
            # Save current volume before ducking
            self._restore_vol = self._read_volume()
            vol = max(0, min(100, int(os.getenv("INTERVIEWEE_VOLUME", "5"))))
        else:
            # Only restore if we previously ducked this session
            if self._restore_vol is None:
                return
            vol = self._restore_vol
        self._set_volume(vol)
        # Refresh status bar to reflect new volume
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(200, self._refresh_status)

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
        interviewee = os.getenv("INTERVIEWEE_SPEAKER_NAME", "Interviewee")
        ee_vol      = os.getenv("INTERVIEWEE_VOLUME", "5")
        cur_vol     = self._get_system_volume()
        sb = self.statusBar()
        if sb is not None:
            sb.showMessage(
                f"  Vol: {cur_vol}%   •   Last save: {ts} ({status})   •   Source: Zoom"
            )

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
                self._zoom_watcher.speaker_changed.connect(self._on_speaker_changed_volume)
                self._zoom_watcher.start()
            sb = self.statusBar()
            if sb is not None:
                interviewee = os.getenv("INTERVIEWEE_SPEAKER_NAME", "Interviewee")
                ee_vol      = os.getenv("INTERVIEWEE_VOLUME", "5")
                cur_vol     = self._get_system_volume()
                sb.showMessage(
                    f"  Vol: {cur_vol}%   •   Waiting for first save…   •   Source: Zoom"
                )
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
