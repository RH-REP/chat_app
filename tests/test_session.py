"""取り決めの新旧・合図・チャットの中のコマンド・合言葉の間違いのテスト。

古いバージョンの相手は tests/legacy/chat_v1.py（取り決めに版の番号が無かったころの chat.py をそのまま写したもの）。

  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chat  # noqa: E402
import chat_term  # noqa: E402

_spec = importlib.util.spec_from_file_location("chat_v1", str(ROOT / "tests" / "legacy" / "chat_v1.py"))
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)  # type: ignore[union-attr]


class RecordingView(chat_term.PlainView):
    """表示の代わりに行をためる。wait_for で、ある字を含む行が出るまで待てる。"""

    def __init__(self):
        super().__init__(io.StringIO(), color=False, unicode=True)
        self.rows: list = []
        self.statuses: list = []
        self.prompts: list = []
        self.cond = threading.Condition()

    def line(self, text: str) -> None:
        with self.cond:
            self.rows.append(text)
            self.cond.notify_all()

    def status(self, st: dict) -> None:
        with self.cond:
            self.statuses.append(dict(st))
            self.cond.notify_all()

    def set_prompt(self, prompt) -> None:
        with self.cond:
            self.prompts.append(prompt)
            self.cond.notify_all()

    def text(self) -> str:
        with self.cond:
            return "\n".join(self.rows)

    def wait_for(self, needle: str, timeout: float = 5) -> str:
        with self.cond:
            ok = self.cond.wait_for(lambda: any(needle in r for r in self.rows), timeout)
        if not ok:
            raise AssertionError(f"{needle!r} が出ない:\n" + self.text())
        return next(r for r in self.rows if needle in r)

    def wait_count(self, needle: str, n: int, timeout: float = 5) -> None:
        if not self.wait_until(lambda: sum(needle in r for r in self.rows) >= n, timeout):
            raise AssertionError(f"{needle!r} が {n} 回出ない:\n" + self.text())

    def wait_until(self, pred, timeout: float = 5) -> bool:
        with self.cond:
            return self.cond.wait_for(pred, timeout)


def start_host(module, code: str = "1234", name: str = "待つ側", receive: bool = False, **kw):
    """module（chat か legacy）の待つ側をスレッドで動かす。(port, 箱, 断った記録)。"""
    ready = threading.Event()
    box: dict = {}
    rejects: list = []

    def on_ready(port: int) -> None:
        box["port"] = port
        ready.set()

    def run() -> None:
        fn = module.host_receive if receive else module.host_wait
        try:
            box["peer"] = fn(code, name, port=0, bind="127.0.0.1", on_ready=on_ready,
                             on_reject=lambda ip, r: rejects.append(r), **kw)
        except Exception as exc:
            box["error"] = exc
            ready.set()
        box["done"] = True

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(3), "待ち受けが始まらない"
    box["thread"] = t
    return box["port"], box, rejects


def collect(peer, into: list) -> threading.Thread:
    t = threading.Thread(target=lambda: into.extend(peer.lines()), daemon=True)
    t.start()
    return t


class VersionCombinationTest(unittest.TestCase):
    """待つ側・つなぐ側の新旧 4 通り。古いバージョンの画面には発言以外の行が出ないこと。"""

    def talk(self, host_mod, join_mod):
        port, box, rejects = start_host(host_mod)
        joiner = join_mod.join(f"127.0.0.1:{port}", "1234", "つなぐ側")
        box["thread"].join(3)
        host = box["peer"]
        got_host, got_join = [], []
        th, tj = collect(host, got_host), collect(joiner, got_join)
        joiner.send("こんにちは")
        host.send("やあ 😀")
        time.sleep(0.2)
        joiner.finish(); host.finish()
        th.join(3); tj.join(3)
        self.assertEqual(got_host, ["こんにちは"])
        self.assertEqual(got_join, ["やあ 😀"])
        self.assertEqual(rejects, [])  # つなぎ直しなどで断った行を出さない
        self.assertEqual((host.name, joiner.name), ("つなぐ側", "待つ側"))
        joiner.close(); host.close()
        return host, joiner

    def test_new_host_new_joiner_uses_frames(self):
        host, joiner = self.talk(chat, chat)
        self.assertEqual((host.version, joiner.version), (2, 2))

    def test_old_host_new_joiner_falls_back_to_lines(self):
        host, joiner = self.talk(legacy, chat)
        self.assertEqual(joiner.version, 1)
        self.assertEqual(host.name, "つなぐ側")  # 古いバージョンはしるしのタブを落として名前だけを見る

    def test_new_host_old_joiner_falls_back_to_lines(self):
        host, _ = self.talk(chat, legacy)
        self.assertEqual(host.version, 1)

    def test_old_host_old_joiner(self):
        self.talk(legacy, legacy)

    def test_session_with_old_peer_sends_no_pings(self):
        """新しいバージョンの ChatSession を合図の間隔 0.05 秒で動かしても、古いバージョンには発言しか届かない。"""
        port, box, _ = start_host(legacy)
        joiner = chat.join(f"127.0.0.1:{port}", "1234", "新しいバージョン")
        box["thread"].join(3)
        old = box["peer"]
        got: list = []
        t = collect(old, got)
        view = RecordingView()
        s = chat.ChatSession(joiner, view, interval=0.05, warn=0.1, limit=0.2)
        s.start()
        self.assertTrue(s.handle_input("ひとこと"))
        time.sleep(0.5)  # 古い相手は合図を返さないが、切れたとはみなさない
        self.assertFalse(s.closed.is_set())
        s.handle_input("/send x.txt")
        view.wait_for("相手が古いバージョンなので使えません")
        s.quit()
        t.join(3)
        self.assertEqual(got, ["ひとこと"])
        joiner.close(); old.close()


class HeartbeatTest(unittest.TestCase):
    def pair(self):
        a, b = socket.socketpair()
        return chat.FramedPeer(a, "B"), chat.FramedPeer(b, "A")

    def test_silent_peer_is_detected(self):
        pa, pb = self.pair()
        view = RecordingView()
        closed = threading.Event()
        s = chat.ChatSession(pa, view, on_closed=closed.set, interval=0.05, warn=0.15, limit=0.4)
        t0 = time.monotonic()
        s.start()  # pb は何も送らない（スリープした相手）
        self.assertTrue(closed.wait(3), "黙った相手を切れたとみなさない")
        took = time.monotonic() - t0
        self.assertGreaterEqual(took, 0.4)
        self.assertLess(took, 2)
        self.assertIn("0.4 秒返事がありません", view.text())
        states = [st["state"] for st in view.statuses]
        self.assertIn("quiet", states)  # 届かない間は状態の行で知らせる
        self.assertEqual(states[-1], "lost")
        pa.close(); pb.close()

    def test_pings_keep_the_line_alive(self):
        pa, pb = self.pair()
        va, vb = RecordingView(), RecordingView()
        sa = chat.ChatSession(pa, va, interval=0.05, warn=0.15, limit=0.3)
        sb = chat.ChatSession(pb, vb, interval=0.05, warn=0.15, limit=0.3)
        sa.start(); sb.start()
        time.sleep(0.9)  # 黙っていても合図が届くので切れない
        self.assertFalse(sa.closed.is_set() or sb.closed.is_set())
        sa.handle_input("まだいる？")
        vb.wait_for("A    › まだいる？")
        sa.quit()
        vb.wait_for("A が抜けました")
        self.assertNotIn("返事がありません", va.text() + vb.text())
        pa.close(); pb.close()

    def test_heartbeat_defaults(self):
        self.assertEqual((chat.HEARTBEAT_INTERVAL, chat.SILENT_WARN, chat.SILENT_LIMIT), (10.0, 20.0, 30.0))

    def test_unknown_frame_kind_is_ignored(self):
        pa, pb = self.pair()
        view = RecordingView()
        s = chat.ChatSession(pa, view)
        s.start()
        pb.send_frame(b"Z", b"future")  # さらに新しいバージョンが足した種類
        pb.send("ふつうの発言")
        view.wait_for("ふつうの発言")
        self.assertFalse(s.closed.is_set())
        s.quit(); pa.close(); pb.close()


class SendCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.src, self.dst_a, self.dst_b = base / "src", base / "a", base / "b"
        self.src.mkdir()
        a, b = socket.socketpair()
        self.pa, self.pb = chat.FramedPeer(a, "B"), chat.FramedPeer(b, "A")
        self.va, self.vb = RecordingView(), RecordingView()
        self.opened: list = []
        self.sa = chat.ChatSession(self.pa, self.va, save_dir=self.dst_a, opener=self.opened.append)
        self.sb = chat.ChatSession(self.pb, self.vb, save_dir=self.dst_b)
        self.sa.start(); self.sb.start()

    def tearDown(self):
        self.sa.quit(); self.sb.quit()
        self.pa.close(); self.pb.close()
        self.tmp.cleanup()

    def test_send_accept_and_chat_continues(self):
        data = os.urandom(300_000)
        (self.src / "報告 書.bin").write_bytes(data)
        if sys.platform == "win32":  # Windows のドラッグは引用符で囲む（\ は場所の区切り）
            self.sa.handle_input(f'/send "{self.src / "報告 書.bin"}"')
        else:  # Mac・Linux のドラッグは \ で空白を逃がす
            self.sa.handle_input(f"/send {self.src}/報告\\ 書.bin")
        self.vb.wait_for("A からファイルが届きました")
        self.vb.wait_for("名前    報告 書.bin")
        self.assertTrue(self.vb.wait_until(lambda: self.vb.prompts and self.vb.prompts[-1]))
        self.assertIn("[Enter で断る]", self.vb.prompts[-1])
        self.sb.handle_input("y")
        self.vb.wait_for("中身の照合 OK")
        self.va.wait_for("送りました  報告 書.bin → 相手の 報告 書.bin")
        self.assertEqual(hashlib.sha256((self.dst_b / "報告 書.bin").read_bytes()).hexdigest(),
                         hashlib.sha256(data).hexdigest())
        self.assertEqual([p.name for p in self.dst_b.iterdir()], ["報告 書.bin"])
        self.sb.handle_input("ありがとう")  # 送った後もチャットは続く
        self.va.wait_for("ありがとう")

    def test_both_directions_and_decline(self):
        (self.src / "q.txt").write_text("Q", encoding="utf-8")
        self.sb.handle_input(f"/send '{self.src / 'q.txt'}'")  # 引用符つき
        self.va.wait_for("B からファイルが届きました")
        self.sa.handle_input("")  # Enter で断る
        self.vb.wait_for("送れません: 断られました")
        self.assertFalse(self.dst_a.exists() and any(self.dst_a.iterdir()))
        self.sb.handle_input(f"/send {self.src / 'q.txt'}")
        self.va.wait_count("B からファイルが届きました", 2)
        self.sa.handle_input("y")
        self.va.wait_for("中身の照合 OK")
        self.assertEqual((self.dst_a / "q.txt").read_text(encoding="utf-8"), "Q")

    def test_other_text_while_asked_is_sent_and_question_stays(self):
        (self.src / "w.txt").write_text("W", encoding="utf-8")
        self.sa.handle_input(f"/send {self.src / 'w.txt'}")
        self.vb.wait_for("A からファイルが届きました")
        self.sb.handle_input("ちょっと待って")
        self.va.wait_for("ちょっと待って")
        self.sb.handle_input("yes")
        self.vb.wait_for("中身の照合 OK")

    def test_local_errors(self):
        self.sa.handle_input("/send")
        self.va.wait_for("使い方: /send ファイル")
        self.sa.handle_input(f"/send {self.src / 'none.txt'}")
        self.va.wait_for("見つかりません")
        self.sa.handle_input(f"/send {self.src}")
        self.va.wait_for("フォルダは zip")
        self.sa.handle_input("/nope")
        self.va.wait_for("知らないコマンドです: /nope")

    def test_help_open_quit(self):
        self.sa.handle_input("/help")
        self.va.wait_for("/send ファイル")
        self.sa.handle_input("/open")
        self.assertEqual(self.opened, [self.dst_a])  # 試験ではフォルダを開かない（opener を差し替え）
        self.assertTrue(self.dst_a.is_dir())
        self.assertFalse(self.sa.handle_input("/quit"))
        self.assertNotIn("›", self.vb.text())  # コマンドは相手に発言として送らない

    def test_parse_send_path(self):
        self.assertEqual(chat.parse_send_path(" '/a b/c.pdf' "), "/a b/c.pdf")
        self.assertEqual(chat.parse_send_path('"C:\\My Docs\\a.pdf"', platform="win32"), "C:\\My Docs\\a.pdf")
        self.assertEqual(chat.parse_send_path("/a\\ b/it\\'s.pdf ", platform="darwin"), "/a b/it's.pdf")
        self.assertEqual(chat.parse_send_path("C:\\x\\a.pdf", platform="win32"), "C:\\x\\a.pdf")


class CodeMissTest(unittest.TestCase):
    def check_stops_after_five(self, receive: bool):
        port, box, rejects = start_host(chat, code="1234", receive=receive)
        joiner = chat.join_send if receive else chat.join
        for i in range(chat.MAX_CODE_MISSES):
            with self.assertRaisesRegex(chat.Rejected, f"残り {4 - i} 回"):
                joiner(f"127.0.0.1:{port}", "0000", "まちがい")
        box["thread"].join(3)
        self.assertIn("error", box)
        self.assertIn("5 回になったので、待つのをやめました", str(box["error"]))
        self.assertEqual(len(rejects), 5)
        with self.assertRaises(chat.ChatError):  # もう待っていない
            joiner(f"127.0.0.1:{port}", "1234", "正しい")

    def test_chat_host_stops(self):
        self.check_stops_after_five(receive=False)

    def test_file_host_stops(self):
        self.check_stops_after_five(receive=True)

    def test_strangers_are_not_counted(self):
        port, box, rejects = start_host(chat, code="1234")
        for _ in range(6):
            with socket.create_connection(("127.0.0.1", port)) as s:
                s.sendall(b"GET /favicon.ico HTTP/1.0\r\n\r\n")
                s.recv(100)
        client = chat.join(f"127.0.0.1:{port}", "1234", "正しい")
        box["thread"].join(3)
        self.assertEqual(len(rejects), 6)
        client.close(); box["peer"].close()


class LegacyFileTest(unittest.TestCase):
    """ファイル送受信のつながりは、古いバージョンとも今までどおり動く。"""

    def run_pair(self, recv_mod, send_mod):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "a.bin"
            src.write_bytes(os.urandom(200_000))
            port, box, _ = start_host(recv_mod, receive=True)
            sock, _ = send_mod.join_send(f"127.0.0.1:{port}", "1234", "送る側")
            box["thread"].join(3)
            rsock, _ = box["peer"]
            rfile = rsock.makefile("rb")
            result: dict = {}
            t = threading.Thread(target=lambda: result.update(saved=recv_mod.receive_files(
                rsock, rfile, d / "in", io.StringIO(), lambda n, s: True)), daemon=True)
            t.start()
            srfile = sock.makefile("rb")
            self.assertEqual(send_mod.send_file(sock, srfile, src, io.StringIO()), "a.bin")
            send_mod.send_frame(sock, send_mod.BYE)
            t.join(5)
            self.assertEqual((d / "in" / "a.bin").read_bytes(), src.read_bytes())
            for f in (srfile, rfile):
                f.close()
            sock.close(); rsock.close()

    def test_new_receiver_old_sender(self):
        self.run_pair(chat, legacy)

    def test_old_receiver_new_sender(self):
        self.run_pair(legacy, chat)


if __name__ == "__main__":
    unittest.main()
