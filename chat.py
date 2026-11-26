"""同じ Wi-Fi の2台で使う、ターミナルの1対1チャットとファイル送受信（標準ライブラリのみ）。

待つ側は IP と合言葉を出し、つなぐ側はそれを入れてつなぐ。通信は平文の TCP。
このファイルは通信と取り決め（名乗り・枠・合図・ファイルの受け渡し）を受け持ち、
ターミナルの画面と入力は chat_term.py、ブラウザの版と相手のページは chat_web.py が受け持つ（どれも同じ ChatSession を呼ぶ）。

  python3 chat.py                              # メニューから選ぶ
  python3 chat.py --host                       # チャットを待つ
  python3 chat.py --join 192.168.0.9           # チャットにつなぐ（合言葉は聞かれる）
  python3 chat.py --receive                    # ファイルを受け取る側として待つ
  python3 chat.py --send 192.168.0.9 a.pdf     # ファイルを送る
  python3 chat.py --web                        # ブラウザの版（chat_web.py）

取り決め
  名乗り（1 行ずつ）  つなぐ側 → "HELLO <合言葉> <名前>" か "FILES <合言葉> <名前>"
                      待つ側   → "OK <名前>" か "NG <理由>"
  チャットの新しいバージョンは、名乗りの行の名前の後ろにタブを 1 つ付ける（V2_MARK）。
  古いバージョンの待つ側は名前の前後の空白を落とすので、タブは見えずに今までどおり "OK" と答える。
  新しいバージョンの待つ側はタブを見て "OK2 <名前>" と答え、以後そのつながりは枠だけで話す。
  枠は「種類 1 バイト＋長さ 4 バイト＋中身」。M 発言、P 合図、B 抜ける、O A R D E V F はファイル。
  古いバージョンと話すときは、今までどおりチャットは 1 行 1 件の UTF-8、ファイルは FILES のつながりで送る。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO, Callable, Dict, Iterator, List, Optional, TextIO, Tuple, TypeVar

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 別の場所から起動しても chat_term を読む
import chat_term  # noqa: E402

# python3 chat.py で動かしたときも、chat_web.py の「import chat」がこのモジュールを指すようにする
# （別の写しが読まれると、ChatError などのクラスが二つになって except で受けられない）
sys.modules.setdefault("chat", sys.modules[__name__])
from chat_term import human_size  # noqa: E402,F401  （昔からの呼び名を残す）

T = TypeVar("T")

DEFAULT_PORT = 5050          # 5000 は macOS の AirPlay 受信が使うので避ける
MAX_MESSAGE_LENGTH = 4096
MAX_NAME_LENGTH = 30
CONNECT_TIMEOUT = 3.0        # 打ち間違えた IP で長く待たない
HELLO_TIMEOUT = 10.0         # つないだまま名乗らない相手で待ち受けを塞がない
MAX_CODE_MISSES = 5          # 合言葉の間違いがこれだけたまったら待つのをやめる
QUIT_COMMAND = "/quit"
WORDS = {"HELLO": "チャット", "FILES": "ファイル受信"}  # 名乗りの最初の語と、その用途
V2_MARK = "\t"               # 新しいバージョンのしるし（名乗りの行の末尾）。古いバージョンは空白として落とす
HEARTBEAT_INTERVAL = 10.0    # 合図を送る間隔（秒）
SILENT_WARN = 20.0           # これだけ届かなければ状態の行で知らせる
SILENT_LIMIT = 30.0          # これだけ届かなければ切れたとみなす
ACCEPT_POLL = 0.2            # 待ち受けが、相手のページ側で相手が決まったか・やめたかを見に行く間隔（秒）
STATE_PATH = Path(os.environ.get("CHAT_APP_STATE") or Path(__file__).resolve().parent / ".chat_state.json")
MY_LABEL = "自分"


class ChatError(Exception):
    """利用者に見せてよい失敗（つながらない、合言葉ちがい など）。"""


class Rejected(ChatError):
    """待つ側にはつながったが断られた（合言葉ちがい など）。IP は合っている。"""


class BadName(ChatError):
    """名前の長さが合わない。"""


def lan_ip() -> str:
    """相手から届く自分の IP を返す。

    gethostbyname(gethostname()) は 127.0.0.1 を返すことがあるので使わない。
    UDP の connect は経路を選ぶだけで、パケットは送らない。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # 届かない試験用アドレス（RFC 5737）
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def new_code() -> str:
    return f"{secrets.randbelow(10000):04d}"


def parse_address(text: str, default_port: int = DEFAULT_PORT) -> Tuple[str, int]:
    """'192.168.0.9' や '192.168.0.9:6000' を (host, port) にする。"""
    text = text.strip()
    if not text:
        raise ChatError("IP が空です")
    host, sep, port = text.rpartition(":")
    if not sep:
        return text, default_port
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ChatError(f"ポートの書き方が違います: {port}")
    return host, int(port)


def check_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        raise BadName(f"名前は1〜{MAX_NAME_LENGTH}文字にしてください")
    return name


def _check_text(text: str) -> bytes:
    if "\n" in text or "\r" in text:
        raise ChatError("1件に改行は入れられません")
    if len(text) > MAX_MESSAGE_LENGTH:
        raise ChatError(f"長すぎます（{len(text)} 文字。上限 {MAX_MESSAGE_LENGTH}）")
    return text.encode("utf-8")


class Peer:
    """古いバージョンの取り決めの相手1人。1行を1件として読み書きする。"""

    version = 1

    def __init__(self, sock: socket.socket, name: str):
        self.sock = sock
        self.name = name
        self._reader = sock.makefile("r", encoding="utf-8", errors="replace", newline="\n")
        self._lock = threading.Lock()
        self.closed_by_me = False

    def send(self, text: str) -> None:
        data = _check_text(text)
        with self._lock:
            self.sock.sendall(data + b"\n")

    def lines(self) -> Iterator[str]:
        """相手の行を順に返す。相手が切断したら終わる。上限を超えた行が来たら切る。"""
        try:
            while True:
                line = self._reader.readline(MAX_MESSAGE_LENGTH + 2)
                if not line:
                    return
                if not line.endswith("\n"):
                    return  # 上限超え、または改行なしで切れた
                yield line.rstrip("\r\n")
        except (OSError, ValueError):
            return

    def finish(self) -> None:
        """もう送らないと相手に伝える（相手の lines() が終わる）。"""
        self.closed_by_me = True
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def close(self) -> None:
        # 先に別スレッドの readline を起こす。
        # 読んでいる最中に reader を閉じると、バッファのロック待ちで止まる。
        self.closed_by_me = True
        _abort(self.sock)
        try:
            self._reader.close()
        finally:
            self.sock.close()


