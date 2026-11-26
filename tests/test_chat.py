"""chat.py のテスト。1台の中で、ホストと相手を別スレッド・別プロセスで動かす。

  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chat  # noqa: E402

# Windows の既定（cp1252 など）でも、子プロセスの入出力を UTF-8 にする
# 前回の選択を覚えるファイルは、repo の中でなく一時フォルダに置く
_STATE_DIR = tempfile.TemporaryDirectory()
ENV = {**os.environ, "PYTHONUTF8": "1", "CHAT_APP_STATE": str(Path(_STATE_DIR.name) / "state.json")}
ENV.pop("NO_COLOR", None)


def read_banner(stream) -> list:
    """待機の枠を「止める」の行まで読む。"""
    lines = []
    while True:
        line = stream.readline()
        if not line:
            raise AssertionError("待機の枠が出ないまま終わった: " + "".join(lines))
        lines.append(line)
        if "止める" in line:
            return lines


def start_host(code: str = "1234", name: str = "ホスト"):
    """port=0 で待ち受けを始め、(port, 結果の箱, 断った記録, スレッド) を返す。"""
    ready = threading.Event()
    box: dict = {}
    rejects: list[tuple[str, str]] = []

    def on_ready(port: int) -> None:
        box["port"] = port
        ready.set()

    def run() -> None:
        try:
            box["peer"] = chat.host_wait(code, name, port=0, bind="127.0.0.1",
                                         on_ready=on_ready, on_reject=lambda ip, r: rejects.append((ip, r)))
        except Exception as exc:  # テストで見えるように残す
            box["error"] = exc
            ready.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(3), "待ち受けが始まらない"
    return box["port"], box, rejects, t


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AddressTest(unittest.TestCase):
    def test_lan_ip_is_not_loopback_when_network_exists(self):
        ip = chat.lan_ip()
        socket.inet_aton(ip)  # IPv4 の形であること
        if ip == "127.0.0.1":
            self.skipTest("ネットワークにつながっていない")
        self.assertFalse(ip.startswith("127."))

    def test_parse_address(self):
        self.assertEqual(chat.parse_address("192.168.0.9"), ("192.168.0.9", chat.DEFAULT_PORT))
        self.assertEqual(chat.parse_address(" 192.168.0.9:6000 "), ("192.168.0.9", 6000))
        self.assertEqual(chat.parse_address("pc-a.local", 7000), ("pc-a.local", 7000))
        for bad in ("", "1.2.3.4:x", "1.2.3.4:70000"):
            with self.assertRaises(chat.ChatError):
                chat.parse_address(bad)

    def test_code_is_four_digits(self):
        for _ in range(50):
            c = chat.new_code()
            self.assertRegex(c, r"^\d{4}$")

    def _host_code_with(self, inputs: list[str], code: str | None = None) -> str:
        import argparse
        from unittest import mock
        args = argparse.Namespace(code=code)
        with mock.patch("builtins.input", side_effect=inputs), mock.patch("sys.stdout", io.StringIO()):
            return chat._host_code(args)

    def test_host_code_uses_typed_four_digits(self):
        self.assertEqual(self._host_code_with(["0042"]), "0042")

    def test_host_code_retries_until_four_digits(self):
        self.assertEqual(self._host_code_with(["12", "abcd", "１２３４", "12345", "7777"]), "7777")

    def test_host_code_blank_is_random(self):
        self.assertRegex(self._host_code_with([""]), r"^\d{4}$")

    def test_host_code_option_skips_prompt(self):
        self.assertEqual(self._host_code_with([], code="9999"), "9999")


class HandshakeTest(unittest.TestCase):
    def test_join_and_exchange_both_ways(self):
        port, box, _, t = start_host()
        client = chat.join(f"127.0.0.1:{port}", "1234", "相手")
        t.join(3)
        host = box["peer"]
        self.assertEqual(client.name, "ホスト")
        self.assertEqual(host.name, "相手")

        client.send("こんにちは")
        host.send("やあ 😀")
        self.assertEqual(next(host.lines()), "こんにちは")
        self.assertEqual(next(client.lines()), "やあ 😀")

        client.finish()  # 相手が抜けたら、ホストの読み出しは終わる
        self.assertEqual(list(host.lines()), [])
        client.close(); host.close()

    def test_wrong_code_is_rejected_and_host_keeps_waiting(self):
        port, box, rejects, t = start_host(code="1234")
        with self.assertRaisesRegex(chat.Rejected, "合言葉がちがいます"):
            chat.join(f"127.0.0.1:{port}", "9999", "まちがい")
        self.assertTrue(t.is_alive(), "1回断っただけで待ち受けをやめた")
        client = chat.join(f"127.0.0.1:{port}", "1234", "正しい")
        t.join(3)
        self.assertEqual(box["peer"].name, "正しい")
        self.assertEqual([r for _, r in rejects], ["合言葉がちがいます（残り 4 回）"])
        client.close(); box["peer"].close()

    def test_stranger_without_hello_is_rejected(self):
        port, box, rejects, t = start_host()
        with socket.create_connection(("127.0.0.1", port)) as s:
            s.sendall(b"GET / HTTP/1.0\r\n\r\n")
            self.assertTrue(s.recv(100).startswith(b"NG "))
        client = chat.join(f"127.0.0.1:{port}", "1234", "相手")
        t.join(3)
        self.assertEqual(len(rejects), 1)
        client.close(); box["peer"].close()

    def test_join_to_closed_port_fails_quickly(self):
        t0 = time.monotonic()
        with self.assertRaisesRegex(chat.ChatError, "つながりません") as cm:
            chat.join(f"127.0.0.1:{free_port()}", "1234", "相手")
        self.assertNotIsInstance(cm.exception, chat.Rejected)  # IP を聞き直す側
        self.assertLess(time.monotonic() - t0, chat.CONNECT_TIMEOUT + 1)

    def test_bad_name_is_rejected(self):
        port, box, rejects, t = start_host()
        with self.assertRaisesRegex(chat.ChatError, "名前"):
            chat.join(f"127.0.0.1:{port}", "1234", "x" * (chat.MAX_NAME_LENGTH + 1))
        client = chat.join(f"127.0.0.1:{port}", "1234", "ok")
        t.join(3)
        client.close(); box["peer"].close()

    def test_port_in_use_is_reported(self):
        with socket.create_server(("127.0.0.1", 0)) as busy:
            port = busy.getsockname()[1]
            with self.assertRaisesRegex(chat.ChatError, "待ち受けられません"):
                chat.host_wait("1234", "ホスト", port=port, bind="127.0.0.1")


class MessageTest(unittest.TestCase):
    def setUp(self):
        a, b = socket.socketpair()
        # Peer の name は「相手の名前」。a の相手は B
        self.a, self.b = chat.Peer(a, "B"), chat.Peer(b, "A")

    def tearDown(self):
        self.a.close(); self.b.close()

    def test_send_refuses_newline_and_too_long(self):
        with self.assertRaises(chat.ChatError):
            self.a.send("1行目\n2行目")
        with self.assertRaises(chat.ChatError):
            self.a.send("あ" * (chat.MAX_MESSAGE_LENGTH + 1))
        got: list[str] = []  # 12 KB は socketpair のバッファを超えるので、先に読み手を立てる
        reader = threading.Thread(target=lambda: got.append(next(self.b.lines())), daemon=True)
        reader.start()
        self.a.send("あ" * chat.MAX_MESSAGE_LENGTH)  # 上限ちょうどは送れる
        reader.join(3)
        self.assertEqual(len(got[0]), chat.MAX_MESSAGE_LENGTH)

    def test_receiver_drops_oversized_line(self):
        self.a.sock.sendall(("x" * (chat.MAX_MESSAGE_LENGTH + 10) + "\n").encode())
        self.assertEqual(list(self.b.lines()), [])

    def test_run_chat_sends_lines_and_stops_at_quit(self):
        out = io.StringIO()
        closed = threading.Event()
        stdin = io.StringIO("一つ目\n\n二つ目\n/quit\n送られない\n")
        chat.run_chat(self.a, stdin, out, on_closed=closed.set)
        self.assertEqual(list(self.b.lines()), ["一つ目", "二つ目"])  # 空行は送らない
        self.b.finish()
        time.sleep(0.2)
        self.assertFalse(closed.is_set(), "自分の /quit を相手の切断として扱った")
        self.assertNotIn("接続が切れました", out.getvalue())
        self.assertRegex(out.getvalue(), r"\d\d:\d\d 自分 › 一つ目")  # 自分の発言も時刻つきで出す

    def test_close_does_not_hang_while_receiving(self):
        t = threading.Thread(target=lambda: list(self.a.lines()), daemon=True)
        t.start()
        time.sleep(0.1)  # 受信スレッドを readline で待たせる
        closer = threading.Thread(target=self.a.close, daemon=True)
        closer.start()
        closer.join(2)
        self.assertFalse(closer.is_alive(), "受信中に close すると止まる")

    def test_run_chat_reports_disconnect(self):
        out = io.StringIO()
        closed = threading.Event()
        r, w = socket.socketpair()  # 主スレッドの入力は、閉じるまで止まったままにする
        stdin = r.makefile("r")
        t = threading.Thread(target=chat.run_chat, args=(self.a, stdin, out, closed.set), daemon=True)
        t.start()
        self.b.send("じゃあね")
        self.b.finish()
        self.assertTrue(closed.wait(3), "相手の切断を受けても on_closed が呼ばれない")
        self.assertRegex(out.getvalue(), r"\d\d:\d\d B +› じゃあね")
        self.assertIn("✕ B との接続が切れました", out.getvalue())
        w.close(); t.join(3); stdin.close(); r.close()


class CommandLineTest(unittest.TestCase):
    """実際の起動口（chat.py）を2つのプロセスで動かす。"""

    def test_menu_retry_asks_only_code_after_rejection(self):
        port = free_port()
        base = [sys.executable, str(ROOT / "chat.py"), "--port", str(port)]
        host = subprocess.Popen(base + ["--host", "--code", "5555", "--name", "ホスト"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, encoding="utf-8", env=ENV)
        read_banner(host.stdout)
        # メニュー 2 → IP → 合言葉ちがい → （IP は聞かれず）正しい合言葉 → 1件送って抜ける
        joiner = subprocess.run(base + ["--name", "相手"], input=f"2\n127.0.0.1\n0000\n5555\nやあ\n/quit\n",
                                capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=10)
        # 待つ側は相手が抜けたのを受けて自分で終わる。先に stdin を閉じると、待つ側が自分から
        # 終わる道と競い、「抜けました」を出す前に終わることがある（2026-11-26、12 回に 1 回落ちた）。
        host.wait(timeout=10)
        host_out = host.stdout.read()
        host.stdin.close(); host.stdout.close()
        self.assertIn("2  チャットにつなぐ", joiner.stdout)  # メニューは 1 段
        import re
        self.assertEqual(len(re.findall(r"相手の IP (?:\[[^\]]*\] )?>", joiner.stdout)), 1, joiner.stdout)
        self.assertEqual(joiner.stdout.count("合言葉 >"), 2)
        self.assertIn("残り 4 回", joiner.stdout)
        self.assertRegex(host_out, r"相手 › やあ")

    def test_host_and_join_processes_talk(self):
        port = free_port()
        cmd = [sys.executable, str(ROOT / "chat.py"), "--port", str(port), "--code", "4821"]
        host = subprocess.Popen(cmd + ["--host", "--name", "ホスト"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, encoding="utf-8", env=ENV)
        banner = read_banner(host.stdout)
        self.assertIn("チャットを待っています", banner[1])
        self.assertIn(f":{port}", banner[2])          # 既定でないポートは IP に付けて見せる
        self.assertIn("4 8 2 1", banner[3])            # 合言葉は 1 字ずつ空ける
        command = next(line for line in banner if "相手の画面で" in line)  # 枠の下の、相手の画面で打つ 1 行
        self.assertIn(f"--join ", command)
        self.assertIn(f":{port} --code 4821", command)
        self.assertIn("ブラウザで  ", "".join(banner))  # 相手のページ（ポート+1）の住所か、出せない理由も枠に出す

        joiner = subprocess.run(
            cmd + ["--join", f"127.0.0.1", "--name", "相手"],
            input="こんにちは\n/quit\n", capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=10,
        )
        # 待つ側は相手が抜けたのを受けて自分で終わる。先に stdin を閉じると、待つ側が自分から
        # 終わる道と競い、「抜けました」を出す前に終わることがある（2026-11-26、12 回に 1 回落ちた）。
        host.wait(timeout=10)
        host_out = host.stdout.read()
        host.stdin.close(); host.stdout.close()
        self.assertEqual(joiner.returncode, 0, joiner.stderr)
        self.assertIn("ホスト とつながりました", joiner.stdout)
        self.assertRegex(host_out, r"相手 › こんにちは")
        self.assertIn("相手 が抜けました", host_out)  # 新しいバージョンどうしは「抜ける」の枠が届く
        self.assertEqual(host.returncode, 0)

    def test_join_with_wrong_code_exits_with_error(self):
        port = free_port()
        base = [sys.executable, str(ROOT / "chat.py"), "--port", str(port)]
        host = subprocess.Popen(base + ["--host", "--code", "1111"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, encoding="utf-8", env=ENV)
        read_banner(host.stdout)
        bad = subprocess.run(base + ["--join", "127.0.0.1", "--code", "2222"],
                             capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=10)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("合言葉がちがいます", bad.stdout)
        self.assertIn("127.0.0.1: 合言葉がちがいます（残り 4 回）", host.stdout.readline())
        host.kill(); host.communicate()


if __name__ == "__main__":
    unittest.main()
