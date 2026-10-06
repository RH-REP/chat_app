"""同じ Wi-Fi の2台で使う、ターミナルだけの1対1チャットとファイル送信（標準ライブラリのみ）。

ホスト側は待ち受けて IP と合言葉を表示し、相手はそれを入力してつなぐ。
通信は平文の TCP。チャットは1行を1件として UTF-8 で送る。
ファイルは「種類1バイト＋長さ4バイト＋中身」の枠で、つなぐ側からホストへ片方向に送る。

  python3 chat.py                              # メニューから選ぶ
  python3 chat.py --host                       # チャットのホストになる
  python3 chat.py --join 192.168.11.53         # チャットのホストにつなぐ（合言葉は聞かれる）
  python3 chat.py --receive                    # ファイルを受け取る側として待つ
  python3 chat.py --send 192.168.11.53 a.pdf   # ファイルを送る
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import secrets
import shutil
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, TextIO, TypeVar

T = TypeVar("T")

DEFAULT_PORT = 5050          # 5000 は macOS の AirPlay 受信が使うので避ける
MAX_MESSAGE_LENGTH = 4096
MAX_NAME_LENGTH = 30
CONNECT_TIMEOUT = 3.0        # 打ち間違えた IP で長く待たない
HELLO_TIMEOUT = 10.0         # つないだまま名乗らない相手で待ち受けを塞がない
QUIT_COMMAND = "/quit"
WORDS = {"HELLO": "チャット", "FILES": "ファイル受信"}  # 名乗りの最初の語と、その用途


class ChatError(Exception):
    """利用者に見せてよい失敗（つながらない、合言葉ちがい など）。"""


class Rejected(ChatError):
    """ホストにはつながったが断られた（合言葉ちがい など）。IP は合っている。"""


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


def parse_address(text: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """'192.168.11.53' や '192.168.11.53:6000' を (host, port) にする。"""
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
        raise ChatError(f"名前は1〜{MAX_NAME_LENGTH}文字にしてください")
    return name


class Peer:
    """つながった相手1人。1行を1件として読み書きする。"""

    def __init__(self, sock: socket.socket, name: str):
        self.sock = sock
        self.name = name
        self._reader = sock.makefile("r", encoding="utf-8", errors="replace", newline="\n")
        self._lock = threading.Lock()
        self.closed_by_me = False

    def send(self, text: str) -> None:
        if "\n" in text or "\r" in text:
            raise ChatError("1件に改行は入れられません")
        if len(text) > MAX_MESSAGE_LENGTH:
            raise ChatError(f"長すぎます（{len(text)} 文字。上限 {MAX_MESSAGE_LENGTH}）")
        with self._lock:
            self.sock.sendall((text + "\n").encode("utf-8"))

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
        # 先に shutdown して、別スレッドの readline を起こす。
        # 読んでいる最中に reader を閉じると、バッファのロック待ちで止まる。
        self.closed_by_me = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._reader.close()
        finally:
            self.sock.close()


def _read_line(sock: socket.socket) -> str:
    """ハンドシェイク用に1行だけ読む（makefile を作る前なのでバイト単位で）。"""
    buf = bytearray()
    while len(buf) <= MAX_NAME_LENGTH * 4 + 64:
        b = sock.recv(1)
        if not b:
            break
        if b == b"\n":
            return buf.decode("utf-8", errors="replace").rstrip("\r")
        buf += b
    raise ChatError("名乗りの行が読めませんでした")


def _host_raw(
    code: str,
    name: str,
    word: str,
    port: int,
    bind: str,
    on_ready: Callable[[int], None] | None,
    on_reject: Callable[[str, str], None] | None,
) -> tuple[socket.socket, str]:
    """合言葉の合う相手が1人つないでくるまで待つ。合わない相手は断って待ち続ける。

    word は名乗りの最初の語。チャットは HELLO、ファイル送信は FILES。
    """
    try:
        srv = socket.create_server((bind, port))
    except OSError as exc:
        raise ChatError(f"ポート {port} で待ち受けられません（{exc.strerror}）。--port で変えてください") from exc
    with srv:
        if on_ready:
            on_ready(srv.getsockname()[1])
        while True:
            conn, addr = srv.accept()
            conn.settimeout(HELLO_TIMEOUT)
            try:
                their_word, _, rest = _read_line(conn).partition(" ")
                their_code, _, their_name = rest.partition(" ")
                if their_word not in WORDS:
                    raise ChatError("このアプリの接続ではありません")
                if their_word != word:
                    raise ChatError(f"このホストは{WORDS[word]}用です")
                if their_code != code:
                    raise ChatError("合言葉がちがいます")
                their_name = check_name(their_name)
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
            conn.sendall(f"OK {name}\n".encode("utf-8"))
            conn.settimeout(None)
            return conn, their_name


def _join_raw(address: str, code: str, name: str, word: str, default_port: int) -> tuple[socket.socket, str]:
    """ホストにつなぎ、合言葉と名前を名乗る。"""
    host, port = parse_address(address, default_port)
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except socket.gaierror as exc:
        raise ChatError(f"{host} が見つかりません") from exc
    except OSError as exc:
        raise ChatError(f"{host}:{port} につながりません（{exc.strerror or exc}）") from exc
    try:
        sock.sendall(f"{word} {code} {name}\n".encode("utf-8"))
        answer, _, rest = _read_line(sock).partition(" ")
    except (OSError, ChatError) as exc:
        sock.close()
        raise ChatError("相手が応答しませんでした（このアプリのホストではないかもしれません）") from exc
    if answer != "OK":
        sock.close()
        raise Rejected(f"断られました: {rest or answer}")
    sock.settimeout(None)
    return sock, rest.strip() or host


def host_wait(
    code: str,
    name: str,
    port: int = DEFAULT_PORT,
    bind: str = "0.0.0.0",
    on_ready: Callable[[int], None] | None = None,
    on_reject: Callable[[str, str], None] | None = None,
) -> Peer:
    """チャットの相手を待つ。"""
    return Peer(*_host_raw(code, name, "HELLO", port, bind, on_ready, on_reject))


def join(address: str, code: str, name: str, default_port: int = DEFAULT_PORT) -> Peer:
    """チャットのホストにつなぐ。"""
    return Peer(*_join_raw(address, code, name, "HELLO", default_port))


def run_chat(peer: Peer, stdin: TextIO, out: TextIO, on_closed: Callable[[], None]) -> None:
    """受信をスレッドで表示しながら、stdin の行を送る。"""

    def receive() -> None:
        for line in peer.lines():
            print(f"{peer.name}> {line}", file=out, flush=True)
        if peer.closed_by_me:  # 自分で /quit したときは知らせない
            return
        print(f"[切断] {peer.name} との接続が切れました", file=out, flush=True)
        on_closed()

    print(f"{peer.name} とつながりました。入力して Enter で送信、{QUIT_COMMAND} で終了", file=out, flush=True)
    threading.Thread(target=receive, daemon=True).start()
    for raw in stdin:
        text = raw.rstrip("\r\n")
        if text == QUIT_COMMAND:
            break
        if not text:
            continue
        try:
            peer.send(text)
        except ChatError as exc:
            print(f"[送れません] {exc}", file=out, flush=True)
        except OSError:
            break
    peer.finish()


# ---------------------------------------------------------------- ファイル送信
#
# 名乗り（FILES <合言葉> <名前>）のあとは、すべて「種類1バイト＋長さ4バイト（ビッグエンディアン）＋中身」。
#   送る側 → OFFER {"name","size"} → 受ける側 ACCEPT か REJECT <理由>
#   送る側 → DATA（64 KiB ずつ）… → DONE {"sha256"} → 受ける側 SAVED {"name"} か FAILED <理由>
#   送る側 → BYE（もう送らない）

CHUNK_SIZE = 64 * 1024
MAX_FRAME = CHUNK_SIZE + 1024
DEFAULT_MAX_FILE_SIZE = 2 * 1024**3  # 2 GiB
FREE_SPACE_MARGIN = 100 * 1024**2    # 受け取ったあとも 100 MiB は空けておく
DEFAULT_SAVE_DIR = Path(__file__).resolve().parent / "received"
PROGRESS_INTERVAL = 0.2              # 進み具合の表示を書き直す間隔（秒）

OFFER, ACCEPT, REJECT, DATA, DONE, SAVED, FAILED, BYE = (bytes([c]) for c in b"OARDEVFB")


class TransferError(ChatError):
    """ファイル1件の受け渡しが成り立たなかった（断られた、照合が合わない など）。"""


def send_frame(sock: socket.socket, kind: bytes, payload: bytes = b"") -> None:
    sock.sendall(struct.pack("!cI", kind, len(payload)) + payload)


def recv_frame(rfile: BinaryIO) -> tuple[bytes, bytes] | None:
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


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


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
    """1行を書き直して進み具合を出す。端末でなければ最後の1行だけ出す。"""

    def __init__(self, label: str, total: int, out: TextIO):
        self.label, self.total, self.out = label, total, out
        self.done = 0
        self.start = self.last = time.monotonic()
        self.live = getattr(out, "isatty", lambda: False)()

    def add(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if self.live and now - self.last >= PROGRESS_INTERVAL:
            self.last = now
            self.out.write("\r" + self._line())
            self.out.flush()

    def _line(self) -> str:
        pct = 100 if self.total == 0 else self.done * 100 // self.total
        bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
        rate = self.done / max(time.monotonic() - self.start, 1e-6)
        return f"{self.label} [{bar}] {pct:3d}% {human_size(self.done)}/{human_size(self.total)} {human_size(rate)}/s"

    def finish(self) -> None:
        if self.live:
            self.out.write("\r" + self._line() + "\n")
            self.out.flush()


def send_file(sock: socket.socket, rfile: BinaryIO, path: Path, out: TextIO) -> str:
    """ファイルを1件送り、相手が保存した名前を返す。"""
    if not path.is_file():
        raise TransferError(f"ファイルではありません（フォルダは zip にして送ってください）: {path}" if path.is_dir()
                            else f"見つかりません: {path}")
    size = path.stat().st_size
    send_frame(sock, OFFER, json.dumps({"name": path.name, "size": size}).encode("utf-8"))
    print(f"[申し出] {path.name} ({human_size(size)}) 相手の応答待ち…", file=out, flush=True)
    frame = recv_frame(rfile)
    if frame is None:
        raise ConnectionError("相手が切断しました")
    kind, payload = frame
    if kind == REJECT:
        raise TransferError(f"断られました: {payload.decode('utf-8', 'replace')}")
    if kind != ACCEPT:
        raise ConnectionError("相手の応答が想定外です")

    digest = hashlib.sha256()
    progress = Progress(f"[送信中] {path.name}", size, out)
    sent = 0
    with path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            send_frame(sock, DATA, chunk)
            digest.update(chunk)
            sent += len(chunk)
            progress.add(len(chunk))
    progress.finish()
    if sent != size:
        raise ConnectionError(f"送っている間にファイルの大きさが変わりました（{size} → {sent}）")
    send_frame(sock, DONE, json.dumps({"sha256": digest.hexdigest()}).encode("utf-8"))

    frame = recv_frame(rfile)
    if frame is None:
        raise ConnectionError("相手が切断しました")
    kind, payload = frame
    if kind == FAILED:
        raise TransferError(f"相手が保存できませんでした: {payload.decode('utf-8', 'replace')}")
    if kind != SAVED:
        raise ConnectionError("相手の応答が想定外です")
    return str(_json(payload).get("name", path.name))


def receive_files(
    sock: socket.socket,
    rfile: BinaryIO,
    save_dir: Path,
    out: TextIO,
    ask: Callable[[str, int], bool],
    max_size: int = DEFAULT_MAX_FILE_SIZE,
) -> list[Path]:
    """相手が BYE を送るか切断するまで受け取り続け、保存したファイルの一覧を返す。

    ask(名前, 大きさ) が True を返したものだけ受け取る。
    途中で切れたものと照合の合わないものは、一時ファイルごと消す。
    """
    saved: list[Path] = []
    save_dir.mkdir(parents=True, exist_ok=True)
    while True:
        frame = recv_frame(rfile)
        if frame is None or frame[0] == BYE:
            return saved
        kind, payload = frame
        if kind != OFFER:
            raise ConnectionError("相手の送ってきた内容が想定外です")
        offer = _json(payload)
        size = offer.get("size")
        try:
            name = safe_file_name(offer.get("name"))
            if not isinstance(size, int) or size < 0:
                raise TransferError("大きさが読めません")
            if size > max_size:
                raise TransferError(f"大きすぎます（{human_size(size)}。上限 {human_size(max_size)}）")
            free = shutil.disk_usage(save_dir).free
            if size + FREE_SPACE_MARGIN > free:
                raise TransferError(f"空き容量が足りません（空き {human_size(free)}）")
        except TransferError as exc:
            print(f"[断りました] {exc}", file=out, flush=True)
            send_frame(sock, REJECT, str(exc).encode("utf-8"))
            continue
        if not ask(name, size):
            print(f"[断りました] {name}", file=out, flush=True)
            send_frame(sock, REJECT, "受け取りを断られました".encode("utf-8"))
            continue
        send_frame(sock, ACCEPT)

        part = save_dir / f".{name}.{secrets.token_hex(4)}.part"
        digest = hashlib.sha256()
        progress = Progress(f"[受信中] {name}", size, out)
        received = 0
        try:
            with part.open("xb") as f:
                while True:
                    frame = recv_frame(rfile)
                    if frame is None:
                        raise ConnectionError("受信の途中で相手が切断しました")
                    kind, payload = frame
                    if kind == DATA:
                        received += len(payload)
                        if received > size:
                            raise ConnectionError("申し出より大きなデータが届きました")
                        f.write(payload)
                        digest.update(payload)
                        progress.add(len(payload))
                    elif kind == DONE:
                        break
                    else:
                        raise ConnectionError("相手の送ってきた内容が想定外です")
            progress.finish()
            if received != size:
                reason = f"大きさが合いません（申し出 {size}、受信 {received}）"
            elif _json(payload).get("sha256") != digest.hexdigest():
                reason = "SHA-256 が合いません（途中で壊れました）"
            else:
                reason = ""
            if reason:
                part.unlink()
                print(f"[失敗] {name}: {reason}", file=out, flush=True)
                send_frame(sock, FAILED, reason.encode("utf-8"))
                continue
            target = unique_path(save_dir, name)
            os.replace(part, target)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        saved.append(target)
        print(f"[保存] {target}（SHA-256 一致）", file=out, flush=True)
        send_frame(sock, SAVED, json.dumps({"name": target.name}).encode("utf-8"))


def host_receive(
    code: str,
    name: str,
    port: int = DEFAULT_PORT,
    bind: str = "0.0.0.0",
    on_ready: Callable[[int], None] | None = None,
    on_reject: Callable[[str, str], None] | None = None,
) -> tuple[socket.socket, str]:
    """ファイルを送ってくる相手を待つ。"""
    return _host_raw(code, name, "FILES", port, bind, on_ready, on_reject)


def join_send(address: str, code: str, name: str, default_port: int = DEFAULT_PORT) -> tuple[socket.socket, str]:
    """ファイルの受け取り側につなぐ。"""
    return _join_raw(address, code, name, "FILES", default_port)


def call_interruptibly(fn: Callable[[], T], on_interrupt: Callable[[], None] | None = None) -> T:
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


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        sys.exit(0)


def _ask_choice(prompt: str) -> str:
    while True:
        choice = _ask(prompt).strip()
        if choice in ("1", "2"):
            return choice


def _ready_banner(title: str, code: str, extra: list[str] | None = None) -> Callable[[int], None]:
    def ready(port: int) -> None:
        print(title, flush=True)
        print(f"  IP:     {lan_ip()}" + ("" if port == DEFAULT_PORT else f":{port}"), flush=True)
        print(f"  合言葉: {code}", flush=True)
        for line in extra or []:
            print(line, flush=True)
        print("相手に IP と合言葉を伝えてください（止める: Ctrl+C）", flush=True)
    return ready


def _print_reject(ip: str, reason: str) -> None:
    print(f"[断りました] {ip}: {reason}", flush=True)


def _join_loop(args: argparse.Namespace, address: str | None, connect: Callable[[str, str], object]):
    """IP と合言葉を聞いてつなぐ。引数で指定したときは聞き直さない。"""
    while True:
        address = address or _ask("ホストの IP > ")
        code = args.code or _ask("合言葉 > ").strip()
        try:
            return connect(address, code)
        except ChatError as exc:
            print(f"[失敗] {exc}", flush=True)
            if args.join or args.send:
                return None
            if not isinstance(exc, Rejected):  # 断られただけなら IP は合っている
                address = None


def run_receiver(args: argparse.Namespace, name: str) -> int:
    save_dir = Path(args.save_dir).expanduser().resolve()
    code = args.code or new_code()
    banner = _ready_banner("ファイルの受け取り待ち", code, [f"  保存先: {save_dir}"])
    sock, peer = call_interruptibly(
        lambda: host_receive(code, name, port=args.port, on_ready=banner, on_reject=_print_reject))
    print(f"{peer} とつながりました。送られてくるのを待ちます", flush=True)

    def ask(file_name: str, size: int) -> bool:
        if args.yes:
            return True
        answer = _ask(f"{peer} が {file_name} ({human_size(size)}) を送ろうとしています。受け取りますか [y/N] > ")
        return answer.strip().lower() in ("y", "yes")

    rfile = sock.makefile("rb")
    try:
        saved = call_interruptibly(
            lambda: receive_files(sock, rfile, save_dir, sys.stdout, ask, max_size=args.max_size * 1024**2),
            on_interrupt=lambda: _shutdown(sock))
    except ConnectionError as exc:
        print(f"[中断] {exc}", flush=True)
        return 1
    finally:
        rfile.close()
        sock.close()
    print(f"[終了] {len(saved)} 件を受け取りました", flush=True)
    return 0


def run_sender(args: argparse.Namespace, name: str) -> int:
    linked = _join_loop(args, args.send, lambda a, c: join_send(a, c, name, default_port=args.port))
    if linked is None:
        return 1
    sock, peer = linked
    print(f"{peer} とつながりました", flush=True)
    rfile = sock.makefile("rb")
    queue = [Path(f) for f in args.files]
    interactive = not queue
    failures = 0

    def work() -> int:
        nonlocal failures
        while True:
            if queue:
                path = queue.pop(0)
            elif interactive:
                text = _ask("送るファイル（空で終了） > ").strip()
                if not text:
                    break
                path = Path(text.strip("'\"")).expanduser()  # Finder やエクスプローラーからドラッグした引用符を外す
            else:
                break
            try:
                saved_as = send_file(sock, rfile, path, sys.stdout)
                print(f"[完了] {path.name} → 相手の {saved_as}", flush=True)
            except TransferError as exc:
                failures += 1
                print(f"[送れません] {exc}", flush=True)
        send_frame(sock, BYE)
        return failures

    try:
        call_interruptibly(work, on_interrupt=lambda: _shutdown(sock))
    except ConnectionError as exc:
        print(f"[中断] {exc}", flush=True)
        return 1
    finally:
        rfile.close()
        sock.close()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同じ Wi-Fi の2台で使う1対1チャットとファイル送信")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--host", action="store_true", help="チャットのホストになって待つ")
    mode.add_argument("--join", metavar="IP[:PORT]", help="チャットのホストにつなぐ")
    mode.add_argument("--receive", action="store_true", help="ファイルを受け取る側として待つ")
    mode.add_argument("--send", metavar="IP[:PORT]", help="ファイルの受け取り側につないで送る")
    parser.add_argument("files", nargs="*", help="--send で送るファイル（省略すると1件ずつ聞く）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"待ち受け・接続のポート（既定 {DEFAULT_PORT}）")
    parser.add_argument("--code", help="合言葉（ホストは省略すると毎回作る）")
    parser.add_argument("--name", default=None, help="自分の名前（既定はログイン名）")
    parser.add_argument("--save-dir", default=str(DEFAULT_SAVE_DIR), help="受け取ったファイルの保存先（既定はこのファイルの隣の received/）")
    parser.add_argument("--max-size", type=int, default=DEFAULT_MAX_FILE_SIZE // 1024**2, help="受け取る1件の上限（MB、既定 2048）")
    parser.add_argument("--yes", action="store_true", help="確認せずに受け取る")
    args = parser.parse_args(argv)
    if args.files and not args.send:
        parser.error("ファイルの指定は --send と一緒に使います")

    try:
        name = check_name(args.name or getpass.getuser())
    except ChatError as exc:
        print(exc, file=sys.stderr)
        return 2

    if not (args.host or args.join or args.receive or args.send):
        if _ask_choice("1) チャット  2) ファイル送受信 > ") == "1":
            args.host = _ask_choice("1) ホストになる  2) つなぐ > ") == "1"
            is_chat = True
        else:
            args.receive = _ask_choice("1) 受信（待機）  2) 送信 > ") == "1"
            is_chat = False
    else:
        is_chat = bool(args.host or args.join)

    try:
        if not is_chat:
            return run_receiver(args, name) if args.receive else run_sender(args, name)
        if args.host:
            code = args.code or new_code()
            peer = call_interruptibly(lambda: host_wait(
                code, name, port=args.port, on_ready=_ready_banner("ホストとして待機中", code), on_reject=_print_reject))
        else:
            peer = _join_loop(args, args.join, lambda a, c: join(a, c, name, default_port=args.port))
            if peer is None:
                return 1
    except ChatError as exc:
        print(exc, file=sys.stderr)
        return 1

    # 相手が切れたら、入力待ちの主スレッドごと終わらせる（sys.exit はスレッドしか終わらない）
    run_chat(peer, sys.stdin, sys.stdout, on_closed=lambda: os._exit(0))
    peer.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