class FramedPeer:
    """新しいバージョンの相手1人。発言・合図・ファイルを、すべて同じ枠で読み書きする。"""

    version = 2

    def __init__(self, sock: socket.socket, name: str):
        self.sock = sock
        self.name = name
        self._reader = sock.makefile("rb")
        self._lock = threading.Lock()
        self.closed_by_me = False

    def send_frame(self, kind: bytes, payload: bytes = b"") -> None:
        with self._lock:
            send_frame(self.sock, kind, payload)

    def send(self, text: str) -> None:
        self.send_frame(MSG, _check_text(text))

    def frames(self) -> Iterator[Tuple[bytes, bytes]]:
        """枠を順に返す。相手が切断したら終わる。枠が壊れていたら ConnectionError。"""
        while True:
            try:
                frame = recv_frame(self._reader)
            except ValueError:  # 自分で閉じた後
                return
            if frame is None:
                return
            yield frame

    def lines(self) -> Iterator[str]:
        """発言だけを順に返す（Peer と同じ使い方ができるように）。"""
        try:
            for kind, payload in self.frames():
                if kind == MSG:
                    yield payload.decode("utf-8", errors="replace")
                elif kind == BYE:
                    return
        except OSError:
            return

    def finish(self) -> None:
        self.closed_by_me = True
        try:
            self.send_frame(BYE)
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def close(self) -> None:
        self.closed_by_me = True
        _abort(self.sock)
        try:
            self._reader.close()
        finally:
            self.sock.close()


def _read_line(sock: socket.socket) -> str:
    """ハンドシェイク用に1行だけ読む（makefile を作る前なのでバイト単位で）。末尾の \\r だけ落とす。"""
    buf = bytearray()
    while len(buf) <= MAX_NAME_LENGTH * 4 + 64:
        b = sock.recv(1)
        if not b:
            break
        if b == b"\n":
            return buf.decode("utf-8", errors="replace").rstrip("\r")
        buf += b
    raise ChatError("名乗りの行が読めませんでした")


class WaitGate:
    """待つ側の「最初に合言葉の合った1人」を決める札。TCP の待ち受けと相手のページで1つを分け合う。

    合言葉の間違いもここで1つに数える。max_misses 回たまるか、cancel されたら done になり、error に理由が入る。
    相手が決まったときは winner に入る（TCP なら "tcp"、相手のページなら chat_web.PagePeer）。
    """

    def __init__(self, max_misses: int = MAX_CODE_MISSES):
        self.max_misses = max_misses
        self.misses = 0
        self.winner: object = None
        self.error = ""
        self.done = threading.Event()
        self._lock = threading.Lock()

    def miss(self) -> int:
        """合言葉の間違いを1つ数え、残りの回数を返す。0 になったら待つのをやめる。"""
        with self._lock:
            self.misses += 1
            left = max(self.max_misses - self.misses, 0)
            if left == 0 and not self.done.is_set():
                self.error = f"合言葉の間違いが {self.max_misses} 回になったので、待つのをやめました"
                self.done.set()
            return left

    def claim(self, winner: object) -> bool:
        """相手に決める。もう決まっていたか、やめていたら False。"""
        with self._lock:
            if self.done.is_set():
                return False
            self.winner = winner
            self.done.set()
            return True

    def cancel(self, reason: str = "待つのをやめました") -> None:
        with self._lock:
            if not self.done.is_set():
                self.error = reason
                self.done.set()

    def check(self) -> None:
        """やめていたら ChatError。"""
        if self.done.is_set() and self.winner is None:
            raise ChatError(self.error or "待つのをやめました")


def _host_raw(
    code: str,
    name: str,
    word: str,
    port: int,
    bind: str,
    on_ready: Optional[Callable[[int], None]],
    on_reject: Optional[Callable[[str, str], None]],
    max_misses: int = MAX_CODE_MISSES,
    gate: Optional[WaitGate] = None,
) -> Optional[Tuple[socket.socket, str, int]]:
    """合言葉の合う相手が1人つないでくるまで待ち、(接続, 相手の名前, 取り決めの版) を返す。

    合わない相手は断って待ち続ける。合言葉の間違いが max_misses 回たまったら ChatError で終わる
    （4 桁の数字は同じ Wi-Fi から順に試せば当たるため）。word は名乗りの最初の語（HELLO か FILES）。
    gate を渡すと、相手のページと間違いの回数・相手を分け合う。ページ側で相手が決まったら None を返す。
    """
    gate = gate or WaitGate(max_misses)
    try:
        srv = socket.create_server((bind, port))
    except OSError as exc:
        raise ChatError(f"ポート {port} で待ち受けられません（{exc.strerror}）。--port で変えてください") from exc
    with srv:
        srv.settimeout(ACCEPT_POLL)  # ページ側で決まった・やめたことに気づけるように
        if on_ready:
            on_ready(srv.getsockname()[1])
        while True:
            if gate.done.is_set():
                gate.check()
                return None  # 相手のページで相手が決まった（この待ち受けは閉じる）
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            conn.settimeout(HELLO_TIMEOUT)
            try:
                line = _read_line(conn)
                version = 2 if word == "HELLO" and line.endswith(V2_MARK) else 1
                their_word, _, rest = line.partition(" ")
                their_code, _, their_name = rest.partition(" ")
                if their_word not in WORDS:
                    raise ChatError("このアプリの接続ではありません")
                if their_word != word:
                    raise ChatError(f"相手は{WORDS[word]}用に待っています")
                if their_code != code:
                    raise ChatError(f"合言葉がちがいます（残り {gate.miss()} 回）")
                their_name = check_name(their_name)
                if not gate.claim("tcp"):
                    raise ChatError("もう相手が決まりました")
            except (ChatError, OSError) as exc:
                reason = str(exc) if isinstance(exc, ChatError) else "名乗りの前に切れました"
                try:
                    conn.sendall(f"NG {reason}\n".encode("utf-8"))
                except OSError:
                    pass
                conn.close()
                if on_reject:
                    on_reject(addr[0], reason)
                continue
            conn.sendall(f"{'OK2' if version == 2 else 'OK'} {name}\n".encode("utf-8"))
            conn.settimeout(None)
            return conn, their_name, version


def _join_raw(address: str, code: str, name: str, word: str, default_port: int) -> Tuple[socket.socket, str, int]:
    """待つ側につなぎ、合言葉と名前を名乗る。(接続, 相手の名前, 取り決めの版) を返す。"""
    host, port = parse_address(address, default_port)
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except socket.gaierror as exc:
        raise ChatError(f"{host} が見つかりません") from exc
    except OSError as exc:
        raise ChatError(f"{host}:{port} につながりません（{exc.strerror or exc}）") from exc
    mark = V2_MARK if word == "HELLO" else ""
    try:
        sock.sendall(f"{word} {code} {name}{mark}\n".encode("utf-8"))
        answer, _, rest = _read_line(sock).partition(" ")
    except (OSError, ChatError) as exc:
        sock.close()
        raise ChatError("相手が応答しませんでした（このアプリの待つ側ではないかもしれません）") from exc
    if answer not in ("OK", "OK2"):
        sock.close()
        raise Rejected(f"断られました: {rest or answer}")
    sock.settimeout(None)
    return sock, rest.strip() or host, 2 if answer == "OK2" else 1


def _make_peer(sock: socket.socket, name: str, version: int):
    return FramedPeer(sock, name) if version == 2 else Peer(sock, name)


