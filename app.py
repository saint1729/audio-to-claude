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
    QListWidget,
    QListWidgetItem,
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
# Rich UI HTML — markdown + mermaid + syntax-highlighted chat renderer.
# Loaded into a QWebEngineView; fed Claude's streamed markdown via QWebChannel.
# All three libraries are vendored in assets/ so this works fully offline,
# mirroring the bundled-xterm.js approach used by the terminal pane.
# ---------------------------------------------------------------------------

_RICH_HTML = """\
<!DOCTYPE html><html>
<head><meta charset="utf-8"/>
<link rel="stylesheet" href="github.min.css"/>
<style>
* { box-sizing: border-box; }
html, body { margin:0; padding:0; height:100%; background:#ffffff; color:#1a1a1a;
  font-family:-apple-system,'Helvetica Neue',Helvetica,Arial,sans-serif; font-size:14px; }
#chat { height:100%; overflow-y:auto; padding:14px 16px 28px; }
.msg { margin:0 0 16px; max-width:100%; }
.msg .role { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
  margin-bottom:4px; color:#888; }
.msg.user { }
.msg.user .role { color:#0078d4; }
.msg.user .body { background:#eaf3fb; border:1px solid #d4e6f6; border-radius:10px;
  padding:8px 12px; white-space:pre-wrap; word-wrap:break-word; }
.msg.assistant .role { color:#7a4ed4; }
.msg.assistant .answer { background:#fafafa; border:1px solid #ececec; border-radius:10px;
  padding:2px 14px; }
.thinking { margin:0 0 6px; border-left:3px solid #e0d4f5; background:#faf7ff;
  border-radius:0 6px 6px 0; }
.thinking-label { cursor:pointer; font-size:11px; color:#9a7fd0; font-weight:700;
  padding:5px 10px; user-select:none; }
.thinking-body { display:none; padding:0 10px 8px 10px; font-size:12px; color:#7d7d7d;
  white-space:pre-wrap; word-wrap:break-word; font-family:Monaco,Menlo,monospace; }
.thinking.open .thinking-body { display:block; }
.tools { display:flex; flex-wrap:wrap; gap:5px; margin:0 0 6px; }
.tool-chip { font-size:11px; background:#f0f0f0; border:1px solid #e0e0e0; border-radius:12px;
  padding:2px 9px; color:#555; font-family:Monaco,Menlo,monospace; }
.tool-chip b { color:#148f77; font-weight:700; }
/* markdown body styling */
.answer h1,.answer h2,.answer h3,.answer h4 { line-height:1.25; margin:14px 0 8px; }
.answer h1 { font-size:1.5em; border-bottom:1px solid #eee; padding-bottom:.2em; }
.answer h2 { font-size:1.3em; border-bottom:1px solid #f0f0f0; padding-bottom:.2em; }
.answer h3 { font-size:1.12em; } .answer h4 { font-size:1em; }
.answer p { margin:8px 0; line-height:1.55; }
.answer ul,.answer ol { margin:8px 0; padding-left:24px; line-height:1.5; }
.answer li { margin:3px 0; }
.answer a { color:#0078d4; text-decoration:none; } .answer a:hover { text-decoration:underline; }
.answer code { background:#f0f0f2; border-radius:4px; padding:1px 5px; font-size:.88em;
  font-family:Monaco,Menlo,'Courier New',monospace; }
.answer pre { background:#f6f8fa; border:1px solid #ececec; border-radius:8px; padding:12px;
  overflow-x:auto; margin:10px 0; }
.answer pre code { background:none; padding:0; font-size:12.5px; }
.answer blockquote { border-left:3px solid #ddd; margin:8px 0; padding:2px 12px; color:#666; }
.answer table { border-collapse:collapse; margin:10px 0; font-size:13px; }
.answer th,.answer td { border:1px solid #e0e0e0; padding:5px 10px; text-align:left; }
.answer th { background:#f6f8fa; font-weight:700; }
.answer .mermaid { background:#fff; text-align:center; margin:12px 0; }
.answer .mermaid svg { max-width:100%; height:auto; }
.empty { color:#bbb; text-align:center; margin-top:40px; font-size:13px; }
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-track { background:#f0f0f0; }
::-webkit-scrollbar-thumb { background:#b0b0b0; border-radius:5px; border:2px solid #f0f0f0; }
::-webkit-scrollbar-thumb:hover { background:#888; }
</style>
<script src="markdown-it.min.js"></script>
<script src="highlight.min.js"></script>
<script src="mermaid.min.js"></script>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
</head>
<body>
<div id="chat"><div class="empty" id="empty">Ask Claude anything — answers render here with rich formatting and diagrams.</div></div>
<script>
var md = window.markdownit({ html:false, linkify:true, typographer:false });
try { mermaid.initialize({ startOnLoad:false, theme:'default', securityLevel:'strict' }); } catch(e){}
var chat = document.getElementById('chat');
var buffers = {};   // msg id -> accumulated raw markdown answer text

function nearBottom() { return chat.scrollTop + chat.clientHeight >= chat.scrollHeight - 50; }
function stick(was) { if (was) chat.scrollTop = chat.scrollHeight; }
function dropEmpty() { var e=document.getElementById('empty'); if(e) e.remove(); }
function esc(s){ var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

function clearChat() {
  chat.innerHTML = '<div class="empty" id="empty">Ask Claude anything — answers render here with rich formatting and diagrams.</div>';
  buffers = {};
}

function addUser(text) {
  dropEmpty(); var was = nearBottom();
  var m = document.createElement('div'); m.className = 'msg user';
  m.innerHTML = '<div class="role">You</div><div class="body"></div>';
  m.querySelector('.body').textContent = text;
  chat.appendChild(m); stick(true);
}

function beginAssistant(id) {
  dropEmpty(); var was = nearBottom();
  buffers[id] = '';
  var m = document.createElement('div'); m.className = 'msg assistant'; m.id = 'm-'+id;
  m.innerHTML =
    '<div class="role">Claude</div>' +
    '<div class="thinking" style="display:none"><div class="thinking-label">&#128173; Thinking (click to expand)</div><div class="thinking-body"></div></div>' +
    '<div class="tools" style="display:none"></div>' +
    '<div class="answer"></div>';
  var th = m.querySelector('.thinking-label');
  th.addEventListener('click', function(){ m.querySelector('.thinking').classList.toggle('open'); });
  chat.appendChild(m); stick(was);
}

function onThinking(id, chunk) {
  var m = document.getElementById('m-'+id); if(!m) return; var was = nearBottom();
  var box = m.querySelector('.thinking'); box.style.display = 'block';
  m.querySelector('.thinking-body').textContent += chunk; stick(was);
}

function onToolUse(id, name, summary) {
  var m = document.getElementById('m-'+id); if(!m) return; var was = nearBottom();
  var tools = m.querySelector('.tools'); tools.style.display = 'flex';
  var chip = document.createElement('span'); chip.className = 'tool-chip';
  chip.innerHTML = '&#128295; <b>' + esc(name) + '</b>' + (summary ? ' ' + esc(summary) : '');
  tools.appendChild(chip); stick(was);
}

function onDelta(id, chunk) {
  var m = document.getElementById('m-'+id); if(!m) return; var was = nearBottom();
  buffers[id] = (buffers[id] || '') + chunk;
  m.querySelector('.answer').innerHTML = md.render(buffers[id]);   // live, plain (no mermaid yet)
  stick(was);
}

function convertMermaid(container) {
  // markdown-it renders ```mermaid as <pre><code class="language-mermaid">; swap for a .mermaid div
  container.querySelectorAll('code.language-mermaid').forEach(function(code){
    var pre = code.parentElement;
    var div = document.createElement('div'); div.className = 'mermaid';
    div.textContent = code.textContent;
    if (pre && pre.parentElement) pre.parentElement.replaceChild(div, pre);
  });
}

function onDone(id, fullMd) {
  var m = document.getElementById('m-'+id); if(!m) return; var was = nearBottom();
  var ans = m.querySelector('.answer');
  ans.innerHTML = md.render(fullMd && fullMd.length ? fullMd : (buffers[id] || ''));
  convertMermaid(ans);
  ans.querySelectorAll('pre code').forEach(function(c){ try { hljs.highlightElement(c); } catch(e){} });
  var nodes = ans.querySelectorAll('.mermaid');
  if (nodes.length) {
    try { mermaid.run({ nodes: Array.prototype.slice.call(nodes), suppressErrors:true }); }
    catch(e) { console.error('mermaid', e); }
  }
  stick(was);
}

new QWebChannel(qt.webChannelTransport, function(channel) {
  var b = channel.objects.bridge;
  b.clearChat.connect(clearChat);
  b.userBubble.connect(addUser);
  b.assistantBegin.connect(beginAssistant);
  b.thinkingDelta.connect(onThinking);
  b.toolUse.connect(onToolUse);
  b.assistantDelta.connect(onDelta);
  b.assistantDone.connect(onDone);
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
# Rich UI — headless `claude -p` stream worker + QWebEngineView renderer
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Permission store — persists per-tool allow/deny choices across sessions
# ---------------------------------------------------------------------------

_ALL_TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep",
              "WebFetch", "WebSearch", "TodoWrite", "Agent"]

_PERMISSIONS_FILE = Path.home() / ".claude" / "audio-to-claude-permissions.json"


def _load_permissions() -> dict[str, bool]:
    """Return {tool_name: allowed} from disk, defaulting all unknown tools to False."""
    try:
        if _PERMISSIONS_FILE.exists():
            import json as _j
            data = _j.loads(_PERMISSIONS_FILE.read_text())
            if isinstance(data, dict):
                return {str(k): bool(v) for k, v in data.items()}
    except Exception:
        pass
    # Defaults: read-only tools on, everything else off
    return {t: t in ("Read", "Glob", "Grep") for t in _ALL_TOOLS}


def _save_permissions(perms: dict[str, bool]) -> None:
    try:
        import json as _j
        _PERMISSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PERMISSIONS_FILE.write_text(_j.dumps(perms, indent=2))
    except Exception:
        pass


class _PermissionDialog(QDialog):
    """VS Code-style 'Claude wants to use tools' pre-send dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Allow Tools")
        self.setMinimumWidth(360)

        self._perms = _load_permissions()

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        lbl = QLabel("Which tools should Claude be allowed to use?")
        lbl.setStyleSheet("font-size:12px; color:#333;")
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        self._checks: dict[str, "QCheckBox"] = {}
        from PyQt6.QtWidgets import QCheckBox, QGroupBox, QGridLayout
        grid = QGroupBox()
        grid.setStyleSheet(
            "QGroupBox{border:1px solid #d0d0d0;border-radius:4px;padding:8px 6px;}"
        )
        gl = QGridLayout(grid)
        gl.setSpacing(4)
        for i, tool in enumerate(_ALL_TOOLS):
            cb = QCheckBox(tool)
            cb.setChecked(self._perms.get(tool, False))
            cb.setStyleSheet("font-size:12px;")
            gl.addWidget(cb, i // 2, i % 2)
            self._checks[tool] = cb
        lay.addWidget(grid)

        from PyQt6.QtWidgets import QCheckBox as _CB
        self._remember = _CB("Remember my choices for future sessions")
        self._remember.setChecked(True)
        self._remember.setStyleSheet("font-size:11px; color:#555;")
        lay.addWidget(self._remember)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _on_ok(self) -> None:
        for tool, cb in self._checks.items():
            self._perms[tool] = cb.isChecked()
        if self._remember.isChecked():
            _save_permissions(self._perms)
        self.accept()

    def allowed_tools(self) -> list[str]:
        return [t for t, cb in self._checks.items() if cb.isChecked()]


class _ClaudeStreamWorker(QThread):
    """Runs `claude -p ... --output-format stream-json` and parses the NDJSON
    event stream into Qt signals. One worker per prompt; multi-turn continuity
    is preserved by passing the previous session_id via --resume."""

    began      = pyqtSignal()              # process launched
    session    = pyqtSignal(str)           # session_id (from init or result)
    thinking   = pyqtSignal(str)           # extended-thinking delta
    tool       = pyqtSignal(str, str)      # (tool_name, short summary)
    delta      = pyqtSignal(str)           # assistant answer text delta
    done       = pyqtSignal(str)           # final clean markdown (result field)
    failed     = pyqtSignal(str)           # human-readable error

    def __init__(self, prompt: str, cwd: Path, resume_sid: str | None,
                 effort: str | None = None,
                 allowed_tools: list[str] | None = None) -> None:
        super().__init__()
        self._prompt = prompt
        self._cwd = cwd
        self._resume_sid = resume_sid
        self._effort = effort
        self._allowed_tools = allowed_tools or ["Read", "Glob", "Grep"]
        self._proc: subprocess.Popen | None = None

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def run(self) -> None:
        import json
        claude = _which_claude()
        cmd = [
            claude, "-p", self._prompt,
            "--output-format", "stream-json",
            "--include-partial-messages", "--verbose",
            "--allowed-tools", *self._allowed_tools,
        ]
        if self._resume_sid:
            cmd += ["--resume", self._resume_sid]
        if self._effort:
            cmd += ["--effort", self._effort]
        env = {**os.environ, "TERM": "dumb"}
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(self._cwd), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            self.failed.emit(
                f"Could not find the 'claude' CLI (tried: {claude}).\n"
                "Install it from https://claude.com/download, or set "
                "CLAUDE_CLI_PATH=/full/path/to/claude in the app's .env "
                "(~/Library/Application Support/audio-to-claude/.env)."
            )
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Could not launch claude: {exc}")
            return

        self.began.emit()
        final_result: str | None = None
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle(evt)
            if evt.get("type") == "result":
                final_result = evt.get("result") or ""
                sid = evt.get("session_id")
                if sid:
                    self.session.emit(sid)

        rc = self._proc.wait()
        if final_result is not None:
            self.done.emit(final_result)
        elif rc != 0:
            err = ""
            if self._proc.stderr is not None:
                err = (self._proc.stderr.read() or "").strip()
            self.failed.emit(err[:500] or f"claude exited with code {rc}")
        else:
            # Stream ended cleanly but no result event — emit whatever we have.
            self.done.emit("")

    def _handle(self, evt: dict) -> None:
        etype = evt.get("type")
        if etype == "system" and evt.get("subtype") == "init":
            sid = evt.get("session_id")
            if sid:
                self.session.emit(sid)
            return
        if etype == "stream_event":
            inner = evt.get("event") or {}
            itype = inner.get("type")
            if itype == "content_block_delta":
                d = inner.get("delta") or {}
                dt = d.get("type")
                if dt == "text_delta" and d.get("text"):
                    self.delta.emit(d["text"])
                elif dt == "thinking_delta" and d.get("thinking"):
                    self.thinking.emit(d["thinking"])
            elif itype == "content_block_start":
                cb = inner.get("content_block") or {}
                if cb.get("type") == "tool_use":
                    self.tool.emit(str(cb.get("name") or "tool"), "")
            return
        if etype == "assistant":
            # Catch tool_use blocks that arrive only in the assembled message.
            msg = evt.get("message") or {}
            for blk in msg.get("content", []):
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    self.tool.emit(str(blk.get("name") or "tool"),
                                   _summarize_tool_input(blk.get("input")))


class _ClaudeBridge(QObject):
    """Exposed to the Rich UI webview; pushes streamed render events into JS."""
    clearChat      = pyqtSignal()
    userBubble     = pyqtSignal(str)
    assistantBegin = pyqtSignal(str)
    thinkingDelta  = pyqtSignal(str, str)
    toolUse        = pyqtSignal(str, str, str)
    assistantDelta = pyqtSignal(str, str)
    assistantDone  = pyqtSignal(str, str)


def _summarize_tool_input(inp) -> str:
    """One-line hint for a tool chip (e.g. the path being read)."""
    if not isinstance(inp, dict):
        return ""
    for key in ("file_path", "path", "pattern", "command", "query", "url"):
        val = inp.get(key)
        if val:
            s = str(val)
            return s if len(s) <= 60 else s[:57] + "…"
    return ""


_CLAUDE_PATH_CACHE: str | None = None


def _sessions_dir_for(cwd: Path) -> Path:
    """Return the ~/.claude/projects/<encoded> directory for a given working path."""
    encoded = str(cwd.resolve()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


def _load_session_messages(session_id: str, cwd: Path) -> list[tuple[str, str, list[str]]]:
    """Parse a session JSONL and return [(role, text, tool_names), ...].

    role is 'user' or 'assistant'. tool_names lists tool_use block names for
    assistant turns. User turns that contain only tool_result content (internal
    plumbing) are skipped so only real user messages are shown.

    The JSONL format has top-level `type` of "user"/"assistant" with the actual
    message nested under the `message` key.
    """
    import json as _json

    sessions_dir = _sessions_dir_for(cwd)
    jf = sessions_dir / f"{session_id}.jsonl"
    if not jf.exists():
        return []

    messages: list[tuple[str, str, list[str]]] = []
    with jf.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                evt = _json.loads(raw)
            except Exception:
                continue

            role = evt.get("type")  # top-level "type" is "user" or "assistant"
            if role not in ("user", "assistant"):
                continue

            # Content lives inside evt["message"]["content"]
            msg = evt.get("message") or {}
            content = msg.get("content", "")

            text_parts: list[str] = []
            tool_names: list[str] = []
            has_real_user_text = False

            if isinstance(content, str):
                text_parts.append(content)
                has_real_user_text = True
            elif isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type")
                    if btype == "text":
                        t = blk.get("text", "").strip()
                        if t:
                            text_parts.append(t)
                            has_real_user_text = True
                    elif btype == "thinking":
                        pass  # skip thinking blocks in history replay
                    elif btype == "tool_use":
                        tool_names.append(str(blk.get("name") or "tool"))
                    # tool_result blocks are internal plumbing — not surfaced

            if role == "user" and not has_real_user_text:
                continue  # skip internal tool-result-only turns

            text = "\n\n".join(text_parts).strip()
            if text or tool_names:
                messages.append((role, text, tool_names))

    return messages


def _load_sessions(cwd: Path) -> list[dict]:
    """Return session metadata dicts for all JSONL sessions under cwd, newest first.

    Each dict has: session_id, preview (first user text, truncated), ts (mtime float),
    message_count.
    """
    import json as _json

    sessions_dir = _sessions_dir_for(cwd)
    if not sessions_dir.is_dir():
        return []

    results: list[dict] = []
    for jf in sessions_dir.glob("*.jsonl"):
        try:
            ts = jf.stat().st_mtime
            preview = ""
            msg_count = 0
            with jf.open(encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        evt = _json.loads(raw)
                    except Exception:
                        continue
                    # top-level "type" is "user" or "assistant"
                    role = evt.get("type")
                    if role in ("user", "assistant"):
                        msg_count += 1
                    # Grab first user text as preview
                    if not preview and role == "user":
                        msg = evt.get("message") or {}
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            preview = content
                        elif isinstance(content, list):
                            for blk in content:
                                if isinstance(blk, dict) and blk.get("type") == "text":
                                    preview = blk.get("text", "")
                                    break
                        preview = " ".join(preview.split())[:120]
            results.append({
                "session_id": jf.stem,
                "preview": preview or "(no preview)",
                "ts": ts,
                "message_count": msg_count,
            })
        except Exception:
            continue

    results.sort(key=lambda x: x["ts"], reverse=True)
    return results


class _HistoryDialog(QDialog):
    """Pick a past Claude Code session to resume."""

    session_selected = pyqtSignal(str)   # session_id

    def __init__(self, cwd: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Session History")
        self.setMinimumSize(520, 360)

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        lbl = QLabel(f"Sessions for:  {cwd}")
        lbl.setStyleSheet("color:#555; font-size:11px;")
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget{border:1px solid #d0d0d0;border-radius:4px;background:#fff;}"
            "QListWidget::item{padding:6px 8px;border-bottom:1px solid #f0f0f0;}"
            "QListWidget::item:selected{background:#eaf3fb;color:#1a1a1a;}"
        )
        self._list.itemDoubleClicked.connect(self._accept)
        lay.addWidget(self._list)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._sessions = _load_sessions(cwd)
        if self._sessions:
            import datetime
            for s in self._sessions:
                dt = datetime.datetime.fromtimestamp(s["ts"]).strftime("%Y-%m-%d %H:%M")
                label = f"{dt}  •  {s['message_count']} msgs  •  {s['session_id']}  —  {s['preview']}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, s["session_id"])
                self._list.addItem(item)
            self._list.setCurrentRow(0)
        else:
            item = QListWidgetItem("No sessions found for this directory.")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._list.addItem(item)

    def _accept(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        sid = item.data(Qt.ItemDataRole.UserRole)
        if sid:
            self.session_selected.emit(sid)
            self.accept()


def _which_claude() -> str:
    """Resolve the claude binary once. A GUI app launched from Finder/.app
    inherits only a minimal PATH (/usr/bin:/bin:...), not the user's interactive
    PATH, so shutil.which() often misses claude. We therefore also honour an
    explicit override, check common install locations, and finally ask a login
    shell (which sources ~/.zprofile) where claude lives — the same trick the
    terminal pane uses successfully."""
    global _CLAUDE_PATH_CACHE
    if _CLAUDE_PATH_CACHE:
        return _CLAUDE_PATH_CACHE
    import shutil

    # 1) Explicit override (set CLAUDE_CLI_PATH in the app-support .env).
    override = os.environ.get("CLAUDE_CLI_PATH", "").strip()
    if override:
        cand = Path(override).expanduser()
        if cand.exists():
            _CLAUDE_PATH_CACHE = str(cand)
            return _CLAUDE_PATH_CACHE

    # 2) Current process PATH.
    found = shutil.which("claude")

    # 3) Common install locations across installers/managers.
    if not found:
        cands = [
            Path.home() / ".local" / "bin" / "claude",     # native installer (current)
            Path.home() / ".claude" / "local" / "claude",  # native installer (legacy)
            Path.home() / ".toolbox" / "bin" / "claude",    # Amazon toolbox
            Path("/opt/homebrew/bin/claude"),               # Homebrew (Apple Silicon)
            Path("/usr/local/bin/claude"),                  # Homebrew (Intel) / manual
            Path.home() / ".npm-global" / "bin" / "claude", # npm global prefix
        ]
        # nvm installs node (and global bins like claude) under a per-version
        # dir; glob every installed version. Prefer the highest version string.
        nvm_bins = sorted(
            (Path.home() / ".nvm" / "versions" / "node").glob("*/bin/claude"),
            reverse=True,
        )
        cands.extend(nvm_bins)
        for cand in cands:
            if cand.exists():
                found = str(cand)
                break

    # 4) Last resort: ask an interactive login shell where claude lives. Must be
    #    interactive (-i) because version managers like nvm initialise in
    #    ~/.zshrc, which non-interactive shells do NOT source — so a plain
    #    `zsh -l -c` would miss an nvm-installed claude. Pick the last stdout
    #    line that is a real path.
    if not found:
        try:
            shell = os.environ.get("SHELL", "/bin/zsh")
            out = subprocess.run(
                [shell, "-lic", "command -v claude"],
                capture_output=True, text=True, timeout=15,
            )
            for line in reversed(out.stdout.splitlines()):
                line = line.strip()
                if line and Path(line).exists():
                    found = line
                    break
        except Exception:  # noqa: BLE001
            pass

    _CLAUDE_PATH_CACHE = found or "claude"
    return _CLAUDE_PATH_CACHE


class _PromptTextEdit(QTextEdit):
    """Multi-line prompt box: Enter submits, Shift+Enter inserts a newline."""
    submitted = pyqtSignal()

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e is not None and e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(e)            # Shift+Enter → newline
            else:
                self.submitted.emit()               # Enter → send
            return
        super().keyPressEvent(e)


class RichClaudePane(QWidget):
    """Rich UI mode: renders Claude's markdown answers (with mermaid diagrams and
    syntax-highlighted code) via a QWebEngineView. Driven by headless `claude -p`,
    reusing the same CLI + auth the terminal uses. Multi-turn within this pane is
    preserved via --resume; it is a separate conversation from the Terminal pane.

    A header row exposes the working directory (with Browse) and an Effort level;
    both apply to the headless claude invocations. A multi-line prompt box at the
    bottom composes questions (Enter sends, Shift+Enter for a newline)."""

    _ASSETS = Path(__file__).parent / "assets"

    # Effort dropdown labels → --effort value (None = CLI default)
    _EFFORT_LEVELS = [("Default", None), ("Low", "low"), ("Medium", "medium"),
                      ("High", "high"), ("Max", "max")]

    def __init__(self, repo_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cwd = repo_path
        self._effort: str | None = None
        self._bridge = _ClaudeBridge()
        self._worker: _ClaudeStreamWorker | None = None
        self._session_id: str | None = None
        self._msg_seq = 0
        self._cur_id: str | None = None
        self._ready = False
        self._pending: str | None = None     # prompt queued until webview loads
        self._busy = False
        self._setup_ui()

    def _setup_ui(self) -> None:
        from PyQt6.QtCore import QUrl
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header: working dir + Browse + effort level ──────────────────────
        ctl = QWidget()
        ctl.setFixedHeight(30)
        ctl.setStyleSheet("background:#f4f4f4; border-bottom:1px solid #d0d0d0;")
        c = QHBoxLayout(ctl)
        c.setContentsMargins(6, 3, 6, 3)
        c.setSpacing(5)

        dir_lbl = QLabel("Dir:")
        dir_lbl.setStyleSheet("color:#555; font-size:11px;")
        c.addWidget(dir_lbl)
        self._dir_edit = QLineEdit(str(self._cwd))
        self._dir_edit.setFixedHeight(22)
        self._dir_edit.setToolTip("Working directory for Claude (Rich UI). Edit and press Enter, or Browse.")
        self._dir_edit.setStyleSheet(
            "QLineEdit{border:1px solid #bbb;border-radius:3px;padding:0 5px;"
            "background:#fff;color:#333;font-size:11px;}"
            "QLineEdit:focus{border:1px solid #0078d4;}"
        )
        self._dir_edit.editingFinished.connect(self._on_dir_edited)
        c.addWidget(self._dir_edit, 1)

        browse = QPushButton("Browse…")
        browse.setFixedHeight(22)
        browse.setStyleSheet(
            "QPushButton{background:#e0e0e0;border:1px solid #bbb;border-radius:3px;"
            "padding:0 8px;font-size:11px;color:#333;}QPushButton:hover{background:#d4d4d4;}"
        )
        browse.clicked.connect(self._on_browse_dir)
        c.addWidget(browse)

        eff_lbl = QLabel("Effort:")
        eff_lbl.setStyleSheet("color:#555; font-size:11px;")
        c.addWidget(eff_lbl)
        self._effort_combo = QComboBox()
        for label, _ in self._EFFORT_LEVELS:
            self._effort_combo.addItem(label)
        self._effort_combo.setFixedHeight(22)
        self._effort_combo.setToolTip("Reasoning effort for Claude (maps to --effort)")
        self._effort_combo.setStyleSheet(
            "QComboBox{border:1px solid #bbb;border-radius:3px;padding:0 5px;"
            "background:#fff;color:#333;font-size:11px;}QComboBox::drop-down{width:14px;}"
        )
        self._effort_combo.currentIndexChanged.connect(self._on_effort_changed)
        c.addWidget(self._effort_combo)
        lay.addWidget(ctl)

        # ── Chat surface ─────────────────────────────────────────────────────
        self._webview = QWebEngineView()
        lay.addWidget(self._webview, 1)

        channel = QWebChannel(self._webview.page())
        channel.registerObject("bridge", self._bridge)
        page = self._webview.page()
        if page is not None:
            page.setWebChannel(channel)
            page.loadFinished.connect(self._on_loaded)

        base_url = QUrl.fromLocalFile(str(self._ASSETS) + "/")
        self._webview.setHtml(_RICH_HTML, base_url)

        # ── Prompt input: multi-line textarea + Send ─────────────────────────
        inp = QWidget()
        inp.setStyleSheet("background:#f4f4f4; border-top:1px solid #d0d0d0;")
        i = QHBoxLayout(inp)
        i.setContentsMargins(6, 6, 6, 6)
        i.setSpacing(5)
        self._prompt_edit = _PromptTextEdit()
        self._prompt_edit.setFixedHeight(60)
        self._prompt_edit.setPlaceholderText("Ask Claude…  (Enter to send, Shift+Enter for newline)")
        self._prompt_edit.setStyleSheet(
            "QTextEdit{background:#fff;color:#1a1a1a;border:1px solid #bbb;"
            "border-radius:4px;padding:4px 6px;font-size:13px;}"
            "QTextEdit:focus{border:1px solid #0078d4;}"
        )
        self._prompt_edit.submitted.connect(self._on_prompt_submitted)
        i.addWidget(self._prompt_edit, 1)
        self._send_btn = QPushButton("Send")
        self._send_btn.setFixedWidth(56)
        self._send_btn.setFixedHeight(60)
        self._send_btn.setStyleSheet(
            "QPushButton{background:#0078d4;color:#fff;border:none;border-radius:4px;"
            "font-size:12px;font-weight:bold;}QPushButton:hover{background:#106ebe;}"
            "QPushButton:pressed{background:#005a9e;}QPushButton:disabled{background:#9bbfdc;}"
        )
        self._send_btn.clicked.connect(self._on_prompt_submitted)
        i.addWidget(self._send_btn)
        # Continue: fires a fixed "Please continue further" prompt so the user can
        # nudge Claude onward without retyping. Context for the prompt is set elsewhere.
        self._continue_btn = QPushButton("Continue")
        self._continue_btn.setFixedWidth(72)
        self._continue_btn.setFixedHeight(60)
        self._continue_btn.setToolTip("Ask Claude to continue further")
        self._continue_btn.setStyleSheet(
            "QPushButton{background:#5a9e5a;color:#fff;border:none;border-radius:4px;"
            "font-size:12px;font-weight:bold;}QPushButton:hover{background:#4a8a4a;}"
            "QPushButton:pressed{background:#3a7a3a;}QPushButton:disabled{background:#aacaaa;}"
        )
        self._continue_btn.clicked.connect(self._on_continue)
        i.addWidget(self._continue_btn)
        lay.addWidget(inp)

    def _on_dir_edited(self) -> None:
        raw = self._dir_edit.text().strip()
        p = Path(raw).expanduser()
        if p.is_dir():
            self._cwd = p.resolve()
            self._dir_edit.setText(str(self._cwd))
        else:
            # revert to the last valid directory rather than launch claude in a bad cwd
            self._dir_edit.setText(str(self._cwd))

    def _on_browse_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Working Directory", str(self._cwd))
        if chosen:
            self._cwd = Path(chosen).resolve()
            self._dir_edit.setText(str(self._cwd))

    def _on_effort_changed(self, idx: int) -> None:
        if 0 <= idx < len(self._EFFORT_LEVELS):
            self._effort = self._EFFORT_LEVELS[idx][1]

    def _on_prompt_submitted(self) -> None:
        text = self._prompt_edit.toPlainText().strip()
        if not text or self._busy:
            return
        self._prompt_edit.clear()
        self.send_and_submit(text)

    def _on_continue(self) -> None:
        """Send a fixed 'Please continue further' prompt and submit immediately."""
        if self._busy:
            return
        self.send_and_submit("Please continue further")

    def _on_loaded(self, ok: bool) -> None:
        self._ready = True
        if self._pending is not None:
            prompt, self._pending = self._pending, None
            self._ask(prompt)

    # Public API — mirrors TerminalPane so LeftPane can route uniformly --------

    def send_and_submit(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if not self._ready:
            self._pending = text
            return
        self._ask(text)

    def set_input(self, text: str, grab_focus: bool = False) -> None:
        # Rich UI has no editable input line; treat Insert like a submit.
        self.send_and_submit(text)
        if grab_focus:
            self._webview.setFocus()

    def _ask(self, prompt: str) -> None:
        if self._busy:
            return  # one in-flight query at a time keeps the UX legible

        # Show permission dialog only on the first turn of a session,
        # or if no remembered preferences exist yet.
        if self._session_id is None or not _PERMISSIONS_FILE.exists():
            dlg = _PermissionDialog(self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            allowed_tools = dlg.allowed_tools() or ["Read"]
        else:
            allowed_tools = [t for t, v in _load_permissions().items() if v] or ["Read"]

        self._busy = True
        self._send_btn.setEnabled(False)
        self._continue_btn.setEnabled(False)
        self._bridge.userBubble.emit(prompt)
        self._msg_seq += 1
        self._cur_id = str(self._msg_seq)
        self._bridge.assistantBegin.emit(self._cur_id)

        self._worker = _ClaudeStreamWorker(
            prompt, self._cwd, self._session_id, effort=self._effort,
            allowed_tools=allowed_tools)
        self._worker.session.connect(self._on_session)
        self._worker.thinking.connect(lambda t: self._cur_id and self._bridge.thinkingDelta.emit(self._cur_id, t))
        self._worker.tool.connect(lambda n, s: self._cur_id and self._bridge.toolUse.emit(self._cur_id, n, s))
        self._worker.delta.connect(lambda t: self._cur_id and self._bridge.assistantDelta.emit(self._cur_id, t))
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_session(self, sid: str) -> None:
        self._session_id = sid

    def _on_done(self, full_md: str) -> None:
        if self._cur_id:
            self._bridge.assistantDone.emit(self._cur_id, full_md)
        self._busy = False
        self._send_btn.setEnabled(True)
        self._continue_btn.setEnabled(True)

    def _on_failed(self, msg: str) -> None:
        if self._cur_id:
            self._bridge.assistantDone.emit(
                self._cur_id, f"⚠️ **Claude error**\n\n```\n{msg}\n```")
        self._busy = False
        self._send_btn.setEnabled(True)
        self._continue_btn.setEnabled(True)

    def clear(self) -> None:
        """Reset the visible chat and start a fresh conversation."""
        self._session_id = None
        self._bridge.clearChat.emit()

    def resume_session(self, session_id: str) -> None:
        """Clear the chat view, replay history from the session file, then resume."""
        self._session_id = session_id
        self._bridge.clearChat.emit()

        messages = _load_session_messages(session_id, self._cwd)
        for role, text, tool_names in messages:
            if role == "user":
                self._bridge.userBubble.emit(text)
            else:
                self._msg_seq += 1
                mid = str(self._msg_seq)
                self._bridge.assistantBegin.emit(mid)
                for tname in tool_names:
                    self._bridge.toolUse.emit(mid, tname, "")
                self._bridge.assistantDone.emit(mid, text)

    def closeEvent(self, a0) -> None:
        try:
            if self._worker and self._worker.isRunning():
                self._worker.stop()
                self._worker.wait(1000)
        except Exception:
            pass
        super().closeEvent(a0)


# ---------------------------------------------------------------------------
# Left pane — mode switcher wrapping Rich UI (default) and Terminal
# ---------------------------------------------------------------------------

class LeftPane(QWidget):
    """Hosts the two left-pane modes behind a top dropdown:
      • Rich UI (default) — markdown/mermaid rendering via headless claude
      • Terminal          — the existing interactive xterm.js + PTY session
    Exposes send_and_submit / set_input, dispatching to the active mode so the
    transcript pane's Insert/Ask/Send buttons work the same in either view."""

    def __init__(self, repo_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._terminal = TerminalPane(repo_path=repo_path)
        self._rich = RichClaudePane(repo_path=repo_path)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        hdr = QWidget()
        hdr.setFixedHeight(28)
        hdr.setStyleSheet("background:#e8e8e8; border-bottom:1px solid #d0d0d0;")
        h = QHBoxLayout(hdr)
        h.setContentsMargins(6, 0, 6, 0)
        h.setSpacing(6)
        lbl = QLabel("View:")
        lbl.setStyleSheet("color:#555; font-weight:bold; font-size:12px;")
        h.addWidget(lbl)
        self._mode = QComboBox()
        self._mode.addItems(["Rich UI", "Terminal"])     # index 0 = default
        self._mode.setFixedHeight(22)
        self._mode.setToolTip("Rich UI renders markdown + mermaid; Terminal is the interactive Claude CLI")
        self._mode.setStyleSheet(
            "QComboBox{border:1px solid #bbb;border-radius:3px;padding:0 6px;"
            "background:#fff;color:#333;font-size:11px;}"
            "QComboBox::drop-down{width:14px;}"
        )
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        h.addWidget(self._mode)
        h.addStretch()

        _hbtn_style = (
            "QPushButton{background:#e0e0e0;border:1px solid #bbb;border-radius:3px;"
            "padding:0 8px;font-size:11px;color:#333;}"
            "QPushButton:hover{background:#d0d0d0;}"
        )
        history_btn = QPushButton("History")
        history_btn.setFixedHeight(22)
        history_btn.setToolTip("Browse past sessions for this directory and resume one")
        history_btn.setStyleSheet(_hbtn_style)
        history_btn.clicked.connect(self._on_history)
        h.addWidget(history_btn)

        new_btn = QPushButton("New Session")
        new_btn.setFixedHeight(22)
        new_btn.setToolTip("Clear the current chat and start a fresh Claude session")
        new_btn.setStyleSheet(_hbtn_style)
        new_btn.clicked.connect(self._on_new_session)
        h.addWidget(new_btn)

        perms_btn = QPushButton("Permissions…")
        perms_btn.setFixedHeight(22)
        perms_btn.setToolTip("Edit which tools Claude is allowed to use")
        perms_btn.setStyleSheet(_hbtn_style)
        perms_btn.clicked.connect(self._on_permissions)
        h.addWidget(perms_btn)

        lay.addWidget(hdr)

        from PyQt6.QtWidgets import QStackedWidget
        self._stack = QStackedWidget()
        self._stack.addWidget(self._rich)       # index 0
        self._stack.addWidget(self._terminal)   # index 1
        lay.addWidget(self._stack)
        self._stack.setCurrentIndex(0)          # Rich UI default

    def _on_mode_changed(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        self._active().setFocus()

    def _on_history(self) -> None:
        dlg = _HistoryDialog(self._rich._cwd, parent=self)
        dlg.session_selected.connect(self._on_resume_session)
        dlg.exec()

    def _on_resume_session(self, session_id: str) -> None:
        self._rich.resume_session(session_id)
        self._mode.setCurrentIndex(0)   # switch to Rich UI if not already there
        self._stack.setCurrentIndex(0)

    def _on_new_session(self) -> None:
        self._rich.clear()
        self._mode.setCurrentIndex(0)
        self._stack.setCurrentIndex(0)

    def _on_permissions(self) -> None:
        dlg = _PermissionDialog(self)
        dlg.exec()  # saves on OK; cancelling discards changes

    def _active(self):
        return self._rich if self._mode.currentIndex() == 0 else self._terminal

    # Routed to whichever mode is active -------------------------------------

    def send_and_submit(self, text: str) -> None:
        self._active().send_and_submit(text)

    def set_input(self, text: str, grab_focus: bool = False) -> None:
        self._active().set_input(text, grab_focus=grab_focus)

    def closeEvent(self, a0) -> None:
        for w in (self._rich, self._terminal):
            try:
                w.close()
            except Exception:
                pass
        super().closeEvent(a0)


# ---------------------------------------------------------------------------
# Left pane — live editable transcript
# ---------------------------------------------------------------------------

class TranscriptPane(QWidget):
    source_changed = pyqtSignal(str)  # "microphone" or "zoom"
    load_all_clicked = pyqtSignal()

    def __init__(self, terminal: "LeftPane", parent: QWidget | None = None) -> None:
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

        # ── Row N+2: Others volume ────────────────────────────────────────────
        row_ovol = QWidget()
        row_ovol.setFixedHeight(28)
        row_ovol.setStyleSheet(_act_bg)
        r_ovol = QHBoxLayout(row_ovol)
        r_ovol.setContentsMargins(4, 3, 4, 3)
        r_ovol.setSpacing(4)
        ovol_lbl = QLabel("Others Vol:")
        ovol_lbl.setStyleSheet("color:#555; font-size:11px;")
        ovol_lbl.setFixedWidth(90)
        r_ovol.addWidget(ovol_lbl)
        self._others_vol_spin = QSpinBox()
        self._others_vol_spin.setRange(0, 100)
        self._others_vol_spin.setSuffix("%")
        self._others_vol_spin.setValue(int(os.getenv("OTHERS_VOLUME", "50")))
        self._others_vol_spin.setFixedHeight(22)
        self._others_vol_spin.setFixedWidth(60)
        self._others_vol_spin.setToolTip("Volume % when anyone other than the Interviewee is speaking")
        self._others_vol_spin.setStyleSheet(
            "QSpinBox{border:1px solid #bbb;border-radius:3px;padding:0 2px;"
            "background:#fff;color:#333;font-size:11px;}"
        )
        self._others_vol_spin.valueChanged.connect(
            lambda v: os.environ.__setitem__("OTHERS_VOLUME", str(v))
        )
        r_ovol.addWidget(self._others_vol_spin)
        r_ovol.addStretch()
        lay.addWidget(row_ovol)

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

        self._term = LeftPane(repo_path=repo_path)
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
        self._trans.source_changed.connect(self._on_source_changed)
        self._trans.load_all_clicked.connect(self._on_load_all)
        # Cached volume for status bar; refreshed at most once per 2 s via osascript.
        self._current_vol: int = self._read_volume_osascript()
        self._vol_cache_ts: float = 0.0
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

    def _read_volume_osascript(self) -> int:
        """Read the actual system output volume via osascript."""
        try:
            result = subprocess.run(
                ["osascript", "-e", "output volume of (get volume settings)"],
                capture_output=True, text=True, timeout=2,
            )
            return int(result.stdout.strip())
        except Exception:
            return self._current_vol if hasattr(self, "_current_vol") else 100

    def _get_system_volume(self) -> str:
        """Return current volume, re-reading from the OS at most once per 2 s."""
        import time
        now = time.monotonic()
        if now - self._vol_cache_ts >= 2.0:
            self._current_vol = self._read_volume_osascript()
            self._vol_cache_ts = now
        return str(self._current_vol)

    def _set_volume(self, vol_pct: int) -> None:
        """Set volume via CoreAudio (if device configured) or osascript fallback."""
        self._current_vol = vol_pct
        self._vol_cache_ts = 0.0  # force re-read on next status refresh
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

    def _on_speaker_changed_volume(self, speaker: str) -> None:
        """Adjust volume based on who is speaking.

        Both volumes are explicitly configured via the UI: the Interviewee Vol
        applies while the interviewee speaks, the Others Vol applies for anyone
        else. We no longer snapshot/restore the prior system volume."""
        interviewee = os.getenv("INTERVIEWEE_SPEAKER_NAME", "Interviewee").strip()
        if speaker.strip().lower() == interviewee.lower():
            vol = max(0, min(100, int(os.getenv("INTERVIEWEE_VOLUME", "5"))))
        else:
            vol = max(0, min(100, int(os.getenv("OTHERS_VOLUME", "50"))))
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
