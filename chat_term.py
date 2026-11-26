"""chat_app のターミナルの表示と入力（標準ライブラリだけ）。

通信と取り決めは chat.py に置き、ここは画面に出すことと、キーを読むことだけを受け持つ。
後でブラウザの版を足すときは、chat.ChatSession に渡す「表示」をこのファイルの TerminalView の
代わりに差し替える。

- 幅：日本語は unicodedata.east_asian_width で 2 字分として数える
- 色：端末でない・NO_COLOR・Windows で VT を有効にできない ときは付けない
- 入力：キーを 1 つずつ読む自前の 1 行入力（Mac/Linux は termios、Windows は msvcrt）。readline は使わない
- 状態の行：DECSTBM（ESC [ 2 ; N r）で 1 行目を残して 2 行目から下を流す
"""

from __future__ import annotations

import codecs
import json
import math
import os
import shutil
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, TextIO, Tuple

Event = Tuple[str, str]  # (種類, 字)。種類は text・enter・backspace・left・right など

# ---------------------------------------------------------------- 幅と字


def char_width(c: str) -> int:
    o = ord(c)
    if o < 32 or 0x7F <= o < 0xA0:
        return 0
    if unicodedata.combining(c):
        return 0
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def text_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def pad(text: str, width: int) -> str:
    """表示の幅が width になるまで右に空白を足す。"""
    return text + " " * max(0, width - text_width(text))


def truncate(text: str, width: int) -> str:
    """表示の幅が width を超えないところで切る。"""
    out, used = [], 0
    for c in text:
        w = char_width(c)
        if used + w > width:
            break
        out.append(c)
        used += w
    return "".join(out)


def clean(text: str) -> str:
    """相手から来た字の制御文字を「?」にする（ESC で画面を書き換えられないように）。"""
    return "".join("?" if (ord(c) < 32 or 0x7F <= ord(c) < 0xA0) else c for c in text)


def can_draw(stream: object, sample: str = "┌─┐│└┘█░●✕✓◐›") -> bool:
    """出力が罫線などの字を書けるか。encoding の無いもの（StringIO）は書けるとみなす。"""
    enc = getattr(stream, "encoding", None)
    if not enc:
        return True
    try:
        sample.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


class Glyphs:
    """UTF-8 で書けるときと書けないときの印。"""

    def __init__(self, unicode: bool):
        self.unicode = unicode
        if unicode:
            self.info, self.error, self.ok, self.quiet, self.arrow = "●", "✕", "✓", "◐", "›"
            self.box = "┌┐└┘─│"
        else:
            self.info, self.error, self.ok, self.quiet, self.arrow = "*", "x", "+", "~", ">"
            self.box = "----" + "-|"


# ---------------------------------------------------------------- 色

GREEN, CYAN, YELLOW, RED, DIM, REVERSE, RESET = (
    "\x1b[32m", "\x1b[36m", "\x1b[33m", "\x1b[31m", "\x1b[2m", "\x1b[7m", "\x1b[0m")
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def _isatty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return False


def enable_windows_vt(stream: object = None) -> bool:
    """Windows のコンソールで ESC の列（色・カーソル移動）を使えるようにする。"""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-12 if stream is sys.stderr else -11)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:  # ctypes が無い・古い Windows など
        return False


def vt_supported(stream: object, env: Optional[Dict[str, str]] = None, platform: Optional[str] = None,
                 enable_vt: Callable[[object], bool] = enable_windows_vt) -> bool:
    """ESC の列（カーソル移動・行の消去）を使える端末か。"""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if not _isatty(stream) or env.get("TERM") == "dumb":
        return False
    if platform == "win32":
        return bool(enable_vt(stream))
    return True


def color_supported(stream: object, env: Optional[Dict[str, str]] = None, platform: Optional[str] = None,
                    enable_vt: Callable[[object], bool] = enable_windows_vt) -> bool:
    """色を付けてよいか。端末でない・NO_COLOR が空でない・VT を有効にできない ときは付けない。"""
    env = os.environ if env is None else env
    if env.get("NO_COLOR"):
        return False
    return vt_supported(stream, env, platform, enable_vt)


