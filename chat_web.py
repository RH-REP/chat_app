"""chat_app のブラウザの版と相手のページ（標準ライブラリだけ）。

ブラウザの版（LocalApp）
  chat.py --web で、127.0.0.1 の空いているポートにページを出し、既定のブラウザで開く。
  URL には起動ごとの鍵を入れ、鍵の無い要求・Host や Origin のずれた要求は 403 で断る。
  ページ → 手元は fetch / XHR、手元 → ページは Server-Sent Events。相手とのやり取りは chat.ChatSession の
  ままで、表示だけを WebView に差し替える（ターミナルの版の相手とも話せる）。
  ページからの SSE のつながりが一定時間（既定 15 秒）途切れたら終わる。

相手のページ（GuestPage と PagePeer）
  チャットを待つとき、待つ側は今の待ち受け（既定 5050）に加えて、同じ Wi-Fi に向けてページを
  ポート+1（既定 5051）に出す。相手はブラウザで開いて合言葉と名前を入れるだけでつなげる。
  合言葉の間違いは TCP とページで1つに数え（chat.WaitGate）、先に合った1人とだけ話す。
  合った相手のブラウザには鍵（HttpOnly・SameSite=Strict の cookie）を渡し、以後は鍵の無い要求を断る。
  PagePeer は、ページの相手を ChatSession から FramedPeer と同じに見せる包み。
"""

from __future__ import annotations

import hashlib
import http.cookies
import json
import os
import queue
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat  # noqa: E402
import chat_term  # noqa: E402
from chat import (ACCEPT, BYE, DATA, DONE, FAILED, MSG, OFFER, PING, REJECT, SAVED,  # noqa: E402
                  ChatError, TransferError, human_size)

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8"}
LOCAL_FILES = ("local.js", "style.css", "theme.css", "feedback.js")   # 手元のページが読むもの
GUEST_FILES = ("guest.js", "style.css", "theme.css")                  # 相手のページが読むもの
MAX_JSON = 64 * 1024
KEEPALIVE = 2.0          # SSE の空の行を送る間隔（秒）。閉じたページに早く気づくため
PAGE_GRACE = 15.0        # 手元のページの SSE が途切れてから終えるまで（秒）
FIRST_GRACE = 60.0       # 起動してから最初にページがつながるまで待つ（秒）
EVENT_KEEP = 1000        # つなぎ直したページに流し直すため、ためておく出来事の数
# 「!」の feedback.js は自分で <style> を足し、手元の issue_hub（127.0.0.1:8934）へ送れるので、手元のページだけ許す
LOCAL_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
             "connect-src 'self' http://127.0.0.1:8934; frame-ancestors 'none'")
GUEST_CSP = "default-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"


# ---------------------------------------------------------------- 共通の部品


class QuietServer(ThreadingHTTPServer):
    """相手が先に切った接続（スマホのブラウザの先回りの接続など）で、トレースバックを出さない。"""

    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"  # Windows の SO_REUSEADDR は使用中のポートにも重ねてしまう

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        err = sys.exc_info()[1]
        if isinstance(err, (ConnectionError, TimeoutError, socket.timeout)):
            return
        super().handle_error(request, client_address)


class EventLog:
    """SSE で流す出来事。番号を付けてためておき、つなぎ直したページには続きから流す。"""

    def __init__(self, keep: int = EVENT_KEEP):
        self.keep = keep
        self.items: List[Tuple[int, dict]] = []
        self.next_id = 1
        self.closed = False
        self.cond = threading.Condition()

    def publish(self, event: dict) -> int:
        with self.cond:
            n = self.next_id
            self.next_id += 1
            self.items.append((n, event))
            del self.items[:-self.keep]
            self.cond.notify_all()
            return n

    def since(self, last: int) -> List[Tuple[int, dict]]:
        with self.cond:
            return [(n, e) for n, e in self.items if n > last]

    def wait(self, last: int, timeout: float) -> List[Tuple[int, dict]]:
        with self.cond:
            self.cond.wait_for(lambda: self.closed or self.next_id - 1 > last, timeout)
            return [(n, e) for n, e in self.items if n > last]

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()


def disposition(name: str) -> str:
    """Content-Disposition。日本語の名前は filename* に、ASCII の代わりを filename に書く。"""
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


