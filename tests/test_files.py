"""ファイル送信のテスト。1台の中で、受け取り側をスレッド・別プロセスで動かす。

  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import _thread
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chat  # noqa: E402

# Windows の既定（cp1252 など）でも、子プロセスの入出力を UTF-8 にする
ENV = {**os.environ, "PYTHONUTF8": "1"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Session:
    """受け取り側をスレッドで待たせ、送り側をつないだ状態を作る。"""

    def __init__(self, save_dir: Path, ask=lambda n, s: True, max_size=chat.DEFAULT_MAX_FILE_SIZE):
        self.out = io.StringIO()
        self.box: dict = {}
        ready = threading.Event()

        def on_ready(port: int) -> None:
            self.box["port"] = port
            ready.set()

        def run() -> None:
            try:
                sock, _ = chat.host_receive("1234", "受け手", port=0, bind="127.0.0.1", on_ready=on_ready)
                rfile = sock.makefile("rb")
                try:
                    self.box["saved"] = chat.receive_files(sock, rfile, save_dir, self.out, ask, max_size)
                finally:
                    rfile.close(); sock.close()
            except Exception as exc:
                self.box["error"] = exc
                ready.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert ready.wait(3)
        self.sock, self.peer = chat.join_send(f"127.0.0.1:{self.box['port']}", "1234", "送り手")
        self.rfile = self.sock.makefile("rb")
        self.sent_out = io.StringIO()

    def send(self, path: Path) -> str:
        return chat.send_file(self.sock, self.rfile, path, self.sent_out)

    def bye(self) -> dict:
        chat.send_frame(self.sock, chat.BYE)
        self.thread.join(5)
        self.rfile.close(); self.sock.close()
        return self.box


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FrameTest(unittest.TestCase):
    def test_round_trip_and_clean_end(self):
        a, b = socket.socketpair()
        with a, b, b.makefile("rb") as rb:
            chat.send_frame(a, chat.DATA, b"\x00\n\xff")
            chat.send_frame(a, chat.BYE)
            a.shutdown(socket.SHUT_WR)
            self.assertEqual(chat.recv_frame(rb), (chat.DATA, b"\x00\n\xff"))
            self.assertEqual(chat.recv_frame(rb), (chat.BYE, b""))
            self.assertIsNone(chat.recv_frame(rb))

    def test_truncated_and_oversized_frames(self):
        for raw in (b"D\x00\x00\x00\x10abc", b"D\x00", b"D" + (chat.MAX_FRAME + 1).to_bytes(4, "big")):
            with self.subTest(raw=raw[:6]):
                with self.assertRaises(ConnectionError):
                    chat.recv_frame(io.BytesIO(raw))


class NameTest(unittest.TestCase):
    def test_bad_names_are_refused(self):
        for bad in ("", ".", "..", "../evil", "a/b", "a\\b", ".ssh", "x\0y", "a\nb", "あ" * 80, None, 3,
                    # Windows で使えないもの（: は NTFS の代替データストリームになる）
                    "a:b", "C:x.txt", "a*b", "a?b", 'a"b', "a<b", "a|b",
                    "CON", "con.txt", "nul", "COM1.log", "LPT9.tar.gz", "aux .txt", "name.", "name "):
            with self.subTest(bad=bad):
                with self.assertRaises(chat.TransferError):
                    chat.safe_file_name(bad)

    def test_good_names_pass(self):
        for good in ("report.pdf", "写真 2026-10-06.jpg", "a b (1).tar.gz", "console.txt", "COM10.txt", "nul_report.pdf"):
            self.assertEqual(chat.safe_file_name(good), good)

    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.txt").write_text("x", encoding="utf-8")
            (d / "a (1).txt").write_text("x", encoding="utf-8")
            self.assertEqual(chat.unique_path(d, "a.txt").name, "a (2).txt")
            self.assertEqual(chat.unique_path(d, "b.txt").name, "b.txt")


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.src, self.dst = base / "src", base / "dst"
        self.src.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.dst.iterdir() if p.name.endswith(".part"))

    def test_files_arrive_intact(self):
        files = {
            "日本語の名前.txt": "こんにちは\n".encode("utf-8"),
            "empty.bin": b"",
            "random.bin": os.urandom(5 * 1024 * 1024 + 123),  # 64 KiB の区切りに合わない大きさ
        }
        for n, data in files.items():
            (self.src / n).write_bytes(data)
        s = Session(self.dst)
        for n in files:
            self.assertEqual(s.send(self.src / n), n)
        box = s.bye()
        self.assertNotIn("error", box)
        self.assertEqual(len(box["saved"]), 3)
        for n in files:
            self.assertEqual(sha(self.dst / n), sha(self.src / n))
        self.assertEqual(self.leftovers(), [])

    def test_same_name_is_renamed(self):
        (self.src / "a.txt").write_text("新しい", encoding="utf-8")
        self.dst.mkdir()
        (self.dst / "a.txt").write_text("前からある", encoding="utf-8")
        s = Session(self.dst)
        self.assertEqual(s.send(self.src / "a.txt"), "a (1).txt")
        s.bye()
        self.assertEqual((self.dst / "a.txt").read_text(encoding="utf-8"), "前からある")
        self.assertEqual((self.dst / "a (1).txt").read_text(encoding="utf-8"), "新しい")

    def test_declined_file_then_next_file_in_same_session(self):
        (self.src / "no.txt").write_text("x", encoding="utf-8")
        (self.src / "yes.txt").write_text("y", encoding="utf-8")
        s = Session(self.dst, ask=lambda name, size: name == "yes.txt")
        with self.assertRaisesRegex(chat.TransferError, "断られました"):
            s.send(self.src / "no.txt")
        self.assertEqual(s.send(self.src / "yes.txt"), "yes.txt")
        s.bye()
        self.assertEqual(sorted(p.name for p in self.dst.iterdir()), ["yes.txt"])

    def test_too_large_is_refused_before_sending(self):
        (self.src / "big.bin").write_bytes(b"x" * 2000)
        s = Session(self.dst, max_size=1000)
        with self.assertRaisesRegex(chat.TransferError, "大きすぎます"):
            s.send(self.src / "big.bin")
        s.bye()
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_folder_and_missing_file_are_refused_locally(self):
        s = Session(self.dst)
        with self.assertRaisesRegex(chat.TransferError, "フォルダ"):
            s.send(self.src)
        with self.assertRaisesRegex(chat.TransferError, "見つかりません"):
            s.send(self.src / "none.txt")
        s.bye()

    def test_wrong_hash_is_discarded(self):
        s = Session(self.dst)
        chat.send_frame(s.sock, chat.OFFER, json.dumps({"name": "x.bin", "size": 3}).encode())
        self.assertEqual(chat.recv_frame(s.rfile)[0], chat.ACCEPT)
        chat.send_frame(s.sock, chat.DATA, b"abc")
        chat.send_frame(s.sock, chat.DONE, json.dumps({"sha256": "0" * 64}).encode())
        kind, reason = chat.recv_frame(s.rfile)
        self.assertEqual(kind, chat.FAILED)
        self.assertIn("SHA-256", reason.decode())
        s.bye()
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_disconnect_midway_leaves_nothing(self):
        s = Session(self.dst)
        chat.send_frame(s.sock, chat.OFFER, json.dumps({"name": "half.bin", "size": 1000}).encode())
        self.assertEqual(chat.recv_frame(s.rfile)[0], chat.ACCEPT)
        chat.send_frame(s.sock, chat.DATA, b"a" * 500)
        s.rfile.close(); s.sock.close()
        s.thread.join(5)
        self.assertIsInstance(s.box.get("error"), ConnectionError)
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_evil_name_from_modified_sender_is_refused(self):
        s = Session(self.dst)
        chat.send_frame(s.sock, chat.OFFER, json.dumps({"name": "../escape.txt", "size": 1}).encode())
        kind, reason = chat.recv_frame(s.rfile)
        self.assertEqual(kind, chat.REJECT)
        s.bye()
        self.assertFalse((self.dst.parent / "escape.txt").exists())

    def test_more_data_than_offered_is_cut(self):
        s = Session(self.dst)
        chat.send_frame(s.sock, chat.OFFER, json.dumps({"name": "x.bin", "size": 2}).encode())
        self.assertEqual(chat.recv_frame(s.rfile)[0], chat.ACCEPT)
        chat.send_frame(s.sock, chat.DATA, b"abc")
        s.thread.join(5)
        self.assertIsInstance(s.box.get("error"), ConnectionError)
        self.assertEqual(self.leftovers(), [])
        s.rfile.close(); s.sock.close()

    def test_chat_client_cannot_join_file_host(self):
        ready = threading.Event()
        box: dict = {}

        def run():
            try:
                chat.host_receive("1234", "受け手", port=0, bind="127.0.0.1",
                                  on_ready=lambda p: (box.update(port=p), ready.set()))
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()
        ready.wait(3)
        with self.assertRaisesRegex(chat.Rejected, "ファイル受信用"):
            chat.join(f"127.0.0.1:{box['port']}", "1234", "チャットの人")


class InterruptTest(unittest.TestCase):
    """Ctrl+C（主スレッドへの KeyboardInterrupt）が、ソケットで待っている間にも届くこと。

    Windows では、主スレッドが accept / recv で止まっていると Ctrl+C が届かない。
    _thread.interrupt_main は、どの OS でも主スレッドに KeyboardInterrupt を起こす。
    """

    def interrupt_soon(self, delay: float = 0.3) -> None:
        t = threading.Timer(delay, _thread.interrupt_main)
        t.daemon = True
        t.start()

    def test_value_and_error_pass_through(self):
        self.assertEqual(chat.call_interruptibly(lambda: 42), 42)
        with self.assertRaises(ZeroDivisionError):
            chat.call_interruptibly(lambda: 1 / 0)

    def test_ctrl_c_while_waiting_for_peer(self):
        self.interrupt_soon()
        t0 = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            chat.call_interruptibly(lambda: chat.host_receive("1234", "受け手", port=0, bind="127.0.0.1"))
        self.assertLess(time.monotonic() - t0, 3)

    def test_ctrl_c_during_receive_leaves_no_part_file(self):
        with tempfile.TemporaryDirectory() as d:
            dst = Path(d)
            a, b = socket.socketpair()
            rb = b.makefile("rb")
            chat.send_frame(a, chat.OFFER, json.dumps({"name": "half.bin", "size": 1000}).encode())
            chat.send_frame(a, chat.DATA, b"x" * 300)  # 残りは送らずに止める
            self.interrupt_soon()
            with self.assertRaises(KeyboardInterrupt):
                chat.call_interruptibly(
                    lambda: chat.receive_files(b, rb, dst, io.StringIO(), lambda n, s: True),
                    on_interrupt=lambda: chat._abort(b))
            self.assertEqual(list(dst.iterdir()), [])
            rb.close(); a.close(); b.close()

    @unittest.skipIf(sys.platform == "win32", "Windows では子プロセスに Ctrl+C を送りにくい")
    def test_sigint_stops_waiting_receiver(self):
        import signal
        port = free_port()
        p = subprocess.Popen([sys.executable, str(ROOT / "chat.py"), "--receive", "--port", str(port), "--code", "1"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=ENV)
        for _ in range(5):
            p.stdout.readline()
        p.send_signal(signal.SIGINT)
        out, err = p.communicate(timeout=5)
        self.assertEqual(p.returncode, 130)
        self.assertNotIn("Traceback", err)


class CommandLineTest(unittest.TestCase):
    def test_receive_and_send_processes(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "送る.bin"
            src.write_bytes(os.urandom(300_000))
            port = free_port()
            base = [sys.executable, str(ROOT / "chat.py"), "--port", str(port), "--code", "7777"]
            recv = subprocess.Popen(base + ["--receive", "--yes", "--save-dir", str(d / "in")],
                                    stdout=subprocess.PIPE, text=True, encoding="utf-8", env=ENV)
            banner = [recv.stdout.readline() for _ in range(5)]
            self.assertIn("ファイルの受け取り待ち", banner[0])
            self.assertIn(str((d / "in").resolve()), banner[3])  # Windows の短い名前（RUNNER~1）を長い名前に
            send = subprocess.run(base + ["--send", "127.0.0.1", str(src)],
                                  capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=20)
            out, _ = recv.communicate(timeout=20)
            self.assertEqual(send.returncode, 0, send.stdout + send.stderr)
            self.assertIn("[完了] 送る.bin", send.stdout)
            self.assertIn("[終了] 1 件", out)
            self.assertEqual(sha(d / "in" / "送る.bin"), sha(src))

    def test_menu_flow_with_confirmation(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.txt").write_text("A", encoding="utf-8")
            (d / "b.txt").write_text("B", encoding="utf-8")
            port = free_port()
            base = [sys.executable, str(ROOT / "chat.py"), "--port", str(port), "--code", "8888"]
            # 受け手: 2) ファイル → 1) 受信。a は y、b は n
            recv = subprocess.Popen(base + ["--save-dir", str(d / "in")], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, text=True, encoding="utf-8", env=ENV)
            recv.stdin.write("2\n1\ny\nn\n"); recv.stdin.flush()
            while "相手に IP" not in recv.stdout.readline():
                pass
            # 送り手: 2) ファイル → 2) 送信 → IP → ファイルを1件ずつ（Finder の引用符付き）→ 空行で終了
            send = subprocess.run(base, input=f"2\n2\n127.0.0.1\n'{d / 'a.txt'}'\n{d / 'b.txt'}\n\n",
                                  capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=20)
            out, _ = recv.communicate(timeout=20)
            self.assertEqual(send.returncode, 1)  # 1件断られたので 1
            self.assertIn("[完了] a.txt", send.stdout)
            self.assertIn("[送れません] 断られました", send.stdout)
            self.assertEqual(sorted(p.name for p in (d / "in").iterdir()), ["a.txt"])
            self.assertIn("[終了] 1 件", out)


if __name__ == "__main__":
    unittest.main()