class Style:
    def __init__(self, color: bool):
        self.color = color

    def paint(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.color and text else text


# ---------------------------------------------------------------- 大きさ・時間


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def format_remaining(seconds: float) -> str:
    if seconds < 1:
        return "残り 1 秒未満"
    if seconds < 60:
        return f"残り {math.ceil(seconds)} 秒"
    return f"残り {math.ceil(seconds / 60)} 分"


def format_progress(label: str, done: int, total: int, rate: float, unicode: bool, width: int = 20) -> str:
    """進み具合の 1 行。UTF-8 なら █ ░ の棒、そうでなければ # の棒。"""
    pct = 100 if total <= 0 else min(100, done * 100 // total)
    n = pct * width // 100
    bar = "█" * n + "░" * (width - n) if unicode else "[" + "#" * n + "-" * (width - n) + "]"
    line = f"{label} {bar} {pct:3d}%  {human_size(rate)}/s"
    if done < total and rate > 0:
        line += "  " + format_remaining((total - done) / rate)
    return line


# ---------------------------------------------------------------- 枠とメニュー


def spaced_code(code: str) -> str:
    """合言葉を 1 字ずつ空ける（読み上げやすく）。"""
    return " ".join(code)


def label_rows(pairs: Sequence[Tuple[str, str]], gap: int = 2) -> List[str]:
    """「見出し  値」の行を、見出しの幅を揃えて作る。"""
    w = max((text_width(k) for k, _ in pairs), default=0)
    return [pad(k, w + gap) + v for k, v in pairs]


def box_lines(rows: Sequence[str], unicode: bool, margin: int = 2) -> List[str]:
    """行を枠で囲む。日本語は 2 字分の幅で数えて右の線を揃える。"""
    tl, tr, bl, br, h, v = Glyphs(unicode).box
    inner = max((text_width(r) for r in rows), default=0) + margin * 2
    out = [tl + h * inner + tr]
    for r in rows:
        out.append(v + " " * margin + pad(r, inner - margin) + v)
    out.append(bl + h * inner + br)
    return out


def wait_banner(title: str, ip_text: str, code: str, command: str, unicode: bool,
                extra: Sequence[Tuple[str, str]] = ()) -> List[str]:
    """待機の枠と、相手の画面で打つ 1 行。"""
    rows = [title] + label_rows([("IP", ip_text), ("合言葉", spaced_code(code)), *extra])
    return box_lines(rows, unicode) + [f"相手の画面で: {command}", "止める: Ctrl+C  待っています …"]


def menu_lines(items: Sequence[Tuple[str, str, str]]) -> List[str]:
    """最初のメニュー。items は (番号, 名前, 説明)。"""
    w = max(text_width(label) for _, label, _ in items)
    return ["chat_app  何をしますか"] + [f"  {key}  {pad(label, w + 2)}{desc}" for key, label, desc in items]


def menu_prompt(last: Optional[str]) -> str:
    return f"番号 [Enter で {last}] > " if last else "番号 > "


def parse_menu_choice(text: str, keys: Sequence[str], last: Optional[str]) -> Optional[str]:
    """メニューの答えを番号にする。空なら前回の番号。合わなければ None。"""
    t = text.strip()
    if not t:
        return last if last in keys else None
    return t if t in keys else None


# ---------------------------------------------------------------- 状態ファイル（前回の選択）


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, data: dict) -> bool:
    """書けなくても落ちない（読み取り専用の場所など）。書けたら True。"""
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(path))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 1 行入力（純粋な部分）


