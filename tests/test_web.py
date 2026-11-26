"""ブラウザの版（手元のページ）と相手のページのテスト。ブラウザは開かず、HTTP を直接たたく。

  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import chat  # noqa: E402
import chat_web  # noqa: E402
from test_session import RecordingView, collect, start_host  # noqa: E402

_spec = importlib.util.spec_from_file_location("chat_v1_web", str(ROOT / "tests" / "legacy" / "chat_v1.py"))
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)  # type: ignore[union-attr]


def request(port: int, method: str, path: str, body=None, headers=None):
    """(状態, 頭書きの dict（小文字）, 本文) を返す。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        conn.close()


class SSE:
    """Server-Sent Events を読み続けて、出来事をためる。"""

    def __init__(self, port: int, path: str, headers=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        self.sock.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{extra}\r\n".encode("utf-8"))
        self.rfile = self.sock.makefile("rb")
        self.status = int(self.rfile.readline().split()[1])
        while self.rfile.readline() not in (b"\r\n", b""):
            pass
        self.events: list = []
        self.ended = threading.Event()
        self.cond = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            for raw in self.rfile:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("data: "):
                    with self.cond:
                        self.events.append(json.loads(line[6:]))
                        self.cond.notify_all()
        except (OSError, ValueError):
            pass
        self.ended.set()

    def wait_for(self, pred, timeout: float = 5) -> dict:
        with self.cond:
            ok = self.cond.wait_for(lambda: any(pred(e) for e in self.events), timeout)
            if not ok:
                raise AssertionError("出来事が届かない: " + json.dumps(self.events, ensure_ascii=False))
            return next(e for e in self.events if pred(e))

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 手元のページ（ブラウザの版）


class LocalBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.opened: list = []
        self.folders: list = []
        self.app = chat_web.LocalApp("自分", chat_port=free_port(), save_dir=self.base / "received",
                                     open_browser=self.opened.append, opener=self.folders.append, bind="127.0.0.1",
                                     page_bind="127.0.0.1", **self.app_args())
        self.app.start()
        self.origin = f"http://127.0.0.1:{self.app.port}"
        self.streams: list = []

    def app_args(self) -> dict:
        return {}

    def tearDown(self):
        for s in self.streams:
            s.close()
        self.app.stop()
        self.tmp.cleanup()

    def key_headers(self, **extra) -> dict:
        return {"X-Chat-Key": self.app.key, **extra}

    def post(self, path: str, body=None, **extra):
        headers = self.key_headers(Origin=self.origin, **extra)
        if isinstance(body, dict) or body is None:
            headers.setdefault("Content-Type", "application/json")
            body = body or {}
        return request(self.app.port, "POST", path, body, headers)

    def events(self) -> SSE:
        s = SSE(self.app.port, f"/api/events?k={self.app.key}&since=0")
        self.assertEqual(s.status, 200)
        self.streams.append(s)
        return s


class LocalGuardTest(LocalBase):
    def test_browser_opens_with_key_in_url(self):
        self.assertEqual(self.opened, [self.app.url])
        self.assertIn(f"127.0.0.1:{self.app.port}/?k={self.app.key}", self.app.url)
        self.assertGreaterEqual(len(self.app.key), 20)

    def test_key_is_required(self):
        port = self.app.port
        self.assertEqual(request(port, "GET", "/")[0], 403)
        self.assertEqual(request(port, "GET", "/?k=wrong")[0], 403)
        self.assertEqual(request(port, "GET", "/api/state")[0], 403)
        self.assertEqual(request(port, "GET", "/local.js")[0], 403)
        status, headers, body = request(port, "GET", f"/?k={self.app.key}")
        self.assertEqual(status, 200)
        self.assertIn("ブラウザの版", body.decode("utf-8"))
        cookie = headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(request(port, "GET", "/local.js", headers={"Cookie": cookie.split(";")[0]})[0], 200)
        self.assertEqual(request(port, "GET", "/api/state", headers=self.key_headers())[0], 200)

    def test_other_host_or_origin_is_refused(self):
        port = self.app.port
        # DNS rebinding（Host がちがう）
        self.assertEqual(request(port, "GET", "/api/state", headers=self.key_headers(Host=f"evil.example:{port}"))[0],
                         403)
        # ほかのサイトからの書き込み・Origin の無い書き込み
        body = {"text": "x"}
        h = self.key_headers(**{"Content-Type": "application/json"})
        self.assertEqual(request(port, "POST", "/api/send", body, {**h, "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(request(port, "POST", "/api/send", body, h)[0], 403)
        self.assertEqual(request(port, "GET", f"/api/state?k={self.app.key}",
                                 headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        # 鍵と Origin が合えば通る（まだつながっていないので 409）
        self.assertEqual(self.post("/api/send", body)[0], 409)

    def test_open_save_dir_uses_opener(self):
        self.assertEqual(self.post("/api/open")[0], 200)
        self.assertEqual(self.folders, [self.base / "received"])


class LocalChatTest(LocalBase):
    def join(self, module=chat):
        port, box, _ = start_host(module)
        status, _, body = self.post("/api/join", {"ip": f"127.0.0.1:{port}", "code": "1234", "name": "ページの人"})
        self.assertEqual(status, 200, body)
        box["thread"].join(3)
        return box["peer"]

    def test_wrong_code_points_to_code_field(self):
        port, box, _ = start_host(chat)
        status, _, body = self.post("/api/join", {"ip": f"127.0.0.1:{port}", "code": "0000", "name": "x"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["field"], "code")
        status, _, body = self.post("/api/join", {"ip": f"127.0.0.1:{free_port()}", "code": "1", "name": "x"})
        self.assertEqual(json.loads(body)["field"], "ip")

    def test_message_reaches_page_by_sse_and_page_reaches_terminal_peer(self):
        es = self.events()
        host = self.join()
        es.wait_for(lambda e: e["type"] == "phase" and e["phase"] == "chat")
        host.send("こんにちは")
        ev = es.wait_for(lambda e: e["type"] == "message" and not e["mine"])
        self.assertEqual((ev["name"], ev["text"]), ("待つ側", "こんにちは"))
        got: list = []
        t = collect(host, got)
        self.assertEqual(self.post("/api/send", {"text": "やあ 😀"})[0], 200)
        es.wait_for(lambda e: e["type"] == "message" and e["mine"] and e["text"] == "やあ 😀")
        host.finish()
        es.wait_for(lambda e: e["type"] == "phase" and e["phase"] == "idle")  # 相手が抜けたら、つなぐ欄へ戻る
        t.join(3)
        self.assertEqual(got, ["やあ 😀"])
        host.close()

    def test_page_reaches_old_version_peer(self):
        es = self.events()
        old = self.join(legacy)
        got: list = []
        t = collect(old, got)
        self.assertEqual(self.post("/api/send", {"text": "古いバージョンへ"})[0], 200)
        status, _, body = self.post("/api/upload?name=a.txt", b"abc", **{"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 409)
        es.wait_for(lambda e: e["type"] == "notice" and "相手が古いバージョンなので使えません" in e["text"])
        self.assertEqual(self.post("/api/leave")[0], 200)
        t.join(3)
        self.assertEqual(got, ["古いバージョンへ"])
        old.close()

    def test_uploaded_file_reaches_peer_intact(self):
        es = self.events()
        host = self.join()
        view = RecordingView()
        session = chat.ChatSession(host, view, save_dir=self.base / "host_in", auto_accept=True)
        session.start()
        data = os.urandom(300_000)
        status, _, body = self.post("/api/upload?name=" + quote("報告 書.bin"), data,
                                    **{"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200, body)
        view.wait_for("中身の照合 OK")
        es.wait_for(lambda e: e["type"] == "notice" and "送りました  報告 書.bin" in e["text"])
        self.assertEqual(sha((self.base / "host_in" / "報告 書.bin").read_bytes()), sha(data))
        es.wait_for(lambda e: e["type"] == "progress" and e["final"])
        # 一時ファイルは残さない
        deadline = time.monotonic() + 3
        while any(self.app.tmp.iterdir()) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(list(self.app.tmp.iterdir()), [])
        # 危ない名前は手元で断る
        status, _, body = self.post("/api/upload?name=" + quote("../x"), b"x",
                                    **{"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 400)
        session.quit()
        host.close()

    def test_file_from_peer_is_asked_and_saved(self):
        es = self.events()
        host = self.join()
        view = RecordingView()
        session = chat.ChatSession(host, view, save_dir=self.base / "unused")
        session.start()
        src = self.base / "q.txt"
        src.write_text("Q", encoding="utf-8")
        session.send_path(str(src))
        ev = es.wait_for(lambda e: e["type"] == "ask")
        self.assertEqual((ev["name"], ev["size"]), ("q.txt", 1))
        self.assertEqual(self.post("/api/answer", {"accept": True})[0], 200)
        view.wait_for("送りました  q.txt")
        self.assertEqual((self.base / "received" / "q.txt").read_text(encoding="utf-8"), "Q")
        es.wait_for(lambda e: e["type"] == "ask_done")
        session.quit()
        host.close()


class LocalLifecycleTest(LocalBase):
    def app_args(self) -> dict:
        return {"grace": 0.3, "first_grace": 5, "keepalive": 0.1}

    def test_stops_when_page_goes_away(self):
        es = self.events()
        time.sleep(0.6)  # つながっている間は終わらない
        self.assertFalse(self.app.done.is_set())
        es.close()
        self.assertTrue(self.app.done.wait(3), "ページが閉じても終わらない")
        self.assertIn("ページが閉じられた", self.app.reason)
        with self.assertRaises(OSError):
            request(self.app.port, "GET", "/")

    def test_first_page_must_come(self):
        app = chat_web.LocalApp("x", open_browser=lambda url: None, first_grace=0.2, grace=0.1)
        app.start()
        self.assertTrue(app.done.wait(3))
        app.stop()


class MenuTest(unittest.TestCase):
    def test_menu_has_browser_item(self):
        self.assertIn(("5", "ブラウザで使う"), [(k, label) for k, label, _, _ in chat.MENU])
        self.assertEqual(chat.MENU[-1][3], "web")

    def test_web_option_starts_local_page(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(chat, "STATE_PATH", Path(d) / "s.json"), \
                mock.patch.object(chat_web, "run_local", return_value=0) as run:
            self.assertEqual(chat.main(["--web", "--name", "x"]), 0)
        self.assertEqual(run.call_count, 1)

    def test_page_row(self):
        self.assertEqual(chat.page_row({}), [])
        self.assertEqual(chat.page_row({"url": "http://192.168.0.9:5051"}), [("ブラウザで", "http://192.168.0.9:5051")])
        self.assertIn("出せません", chat.page_row({"error": "使用中"})[0][1])


# ---------------------------------------------------------------- 相手のページ


def start_page_host(code: str = "1234", name: str = "待つ側", **kw):
    """相手のページつきで待ち受ける。(TCP のポート, ページのポート, 箱, 断った記録)。"""
    ready = threading.Event()
    box: dict = {}
    rejects: list = []

    def on_page(port, error):
        box["page_port"], box["page_error"] = port, error

    def on_ready(port):
        box["port"] = port
        ready.set()

    def run():
        try:
            box["peer"] = chat.host_wait(code, name, port=0, bind="127.0.0.1", on_ready=on_ready,
                                         on_reject=lambda ip, r: rejects.append(r), page_port=0, on_page=on_page,
                                         **kw)
        except Exception as exc:
            box["error"] = exc
            ready.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(3)
    box["thread"] = t
    return box["port"], box["page_port"], box, rejects


class Guest:
    """相手のページを開いたブラウザの代わり（cookie を覚える）。"""

    def __init__(self, port: int):
        self.port = port
        self.cookie = ""
        self.origin = f"http://127.0.0.1:{port}"

    def headers(self, **extra) -> dict:
        h = {"Origin": self.origin, **extra}
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    def post(self, path: str, body=None, ctype: str = "application/json", **extra):
        return request(self.port, "POST", path, body if body is not None else {},
                       self.headers(**{"Content-Type": ctype}, **extra))

    def login(self, code: str, name: str = "スマホ"):
        status, headers, body = self.post("/api/login", {"code": code, "name": name})
        if status == 200:
            self.raw_cookie = headers["set-cookie"]
            self.cookie = self.raw_cookie.split(";")[0]
        return status, json.loads(body or b"{}")

    def events(self) -> SSE:
        return SSE(self.port, "/api/events", {"Cookie": self.cookie})


class GuestPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cleanup: list = []

    def tearDown(self):
        for fn in reversed(self.cleanup):
            try:
                fn()
            except Exception:
                pass
        self.tmp.cleanup()

    def test_page_is_open_without_key_but_api_needs_cookie(self):
        _, page_port, box, _ = start_page_host()
        status, _, body = request(page_port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("合言葉", body.decode("utf-8"))
        self.assertEqual(request(page_port, "GET", "/api/events")[0], 403)
        g = Guest(page_port)
        self.assertEqual(g.post("/api/send", {"text": "x"})[0], 403)
        # Origin の無い・ちがうログインは数えずに断る
        self.assertEqual(request(page_port, "POST", "/api/login", {"code": "0000", "name": "x"},
                                 {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(g.post("/api/login", {"code": "0000", "name": "x"}, Origin="http://evil.example")[0], 403)
        st = json.loads(request(page_port, "GET", "/api/state")[2])
        self.assertEqual((st["joined"], st["open"]), (False, True))
        g.login("1234")  # 後片付け（待ち受けを終わらせる）
        box["thread"].join(3)
        box["peer"].close()

    def test_misses_are_counted_with_tcp_and_stop_at_five(self):
        port, page_port, box, rejects = start_page_host()
        g = Guest(page_port)
        for i in range(4):
            status, body = g.login("0000")
            self.assertEqual(status, 403)
            self.assertIn(f"残り {4 - i} 回", body["error"])
            self.assertEqual(request(page_port, "GET", "/favicon.ico")[0], 404)  # 合言葉の形でないものは数えない
            request(page_port, "GET", "/")
        with self.assertRaisesRegex(chat.Rejected, "残り 0 回"):
            chat.join(f"127.0.0.1:{port}", "9999", "TCP の人")
        box["thread"].join(3)
        self.assertIn("5 回になったので、待つのをやめました", str(box.get("error")))
        self.assertEqual(len(rejects), 5)
        with self.assertRaises(OSError):  # ページも閉じた
            g.login("1234")
        with self.assertRaises(chat.ChatError):
            chat.join(f"127.0.0.1:{port}", "1234", "正しい")

    def test_page_wins_then_tcp_and_second_browser_are_refused(self):
        port, page_port, box, _ = start_page_host()
        g = Guest(page_port)
        self.assertEqual(g.login("1234", "")[0], 400)   # 名前が空（数えない）
        status, body = g.login("1234")
        self.assertEqual(status, 200)
        self.assertEqual(body["host"], "待つ側")
        self.assertIn("HttpOnly", g.raw_cookie)
        self.assertIn("SameSite=Strict", g.raw_cookie)
        box["thread"].join(3)
        peer = box["peer"]
        self.cleanup.append(peer.close)
        self.assertIsInstance(peer, chat_web.PagePeer)
        self.assertEqual((peer.name, peer.version, peer.sock.getpeername()[0]), ("スマホ", 2, "127.0.0.1"))
        other = Guest(page_port)
        self.assertEqual(other.login("1234")[0], 409)          # 2 人目のブラウザ
        self.assertEqual(other.post("/api/send", {"text": "x"})[0], 403)
        with self.assertRaises(chat.ChatError):                # TCP の待ち受けは閉じた
            chat.join(f"127.0.0.1:{port}", "1234", "TCP の人")

    def test_tcp_wins_then_page_closes(self):
        port, page_port, box, _ = start_page_host()
        joiner = chat.join(f"127.0.0.1:{port}", "1234", "TCP の人")
        box["thread"].join(3)
        self.assertIsInstance(box["peer"], chat.FramedPeer)
        with self.assertRaises(OSError):
            Guest(page_port).login("1234")
        joiner.close()
        box["peer"].close()

    def test_chat_and_files_through_page_peer(self):
        _, page_port, box, _ = start_page_host()
        g = Guest(page_port)
        self.assertEqual(g.login("1234")[0], 200)
        box["thread"].join(3)
        peer = box["peer"]
        view = RecordingView()
        session = chat.ChatSession(peer, view, save_dir=self.base / "host_in", auto_accept=True,
                                   interval=0.05, warn=5, limit=10)
        session.start()
        self.cleanup.append(peer.close)
        self.cleanup.append(session.quit)
        es = g.events()
        self.cleanup.append(es.close)
        self.assertEqual(es.status, 200)
        self.assertIn("スマホ とつながりました", view.text())
        # ページ → 待つ側
        self.assertEqual(g.post("/api/send", {"text": "見える？"})[0], 200)
        view.wait_for("スマホ › 見える？")
        es.wait_for(lambda e: e["type"] == "message" and e["mine"] and e["text"] == "見える？")
        # 待つ側 → ページ（ターミナルの版と同じ入力で）
        session.handle_input("見えるよ")
        es.wait_for(lambda e: e["type"] == "message" and not e["mine"] and e["text"] == "見えるよ")
        # 待つ側 → ページのファイル（/send）。ページには「保存」のリンク
        data = os.urandom(200_000)
        src = self.base / "資料 1.bin"
        src.write_bytes(data)
        session.handle_input(f"/send '{src}'")
        view.wait_for("送りました  資料 1.bin → 相手の 資料 1.bin")
        ev = es.wait_for(lambda e: e["type"] == "file")
        self.assertEqual(request(page_port, "GET", f"/api/file/{ev['id']}")[0], 403)  # 鍵が無い
        status, headers, body = request(page_port, "GET", f"/api/file/{ev['id']}", headers={"Cookie": g.cookie})
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-disposition"].startswith("attachment;"))
        self.assertIn("filename*=UTF-8''" + quote("資料 1.bin"), headers["content-disposition"])
        self.assertEqual(sha(body), sha(data))
        # ページ → 待つ側のファイル（アップロード）
        data2 = os.urandom(150_000)
        status, _, _ = g.post("/api/upload?name=" + quote("写真.jpg"), data2, ctype="application/octet-stream")
        self.assertEqual(status, 202)
        view.wait_for("中身の照合 OK")
        es.wait_for(lambda e: e["type"] == "notice" and e["text"].startswith("送りました  写真.jpg"))
        self.assertEqual(sha((self.base / "host_in" / "写真.jpg").read_bytes()), sha(data2))
        # 合図
        for _ in range(3):
            self.assertEqual(g.post("/api/ping")[0], 200)
        # ページの「抜ける」
        self.assertEqual(g.post("/api/leave")[0], 200)
        view.wait_for("スマホ が抜けました")
        es.wait_for(lambda e: e["type"] == "end")

    def test_silent_page_is_lost_on_host(self):
        _, page_port, box, _ = start_page_host()
        g = Guest(page_port)
        g.login("1234")
        box["thread"].join(3)
        peer = box["peer"]
        view = RecordingView()
        closed = threading.Event()
        session = chat.ChatSession(peer, view, on_closed=closed.set, interval=0.05, warn=0.15, limit=0.4)
        session.start()
        self.assertTrue(closed.wait(3))  # ページの合図が来ないと、ターミナルの相手と同じく切れたとみなす
        self.assertIn("返事がありません", view.text())
        peer.close()

    def test_host_quit_ends_page(self):
        _, page_port, box, _ = start_page_host()
        g = Guest(page_port)
        g.login("1234")
        box["thread"].join(3)
        peer = box["peer"]
        session = chat.ChatSession(peer, RecordingView())
        session.start()
        es = g.events()
        session.quit()
        peer.close()
        es.wait_for(lambda e: e["type"] == "end" and "抜けました" in e["text"])
        self.assertTrue(es.ended.wait(3))
        es.close()


class HandleErrorTest(unittest.TestCase):
    def test_connection_errors_print_no_traceback(self):
        srv = chat_web.QuietServer(("127.0.0.1", 0), chat_web._Handler)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                for exc in (ConnectionResetError(), BrokenPipeError(), TimeoutError()):
                    try:
                        raise exc
                    except OSError:
                        srv.handle_error(None, ("192.168.0.9", 1))
            self.assertEqual(err.getvalue(), "")
            with contextlib.redirect_stderr(err):
                try:
                    raise ValueError("ほかの例外は今までどおり出す")
                except ValueError:
                    srv.handle_error(None, ("192.168.0.9", 1))
            self.assertIn("Traceback", err.getvalue())
        finally:
            srv.server_close()

    def test_reset_before_request_is_quiet(self):
        _, page_port, box, _ = start_page_host()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(3):  # スマホのブラウザの先回りの接続のように、何も送らずに切る（RST）
                s = socket.create_connection(("127.0.0.1", page_port))
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                s.close()
            time.sleep(0.3)
            self.assertEqual(request(page_port, "GET", "/")[0], 200)
        self.assertNotIn("Traceback", err.getvalue())
        Guest(page_port).login("1234")
        box["thread"].join(3)
        box["peer"].close()


if __name__ == "__main__":
    unittest.main()
