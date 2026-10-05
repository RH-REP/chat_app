from __future__ import annotations

import argparse
import socket
import threading

MAX_MESSAGE_LENGTH = 4096

clients: dict[socket.socket, str] = {}
clients_lock = threading.Lock()


def send_line(sock: socket.socket, message: str) -> None:
    """改行をメッセージの区切りとして送信する。"""
    sock.sendall((message + "\n").encode("utf-8"))


def remove_client(sock: socket.socket, announce: bool = True) -> None:
    with clients_lock:
        name = clients.pop(sock, None)

    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass

    sock.close()

    if announce and name:
        broadcast(f"[system] {name} が退出しました")


def broadcast(message: str) -> None:
    """接続中の全クライアントへ送信する。"""
    with clients_lock:
        targets = list(clients.keys())

    disconnected: list[socket.socket] = []

    for sock in targets:
        try:
            send_line(sock, message)
        except OSError:
            disconnected.append(sock)

    for sock in disconnected:
        remove_client(sock, announce=False)


def handle_client(conn: socket.socket, address: tuple[str, int]) -> None:
    name: str | None = None
    reader = conn.makefile("r", encoding="utf-8", newline="\n")

    try:
        # 最初の1行をユーザー名として扱う
        name_line = reader.readline(64)

        if not name_line:
            return

        name = name_line.strip()

        if not 1 <= len(name) <= 30:
            send_line(conn, "[error] 名前は1〜30文字にしてください")
            return

        with clients_lock:
            duplicate = name in clients.values()

            if not duplicate:
                clients[conn] = name

        if duplicate:
            send_line(conn, "[error] その名前はすでに使われています")
            return

        print(f"接続: {name} ({address[0]}:{address[1]})")
        broadcast(f"[system] {name} が参加しました")

        while True:
            # TCPにはメッセージ境界がないため、改行まで読み取る
            line = reader.readline(MAX_MESSAGE_LENGTH + 2)

            if not line:
                break

            if len(line) > MAX_MESSAGE_LENGTH + 1 or not line.endswith("\n"):
                send_line(conn, "[error] メッセージが長すぎます")
                break

            message = line.rstrip("\r\n")

            if message == "/quit":
                break

            if not message:
                continue

            broadcast(f"{name}: {message}")

    except (ConnectionError, UnicodeError) as exc:
        print(f"通信エラー: {address}: {exc}")

    finally:
        reader.close()
        remove_client(conn)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen()

        print(f"チャットサーバー起動: {args.host}:{args.port}")

        while True:
            conn, address = server.accept()

            thread = threading.Thread(
                target=handle_client,
                args=(conn, address),
                daemon=True,
            )
            thread.start()


if __name__ == "__main__":
    main()