class LineEditor:
    """打ちかけの 1 行。端末に触らないので試験できる。"""

    def __init__(self, text: str = ""):
        self.chars: List[str] = list(text)
        self.pos = len(self.chars)

    @property
    def text(self) -> str:
        return "".join(self.chars)

    def insert(self, text: str) -> None:
        add = [c for c in text if char_width(c) > 0 or unicodedata.combining(c)]
        self.chars[self.pos:self.pos] = add
        self.pos += len(add)

    def backspace(self) -> None:
        if self.pos > 0:
            self.pos -= 1
            del self.chars[self.pos]

    def delete(self) -> None:
        if self.pos < len(self.chars):
            del self.chars[self.pos]

    def left(self) -> None:
        self.pos = max(0, self.pos - 1)

    def right(self) -> None:
        self.pos = min(len(self.chars), self.pos + 1)

    def home(self) -> None:
        self.pos = 0

    def end(self) -> None:
        self.pos = len(self.chars)

    def clear(self) -> None:
        self.chars, self.pos = [], 0

    def take(self) -> str:
        text = self.text
        self.clear()
        return text

    def edit(self, kind: str, text: str = "") -> bool:
        """編集のキーなら当てて True。ほかのキーは False。"""
        actions = {"backspace": self.backspace, "delete": self.delete, "left": self.left, "right": self.right,
                   "home": self.home, "end": self.end, "ctrl_u": self.clear}
        if kind == "text":
            self.insert(text)
            return True
        if kind in actions:
            actions[kind]()
            return True
        return False

    def view(self, width: int) -> Tuple[str, int]:
        """幅 width に収まる見える部分と、その中のカーソルの桁。長ければ横に送る。"""
        width = max(width, 2)
        start = 0
        while text_width("".join(self.chars[start:self.pos])) > width - 1:
            start += 1
        visible, used = [], 0
        for c in self.chars[start:]:
            w = char_width(c)
            if used + w > width:
                break
            visible.append(c)
            used += w
        return "".join(visible), text_width("".join(self.chars[start:self.pos]))


_CSI_FINAL = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "shift_tab"}
_CSI_TILDE = {"1": "home", "7": "home", "4": "end", "8": "end", "3": "delete"}
_CONTROL = {"\x7f": "backspace", "\x08": "backspace", "\t": "tab", "\x03": "ctrl_c", "\x04": "ctrl_d",
            "\x01": "home", "\x05": "end", "\x15": "ctrl_u", "\x02": "left", "\x06": "right", "\x1a": "ctrl_d"}


def decode_keys(text: str) -> List[Event]:
    """端末から一度に届いた字（貼り付けなら複数）を、キーの並びにする（Mac/Linux の ESC の列）。"""
    events: List[Event] = []
    buf: List[str] = []

    def flush() -> None:
        if buf:
            events.append(("text", "".join(buf)))
            buf.clear()

    i = 0
    while i < len(text):
        c = text[i]
        if c == "\x1b":
            flush()
            if text[i + 1:i + 2] in ("[", "O"):
                j = i + 2
                while j < len(text) and not ("@" <= text[j] <= "~"):
                    j += 1
                if j >= len(text):  # 途中で切れた列は Esc とみなす
                    events.append(("esc", ""))
                    break
                params, final = text[i + 2:j], text[j]
                if final == "~":
                    key = _CSI_TILDE.get(params.split(";")[0])
                else:
                    key = _CSI_FINAL.get(final)
                if key:
                    events.append((key, ""))
                i = j + 1
                continue
            events.append(("esc", ""))
            i += 1
            continue
        if c in "\r\n":
            flush()
            events.append(("enter", ""))
            if c == "\r" and text[i + 1:i + 2] == "\n":
                i += 1
            i += 1
            continue
        if c in _CONTROL:
            flush()
            events.append((_CONTROL[c], ""))
        elif ord(c) >= 32:
            buf.append(c)
        i += 1
    flush()
    return events


_WIN_SCAN = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end", "S": "delete",
             "\x0f": "shift_tab"}


def decode_windows_keys(text: str) -> List[Event]:
    """msvcrt.getwch で読んだ字の並びをキーにする。矢印などは '\\x00' か '\\xe0' の後に 1 字が続く。"""
    events: List[Event] = []
    start = i = 0
    while i < len(text):
        if text[i] in "\x00\xe0" and i + 1 < len(text) and (text[i] == "\x00" or text[i + 1] in _WIN_SCAN):
            events.extend(decode_keys(text[start:i]))
            key = _WIN_SCAN.get(text[i + 1])
            if key:
                events.append((key, ""))
            i += 2
            start = i
            continue
        i += 1
    events.extend(decode_keys(text[start:]))
    # 連なった text をまとめる（decode_keys を区切って呼んだぶん）
    merged: List[Event] = []
    for ev in events:
        if ev[0] == "text" and merged and merged[-1][0] == "text":
            merged[-1] = ("text", merged[-1][1] + ev[1])
        else:
            merged.append(ev)
    return merged