def host_wait(
    code: str,
    name: str,
    port: int = DEFAULT_PORT,
    bind: str = "0.0.0.0",
    on_ready: Optional[Callable[[int], None]] = None,
    on_reject: Optional[Callable[[str, str], None]] = None,
    max_misses: int = MAX_CODE_MISSES,
    page_port: Optional[int] = None,
    on_page: Optional[Callable[[Optional[int], str], None]] = None,
    gate: Optional[WaitGate] = None,
    page_bind: Optional[str] = None,
):
    """チャットの相手を待つ。相手の版に合わせて Peer か FramedPeer を返す。

    page_port を渡すと、同じ Wi-Fi に相手のページ（chat_web.GuestPage）も出し、先に合言葉の合った
    1人と話す。ページの相手なら chat_web.PagePeer を返す（ChatSession からは FramedPeer と同じに使える）。
    on_page(ページのポート, 出せなかった理由) は、待ち受けの on_ready より先に呼ぶ。
    """
    gate = gate or WaitGate(max_misses)
    page = None
    if page_port is not None:
        import chat_web  # noqa: WPS433  （ページを使うときだけ読む）

        try:
            page = chat_web.GuestPage(gate, code, name, page_bind or bind, page_port, on_reject)
        except ChatError as exc:
            if on_page:
                on_page(None, str(exc))
        else:
            if on_page:
                on_page(page.port, "")
    try:
        raw = _host_raw(code, name, "HELLO", port, bind, on_ready, on_reject, max_misses, gate)
    except BaseException:
        if page is not None:
            page.close()
        raise
    if raw is None:
        gate.check()
        return gate.winner  # 相手のページの相手（ページはそのまま、その相手との通り道になる）
    if page is not None:
        page.close()  # TCP の相手に決まったので、相手のページは閉じる
    return _make_peer(*raw)


def join(address: str, code: str, name: str, default_port: int = DEFAULT_PORT):
    """チャットの待つ側につなぐ。相手の版に合わせて Peer か FramedPeer を返す。"""
    return _make_peer(*_join_raw(address, code, name, "HELLO", default_port))


# ---------------------------------------------------------------- 枠とファイル
#
# すべて「種類1バイト＋長さ4バイト（ビッグエンディアン）＋中身」。
#   送る側 → OFFER {"name","size"} → 受ける側 ACCEPT か REJECT <理由>
#   送る側 → DATA（64 KiB ずつ）… → DONE {"sha256"} → 受ける側 SAVED {"name"} か FAILED <理由>
#   送る側 → BYE（もう送らない）
# 新しいバージョンのチャットでは、これに MSG（発言）と PING（合図）を混ぜる。どちらの向きにも送れる。

CHUNK_SIZE = 64 * 1024
MAX_FRAME = CHUNK_SIZE + 1024
DEFAULT_MAX_FILE_SIZE = 2 * 1024**3  # 2 GiB
FREE_SPACE_MARGIN = 100 * 1024**2    # 受け取ったあとも 100 MiB は空けておく
DEFAULT_SAVE_DIR = Path(__file__).resolve().parent / "received"
PROGRESS_INTERVAL = 0.2              # 進み具合の表示を書き直す間隔（秒）

OFFER, ACCEPT, REJECT, DATA, DONE, SAVED, FAILED, BYE, MSG, PING = (bytes([c]) for c in b"OARDEVFBMP")


class TransferError(ChatError):
    """ファイル1件の受け渡しが成り立たなかった（断られた、照合が合わない など）。"""


def send_frame(sock: socket.socket, kind: bytes, payload: bytes = b"") -> None:
    sock.sendall(struct.pack("!cI", kind, len(payload)) + payload)


def recv_frame(rfile: BinaryIO) -> Optional[Tuple[bytes, bytes]]:
    """枠を1つ読む。相手がきれいに閉じたら None。途中で切れたら ConnectionError。"""
    head = rfile.read(5)
    if not head:
        return None
    if len(head) < 5:
        raise ConnectionError("枠の途中で切れました")
    kind, length = struct.unpack("!cI", head)
    if length > MAX_FRAME:
        raise ConnectionError(f"枠が大きすぎます（{length} バイト）")
    payload = rfile.read(length)
    if len(payload) < length:
        raise ConnectionError("枠の途中で切れました")
    return kind, payload


def _json(payload: bytes) -> dict:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectionError("相手の送ってきた内容が読めません") from exc
    if not isinstance(data, dict):
        raise ConnectionError("相手の送ってきた内容が読めません")
    return data


WINDOWS_BAD_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_file_name(name: object) -> str:
    """相手から来たファイル名を、保存先の中だけに収まる名前として確かめる。

    受け取る OS によらず、Windows でも使える名前だけを通す。
    Windows では「:」が NTFS の代替データストリームの区切りになり、別のファイルに書き込まれうる。
    """
    if not isinstance(name, str):
        raise TransferError("ファイル名がありません")
    if (not name or name in (".", "..") or any(c in WINDOWS_BAD_CHARS or ord(c) < 32 for c in name)):
        raise TransferError(f"使えないファイル名です: {name!r}")
    if name.startswith("."):
        raise TransferError(f"隠しファイルの名前は受け取りません: {name!r}")
    if name.endswith((".", " ")):
        raise TransferError(f"末尾が「.」か空白の名前は Windows で使えません: {name!r}")
    if name.split(".")[0].strip().upper() in WINDOWS_RESERVED:
        raise TransferError(f"Windows の予約名です: {name!r}")
    if len(name.encode("utf-8")) > 200:
        raise TransferError("ファイル名が長すぎます")
    return name


def unique_path(folder: Path, name: str) -> Path:
    """同じ名前があれば「名前 (1).拡張子」のように付け替える。"""
    target = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while target.exists():
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return target


