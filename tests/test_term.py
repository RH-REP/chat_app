"""chat_term.py（表示と入力）の、端末に触らない部分のテスト。

  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chat  # noqa: E402
import chat_term as ct  # noqa: E402


class FakeTTY(io.StringIO):
    def __init__(self, tty: bool = True, encoding: str = "utf-8"):
        super().__init__()
        self._tty, self._enc = tty, encoding

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self):  # type: ignore[override]
        return self._enc


class WidthTest(unittest.TestCase):
    def test_japanese_counts_two(self):
        self.assertEqual(ct.text_width("abc"), 3)
        self.assertEqual(ct.text_width("合言葉"), 6)
        self.assertEqual(ct.text_width("ｱｲｳ"), 3)        # 半角カナは 1
        self.assertEqual(ct.text_width("e\u0301"), 1)    # 結合文字は 0
        self.assertEqual(ct.truncate("あいう", 5), "あい")
        self.assertEqual(ct.pad("あ", 4), "あ  ")

    def test_clean_removes_escape(self):
        self.assertEqual(ct.clean("a\x1b[2Jb\x9bc"), "a?[2Jb?c")


class BoxTest(unittest.TestCase):
    def rows(self):
        return ["チャットを待っています"] + ct.label_rows([("IP", "192.168.0.9"), ("合言葉", ct.spaced_code("4821"))])

    def test_right_edge_lines_up_with_japanese(self):
        lines = ct.box_lines(self.rows(), unicode=True)
        widths = {ct.text_width(l) for l in lines}
        self.assertEqual(len(widths), 1, lines)
        self.assertTrue(lines[0].startswith("┌") and lines[0].endswith("┐"))
        self.assertTrue(all(l.endswith("│") for l in lines[1:-1]))
        self.assertIn("4 8 2 1", lines[3])

    def test_ascii_fallback(self):
        lines = ct.box_lines(self.rows(), unicode=False)
        self.assertEqual(len({ct.text_width(l) for l in lines}), 1)
        self.assertTrue(set(lines[0]) == {"-"})
        self.assertTrue(lines[1].startswith("|") and lines[1].endswith("|"))
        self.assertTrue(all(l.isascii() or "|" in l for l in lines))

    def test_banner_has_command_line(self):
        lines = ct.wait_banner("チャットを待っています", "192.168.0.9", "4821",
                               "python3 chat.py --join 192.168.0.9 --code 4821", True)
        self.assertEqual(lines[-2], "相手の画面で: python3 chat.py --join 192.168.0.9 --code 4821")
        self.assertIn("Ctrl+C", lines[-1])

    def test_can_draw(self):
        self.assertTrue(ct.can_draw(FakeTTY(encoding="utf-8")))
        self.assertFalse(ct.can_draw(FakeTTY(encoding="cp1252")))
        self.assertFalse(ct.can_draw(FakeTTY(encoding="ascii")))
        self.assertTrue(ct.can_draw(io.StringIO()))  # encoding の無いものは書けるとみなす


class MenuTest(unittest.TestCase):
    ITEMS = [(k, label, desc) for k, label, desc, _ in chat.MENU]
    KEYS = [k for k, *_ in chat.MENU]

    def test_lines_are_aligned(self):
        lines = ct.menu_lines(self.ITEMS)
        self.assertEqual(lines[0], "chat_app  何をしますか")
        cols = {ct.text_width(l[:l.index(desc)]) for l, (_, _, desc) in zip(lines[1:], self.ITEMS)}
        self.assertEqual(len(cols), 1, lines)

    def test_choice_and_default(self):
        self.assertEqual(ct.parse_menu_choice("3", self.KEYS, None), "3")
        self.assertEqual(ct.parse_menu_choice(" 4 ", self.KEYS, "2"), "4")
        self.assertEqual(ct.parse_menu_choice("", self.KEYS, "2"), "2")     # Enter で前回
        self.assertIsNone(ct.parse_menu_choice("", self.KEYS, None))
        self.assertIsNone(ct.parse_menu_choice("9", self.KEYS, "2"))
        self.assertEqual(ct.menu_prompt("2"), "番号 [Enter で 2] > ")
        self.assertEqual(ct.menu_prompt(None), "番号 > ")

    def test_ask_menu_remembers_choice(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            with mock.patch.object(chat, "STATE_PATH", path), mock.patch("sys.stdout", io.StringIO()) as out:
                with mock.patch("builtins.input", side_effect=["x", "3"]):
                    self.assertEqual(chat.ask_menu({}), "receive")
                state = ct.load_state(path)
                self.assertEqual(state["menu"], "3")
                with mock.patch("builtins.input", side_effect=[""]) as ask:
                    self.assertEqual(chat.ask_menu(state), "receive")
                ask.assert_called_with("番号 [Enter で 3] > ")
            self.assertIn("4  ファイルを送る", out.getvalue())

    def test_menu_is_extendable(self):
        extra = chat.MENU + [("5", "ブラウザで使う", "ページで使う", "web")]
        with mock.patch.object(chat, "MENU", extra), mock.patch.object(chat, "STATE_PATH", Path(os.devnull) / "x"), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("builtins.input", side_effect=["5"]):
            self.assertEqual(chat.ask_menu({}), "web")


class StateTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".chat_state.json"
            self.assertEqual(ct.load_state(path), {})
            self.assertTrue(ct.save_state(path, {"ip": "192.168.0.9", "name": "あ"}))
            self.assertEqual(ct.load_state(path), {"ip": "192.168.0.9", "name": "あ"})

    def test_broken_or_unwritable_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            path.write_text("{壊れた", encoding="utf-8")
            self.assertEqual(ct.load_state(path), {})
            path.write_text("[1, 2]", encoding="utf-8")
            self.assertEqual(ct.load_state(path), {})
            self.assertFalse(ct.save_state(Path(d) / "無い" / "s.json", {"a": 1}))

    def test_join_prompt_uses_last_ip_and_never_stores_code(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            state: dict = {}
            with mock.patch.object(chat, "STATE_PATH", path):
                chat._remember_join(state, "192.168.0.9", "bob", "alice")
            saved = ct.load_state(path)
            self.assertEqual(saved, {"ip": "192.168.0.9", "name": "bob", "peer_name": "alice"})
            with mock.patch("builtins.input", side_effect=[""]) as ask:
                self.assertEqual(chat._ask_address(saved), "192.168.0.9")
            ask.assert_called_with("相手の IP [Enter で 192.168.0.9（alice）] > ")

    def test_gitignore_has_state_file(self):
        self.assertIn(".chat_state.json", (ROOT / ".gitignore").read_text(encoding="utf-8").split())


class ColorTest(unittest.TestCase):
    def test_tty_gets_color(self):
        self.assertTrue(ct.color_supported(FakeTTY(), env={}, platform="darwin"))
        self.assertTrue(ct.color_supported(FakeTTY(), env={"NO_COLOR": ""}, platform="linux"))  # 空は無いのと同じ

    def test_no_color_cases(self):
        self.assertFalse(ct.color_supported(FakeTTY(tty=False), env={}, platform="darwin"))
        self.assertFalse(ct.color_supported(io.StringIO(), env={}, platform="darwin"))
        self.assertFalse(ct.color_supported(FakeTTY(), env={"NO_COLOR": "1"}, platform="darwin"))
        self.assertFalse(ct.color_supported(FakeTTY(), env={"TERM": "dumb"}, platform="linux"))

    def test_windows_depends_on_vt(self):
        self.assertTrue(ct.color_supported(FakeTTY(), env={}, platform="win32", enable_vt=lambda s: True))
        self.assertFalse(ct.color_supported(FakeTTY(), env={}, platform="win32", enable_vt=lambda s: False))
        self.assertFalse(ct.vt_supported(FakeTTY(), env={}, platform="win32", enable_vt=lambda s: False))

    def test_enable_vt_never_raises_off_windows(self):
        if sys.platform != "win32":
            self.assertFalse(ct.enable_windows_vt())

    def test_plain_view_marks(self):
        out = io.StringIO()
        v = ct.PlainView(out, color=False, unicode=True)
        v.start("alice", "自分")
        v.notice("つながりました")
        v.notice("送れません", "error")
        v.message(True, "自分", "やあ", when=0)
        v.message(False, "alice", "\x1b[31m赤", when=0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "● つながりました")
        self.assertEqual(lines[1], "✕ 送れません")
        self.assertRegex(lines[2], r"^\d\d:\d\d 自分  › やあ$")    # 名前の幅を揃える
        self.assertRegex(lines[3], r"^\d\d:\d\d alice › \?\[31m赤$")  # 相手の ESC は消す
        self.assertNotIn("\x1b", out.getvalue())

    def test_colored_and_ascii_view(self):
        out = io.StringIO()
        v = ct.PlainView(out, color=True, unicode=False)
        v.start("a", "自分")
        v.notice("x", "error")
        v.message(True, "自分", "m", when=0)
        self.assertIn(ct.RED + "x x" + ct.RESET, out.getvalue())
        self.assertIn(ct.GREEN, out.getvalue())
        self.assertIn("自分 >", out.getvalue())  # UTF-8 でなければ › の代わりに >


class ProgressTest(unittest.TestCase):
    def test_bar_styles(self):
        line = ct.format_progress("受信中 a", 60, 100, 5 * 1024**2, True)
        self.assertIn("████████████░░░░░░░░", line)
        self.assertIn(" 60%", line)
        self.assertIn("残り 1 秒未満", line)
        self.assertIn("[############--------]", ct.format_progress("x", 60, 100, 1, False))
        self.assertNotIn("残り", ct.format_progress("x", 100, 100, 1, True))

    def test_remaining(self):
        self.assertEqual(ct.format_remaining(0.3), "残り 1 秒未満")
        self.assertEqual(ct.format_remaining(2.1), "残り 3 秒")
        self.assertEqual(ct.format_remaining(150), "残り 3 分")


class LineEditorTest(unittest.TestCase):
    def test_edit_keys(self):
        e = ct.LineEditor()
        for ev in ct.decode_keys("明日の会議"):
            e.edit(*ev)
        e.edit("left"); e.edit("left")
        e.edit("text", "X")
        self.assertEqual(e.text, "明日のX会議")
        e.edit("backspace")
        e.edit("home"); e.edit("delete")
        self.assertEqual(e.text, "日の会議")
        e.edit("end"); e.edit("text", "は")
        self.assertEqual((e.text, e.pos), ("日の会議は", 5))
        e.edit("text", "a\x07b")  # 制御文字は入れない
        self.assertEqual(e.text, "日の会議はab")
        self.assertEqual(e.take(), "日の会議はab")
        self.assertEqual((e.text, e.pos), ("", 0))

    def test_view_scrolls_with_wide_chars(self):
        e = ct.LineEditor("あいうえおかきくけこ")  # 幅 20
        visible, col = e.view(10)
        self.assertLessEqual(ct.text_width(visible), 10)
        self.assertTrue(visible.endswith("こ"))
        self.assertEqual(col, ct.text_width(visible))
        e.home()
        visible, col = e.view(10)
        self.assertEqual((visible, col), ("あいうえお", 0))
        self.assertEqual(ct.LineEditor("ab").view(10), ("ab", 2))


class DecodeKeysTest(unittest.TestCase):
    def test_posix_sequences(self):
        self.assertEqual(ct.decode_keys("\x1b[A\x1b[B\x1b[C\x1b[D\x1b[H\x1b[F\x1b[3~\x1b[Z\t"),
                         [("up", ""), ("down", ""), ("right", ""), ("left", ""), ("home", ""), ("end", ""),
                          ("delete", ""), ("shift_tab", ""), ("tab", "")])
        self.assertEqual(ct.decode_keys("\x1bOA\x1b[1~\x1b[4~"), [("up", ""), ("home", ""), ("end", "")])
        self.assertEqual(ct.decode_keys("\x1b"), [("esc", "")])
        self.assertEqual(ct.decode_keys("\x7f\x08\x03\x04\x15"),
                         [("backspace", ""), ("backspace", ""), ("ctrl_c", ""), ("ctrl_d", ""), ("ctrl_u", "")])

    def test_paste_arrives_at_once(self):
        self.assertEqual(ct.decode_keys("こんにちは\r\nまた\rね"),
                         [("text", "こんにちは"), ("enter", ""), ("text", "また"), ("enter", ""), ("text", "ね")])

    def test_windows_scan_codes(self):
        self.assertEqual(ct.decode_windows_keys("ab\xe0K\xe0Hc\x00\x0f\r\x08"),
                         [("text", "ab"), ("left", ""), ("up", ""), ("text", "c"), ("shift_tab", ""),
                          ("enter", ""), ("backspace", "")])
        self.assertEqual(ct.decode_windows_keys("voilà"), [("text", "voilà")])  # à だけなら字
        self.assertEqual(ct.decode_windows_keys("\x1a"), [("ctrl_d", "")])


class JoinFormTest(unittest.TestCase):
    def form(self, **kw):
        return ct.JoinForm("チャットにつなぐ", **kw)

    def test_focus_starts_at_first_empty(self):
        self.assertEqual(self.form().key, "ip")
        self.assertEqual(self.form(ip="192.168.0.9", name="bob").key, "code")

    def test_tab_and_arrows_wrap(self):
        f = self.form()
        seen = []
        for k in ("tab", "tab", "tab", "shift_tab", "up", "down"):
            f.handle(k)
            seen.append(f.key)
        self.assertEqual(seen, ["code", "name", "ip", "name", "code", "name"])

    def test_typing_goes_to_focused_field(self):
        f = self.form(name="bob")
        for ev in ct.decode_keys("192.168.0.9\t4821"):
            self.assertIsNone(f.handle(*ev))
        self.assertEqual(f.values(), {"ip": "192.168.0.9", "code": "4821", "name": "bob"})
        self.assertEqual(f.handle("enter"), "submit")

    def test_enter_with_empty_field_moves_there(self):
        f = self.form(ip="192.168.0.9")
        f.handle("tab"); f.handle("tab")  # 名前の欄
        self.assertIsNone(f.handle("enter"))
        self.assertEqual(f.key, "code")
        self.assertIn("合言葉を入れてください", f.message)

    def test_fail_returns_to_the_wrong_field_and_keeps_others(self):
        f = self.form(ip="192.168.0.19", code="4821", name="bob")
        f.fail("code", "断られました: 合言葉がちがいます")
        self.assertEqual((f.key, f.values()["code"]), ("code", ""))
        self.assertEqual(f.values()["ip"], "192.168.0.19")
        f.editors["code"].insert("1111")
        f.fail("ip", "つながりません")
        self.assertEqual(f.key, "ip")
        self.assertEqual(f.values(), {"ip": "192.168.0.19", "code": "1111", "name": "bob"})
        self.assertEqual(f.editors["ip"].pos, len("192.168.0.19"))  # カーソルは末尾。打ち直しは要らない

    def test_esc_and_ctrl_c(self):
        self.assertEqual(self.form().handle("esc"), "back")
        self.assertEqual(self.form().handle("ctrl_c"), "quit")

    def test_render(self):
        f = self.form(ip="192.168.0.9", name="bob")
        lines, row, col = f.render(ct.Glyphs(True))
        self.assertEqual(lines[0], "チャットにつなぐ")
        self.assertIn("› 合言葉", lines[2])
        self.assertEqual(row, 2)
        self.assertEqual(col, ct.text_width(lines[2]))
        self.assertIn("Esc 戻る", lines[-1])
        f.fail("code", "断られました")
        self.assertEqual(f.render(ct.Glyphs(False))[0][-2], "x 断られました")


class FakeKeys:
    def __init__(self, text: str):
        self.events = [[ev] for ev in ct.decode_keys(text)]

    def read(self, timeout: float):
        return self.events.pop(0) if self.events else [("esc", "")]


class RunFormTest(unittest.TestCase):
    def test_failure_then_success(self):
        calls = []

        def connect(values):
            calls.append(dict(values))
            if values["code"] != "4821":
                raise ct.FormFailure("code", "断られました: 合言葉がちがいます")
            return "つながった"

        out = io.StringIO()
        form = ct.JoinForm("チャットにつなぐ", ip="127.0.0.1", name="bob")
        result = ct.run_form(form, FakeKeys("0000\r4821\r"), out, connect, unicode=True, color=False)
        self.assertEqual(result, "つながった")
        self.assertEqual([c["code"] for c in calls], ["0000", "4821"])
        self.assertIn("✕ 断られました", out.getvalue())

    def test_esc_goes_back(self):
        out = io.StringIO()
        self.assertIsNone(ct.run_form(ct.JoinForm("t"), FakeKeys("\x1b"), out, lambda v: 1, True, False))


class TerminalViewTest(unittest.TestCase):
    """端末の描き直しの列を、偽の端末（StringIO）で確かめる。"""

    def view(self, rows=24):
        out = FakeTTY()
        size = lambda: os.terminal_size((60, rows))  # noqa: E731
        return ct.TerminalView(out, color=False, unicode=True, size=size), out

    def test_status_region_set_and_reset(self):
        v, out = self.view()
        v.start("alice", "自分")
        self.assertIn("\x1b[2;24r", out.getvalue())
        v.status({"name": "alice", "ip": "192.168.0.9", "elapsed": 720, "state": "ok"})
        self.assertIn("● alice  192.168.0.9  つないで 12 分  /help  /quit", out.getvalue())
        v.stop()
        self.assertIn("\x1b[r", out.getvalue())  # スクロール範囲を戻す

    def test_small_terminal_has_no_status(self):
        v, out = self.view(rows=3)
        v.start("alice", "自分")
        self.assertNotRegex(out.getvalue(), r"\x1b\[2;\d+r")

    def test_incoming_line_redraws_the_typed_text(self):
        v, out = self.view()
        v.start("alice", "自分")
        v.editor.insert("明日の会議は")
        v.message(False, "alice", "何時からだっけ", when=0)
        tail = out.getvalue().rsplit("何時からだっけ", 1)[1]
        self.assertIn("› 明日の会議は", tail)  # 打ちかけの字を入力の行に描き直す

    def test_progress_line_is_replaced(self):
        v, out = self.view()
        v.start("alice", "自分")
        v.progress("受信中 a 10%", False)
        v.progress("受信中 a 50%", False)
        self.assertIn("\x1b[1A", out.getvalue())
        v.progress("受信中 a 100%", True)
        self.assertIsNone(v.live)

    def test_status_kinds(self):
        g = ct.Glyphs(True)
        self.assertEqual(ct.format_status({"name": "a", "state": "quiet", "silent": 21.4}, g),
                         (" ◐ a  返事が 21 秒ありません … ", "quiet"))
        self.assertEqual(ct.format_status({"name": "a", "state": "lost"}, g)[1], "lost")
        self.assertIn("✕", ct.format_status({"name": "a", "state": "lost"}, g)[0])


if __name__ == "__main__":
    unittest.main()