# ---------------------------------------------------------------- キーを読む（端末に触る部分）


def raw_input_available(stdin: object = None) -> bool:
    stdin = sys.stdin if stdin is None else stdin
    if not _isatty(stdin):
        return False
    if sys.platform == "win32":
        try:
            import msvcrt  # noqa: F401
            return True
        except ImportError:
            return False
    try:
        import termios  # noqa: F401
        return True
    except ImportError:
        return False


def interactive_ok(stdin: object = None, stdout: object = None) -> bool:
    """自前の 1 行入力・状態の行・つなぐ欄を使えるか。使えなければ行ごとに読む形に戻す。"""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    return raw_input_available(stdin) and vt_supported(stdout)


class KeyReader:
    """with の間、端末を 1 キーずつ読む形にする。抜けると（例外でも）元に戻す。"""

    def __init__(self, stdin: object = None):
        self.stdin = sys.stdin if stdin is None else stdin
        self._old = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def __enter__(self) -> "KeyReader":
        if sys.platform != "win32":
            import termios

            fd = self.stdin.fileno()  # type: ignore[attr-defined]
            self._old = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            new[0] &= ~(termios.IXON | termios.ICRNL)
            new[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._old is not None:
            import termios

            termios.tcsetattr(self.stdin.fileno(), termios.TCSADRAIN, self._old)  # type: ignore[attr-defined]
            self._old = None

    def read(self, timeout: float) -> List[Event]:
        """timeout 秒までキーを待つ。届かなければ空。Ctrl+C は ctrl_c として返す。"""
        try:
            return self._read_windows(timeout) if sys.platform == "win32" else self._read_posix(timeout)
        except KeyboardInterrupt:
            return [("ctrl_c", "")]

    def _read_posix(self, timeout: float) -> List[Event]:
        import select

        fd = self.stdin.fileno()  # type: ignore[attr-defined]
        if not select.select([fd], [], [], timeout)[0]:
            return []
        data = os.read(fd, 4096)
        if not data:
            return [("ctrl_d", "")]
        text = self._decoder.decode(data)
        if text.endswith("\x1b") or text.endswith("\x1b["):  # 列の続きを少しだけ待つ
            if select.select([fd], [], [], 0.03)[0]:
                text += self._decoder.decode(os.read(fd, 4096))
        return decode_keys(text)

    def _read_windows(self, timeout: float) -> List[Event]:
        import msvcrt

        end = time.monotonic() + timeout
        while not msvcrt.kbhit():  # type: ignore[attr-defined]
            if time.monotonic() >= end:
                return []
            time.sleep(0.02)
        chars = []
        while msvcrt.kbhit() and len(chars) < 4096:  # type: ignore[attr-defined]
            chars.append(msvcrt.getwch())  # type: ignore[attr-defined]
        return decode_windows_keys("".join(chars))


# ---------------------------------------------------------------- 表示


def format_message(when: float, name: str, text: str, mine: bool, name_width: int, style: Style,
                   glyphs: Glyphs) -> str:
    t = time.strftime("%H:%M", time.localtime(when))
    who = style.paint(f"{pad(clean(name), name_width)} {glyphs.arrow}", GREEN if mine else CYAN)
    return f"{style.paint(t, DIM)} {who} {clean(text)}"


def format_status(st: dict, glyphs: Glyphs) -> Tuple[str, str]:
    """状態の行の字と種類（ok・quiet・lost）。st は ChatSession.status_info() の形。"""
    name = clean(st.get("name", ""))
    state = st.get("state", "ok")
    if state == "lost":
        return f" {glyphs.error} {name}  切れました ", "lost"
    if state == "quiet":
        return f" {glyphs.quiet} {name}  返事が {int(st.get('silent', 0))} 秒ありません … ", "quiet"
    minutes = int(st.get("elapsed", 0) // 60)
    return f" {glyphs.info} {name}  {st.get('ip', '')}  つないで {minutes} 分  /help  /quit ", "ok"


class PlainView:
    """端末でないとき（パイプ・テスト）の表示。1 行ずつ書くだけ。"""

    KIND_COLOR = {"info": YELLOW, "error": RED, "ok": GREEN}

    def __init__(self, out: TextIO, color: Optional[bool] = None, unicode: Optional[bool] = None):
        self.out = out
        self.style = Style(color_supported(out) if color is None else color)
        self.glyphs = Glyphs(can_draw(out) if unicode is None else unicode)
        self.lock = threading.RLock()
        self.name_width = 0

    # 書き出し
    def _write(self, text: str) -> None:
        try:
            self.out.write(text)
            self.out.flush()
        except (OSError, ValueError):
            pass

    def line(self, text: str) -> None:
        with self.lock:
            self._write(text + "\n")

    def mark(self, kind: str) -> str:
        return {"info": self.glyphs.info, "error": self.glyphs.error, "ok": self.glyphs.ok}.get(kind, "")

    def format_notice(self, text: str, kind: str = "info") -> str:
        return self.style.paint(f"{self.mark(kind)} {text}", self.KIND_COLOR.get(kind, YELLOW))

    # ChatSession が呼ぶもの
    def start(self, peer_name: str, my_label: str) -> None:
        self.name_width = max(text_width(clean(peer_name)), text_width(my_label))

    def stop(self) -> None:
        pass

    def message(self, mine: bool, name: str, text: str, when: Optional[float] = None) -> None:
        self.line(format_message(time.time() if when is None else when, name, text, mine,
                                 self.name_width, self.style, self.glyphs))

    def notice(self, text: str, kind: str = "info") -> None:
        self.line(self.format_notice(clean(text), kind))

    def lines(self, rows: Sequence[str]) -> None:
        with self.lock:
            for r in rows:
                self.line(r)

    def progress(self, text: str, final: bool) -> None:
        if final:
            self.line(text)

    def status(self, st: dict) -> None:
        pass

    def set_prompt(self, prompt: Optional[str]) -> None:
        if prompt:
            self.line(prompt)


class TerminalView(PlainView):
    """端末のときの表示。入力の行（一番下）と状態の行（一番上）を描き直しながら、発言をその間に流す。"""

    def __init__(self, out: TextIO, color: Optional[bool] = None, unicode: Optional[bool] = None,
                 size: Callable[[], os.terminal_size] = shutil.get_terminal_size):
        super().__init__(out, color, unicode)
        self.size = size
        self.editor = LineEditor()
        self.base_prompt = self.glyphs.arrow + " "
        self.prompt = self.base_prompt
        self.use_status = False
        self.status_text, self.status_kind = "", "ok"
        self.live: Optional[str] = None
        self._resized = False
        self._last_size = None
        self._last_poll = 0.0
        self._old_winch = None

    # 大きさ
    def _cols_rows(self) -> Tuple[int, int]:
        s = self.size()
        return max(s.columns, 10), max(s.lines, 1)

    def start(self, peer_name: str, my_label: str, status: bool = True) -> None:
        super().start(peer_name, my_label)
        cols, rows = self._cols_rows()
        self._last_size = (cols, rows)
        self.use_status = status and rows >= 5
        if self.use_status and sys.platform != "win32" and threading.current_thread() is threading.main_thread():
            import signal

            try:
                self._old_winch = signal.signal(signal.SIGWINCH, self._on_winch)
            except (ValueError, OSError, AttributeError):
                self._old_winch = None
        with self.lock:
            if self.use_status:
                self._write(f"\n\x1b[2;{rows}r")
                self._draw_status()
                self._write(f"\x1b[{rows};1H")
            self._draw_input()

    def stop(self) -> None:
        with self.lock:
            self._write("\r\x1b[K")
            if self.use_status:
                _, rows = self._cols_rows()
                self._draw_status()
                self._write(f"\x1b[r\x1b[{rows};1H\r\x1b[K")
            self.use_status = False
        if self._old_winch is not None:
            import signal

            try:
                signal.signal(signal.SIGWINCH, self._old_winch)
            except (ValueError, OSError):
                pass
            self._old_winch = None

    def _on_winch(self, *_: object) -> None:
        self._resized = True

    def poll_resize(self) -> None:
        """大きさが変わったら描き直す。Mac/Linux は SIGWINCH、Windows は 0.5 秒ごとに見に行く。"""
        now = time.monotonic()
        if not self._resized and now - self._last_poll >= 0.5:
            self._last_poll = now
            if self._cols_rows() != self._last_size:
                self._resized = True
        if self._resized:
            self._resized = False
            self.resize()

    def resize(self) -> None:
        with self.lock:
            cols, rows = self._cols_rows()
            self._last_size = (cols, rows)
            if self.use_status:
                self._write(f"\x1b[2;{rows}r")
                self._draw_status()
                self._write(f"\x1b[{rows};1H")
            self.live = None
            self._draw_input()

    # 描く
    def _draw_input(self) -> None:
        cols, _ = self._cols_rows()
        pw = text_width(self.prompt)
        visible, col = self.editor.view(max(cols - pw - 1, 4))
        self._write("\r\x1b[K" + self.style.paint(self.prompt, GREEN) + visible + "\r"
                    + (f"\x1b[{pw + col}C" if pw + col else ""))

    def _draw_status(self) -> None:
        if not self.use_status:
            return
        cols, _ = self._cols_rows()
        text = pad(truncate(self.status_text, cols - 1), cols - 1)
        code = {"lost": RED, "quiet": YELLOW}.get(self.status_kind, "")
        painted = f"{REVERSE}{code}{text}{RESET}" if self.style.color else f"{REVERSE}{text}{RESET}"
        self._write("\x1b7\x1b[1;1H\x1b[2K" + painted + "\x1b8")

    def redraw_input(self) -> None:
        with self.lock:
            self._draw_input()

    def line(self, text: str) -> None:
        with self.lock:
            self._write("\r\x1b[K")
            if self.live is not None:
                self._write("\x1b[1A\r\x1b[K")
            self._write(text + "\n")
            if self.live is not None:
                self._write(self.live + "\n")
            self._draw_input()

    def progress(self, text: str, final: bool) -> None:
        with self.lock:
            cols, _ = self._cols_rows()
            text = truncate(text, cols - 1)
            self._write("\r\x1b[K")
            if self.live is not None:
                self._write("\x1b[1A\r\x1b[K")
            self._write(text + "\n")
            self.live = None if final else text
            self._draw_input()

    def status(self, st: dict) -> None:
        text, kind = format_status(st, self.glyphs)
        if (text, kind) == (self.status_text, self.status_kind):
            return
        with self.lock:
            self.status_text, self.status_kind = text, kind
            self._draw_status()

    def set_prompt(self, prompt: Optional[str]) -> None:
        with self.lock:
            self.prompt = (prompt + " " + self.glyphs.arrow + " ") if prompt else self.base_prompt
            self._draw_input()


def input_loop(handle: Callable[[str], bool], keys: KeyReader, view: TerminalView, closed: threading.Event) -> None:
    """キーを読んで入力の行を編集し、Enter で handle(行) を呼ぶ。handle が False か、切れたら終わる。"""
    editor = view.editor
    while not closed.is_set():
        events = keys.read(0.1)
        view.poll_resize()
        for kind, text in events:
            if kind == "enter":
                line = editor.take()
                view.redraw_input()
                if not handle(line):
                    return
            elif kind == "ctrl_c":
                return
            elif kind == "ctrl_d":
                if not editor.text:
                    return
                editor.delete()
            else:
                editor.edit(kind, text)
        if events:
            view.redraw_input()


# ---------------------------------------------------------------- つなぐ欄


class JoinForm:
    """つなぐ欄（相手の IP・合言葉・自分の名前）の状態。端末に触らないので試験できる。"""

    FIELDS = (("ip", "相手の IP"), ("code", "合言葉"), ("name", "自分の名前"))

    def __init__(self, title: str, ip: str = "", code: str = "", name: str = ""):
        self.title = title
        self.editors = {"ip": LineEditor(ip), "code": LineEditor(code), "name": LineEditor(name)}
        self.message, self.message_kind = "", "info"
        empties = [i for i, (k, _) in enumerate(self.FIELDS) if not self.editors[k].text.strip()]
        self.focus = empties[0] if empties else 0

    @property
    def key(self) -> str:
        return self.FIELDS[self.focus][0]

    def values(self) -> Dict[str, str]:
        return {k: e.text.strip() for k, e in self.editors.items()}

    def move(self, step: int) -> None:
        self.focus = (self.focus + step) % len(self.FIELDS)

    def handle(self, kind: str, text: str = "") -> Optional[str]:
        """キーを 1 つ当てる。つなぐなら "submit"、戻るなら "back"、終えるなら "quit"。"""
        if kind in ("tab", "down"):
            self.move(1)
        elif kind in ("shift_tab", "up"):
            self.move(-1)
        elif kind == "esc":
            return "back"
        elif kind == "ctrl_c":
            return "quit"
        elif kind == "enter":
            values = self.values()
            for i, (k, label) in enumerate(self.FIELDS):
                if not values[k]:
                    self.focus = i
                    self.message, self.message_kind = f"{label}を入れてください", "error"
                    return None
            return "submit"
        else:
            self.editors[self.key].edit(kind, text)
        return None

    def fail(self, field: str, message: str) -> None:
        """つなげなかったとき。まちがえた欄へ移る（合言葉は空にする）。ほかの欄は残す。"""
        self.focus = [k for k, _ in self.FIELDS].index(field)
        if field == "code":
            self.editors["code"].clear()
        else:
            self.editors[field].end()
        self.message, self.message_kind = message, "error"

    def render(self, glyphs: Glyphs, width: int = 40) -> Tuple[List[str], int, int]:
        """(行, カーソルの行, カーソルの桁)。"""
        lw = max(text_width(label) for _, label in self.FIELDS) + 2
        lines = [self.title]
        cursor = (0, 0)
        for i, (k, label) in enumerate(self.FIELDS):
            mark = glyphs.arrow if i == self.focus else " "
            head = f"  {mark} {pad(label, lw)}"
            editor = self.editors[k]
            visible, col = editor.view(width)
            if i == self.focus:
                cursor = (len(lines), text_width(head) + col)
            lines.append(head + visible)
        lines.append(f"{self.mark(glyphs)} {self.message}" if self.message else "")
        lines.append("Tab ↑↓ 欄を移る  Enter つなぐ  Esc 戻る" if glyphs.unicode
                     else "Tab/Up/Down 欄を移る  Enter つなぐ  Esc 戻る")
        return lines, cursor[0], cursor[1]

    def mark(self, glyphs: Glyphs) -> str:
        return glyphs.error if self.message_kind == "error" else glyphs.info


class FormFailure(Exception):
    """つなぐ欄でつなげなかった。field は戻る欄。"""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def run_form(form: JoinForm, keys: KeyReader, out: TextIO, connect: Callable[[Dict[str, str]], object],
             unicode: Optional[bool] = None, color: Optional[bool] = None) -> object:
    """つなぐ欄を描いてキーを当て、Enter で connect(値) を呼ぶ。Esc なら None。

    connect は FormFailure(欄, 知らせ) を投げると、その欄に戻る。
    """
    glyphs = Glyphs(can_draw(out) if unicode is None else unicode)
    style = Style(color_supported(out) if color is None else color)
    state = {"drawn": 0, "row": 0}

    def w(text: str) -> None:
        out.write(text)
        out.flush()

    def draw() -> None:
        lines, crow, ccol = form.render(glyphs)
        if state["drawn"]:
            w((f"\x1b[{state['row']}A" if state["row"] else "") + "\r")
        for i, ln in enumerate(lines):
            if i == len(lines) - 2 and form.message:
                ln = style.paint(ln, RED if form.message_kind == "error" else YELLOW)
            w("\x1b[2K" + ln + ("\n" if i < len(lines) - 1 else ""))
        up = len(lines) - 1 - crow
        w((f"\x1b[{up}A" if up else "") + "\r" + (f"\x1b[{ccol}C" if ccol else ""))
        state["drawn"], state["row"] = len(lines), crow

    def leave() -> None:
        down = state["drawn"] - 1 - state["row"]
        w((f"\x1b[{down}B" if down > 0 else "") + "\r\n")

    draw()
    while True:
        for kind, text in keys.read(0.2):
            result = form.handle(kind, text)
            if result == "back":
                leave()
                return None
            if result == "quit":
                leave()
                raise KeyboardInterrupt
            if result == "submit":
                form.message, form.message_kind = "つないでいます …", "info"
                draw()
                try:
                    value = connect(form.values())
                except FormFailure as exc:
                    form.fail(exc.field, str(exc))
                else:
                    leave()
                    return value
            draw()