class Progress:
    """進み具合。show(字, 最後か) に渡す。show が無ければ、端末のときだけ 1 行を書き直す。"""

    def __init__(self, label: str, total: int, out: Optional[TextIO] = None,
                 show: Optional[Callable[[str, bool], None]] = None, unicode: Optional[bool] = None):
        self.label, self.total = label, total
        self.done = 0
        self.start = self.last = time.monotonic()
        self.unicode = chat_term.can_draw(out) if unicode is None else unicode
        if show is None:
            live = chat_term._isatty(out)

            def show(text: str, final: bool) -> None:
                if live and out is not None:
                    out.write("\r" + text + ("\n" if final else ""))
                    out.flush()
        self.show = show

    def add(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if now - self.last >= PROGRESS_INTERVAL:
            self.last = now
            self.show(self._line(), False)

    def _line(self) -> str:
        rate = self.done / max(time.monotonic() - self.start, 1e-6)
        return chat_term.format_progress(self.label, self.done, self.total, rate, self.unicode)

    def finish(self) -> None:
        self.show(self._line(), True)


def check_send_path(path: Path) -> int:
    """送れるファイルか確かめて大きさを返す。"""
    if not path.is_file():
        raise TransferError(f"ファイルではありません。フォルダは zip にして送ってください: {path}" if path.is_dir()
                            else f"見つかりません: {path}")
    return path.stat().st_size


def _send_one(send: Callable[[bytes, bytes], None], reply: Callable[[], Optional[Tuple[bytes, bytes]]],
              path: Path, on_offer: Callable[[str, int], None],
              make_progress: Callable[[str, int], Progress]) -> str:
    """ファイルを1件送り、相手が保存した名前を返す。send は枠を送り、reply は相手の答えの枠を待つ。"""
    size = check_send_path(path)
    send(OFFER, json.dumps({"name": path.name, "size": size}).encode("utf-8"))
    on_offer(path.name, size)
    frame = reply()
    if frame is None:
        raise ConnectionError("相手が切断しました")
    kind, payload = frame
    if kind == REJECT:
        raise TransferError(f"断られました: {payload.decode('utf-8', 'replace')}")
    if kind != ACCEPT:
        raise ConnectionError("相手の応答が想定外です")

    digest = hashlib.sha256()
    progress = make_progress(path.name, size)
    sent = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            send(DATA, chunk)
            digest.update(chunk)
            sent += len(chunk)
            progress.add(len(chunk))
    progress.finish()
    if sent != size:
        raise ConnectionError(f"送っている間にファイルの大きさが変わりました（{size} → {sent}）")
    send(DONE, json.dumps({"sha256": digest.hexdigest()}).encode("utf-8"))

    frame = reply()
    if frame is None:
        raise ConnectionError("相手が切断しました")
    kind, payload = frame
    if kind == FAILED:
        raise TransferError(f"相手が保存できませんでした: {payload.decode('utf-8', 'replace')}")
    if kind != SAVED:
        raise ConnectionError("相手の応答が想定外です")
    return str(_json(payload).get("name", path.name))


def send_file(sock: socket.socket, rfile: BinaryIO, path: Path, out: TextIO) -> str:
    """ファイルを1件送り、相手が保存した名前を返す（ファイル送受信のつながり用）。"""
    view = chat_term.PlainView(out)
    return _send_one(
        lambda kind, payload: send_frame(sock, kind, payload),
        lambda: recv_frame(rfile),
        path,
        lambda name, size: view.notice(f"{name}（{human_size(size)}）を申し出ました。相手の応答待ち"),
        lambda name, size: Progress(f"送信中 {name}", size, out))


class Incoming:
    """受け取るファイル1件。申し出の確かめ、一時ファイルへの書き込み、照合、正式な名前への付け替え。"""

    def __init__(self, save_dir: Path, max_size: int = DEFAULT_MAX_FILE_SIZE):
        self.save_dir, self.max_size = save_dir, max_size
        self.name, self.size, self.received = "", 0, 0
        self.part: Optional[Path] = None
        self._file: Optional[BinaryIO] = None
        self.digest = hashlib.sha256()

    def check(self, payload: bytes) -> Tuple[str, int]:
        """申し出を確かめて (名前, 大きさ) を返す。受け取れないものは TransferError。"""
        offer = _json(payload)
        size = offer.get("size")
        name = safe_file_name(offer.get("name"))
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise TransferError("大きさが読めません")
        if size > self.max_size:
            raise TransferError(f"大きすぎます（{human_size(size)}。上限 {human_size(self.max_size)}）")
        self.save_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.save_dir).free
        if size + FREE_SPACE_MARGIN > free:
            raise TransferError(f"空き容量が足りません（空き {human_size(free)}）")
        self.name, self.size = name, size
        return name, size

    def begin(self) -> None:
        self.part = self.save_dir / f".{self.name}.{secrets.token_hex(4)}.part"
        self._file = self.part.open("xb")

    def write(self, payload: bytes) -> None:
        self.received += len(payload)
        if self.received > self.size:
            raise ConnectionError("申し出より大きなデータが届きました")
        assert self._file is not None
        self._file.write(payload)
        self.digest.update(payload)

    def finish(self, payload: bytes) -> Path:
        """DONE を受けて照合する。合えば正式な名前にして返す。合わなければ消して TransferError。"""
        assert self._file is not None and self.part is not None
        self._file.close()
        if self.received != self.size:
            reason = f"大きさが合いません（申し出 {self.size}、受信 {self.received}）"
        elif _json(payload).get("sha256") != self.digest.hexdigest():
            reason = "中身の照合が合いません。途中で壊れました"
        else:
            reason = ""
        if reason:
            self.part.unlink()
            self.part = None
            raise TransferError(reason)
        target = unique_path(self.save_dir, self.name)
        os.replace(str(self.part), str(target))
        self.part = None
        return target

    def abort(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
        if self.part is not None:
            try:
                self.part.unlink()
            except FileNotFoundError:
                pass
            self.part = None


def receive_files(
    sock: socket.socket,
    rfile: BinaryIO,
    save_dir: Path,
    out: TextIO,
    ask: Callable[[str, int], bool],
    max_size: int = DEFAULT_MAX_FILE_SIZE,
) -> List[Path]:
    """相手が BYE を送るか切断するまで受け取り続け、保存したファイルの一覧を返す。

    ask(名前, 大きさ) が True を返したものだけ受け取る。
    途中で切れたものと照合の合わないものは、一時ファイルごと消す。
    """
    view = chat_term.PlainView(out)
    saved: List[Path] = []
    save_dir.mkdir(parents=True, exist_ok=True)
    while True:
        frame = recv_frame(rfile)
        if frame is None or frame[0] == BYE:
            return saved
        kind, payload = frame
        if kind != OFFER:
            raise ConnectionError("相手の送ってきた内容が想定外です")
        inc = Incoming(save_dir, max_size)
        try:
            name, size = inc.check(payload)
        except TransferError as exc:
            view.notice(f"断りました: {exc}", "error")
            send_frame(sock, REJECT, str(exc).encode("utf-8"))
            continue
        if not ask(name, size):
            view.notice(f"断りました: {name}", "error")
            send_frame(sock, REJECT, "受け取りを断られました".encode("utf-8"))
            continue
        send_frame(sock, ACCEPT)

        progress = Progress(f"受信中 {name}", size, out)
        try:
            inc.begin()
            while True:
                frame = recv_frame(rfile)
                if frame is None:
                    raise ConnectionError("受信の途中で相手が切断しました")
                kind, payload = frame
                if kind == DATA:
                    inc.write(payload)
                    progress.add(len(payload))
                elif kind == DONE:
                    break
                else:
                    raise ConnectionError("相手の送ってきた内容が想定外です")
            progress.finish()
            try:
                target = inc.finish(payload)
            except TransferError as exc:
                view.notice(f"失敗しました: {name}: {exc}", "error")
                send_frame(sock, FAILED, str(exc).encode("utf-8"))
                continue
        except BaseException:
            inc.abort()
            raise
        saved.append(target)
        view.notice(f"保存しました  {target}  中身の照合 OK", "ok")
        send_frame(sock, SAVED, json.dumps({"name": target.name}).encode("utf-8"))


def host_receive(
    code: str,
    name: str,
    port: int = DEFAULT_PORT,
    bind: str = "0.0.0.0",
    on_ready: Optional[Callable[[int], None]] = None,
    on_reject: Optional[Callable[[str, str], None]] = None,
    max_misses: int = MAX_CODE_MISSES,
) -> Tuple[socket.socket, str]:
    """ファイルを送ってくる相手を待つ。"""
    sock, their_name, _ = _host_raw(code, name, "FILES", port, bind, on_ready, on_reject, max_misses)
    return sock, their_name


def join_send(address: str, code: str, name: str, default_port: int = DEFAULT_PORT) -> Tuple[socket.socket, str]:
    """ファイルの受け取り側につなぐ。"""
    sock, their_name, _ = _join_raw(address, code, name, "FILES", default_port)
    return sock, their_name


# ---------------------------------------------------------------- チャットの進行（表示と切り離した核）


HELP_LINES = [
    ("/send ファイル", "ファイルを送る（ドラッグしてもよい）"),
    ("/open", "保存先を開く"),
    ("/help", "この一覧"),
    ("/quit", "終わる"),
]


def parse_send_path(arg: str, platform: Optional[str] = None) -> str:
    """/send の後ろの字をファイルの場所にする。ドラッグで入る引用符と、\\ で逃がした空白を外す。"""
    platform = sys.platform if platform is None else platform
    s = arg.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    if platform != "win32":  # Windows の \ は場所の区切りなので触らない
        s = re.sub(r"\\(.)", r"\1", s)
    return s


def open_folder(folder: Path) -> None:
    """フォルダを Finder / エクスプローラー / ファイルマネージャーで開く。開けなくても止めない。"""
    try:
        if sys.platform == "win32":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            # 終わるまで待つ（Popen のまま抜けると、すぐ後の終了で一緒に止められて開かないことがある）
            subprocess.run([opener, str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"保存先を開けませんでした: {exc}", flush=True)


class ChatSession:
    """つながった相手1人とのチャット。受信・合図・ファイルの受け渡しを受け持ち、表示は ui に任せる。

    ui は chat_term.PlainView と同じ形（message・notice・lines・progress・status・set_prompt）。
    入力は handle_input(行) で渡す（False が返ったら終わり）。
    """

    def __init__(self, peer, ui, save_dir: Path = DEFAULT_SAVE_DIR, max_size: int = DEFAULT_MAX_FILE_SIZE,
                 auto_accept: bool = False, on_closed: Optional[Callable[[], None]] = None,
                 interval: float = HEARTBEAT_INTERVAL, warn: float = SILENT_WARN, limit: float = SILENT_LIMIT,
                 opener: Callable[[Path], None] = open_folder):
        self.peer, self.ui = peer, ui
        self.save_dir, self.max_size, self.auto_accept = save_dir, max_size, auto_accept
        self.on_closed = on_closed
        self.interval, self.warn, self.limit = interval, warn, limit
        self.opener = opener
        self.closed = threading.Event()   # つながりが終わった（どちらの理由でも）
        self.stopped = threading.Event()  # 自分で終えた
        self.started = self.last_recv = time.monotonic()
        self._lock = threading.Lock()
        self._replies: "queue.Queue[Optional[Tuple[bytes, bytes]]]" = queue.Queue()
        self._offer: Optional[Incoming] = None      # 答え待ちの申し出
        self._incoming: Optional[Incoming] = None   # 受け取り中
        self._incoming_progress: Optional[Progress] = None
        self._sending = False
        self.saved: List[Path] = []
        try:
            self.address = peer.sock.getpeername()[0]
        except (OSError, IndexError, AttributeError):
            self.address = ""

    @property
    def _unicode(self) -> bool:
        glyphs = getattr(self.ui, "glyphs", None)
        return glyphs.unicode if glyphs is not None else True

    @property
    def framed(self) -> bool:
        return getattr(self.peer, "version", 1) == 2

    # 始める・終える
    def start(self) -> None:
        self.ui.start(self.peer.name, MY_LABEL)
        self.ui.status(self.status_info())
        self.ui.notice(f"{self.peer.name} とつながりました  /help でコマンド一覧")
        if not self.framed:
            self.ui.notice("相手は古いバージョンです。/send と切れたことの早い知らせは使えません")
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        if self.framed:
            threading.Thread(target=self._ping, daemon=True).start()

    def quit(self) -> None:
        """自分で終える（/quit・Ctrl+C）。"""
        self.stopped.set()
        self.peer.finish()

    def status_info(self) -> dict:
        now = time.monotonic()
        silent = now - self.last_recv
        if self.closed.is_set() and not self.stopped.is_set():
            state = "lost"
        elif self.framed and silent >= self.warn:
            state = "quiet"
        else:
            state = "ok"
        return {"name": self.peer.name, "ip": self.address, "elapsed": now - self.started,
                "state": state, "silent": silent, "version": getattr(self.peer, "version", 1)}

    def _end(self, reason: str = "", kind: str = "error") -> None:
        with self._lock:
            if self.closed.is_set():
                return
            self.closed.set()
            pending, self._incoming, self._offer = self._incoming, None, None
        if pending is not None:
            pending.abort()
        self._replies.put(None)
        if self.peer.closed_by_me or self.stopped.is_set():
            return
        self.ui.set_prompt(None)
        self.ui.notice(reason or f"{self.peer.name} との接続が切れました", kind)
        self.ui.status(self.status_info())
        if self.on_closed:
            self.on_closed()

    # 受信
    def _reader(self) -> None:
        reason, kind = "", "error"
        try:
            if not self.framed:
                for line in self.peer.lines():
                    self.last_recv = time.monotonic()
                    self.ui.message(False, self.peer.name, line)
            else:
                for frame_kind, payload in self.peer.frames():
                    self.last_recv = time.monotonic()
                    if frame_kind == BYE:
                        reason, kind = f"{self.peer.name} が抜けました", "info"
                        break
                    self._dispatch(frame_kind, payload)
        except OSError as exc:  # ConnectionError（枠が壊れている）を含む
            if not (self.stopped.is_set() or self.closed.is_set()):
                reason = f"{self.peer.name} との接続が切れました: {exc}"
                _abort(self.peer.sock)
        self._end(reason, kind)

    def _dispatch(self, kind: bytes, payload: bytes) -> None:
        if kind == PING:
            return
        if kind == MSG:
            self.ui.message(False, self.peer.name, payload.decode("utf-8", errors="replace"))
        elif kind == OFFER:
            self._on_offer(payload)
        elif kind in (DATA, DONE):
            self._on_data(kind, payload)
        elif kind in (ACCEPT, REJECT, SAVED, FAILED):
            self._replies.put((kind, payload))
        # 知らない種類は読み捨てる（さらに新しいバージョンが足したもの）

    def _on_offer(self, payload: bytes) -> None:
        if self._offer is not None or self._incoming is not None:
            raise ConnectionError("前のファイルの途中で次の申し出が届きました")
        inc = Incoming(self.save_dir, self.max_size)
        try:
            name, size = inc.check(payload)
        except TransferError as exc:
            self.ui.notice(f"断りました: {exc}", "error")
            self.peer.send_frame(REJECT, str(exc).encode("utf-8"))
            return
        self.ui.notice(f"{self.peer.name} からファイルが届きました")
        self.ui.lines(["    " + r for r in chat_term.label_rows([("名前", chat_term.clean(name)),
                                                                   ("大きさ", human_size(size))])])
        if self.auto_accept:
            self._offer = inc
            self.answer_offer(True)
            return
        self._offer = inc
        self.ui.set_prompt("y 受け取る  n 断る [Enter で断る]")

    def answer_offer(self, accept: bool) -> None:
        with self._lock:
            inc, self._offer = self._offer, None
            if inc is None:
                return
            if accept:
                try:
                    inc.begin()
                except OSError as exc:
                    accept = False
                    self.ui.notice(f"保存先に書けません: {exc}", "error")
                else:
                    self._incoming = inc
                    self._incoming_progress = Progress(f"受信中 {inc.name}", inc.size,
                                                       show=self.ui.progress, unicode=self._unicode)
        self.ui.set_prompt(None)
        try:
            if accept:
                self.peer.send_frame(ACCEPT)
            else:
                self.ui.notice(f"断りました: {inc.name}")
                self.peer.send_frame(REJECT, "受け取りを断られました".encode("utf-8"))
        except OSError:
            pass

    def _on_data(self, kind: bytes, payload: bytes) -> None:
        inc, progress = self._incoming, self._incoming_progress
        if inc is None or progress is None:
            raise ConnectionError("申し出の無いデータが届きました")
        if kind == DATA:
            inc.write(payload)
            progress.add(len(payload))
            return
        progress.finish()
        with self._lock:
            self._incoming = None
        try:
            target = inc.finish(payload)
        except TransferError as exc:
            self.ui.notice(f"失敗しました: {inc.name}: {exc}", "error")
            self.peer.send_frame(FAILED, str(exc).encode("utf-8"))
            return
        except BaseException:
            inc.abort()
            raise
        self.saved.append(target)
        self.ui.notice(f"保存しました  {self._short(target)}  中身の照合 OK", "ok")
        self.peer.send_frame(SAVED, json.dumps({"name": target.name}).encode("utf-8"))

    def _short(self, target: Path) -> str:
        try:
            return str(target.relative_to(Path.cwd()))
        except ValueError:
            return str(target)

    # 合図
    def _ping(self) -> None:
        while not self.closed.wait(self.interval):
            try:
                self.peer.send_frame(PING)
            except OSError:
                return

    def _watch(self) -> None:
        tick = max(0.02, min(1.0, self.warn / 4, self.limit / 4))
        while not self.closed.wait(tick):
            if self.stopped.is_set():
                return
            if self.framed and time.monotonic() - self.last_recv >= self.limit:
                self._end(f"{self.peer.name} との接続が切れました（{self.limit:g} 秒返事がありません）")
                _abort(self.peer.sock)
                return
            self.ui.status(self.status_info())

    # 入力
    def handle_input(self, line: str) -> bool:
        """入力の 1 行を受ける。False を返したら終わる。"""
        text = line.rstrip("\r\n")
        if self._offer is not None:
            answer = text.strip().lower()
            if answer in ("y", "yes"):
                self.answer_offer(True)
                return True
            if answer in ("", "n", "no"):
                self.answer_offer(False)
                return True
        if text == QUIT_COMMAND:
            return False
        if not text:
            return True
        if text.startswith("/"):
            self._command(text)
            return True
        try:
            self.peer.send(text)
        except ChatError as exc:
            self.ui.notice(f"送れません: {exc}", "error")
            return True
        except OSError:
            return False
        self.ui.message(True, MY_LABEL, text)
        return True

    def _command(self, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        if cmd == "/help":
            self.ui.lines(["  " + r for r in chat_term.label_rows(HELP_LINES, gap=3)])
        elif cmd == "/open":
            self.save_dir.mkdir(parents=True, exist_ok=True)
            self.ui.notice(f"保存先を開きます: {self.save_dir}")
            self.opener(self.save_dir)
        elif cmd == "/send":
            self.send_path(arg)
        else:
            self.ui.notice(f"知らないコマンドです: {cmd}  /help で一覧", "error")

    def send_path(self, arg: str) -> Optional[threading.Thread]:
        """/send の中身。送る処理は別のスレッドで動かし、そのスレッドを返す。"""
        if not self.framed:
            self.ui.notice("相手が古いバージョンなので使えません。ファイルはメニューの「ファイルを送る」で送ってください", "error")
            return None
        raw = parse_send_path(arg)
        if not raw:
            self.ui.notice("使い方: /send ファイル", "error")
            return None
        path = Path(raw).expanduser()
        try:
            check_send_path(path)
        except TransferError as exc:
            self.ui.notice(f"送れません: {exc}", "error")
            return None
        with self._lock:
            if self._sending:
                self.ui.notice("前のファイルを送っています。終わってから送ってください", "error")
                return None
            self._sending = True
        t = threading.Thread(target=self._send_worker, args=(path,), daemon=True)
        t.start()
        return t

    def _next_reply(self) -> Optional[Tuple[bytes, bytes]]:
        return self._replies.get()

    def _send_worker(self, path: Path) -> None:
        try:
            saved_as = _send_one(
                self.peer.send_frame, self._next_reply, path,
                lambda name, size: self.ui.notice(
                    f"{name}（{human_size(size)}）を申し出ました。{self.peer.name} の応答待ち"),
                lambda name, size: Progress(f"送信中 {name}", size, show=self.ui.progress,
                                            unicode=self._unicode))
            self.ui.notice(f"送りました  {path.name} → 相手の {saved_as}", "ok")
        except TransferError as exc:
            self.ui.notice(f"送れません: {exc}", "error")
        except OSError as exc:
            if not self.closed.is_set():
                self.ui.notice(f"送れません: {exc}", "error")
        finally:
            with self._lock:
                self._sending = False


def run_chat(peer, stdin: TextIO, out: TextIO, on_closed: Callable[[], None], **session_args) -> None:
    """受信をスレッドで表示しながら、stdin の行を送る（端末でないときの形）。"""
    session = ChatSession(peer, chat_term.PlainView(out), on_closed=on_closed, **session_args)
    session.start()
    for raw in stdin:
        if not session.handle_input(raw):
            break
    session.quit()


# ---------------------------------------------------------------- Ctrl+C と接続の後始末


def call_interruptibly(fn: Callable[[], T], on_interrupt: Optional[Callable[[], None]] = None) -> T:
    """fn を別スレッドで動かし、主スレッドは短い間隔で待って Ctrl+C を受けられるようにする。

    Windows では、ソケットの accept や recv で止まっている主スレッドに Ctrl+C が届かない。
    Ctrl+C を受けたら on_interrupt（ソケットを閉じるなど）で fn を起こし、後始末を少し待ってから伝える。
    """
    box: dict = {}
    done = threading.Event()  # Thread.join は Ctrl+C で中断されると、次の join がすぐ戻ってしまう

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # 主スレッドで投げ直す
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    try:
        while not done.wait(0.2):
            pass
    except KeyboardInterrupt:
        if on_interrupt:
            on_interrupt()
        done.wait(2)  # 一時ファイルの削除などを待つ
        raise
    if "error" in box:
        raise box["error"]
    return box["value"]


def _abort(sock: socket.socket) -> None:
    """別スレッドが recv で待っている接続を切り、その recv を起こす。

    POSIX は shutdown で起きる。Windows は shutdown では起きないので、ハンドルも閉じる
    （makefile が参照を持っていると sock.close() ではハンドルが閉じないため、detach して閉じる）。
    """
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            fd = sock.detach()
        except OSError:
            return
        if fd != -1:
            try:
                socket.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------- 起動口（メニュー・待機・つなぐ）

# 最初のメニュー。(番号, 名前, 説明, 動き)
MENU = [
    ("1", "チャットを待つ", "IP と合言葉を出して相手を待つ", "host"),
    ("2", "チャットにつなぐ", "相手の IP と合言葉を入れる", "join"),
    ("3", "ファイルを受け取る", "待って、届いたら保存先へ", "receive"),
    ("4", "ファイルを送る", "相手の IP と合言葉を入れる", "send"),
    ("5", "ブラウザで使う", "このパソコンのブラウザでページを開く", "web"),
]
BACK = "back"  # つなぐ欄で Esc を押した


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        sys.exit(0)


def ask_menu(state: dict) -> str:
    """最初のメニューを出して、動き（host・join・receive・send）を返す。"""
    items = [(k, label, desc) for k, label, desc, _ in MENU]
    keys = [k for k, *_ in MENU]
    last = state.get("menu") if state.get("menu") in keys else None
    for line in chat_term.menu_lines(items):
        print(line, flush=True)
    while True:
        choice = chat_term.parse_menu_choice(_ask(chat_term.menu_prompt(last)), keys, last)
        if choice:
            state["menu"] = choice
            chat_term.save_state(STATE_PATH, state)
            return next(action for k, *_, action in MENU if k == choice)


def _host_code(args: argparse.Namespace) -> str:
    """待つ側の合言葉。--code があればそれ、無ければ 4 桁を聞く（空ならランダム）。"""
    if args.code:
        return args.code
    while True:
        text = _ask("合言葉 [Enter でランダム] > ").strip()
        if not text:
            return new_code()
        if len(text) == 4 and text.isascii() and text.isdigit():
            return text
        print("4桁の数字で入れてください", flush=True)


def _ready_banner(title: str, code: str, join_option: str,
                  extra: "Optional[List[Tuple[str, str]] | Callable[[], List[Tuple[str, str]]]]" = None,
                  out: Optional[TextIO] = None) -> Callable[[int], None]:
    """待機の枠を出す関数を返す。extra は枠に足す行（呼ぶときに決まるものは関数で渡す）。"""
    def ready(port: int) -> None:
        stream = out or sys.stdout
        ip = lan_ip() + ("" if port == DEFAULT_PORT else f":{port}")
        command = f"python3 chat.py {join_option} {ip} --code {code}"
        rows = extra() if callable(extra) else (extra or [])
        for line in chat_term.wait_banner(title, ip, code, command, chat_term.can_draw(stream), rows):
            print(line, file=stream, flush=True)
    return ready


def page_row(page: dict) -> List[Tuple[str, str]]:
    """待機の枠の「ブラウザで」の行。page は on_page が埋める（url か error）。"""
    if page.get("url"):
        return [("ブラウザで", page["url"])]
    if page.get("error"):
        return [("ブラウザで", f"出せません（{page['error']}）")]
    return []


def page_recorder(page: dict) -> Callable[[Optional[int], str], None]:
    """host_wait の on_page。相手のページの URL か、出せなかった理由を page に入れる。"""
    def on_page(port: Optional[int], error: str) -> None:
        if port:
            page["url"] = f"http://{lan_ip()}:{port}"
        else:
            page["error"] = error
    return on_page


def _print_reject(ip: str, reason: str) -> None:
    chat_term.PlainView(sys.stdout).notice(f"{ip}: {reason}", "error")


def _ask_address(state: dict) -> str:
    last = state.get("ip")
    if last:
        peer = state.get("peer_name")
        hint = f"{last}（{peer}）" if peer else last
        return _ask(f"相手の IP [Enter で {hint}] > ").strip() or last
    while True:
        text = _ask("相手の IP > ").strip()
        if text:
            return text


def _join_loop(args: argparse.Namespace, address: Optional[str], connect: Callable[[str, str], object],
               state: dict):
    """IP と合言葉を順に聞いてつなぐ（端末でないとき）。引数で指定したときは聞き直さない。"""
    view = chat_term.PlainView(sys.stdout)
    while True:
        address = address or _ask_address(state)
        code = args.code or _ask("合言葉 > ").strip()
        try:
            return address, connect(address, code)
        except ChatError as exc:
            view.notice(str(exc), "error")
            if args.join or args.send:
                return None
            if not isinstance(exc, Rejected):  # 断られただけなら IP は合っている
                address = None


def _join_form(title: str, args: argparse.Namespace, state: dict, name: str,
               connect: Callable[[str, str, str], object]):
    """つなぐ欄で IP・合言葉・名前を入れてつなぐ。Esc なら BACK。"""
    form = chat_term.JoinForm(title, ip=state.get("ip", ""), code=args.code or "", name=name)

    def attempt(values: Dict[str, str]):
        try:
            my_name = check_name(values["name"])
        except BadName as exc:
            raise chat_term.FormFailure("name", str(exc))
        try:
            return values["ip"], my_name, connect(values["ip"], values["code"], my_name)
        except Rejected as exc:
            raise chat_term.FormFailure("code", str(exc))
        except ChatError as exc:
            raise chat_term.FormFailure("ip", str(exc))

    with chat_term.KeyReader() as keys:
        result = chat_term.run_form(form, keys, sys.stdout, attempt)
    return BACK if result is None else result


def _remember_join(state: dict, address: str, my_name: str, peer_name: str) -> None:
    state.update(ip=address, name=my_name, peer_name=peer_name)
    chat_term.save_state(STATE_PATH, state)


def _connect(args: argparse.Namespace, state: dict, name: str, interactive: bool, word: str):
    """つなぐ側の入り口。(相手, 自分の名前) か None（失敗）か BACK を返す。"""
    given = args.join if word == "HELLO" else args.send
    if word == "HELLO":
        def connect(a: str, c: str, n: str):
            return join(a, c, n, default_port=args.port)
        title = "チャットにつなぐ"
    else:
        def connect(a: str, c: str, n: str):
            return join_send(a, c, n, default_port=args.port)
        title = "ファイルを送る相手につなぐ"
    if interactive and not given:
        result = _join_form(title, args, state, name, connect)
        if result == BACK:
            return BACK
        address, name, linked = result
    else:
        result = _join_loop(args, given, lambda a, c: connect(a, c, name), state)
        if result is None:
            return None
        address, linked = result
    peer_name = linked.name if word == "HELLO" else linked[1]
    _remember_join(state, address, name, peer_name)
    return linked, name


def run_receiver(args: argparse.Namespace, name: str) -> int:
    view = chat_term.PlainView(sys.stdout)
    save_dir = Path(args.save_dir).expanduser().resolve()
    code = _host_code(args)
    banner = _ready_banner("ファイルを待っています", code, "--send", [("保存先", str(save_dir))])
    sock, peer = call_interruptibly(
        lambda: host_receive(code, name, port=args.port, on_ready=banner, on_reject=_print_reject))
    view.notice(f"{peer} とつながりました。送られてくるのを待ちます")

    def ask(file_name: str, size: int) -> bool:
        view.notice(f"{peer} からファイルが届きました")
        view.lines(["    " + r for r in chat_term.label_rows([("名前", file_name), ("大きさ", human_size(size))])])
        if args.yes:
            return True
        answer = _ask("y 受け取る  n 断る [Enter で断る] > ")
        return answer.strip().lower() in ("y", "yes")

    rfile = sock.makefile("rb")
    try:
        saved = call_interruptibly(
            lambda: receive_files(sock, rfile, save_dir, sys.stdout, ask, max_size=args.max_size * 1024**2),
            on_interrupt=lambda: _abort(sock))
    except ConnectionError as exc:
        view.notice(f"中断しました: {exc}", "error")
        return 1
    finally:
        rfile.close()
        sock.close()
    view.notice(f"{len(saved)} 件を受け取りました")
    # 端末で使っているときだけ開く（テストやスクリプトからの実行では開かない）
    if saved and not args.no_open and sys.stdout.isatty():
        open_folder(save_dir)
    return 0


def run_sender(args: argparse.Namespace, name: str, state: dict, interactive: bool) -> object:
    view = chat_term.PlainView(sys.stdout)
    linked = _connect(args, state, name, interactive, "FILES")
    if linked is None or linked == BACK:
        return 1 if linked is None else BACK
    (sock, peer), _ = linked
    view.notice(f"{peer} とつながりました")
    rfile = sock.makefile("rb")
    queue_ = [Path(f) for f in args.files]
    ask_each = not queue_
    failures = 0

    def work() -> int:
        nonlocal failures
        while True:
            if queue_:
                path = queue_.pop(0)
            elif ask_each:
                text = _ask("送るファイル [Enter で終わる] > ").strip()
                if not text:
                    break
                path = Path(parse_send_path(text)).expanduser()  # ドラッグで入る引用符・\ を外す
            else:
                break
            try:
                saved_as = send_file(sock, rfile, path, sys.stdout)
                view.notice(f"送りました  {path.name} → 相手の {saved_as}", "ok")
            except TransferError as exc:
                failures += 1
                view.notice(f"送れません: {exc}", "error")
        send_frame(sock, BYE)
        return failures

    try:
        call_interruptibly(work, on_interrupt=lambda: _abort(sock))
    except ConnectionError as exc:
        view.notice(f"中断しました: {exc}", "error")
        return 1
    finally:
        rfile.close()
        sock.close()
    return 1 if failures else 0


def run_chat_terminal(peer, args: argparse.Namespace, interactive: bool) -> int:
    """つながった後のチャット。端末なら入力の行と状態の行を使い、そうでなければ行ごとに読む。"""
    save_dir = Path(args.save_dir).expanduser().resolve()
    common = dict(save_dir=save_dir, max_size=args.max_size * 1024**2, auto_accept=args.yes)
    if not interactive:
        def closed() -> None:  # 相手が切れたら、入力待ちの主スレッドごと終わらせる
            sys.stdout.flush()
            os._exit(0)
        run_chat(peer, sys.stdin, sys.stdout, on_closed=closed, **common)
        peer.close()
        return 0
    view = chat_term.TerminalView(sys.stdout)
    session = ChatSession(peer, view, **common)
    try:
        with chat_term.KeyReader() as keys:
            try:
                session.start()  # 中で view.start（状態の行・入力の行を描く）
                chat_term.input_loop(session.handle_input, keys, view, session.closed)
            finally:
                view.stop()
    finally:
        session.quit()
        peer.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="同じ Wi-Fi の2台で使う1対1チャットとファイル送受信")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--host", action="store_true", help="チャットを待つ")
    mode.add_argument("--join", metavar="IP[:PORT]", help="チャットにつなぐ")
    mode.add_argument("--receive", action="store_true", help="ファイルを受け取る側として待つ")
    mode.add_argument("--send", metavar="IP[:PORT]", help="ファイルを受け取る側につないで送る")
    mode.add_argument("--web", action="store_true", help="ブラウザの版（このパソコンのブラウザでページを開く）")
    parser.add_argument("files", nargs="*", help="--send で送るファイル（省略すると1件ずつ聞く）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"待ち受け・接続のポート（既定 {DEFAULT_PORT}）")
    parser.add_argument("--code", help="合言葉（待つ側は省略すると 4 桁を聞く。空ならランダム）")
    parser.add_argument("--name", default=None, help="自分の名前（既定は前回の名前か、ログイン名）")
    parser.add_argument("--save-dir", default=str(DEFAULT_SAVE_DIR), help="受け取ったファイルの保存先（既定はこのファイルの隣の received/）")
    parser.add_argument("--max-size", type=int, default=DEFAULT_MAX_FILE_SIZE // 1024**2, help="受け取る1件の上限（MB、既定 2048）")
    parser.add_argument("--yes", action="store_true", help="確認せずに受け取る")
    parser.add_argument("--no-open", action="store_true", help="受け取り後に保存先のフォルダを開かない")
    parser.add_argument("--no-page", action="store_true",
                        help="チャットを待つときに、相手のページ（ポート+1、既定 5051）を出さない")
    args = parser.parse_args(argv)
    if args.files and not args.send:
        parser.error("ファイルの指定は --send と一緒に使います")

    state = chat_term.load_state(STATE_PATH)
    try:
        name = check_name(args.name or state.get("name") or getpass.getuser())
    except ChatError as exc:
        print(exc, file=sys.stderr)
        return 2
    interactive = chat_term.interactive_ok()

    given = {"host": args.host, "join": bool(args.join), "receive": args.receive, "send": bool(args.send),
             "web": args.web}
    action = next((k for k, v in given.items() if v), None)
    from_menu = action is None
    while True:
        if from_menu:
            action = ask_menu(state)
        try:
            if action == "receive":
                return run_receiver(args, name)
            if action == "web":
                import chat_web  # noqa: WPS433

                return chat_web.run_local(args, name, state)
            if action == "send":
                result = run_sender(args, name, state, interactive)
                if result == BACK:
                    continue
                return int(result)  # type: ignore[arg-type]
            if action == "host":
                code = _host_code(args)
                page: dict = {}
                peer = call_interruptibly(lambda: host_wait(
                    code, name, port=args.port,
                    on_ready=_ready_banner("チャットを待っています", code, "--join", lambda: page_row(page)),
                    on_reject=_print_reject, page_port=None if args.no_page else args.port + 1,
                    on_page=page_recorder(page)))
            else:
                linked = _connect(args, state, name, interactive, "HELLO")
                if linked == BACK:
                    continue
                if linked is None:
                    return 1
                peer, name = linked
        except ChatError as exc:
            chat_term.PlainView(sys.stderr).notice(str(exc), "error")
            return 1
        return run_chat_terminal(peer, args, interactive)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