class _Handler(BaseHTTPRequestHandler):
    """両方のページに共通の書き出し。"""

    server_version = "chat_app"
    sys_version = ""

    def log_message(self, *args) -> None:  # noqa: ANN002
        pass

    def _send(self, code: int, body: bytes, ctype: str, headers: Tuple[Tuple[str, str], ...] = ()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: dict, headers: Tuple[Tuple[str, str], ...] = ()) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   headers)

    csp = GUEST_CSP

    def _static(self, name: str) -> None:
        path = WEB_DIR / name
        try:
            body = path.read_bytes()
        except OSError:
            self._json(404, {"error": "見つかりません"})
            return
        self._send(200, body, STATIC_TYPES.get(path.suffix, "application/octet-stream"),
                   (("Content-Security-Policy", self.csp),))

    def _read_json(self) -> dict:
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise ValueError("JSON で送ってください")
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 <= n <= MAX_JSON:
            raise ValueError("大きすぎます")
        data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("形が違います")
        return data

    def _same_origin(self, require: bool) -> bool:
        """Origin が Host と同じか。require なら Origin が無いものも断る。"""
        origin = self.headers.get("Origin")
        if origin is None:
            return not require
        return origin == f"http://{self.headers.get('Host', '')}"

    def _cookie(self, name: str) -> str:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return ""
        return jar[name].value if name in jar else ""

    def _stream(self, log: EventLog, last: int, keepalive: float, on_open: Callable[[], None] = lambda: None,
                on_close: Callable[[], None] = lambda: None) -> None:
        """出来事を Server-Sent Events で流し続ける。log が閉じるか、ページが切れたら終わる。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        on_open()
        try:
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            while True:
                items = log.wait(last, keepalive)
                if items:
                    chunks = []
                    for n, event in items:
                        chunks.append(f"id: {n}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n")
                        last = n
                    self.wfile.write("".join(chunks).encode("utf-8"))
                elif log.closed:
                    return
                else:
                    self.wfile.write(b": k\n\n")
                self.wfile.flush()
                if log.closed and not log.since(last):
                    return
        except (OSError, ValueError):
            return
        finally:
            on_close()
            self.close_connection = True

    def _last_event_id(self) -> int:
        q = parse_qs(urlsplit(self.path).query)
        raw = self.headers.get("Last-Event-ID") or (q.get("since") or ["0"])[0]
        try:
            return max(int(raw), 0)
        except ValueError:
            return 0

    def _receive_body(self, folder: Path, name: str, max_size: int) -> Tuple[Path, int, str]:
        """要求の本文をそのまま folder/name に書く。(場所, 大きさ, sha256) を返す。"""
        try:
            size = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            size = -1
        if size < 0:
            raise TransferError("大きさがわかりません")
        if size > max_size:
            raise TransferError(f"大きすぎます（{human_size(size)}。上限 {human_size(max_size)}）")
        folder.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(folder).free
        if size + chat.FREE_SPACE_MARGIN > free:
            raise TransferError(f"空き容量が足りません（空き {human_size(free)}）")
        path = folder / name
        digest = hashlib.sha256()
        left = size
        with path.open("xb") as f:
            while left > 0:
                chunk = self.rfile.read(min(chat.CHUNK_SIZE, left))
                if not chunk:
                    break
                f.write(chunk)
                digest.update(chunk)
                left -= len(chunk)
        if left:
            path.unlink()
            raise TransferError("途中で切れました")
        return path, size, digest.hexdigest()


def _put(q: "queue.Queue", item: object, stopped: threading.Event) -> bool:
    """満杯の列に、止まっていない間だけ入れようとし続ける。"""
    while not stopped.is_set():
        try:
            q.put(item, timeout=0.2)
            return True
        except queue.Full:
            continue
    return False


# ---------------------------------------------------------------- 相手のページ（同じ Wi-Fi に出す）


class _PageSock:
    """ChatSession が peer.sock に求める口（getpeername・shutdown・detach）だけを持つ代わり。"""

    def __init__(self, address: str, peer: "PagePeer"):
        self.address, self.peer = address, peer

    def getpeername(self) -> Tuple[str, int]:
        return self.address, 0

    def shutdown(self, how: int) -> None:  # noqa: ARG002
        self.peer.stopped.set()

    def detach(self) -> int:
        return -1

    def close(self) -> None:
        self.peer.stopped.set()


class PagePeer:
    """相手のページの相手1人を、ChatSession から FramedPeer と同じに見せる。

    ChatSession が送る枠（send_frame）をページの出来事に直し、ページから来たもの（発言・合図・抜ける・
    ファイル）を枠にして frames() から返す。待つ側から送られたファイルは一時フォルダで受けて照合し、
    ページに「保存」のリンクを出す（ダウンロードで受け取る）。
    """

    version = 2

    def __init__(self, name: str, address: str, host_name: str, page: Optional["GuestPage"] = None,
                 max_size: int = chat.DEFAULT_MAX_FILE_SIZE):
        self.name, self.host_name, self.page, self.max_size = name, host_name, page, max_size
        self.sock = _PageSock(address, self)
        self.closed_by_me = False
        self.stopped = threading.Event()       # frames() を終わらせる
        self.inbox: "queue.Queue[Tuple[bytes, bytes]]" = queue.Queue(maxsize=64)  # 64 KiB の DATA で 4 MiB まで
        self.replies: "queue.Queue[Tuple[bytes, bytes]]" = queue.Queue()
        self.events = EventLog()
        self.tmp = Path(tempfile.mkdtemp(prefix="chat_app_page_"))
        self.files: Dict[str, Tuple[Path, str]] = {}
        self._incoming: Optional[chat.Incoming] = None
        self._uploading = False
        self._lock = threading.Lock()
        self._closed = False
        self._ended = False
        self.events.publish({"type": "notice", "kind": "info", "text": f"{host_name} とつながりました"})

    def _end_event(self, text: str) -> None:
        """チャットが終わったことを、ページに1度だけ知らせる。"""
        with self._lock:
            if self._ended:
                return
            self._ended = True
        self.events.publish({"type": "end", "text": text})

    # ChatSession から呼ばれる口
    def send_frame(self, kind: bytes, payload: bytes = b"") -> None:
        if self._closed or self.stopped.is_set():
            raise ConnectionError("相手のページは閉じています")
        if kind == MSG:
            self.events.publish({"type": "message", "mine": False, "name": self.host_name,
                                 "text": payload.decode("utf-8", errors="replace"), "time": time.time()})
        elif kind == BYE:
            self._end_event(f"{self.host_name} が抜けました")
        elif kind == OFFER:
            self._host_offer(payload)
        elif kind in (DATA, DONE):
            self._host_data(kind, payload)
        elif kind in (ACCEPT, REJECT, SAVED, FAILED):
            self.replies.put((kind, payload))
        # PING とまだ知らない種類は読み捨てる（ページの合図は /api/ping で来る）

    def send(self, text: str) -> None:
        self.send_frame(MSG, chat._check_text(text))

    def frames(self):
        while True:
            try:
                item = self.inbox.get(timeout=0.2)
            except queue.Empty:
                if self.stopped.is_set():
                    return
                continue
            yield item

    def lines(self):
        for kind, payload in self.frames():
            if kind == MSG:
                yield payload.decode("utf-8", errors="replace")
            elif kind == BYE:
                return

    def finish(self) -> None:
        self.closed_by_me = True
        self._end_event(f"{self.host_name} が抜けました")
        self.stopped.set()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.closed_by_me = True
        self.stopped.set()
        self._end_event("チャットは終わりました")
        self.events.close()
        if self._incoming is not None:
            self._incoming.abort()
        if self.page is not None:
            self.page.close()
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def close_quietly(self) -> None:
        """選ばれなかった PagePeer の後始末（ページは閉じない）。"""
        self.page = None
        self.close()

    # 待つ側からページへのファイル
    def _host_offer(self, payload: bytes) -> None:
        inc = chat.Incoming(self.tmp, self.max_size)
        try:
            name, size = inc.check(payload)
            inc.begin()
        except (TransferError, OSError) as exc:
            _put(self.inbox, (REJECT, str(exc).encode("utf-8")), self.stopped)
            return
        self._incoming = inc
        self.events.publish({"type": "incoming", "name": name, "size": size, "size_h": human_size(size)})
        _put(self.inbox, (ACCEPT, b""), self.stopped)

    def _host_data(self, kind: bytes, payload: bytes) -> None:
        inc = self._incoming
        if inc is None:
            raise ConnectionError("申し出の無いデータです")
        if kind == DATA:
            inc.write(payload)
            return
        self._incoming = None
        try:
            path = inc.finish(payload)
        except TransferError as exc:
            _put(self.inbox, (FAILED, str(exc).encode("utf-8")), self.stopped)
            return
        fid = secrets.token_urlsafe(9)
        self.files[fid] = (path, inc.name)
        self.events.publish({"type": "file", "id": fid, "name": inc.name, "size": inc.size,
                             "size_h": human_size(inc.size)})
        _put(self.inbox, (SAVED, json.dumps({"name": inc.name}).encode("utf-8")), self.stopped)

    # ページから来るもの
    def page_message(self, text: str) -> None:
        data = chat._check_text(text)
        if not text.strip():
            return
        if _put(self.inbox, (MSG, data), self.stopped):
            self.events.publish({"type": "message", "mine": True, "name": self.name, "text": text,
                                 "time": time.time()})

    def page_ping(self) -> None:
        try:
            self.inbox.put_nowait((PING, b""))
        except queue.Full:  # ファイルを流している間は、DATA が届いていることが合図になる
            pass

    def page_leave(self) -> None:
        self._end_event("チャットを抜けました")  # 先に知らせる（待つ側はこの後すぐ終わることがある）
        _put(self.inbox, (BYE, b""), self.stopped)

    def page_upload(self, handler: _Handler, name: str) -> Tuple[int, str]:
        """ページから送られたファイルを一時フォルダに受け、待つ側へ申し出る（答えは出来事で知らせる）。"""
        with self._lock:
            if self._uploading:
                return 409, "前のファイルを送っています。終わってから送ってください"
            self._uploading = True
        try:
            name = chat.safe_file_name(name)
            path, size, digest = handler._receive_body(self.tmp / secrets.token_hex(6), name, self.max_size)
        except (TransferError, OSError) as exc:
            with self._lock:
                self._uploading = False
            return 400, str(exc)
        threading.Thread(target=self._offer_to_host, args=(path, name, size, digest), daemon=True).start()
        return 202, ""

    def _reply(self) -> Optional[Tuple[bytes, bytes]]:
        while not self.stopped.is_set():
            try:
                return self.replies.get(timeout=0.2)
            except queue.Empty:
                continue
        return None

    def _offer_to_host(self, path: Path, name: str, size: int, digest: str) -> None:
        def notice(text: str, kind: str = "info") -> None:
            self.events.publish({"type": "notice", "kind": kind, "text": text})

        try:
            notice(f"{name}（{human_size(size)}）を申し出ました。{self.host_name} の応答待ち")
            if not _put(self.inbox, (OFFER, json.dumps({"name": name, "size": size}).encode("utf-8")), self.stopped):
                return
            reply = self._reply()
            if reply is None:
                return
            if reply[0] == REJECT:
                notice(f"送れません: 断られました: {reply[1].decode('utf-8', 'replace')}", "error")
                return
            with path.open("rb") as f:
                while True:
                    chunk = f.read(chat.CHUNK_SIZE)
                    if not chunk:
                        break
                    if not _put(self.inbox, (DATA, chunk), self.stopped):
                        return
            _put(self.inbox, (DONE, json.dumps({"sha256": digest}).encode("utf-8")), self.stopped)
            reply = self._reply()
            if reply is None:
                return
            if reply[0] == SAVED:
                notice(f"送りました  {name}", "ok")
            else:
                notice(f"送れません: {self.host_name} が保存できませんでした: {reply[1].decode('utf-8', 'replace')}",
                       "error")
        finally:
            shutil.rmtree(str(path.parent), ignore_errors=True)
            with self._lock:
                self._uploading = False


class GuestPage:
    """相手のページ。同じ Wi-Fi に向けて出し、合言葉の合った最初の1人に鍵の cookie を渡す。"""

    COOKIE = "chat_guest"

    def __init__(self, gate: chat.WaitGate, code: str, host_name: str, bind: str, port: int,
                 on_reject: Optional[Callable[[str, str], None]] = None, keepalive: float = 10.0):
        self.gate, self.code, self.host_name = gate, code, host_name
        self.on_reject = on_reject
        self.keepalive = keepalive
        self.key = secrets.token_urlsafe(24)
        self.peer: Optional[PagePeer] = None
        self._closed = threading.Event()
        try:
            self.server = QuietServer((bind, port), self._handler())
        except OSError as exc:
            raise ChatError(f"ポート {port} が使えません（{exc.strerror or exc}）") from exc
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1},
                                        daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self.peer is not None:
            self.peer.events.close()
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        page = self

        class H(_Handler):
            def _guest(self) -> Optional[PagePeer]:
                got = self._cookie(page.COOKIE)
                if page.peer is not None and got and secrets.compare_digest(got, page.key):
                    return page.peer
                return None

            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path in ("/", "/index.html"):
                    return self._static("guest.html")
                if path.lstrip("/") in GUEST_FILES:
                    return self._static(path.lstrip("/"))
                if path == "/api/state":
                    me = self._guest()
                    return self._json(200, {"joined": me is not None, "host": page.host_name if me else "",
                                            "open": not page.gate.done.is_set(),
                                            "files": len(me.files) if me else 0})
                if not path.startswith("/api/"):
                    return self._json(404, {"error": "見つかりません"})  # favicon.ico などは数えない
                me = self._guest()
                if me is None:
                    return self._json(403, {"error": "鍵がありません。合言葉を入れてつないでください"})
                if path == "/api/events":
                    return self._stream(me.events, self._last_event_id(), page.keepalive)
                m = re.fullmatch(r"/api/file/([A-Za-z0-9_-]+)", path)
                if m and m.group(1) in me.files:
                    return self._file(*me.files[m.group(1)])
                return self._json(404, {"error": "見つかりません"})

            def _file(self, path: Path, name: str) -> None:
                try:
                    size = path.stat().st_size
                    f = path.open("rb")
                except OSError:
                    return self._json(404, {"error": "見つかりません"})
                with f:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Content-Disposition", disposition(name))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    shutil.copyfileobj(f, self.wfile, chat.CHUNK_SIZE)

            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if not self._same_origin(require=True):
                    return self._json(403, {"error": "forbidden"})
                if path == "/api/login":
                    return self._login()
                me = self._guest()
                if me is None:
                    return self._json(403, {"error": "鍵がありません。合言葉を入れてつないでください"})
                if path == "/api/upload":
                    name = (parse_qs(urlsplit(self.path).query).get("name") or [""])[0]
                    code, error = me.page_upload(self, name)
                    return self._json(code, {"error": error} if error else {"ok": True})
                try:
                    body = self._read_json()
                except ValueError as exc:
                    return self._json(415, {"error": str(exc)})
                if path == "/api/send":
                    try:
                        me.page_message(str(body.get("text", "")))
                    except ChatError as exc:
                        return self._json(400, {"error": str(exc)})
                    return self._json(200, {"ok": True})
                if path == "/api/ping":
                    me.page_ping()
                    return self._json(200, {"ok": True, "open": not me.stopped.is_set()})
                if path == "/api/leave":
                    me.page_leave()
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "見つかりません"})

            def _login(self) -> None:
                try:
                    body = self._read_json()
                except ValueError as exc:
                    return self._json(415, {"error": str(exc)})
                ip = self.client_address[0]
                if page.gate.done.is_set():
                    if page.gate.winner is None:
                        return self._json(410, {"error": "待つ側はもう待っていません"})
                    return self._json(409, {"error": "もう相手が決まりました"})
                code, name = str(body.get("code", "")).strip(), str(body.get("name", ""))
                if not code:
                    return self._json(400, {"error": "合言葉を入れてください", "field": "code"})
                if not secrets.compare_digest(code.encode("utf-8"), page.code.encode("utf-8")):
                    left = page.gate.miss()
                    reason = f"合言葉がちがいます（残り {left} 回）"
                    if page.on_reject:
                        page.on_reject(ip, reason)
                    return self._json(403, {"error": reason, "field": "code", "left": left})
                try:
                    name = chat.check_name(name)
                except ChatError as exc:
                    return self._json(400, {"error": str(exc), "field": "name"})
                peer = PagePeer(name, ip, page.host_name, page)
                if not page.gate.claim(peer):
                    peer.close_quietly()
                    return self._json(409, {"error": "もう相手が決まりました"})
                page.peer = peer
                cookie = f"{page.COOKIE}={page.key}; HttpOnly; SameSite=Strict; Path=/"
                return self._json(200, {"ok": True, "host": page.host_name}, (("Set-Cookie", cookie),))

        return H


# ---------------------------------------------------------------- ブラウザの版（127.0.0.1 の手元のページ）


_BAR = re.compile(r"[█░]+ |\[[#-]+\] ")


class WebView:
    """ChatSession の表示（chat_term.PlainView と同じ形）を、ページへの出来事にする。"""

    glyphs = chat_term.Glyphs(True)

    def __init__(self, app: "LocalApp"):
        self.app = app
        self.session: Optional[chat.ChatSession] = None
        self._status: Optional[tuple] = None

    def _pub(self, event: dict) -> None:
        self.app.events.publish(event)

    def start(self, peer_name: str, my_label: str) -> None:
        pass

    def stop(self) -> None:
        pass

    def message(self, mine: bool, name: str, text: str, when: Optional[float] = None) -> None:
        self._pub({"type": "message", "mine": mine, "name": name, "text": text,
                   "time": time.time() if when is None else when})

    def notice(self, text: str, kind: str = "info") -> None:
        self._pub({"type": "notice", "kind": kind, "text": text})

    def lines(self, rows) -> None:  # noqa: ANN001
        self._pub({"type": "lines", "rows": list(rows)})

    def progress(self, text: str, final: bool) -> None:
        m = re.search(r"(\d+)%", text)
        self._pub({"type": "progress", "text": _BAR.sub("", text), "pct": int(m.group(1)) if m else 0,
                   "final": final})

    def status(self, st: dict) -> None:
        key = (st.get("state"), int(st.get("elapsed", 0) // 60), int(st.get("silent", 0)) if st.get("state") == "quiet"
               else 0)
        if key == self._status:
            return
        self._status = key
        self._pub({"type": "status", "state": st.get("state"), "name": st.get("name", ""), "ip": st.get("ip", ""),
                   "minutes": key[1], "silent": key[2], "version": st.get("version", 1)})

    def set_prompt(self, prompt: Optional[str]) -> None:
        offer = getattr(self.session, "_offer", None) if prompt else None
        if offer is not None:
            self._pub({"type": "ask", "name": offer.name, "size": offer.size, "size_h": human_size(offer.size)})
        else:
            self._pub({"type": "ask_done"})


class LocalApp:
    """ブラウザの版。127.0.0.1 にページを出し、つなぐ・待つ・話す・ファイルを送る を受け持つ。"""

    def __init__(self, name: str, chat_port: int = chat.DEFAULT_PORT, save_dir: Path = chat.DEFAULT_SAVE_DIR,
                 max_size: int = chat.DEFAULT_MAX_FILE_SIZE, state: Optional[dict] = None,
                 state_path: Optional[Path] = None, no_page: bool = False,
                 open_browser: Callable[[str], object] = webbrowser.open,
                 opener: Callable[[Path], None] = chat.open_folder,
                 grace: float = PAGE_GRACE, first_grace: float = FIRST_GRACE, keepalive: float = KEEPALIVE,
                 bind: str = "0.0.0.0", page_bind: Optional[str] = None, session_args: Optional[dict] = None,
                 code: str = ""):
        self.name, self.chat_port, self.save_dir, self.max_size = name, chat_port, save_dir, max_size
        self.state = state if state is not None else {}
        self.state_path = state_path
        self.no_page, self.open_browser, self.opener = no_page, open_browser, opener
        self.grace, self.first_grace, self.keepalive = grace, first_grace, keepalive
        self.bind, self.page_bind = bind, page_bind
        self.session_args = session_args or {}
        self.fixed_code = code
        self.key = secrets.token_urlsafe(18)
        self.events = EventLog()
        self.phase = "idle"            # idle・waiting・chat
        self.session: Optional[chat.ChatSession] = None
        self.peer = None
        self.peer_ip = ""
        self.gate: Optional[chat.WaitGate] = None
        self.wait_info: dict = {}
        self.done = threading.Event()
        self.reason = ""
        self.tmp = Path(tempfile.mkdtemp(prefix="chat_app_web_"))
        self._lock = threading.RLock()
        self._streams = 0
        self._ever = False
        self._last_seen = time.monotonic()
        self.server = QuietServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?k={self.key}"
        self.cookie = f"chat_key_{self.port}"

    # 始める・終える
    def start(self, open_page: bool = True) -> None:
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        if open_page:
            try:
                self.open_browser(self.url)
            except Exception:  # ブラウザを開けなくても、URL を端末に出してあるので続ける
                pass

    def _watch(self) -> None:
        tick = max(0.02, min(0.5, self.grace / 5))
        while not self.done.wait(tick):
            with self._lock:
                if self._streams > 0:
                    self._last_seen = time.monotonic()
                    continue
                limit = self.grace if self._ever else self.first_grace
                quiet = time.monotonic() - self._last_seen
            if quiet >= limit:
                self.stop("ページが閉じられたので終わります")
                return

    def stop(self, reason: str = "") -> None:
        with self._lock:
            if self.done.is_set():
                return
            self.reason = reason
            self.done.set()
        self._end_chat(publish=False)
        if self.gate is not None:
            self.gate.cancel("終わりました")
        self.events.publish({"type": "quit", "text": reason or "終わりました"})
        self.events.close()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _set_phase(self, phase: str) -> None:
        self.phase = phase
        self.events.publish({"type": "phase", **self._state()})

    def _state(self) -> dict:
        peer = None
        if self.phase == "chat" and self.session is not None:
            peer = {"name": self.peer.name, "ip": self.session.address, "framed": self.session.framed}
        return {"phase": self.phase, "peer": peer, "wait": self.wait_info if self.phase == "waiting" else None,
                "sending": bool(self.session is not None and self.session._sending)}

    def _remember(self, **values: str) -> None:
        self.state.update(values)
        if self.state_path is not None:
            chat_term.save_state(self.state_path, self.state)

    # つなぐ・待つ
    def join(self, ip: str, code: str, name: str) -> Tuple[int, dict]:
        with self._lock:
            if self.phase != "idle":
                return 409, {"error": "いまはつなげません（待っているか、話している途中です）"}
            self.phase = "joining"
        try:
            if not ip.strip():
                return 400, {"error": "相手の IP を入れてください", "field": "ip"}
            if not code.strip():
                return 400, {"error": "合言葉を入れてください", "field": "code"}
            try:
                my_name = chat.check_name(name)
            except ChatError as exc:
                return 400, {"error": str(exc), "field": "name"}
            try:
                peer = chat.join(ip.strip(), code.strip(), my_name, default_port=self.chat_port)
            except chat.Rejected as exc:
                return 403, {"error": str(exc), "field": "code"}
            except ChatError as exc:
                return 400, {"error": str(exc), "field": "ip"}
            self.name = my_name
            self._remember(ip=ip.strip(), name=my_name, peer_name=peer.name)
            self._start_session(peer)
            return 200, {"ok": True}
        finally:
            with self._lock:
                if self.phase == "joining":
                    self.phase = "idle"

    def host(self, code: str, name: str) -> Tuple[int, dict]:
        try:
            my_name = chat.check_name(name)
        except ChatError as exc:
            return 400, {"error": str(exc), "field": "name"}
        code = code.strip() or self.fixed_code
        if code and not (len(code) == 4 and code.isascii() and code.isdigit()):
            return 400, {"error": "合言葉は 4 桁の数字にしてください（空ならランダム）", "field": "code"}
        code = code or chat.new_code()
        with self._lock:
            if self.phase != "idle":
                return 409, {"error": "いまは待てません"}
            self.phase = "starting"
            gate = self.gate = chat.WaitGate()
        self.name = my_name
        self._remember(name=my_name)
        ready = threading.Event()
        result: dict = {}
        page: dict = {}

        def on_page(port: Optional[int], error: str) -> None:
            page["url"] = f"http://{chat.lan_ip()}:{port}" if port else ""
            page["error"] = error

        def on_ready(port: int) -> None:
            ip = chat.lan_ip() + ("" if port == chat.DEFAULT_PORT else f":{port}")
            self.wait_info = {"ip": ip, "code": code, "port": port, "page_url": page.get("url", ""),
                              "page_error": page.get("error", "")}
            self._set_phase("waiting")
            ready.set()

        def on_reject(ip: str, reason: str) -> None:
            self.events.publish({"type": "notice", "kind": "error", "text": f"{ip}: {reason}"})

        def run() -> None:
            try:
                peer = chat.host_wait(code, my_name, port=self.chat_port, bind=self.bind, on_ready=on_ready,
                                      on_reject=on_reject, page_port=None if self.no_page else self.chat_port + 1,
                                      on_page=on_page, gate=gate, page_bind=self.page_bind)
            except ChatError as exc:
                result["error"] = str(exc)
                with self._lock:
                    self.gate = None
                if not self.done.is_set():
                    self.events.publish({"type": "notice", "kind": "info" if gate.error == "待つのをやめました"
                                         else "error", "text": str(exc)})
                    self._set_phase("idle")
                ready.set()
                return
            with self._lock:
                self.gate = None
                if self.done.is_set():
                    peer.close()
                    return
            self._start_session(peer)

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)
        if "error" in result:
            return 400, {"error": result["error"]}
        return 200, {"ok": True, "wait": self.wait_info}

    def cancel(self) -> None:
        with self._lock:
            gate = self.gate
        if gate is not None:
            gate.cancel("待つのをやめました")

    def _start_session(self, peer) -> None:  # noqa: ANN001
        view = WebView(self)
        session = chat.ChatSession(peer, view, save_dir=self.save_dir, max_size=self.max_size,
                                   on_closed=self._on_closed, opener=self.opener, **self.session_args)
        view.session = session
        with self._lock:
            self.session, self.peer = session, peer
        self._set_phase("chat")
        session.start()

    def _on_closed(self) -> None:
        """相手が抜けた・切れた（ChatSession が知らせを出した後）。つなぐ欄に戻る。"""
        self._end_chat(publish=True, notice="")

    def _end_chat(self, publish: bool, notice: str = "") -> None:
        with self._lock:
            session, peer = self.session, self.peer
            self.session = self.peer = None
        if session is not None:
            session.quit()
        if peer is not None:
            peer.close()
        if publish and session is not None:
            if notice:
                self.events.publish({"type": "notice", "kind": "info", "text": notice})
            self._set_phase("idle")

    # 話す
    def send(self, text: str) -> Tuple[int, dict]:
        session = self.session
        if session is None:
            return 409, {"error": "つながっていません"}
        if not session.handle_input(text):  # /quit
            self._end_chat(publish=True, notice="チャットを抜けました")
        return 200, {"ok": True}

    def answer(self, accept: bool) -> Tuple[int, dict]:
        session = self.session
        if session is None or session._offer is None:
            return 409, {"error": "答える申し出がありません"}
        session.answer_offer(accept)
        return 200, {"ok": True}

    def upload(self, handler: _Handler, name: str) -> Tuple[int, dict]:
        session = self.session
        if session is None:
            return 409, {"error": "つながっていません"}
        if not session.framed:
            session.ui.notice("相手が古いバージョンなので使えません。ファイルはターミナルの版の「ファイルを送る」で送ってください",
                              "error")
            return 409, {"error": "相手が古いバージョンなので使えません"}
        if session._sending:
            return 409, {"error": "前のファイルを送っています。終わってから送ってください"}
        try:
            name = chat.safe_file_name(name)
            path, _, _ = handler._receive_body(self.tmp / secrets.token_hex(6), name, self.max_size)
        except (TransferError, OSError) as exc:
            return 400, {"error": str(exc)}
        worker = session.send_path(str(path))

        def clean() -> None:
            if worker is not None:
                worker.join()
            shutil.rmtree(str(path.parent), ignore_errors=True)

        threading.Thread(target=clean, daemon=True).start()
        if worker is None:
            return 409, {"error": "送れませんでした"}
        return 200, {"ok": True}

    def open_save_dir(self) -> None:
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.opener(self.save_dir)

    # ページの見張り
    def _stream_open(self) -> None:
        with self._lock:
            self._streams += 1
            self._ever = True

    def _stream_close(self) -> None:
        with self._lock:
            self._streams -= 1
            self._last_seen = time.monotonic()

    # HTTP
    def _handler(self):
        app = self

        class H(_Handler):
            csp = LOCAL_CSP

            def _allowed(self, post: bool) -> bool:
                host = self.headers.get("Host", "")
                if host not in (f"127.0.0.1:{app.port}", f"localhost:{app.port}"):
                    return False  # DNS rebinding を防ぐ
                if not self._same_origin(require=post):
                    return False
                if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                    return False
                q = parse_qs(urlsplit(self.path).query)
                for got in ((q.get("k") or [""])[0], self.headers.get("X-Chat-Key", ""), self._cookie(app.cookie)):
                    if got and secrets.compare_digest(got.encode("utf-8"), app.key.encode("utf-8")):
                        return True
                return False

            def do_GET(self) -> None:  # noqa: N802
                if not self._allowed(post=False):
                    return self._json(403, {"error": "forbidden"})
                path = urlsplit(self.path).path
                if path in ("/", "/index.html"):
                    cookie = f"{app.cookie}={app.key}; HttpOnly; SameSite=Strict; Path=/"
                    try:
                        body = (WEB_DIR / "local.html").read_bytes()
                    except OSError:
                        return self._json(500, {"error": "local.html がありません"})
                    return self._send(200, body, STATIC_TYPES[".html"], (
                        ("Set-Cookie", cookie),
                        ("Content-Security-Policy", LOCAL_CSP)))
                if path.lstrip("/") in LOCAL_FILES:
                    return self._static(path.lstrip("/"))
                if path == "/api/state":
                    with app._lock:
                        st = app._state()
                    st.update(defaults={"ip": app.state.get("ip", ""), "name": app.name,
                                        "peer_name": app.state.get("peer_name", ""), "code": app.fixed_code},
                              last_event=app.events.next_id - 1)
                    return self._json(200, st)
                if path == "/api/events":
                    return self._stream(app.events, self._last_event_id(), app.keepalive,
                                        app._stream_open, app._stream_close)
                return self._json(404, {"error": "見つかりません"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._allowed(post=True):
                    return self._json(403, {"error": "forbidden"})
                path = urlsplit(self.path).path
                if path == "/api/upload":
                    name = (parse_qs(urlsplit(self.path).query).get("name") or [""])[0]
                    return self._json(*app.upload(self, name))
                try:
                    body = self._read_json()
                except ValueError as exc:
                    return self._json(415, {"error": str(exc)})
                if path == "/api/join":
                    return self._json(*app.join(str(body.get("ip", "")), str(body.get("code", "")),
                                                str(body.get("name", ""))))
                if path == "/api/host":
                    return self._json(*app.host(str(body.get("code", "")), str(body.get("name", ""))))
                if path == "/api/cancel":
                    app.cancel()
                    return self._json(200, {"ok": True})
                if path == "/api/send":
                    return self._json(*app.send(str(body.get("text", ""))))
                if path == "/api/answer":
                    return self._json(*app.answer(bool(body.get("accept"))))
                if path == "/api/open":
                    app.open_save_dir()
                    return self._json(200, {"ok": True})
                if path == "/api/leave":
                    app._end_chat(publish=True, notice="チャットを抜けました")
                    return self._json(200, {"ok": True})
                if path == "/api/quit":
                    self._json(200, {"ok": True})
                    threading.Thread(target=app.stop, args=("ページの「終わる」で終わりました",), daemon=True).start()
                    return None
                return self._json(404, {"error": "見つかりません"})

        return H


def run_local(args, name: str, state: dict) -> int:  # noqa: ANN001
    """chat.py --web（メニューの 5）。ページを開き、閉じられるか Ctrl+C まで動く。"""
    app = LocalApp(name, chat_port=args.port, save_dir=Path(args.save_dir).expanduser().resolve(),
                   max_size=args.max_size * 1024**2, state=state, state_path=chat.STATE_PATH,
                   no_page=args.no_page, code=args.code or "")
    app.start()
    view = chat_term.PlainView(sys.stdout)
    view.notice(f"ブラウザの版を開きました: {app.url}")
    view.line("ブラウザが開かなければ、上の URL を開いてください。止める: Ctrl+C（ページを閉じても終わります）")
    try:
        while not app.done.wait(0.2):
            pass
    except KeyboardInterrupt:
        app.stop("Ctrl+C で終わりました")
        raise
    view.notice(app.reason or "終わりました")
    return 0
