"""同じ Wi-Fi の2台で使う、ターミナルだけの1対1チャット（標準ライブラリのみ）。

ホスト側は待ち受けて IP と合言葉を表示し、相手はそれを入力してつなぐ。
通信は平文の TCP。1行を1件として UTF-8 で送る。

  python3 chat.py                      # メニューから選ぶ
  python3 chat.py --host               # ホストになる
  python3 chat.py --join 192.168.11.53 # つなぐ（合言葉は聞かれる）
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import socket
import sys
import threading
from typing import Callable, Iterator, TextIO

DEFAULT_PORT = 5050          # 5000 は macOS の AirPlay 受信が使うので避ける
MAX_MESSAGE_LENGTH = 4096
MAX_NAME_LENGTH = 30
CONNECT_TIMEOUT = 3.0        # 打ち間違えた IP で長く待たない
HELLO_TIMEOUT = 10.0         # つないだまま名乗らない相手で待ち受けを塞がない
QUIT_COMMAND = "/quit"


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


def host_wait(
    code: str,
    name: str,
    port: int = DEFAULT_PORT,
    bind: str = "0.0.0.0",
    on_ready: Callable[[int], None] | None = None,
    on_reject: Callable[[str, str], None] | None = None,
) -> Peer:
    """合言葉の合う相手が1人つないでくるまで待つ。合わない相手は断って待ち続ける。"""
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
                word, _, rest = _read_line(conn).partition(" ")
                their_code, _, their_name = rest.partition(" ")
                if word != "HELLO":
                    raise ChatError("このアプリの接続ではありません")
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
            return Peer(conn, their_name)


def join(address: str, code: str, name: str, default_port: int = DEFAULT_PORT) -> Peer:
    """ホストにつなぎ、合言葉と名前を名乗る。"""
    host, port = parse_address(address, default_port)
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except socket.gaierror as exc:
        raise ChatError(f"{host} が見つかりません") from exc
    except OSError as exc:
        raise ChatError(f"{host}:{port} につながりません（{exc.strerror or exc}）") from exc
    try:
        sock.sendall(f"HELLO {code} {name}\n".encode("utf-8"))
        word, _, rest = _read_line(sock).partition(" ")
    except (OSError, ChatError) as exc:
        sock.close()
        raise ChatError("相手が応答しませんでした（このアプリのホストではないかもしれません）") from exc
    if word != "OK":
        sock.close()
        raise Rejected(f"断られました: {rest or word}")
    sock.settimeout(None)
    return Peer(sock, rest.strip() or host)


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


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        sys.exit(0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同じ Wi-Fi の2台で使う1対1チャット")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--host", action="store_true", help="ホストになって待つ")
    mode.add_argument("--join", metavar="IP[:PORT]", help="ホストにつなぐ")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"待ち受け・接続のポート（既定 {DEFAULT_PORT}）")
    parser.add_argument("--code", help="合言葉（ホストは省略すると毎回作る）")
    parser.add_argument("--name", default=None, help="自分の名前（既定はログイン名）")
    args = parser.parse_args(argv)

    try:
        name = check_name(args.name or getpass.getuser())
    except ChatError as exc:
        print(exc, file=sys.stderr)
        return 2

    is_host = args.host
    if not args.host and not args.join:
        while True:
            choice = _ask("1) ホストになる  2) つなぐ > ").strip()
            if choice in ("1", "2"):
                is_host = choice == "1"
                break

    try:
        if is_host:
            code = args.code or new_code()

            def ready(port: int) -> None:
                print("ホストとして待機中", flush=True)
                print(f"  IP:     {lan_ip()}" + ("" if port == DEFAULT_PORT else f":{port}"), flush=True)
                print(f"  合言葉: {code}", flush=True)
                print("相手に IP と合言葉を伝えてください（止める: Ctrl+C）", flush=True)

            def reject(ip: str, reason: str) -> None:
                print(f"[断りました] {ip}: {reason}", flush=True)

            peer = host_wait(code, name, port=args.port, on_ready=ready, on_reject=reject)
        else:
            address = args.join
            while True:
                address = address or _ask("ホストの IP > ")
                code = args.code or _ask("合言葉 > ").strip()
                try:
                    peer = join(address, code, name, default_port=args.port)
                    break
                except ChatError as exc:
                    print(f"[失敗] {exc}", flush=True)
                    if args.join:  # 引数で指定したときは聞き直さない
                        return 1
                    if not isinstance(exc, Rejected):  # 断られただけなら IP は合っている
                        address = None
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
