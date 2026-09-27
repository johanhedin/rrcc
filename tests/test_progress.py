"""Tests for the progress line that rrcc shows on stderr when that is a
terminal."""

import io
import os
import pty
import subprocess
import sys
import threading
import unittest

from support import RRCC, TestCase, load_rrcc
from support.repo import standard_repo
from support.servers import StaticServer


class FakeTerminal(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


def text_of(segments):
    return "".join(text for text, _ in segments)


class FormatTest(unittest.TestCase):
    def test_format_size(self):
        rrcc = load_rrcc()
        self.assertEqual(rrcc.format_size(0), "0 B")
        self.assertEqual(rrcc.format_size(1023), "1023 B")
        self.assertEqual(rrcc.format_size(1536), "1.5 KiB")
        self.assertEqual(rrcc.format_size(5 * 1024**3), "5.0 GiB")
        self.assertEqual(rrcc.format_size(3 * 1024**5), "3072.0 TiB")

    def test_format_duration(self):
        rrcc = load_rrcc()
        self.assertEqual(rrcc.format_duration(0), "0:00")
        self.assertEqual(rrcc.format_duration(75.9), "1:15")
        self.assertEqual(rrcc.format_duration(3 * 3600 + 5), "3:00:05")

    def test_terminal_can_color(self):
        can = load_rrcc().terminal_can_color
        self.assertTrue(can({"TERM": "xterm-256color"}))
        self.assertFalse(can({"TERM": "dumb"}))
        self.assertFalse(can({}))
        self.assertFalse(can({"TERM": "xterm", "NO_COLOR": "1"}))
        self.assertTrue(can({"TERM": "xterm", "NO_COLOR": ""}))
        self.assertTrue(can({"TERM": "dumb", "FORCE_COLOR": "1"}))
        self.assertFalse(can({"TERM": "xterm", "FORCE_COLOR": "1", "PYTHON_COLORS": "0"}))
        self.assertTrue(can({"NO_COLOR": "1", "PYTHON_COLORS": "1"}))


class RenderTest(unittest.TestCase):
    LABEL = "/srv/mirror/rocky/9/BaseOS/x86_64/os"

    def progress(self, unicode, color, by_bytes=True, done=0.4):
        rrcc = load_rrcc()
        p = rrcc.Progress(stream=FakeTerminal(), unicode=unicode, color=color)
        p._reset(self.LABEL)
        p.count("packages", 1000, 5 * 1024**3 if by_bytes else None)
        p.counting_since -= 10
        p.advance(int(1000 * done), int(5 * 1024**3 * done))
        return p

    def render(self, p, width):
        return text_of(p.render(width, load_rrcc().time.monotonic()))

    def test_fits_any_width(self):
        for unicode, color in ((True, True), (True, False), (False, False)):
            for by_bytes in (True, False):
                p = self.progress(unicode, color, by_bytes)
                for width in range(0, 200):
                    with self.subTest(unicode=unicode, color=color, by_bytes=by_bytes, width=width):
                        self.assertLessEqual(len(self.render(p, width)), max(width, 2))

    def test_wide(self):
        line = self.render(self.progress(False, False), 200)
        self.assertTrue(line.startswith(f"| {self.LABEL}: packages  [############"), line)
        self.assertIn("]  40% 400/1000 2.0 GiB/5.0 GiB 204.8 MiB/s 0:15 left", line)

    def test_narrow_keeps_the_most_useful(self):
        line = self.render(self.progress(False, False), 60)
        self.assertEqual(line, "| .../BaseOS/x86_64/os: packages  40% 400/1000 0:15 left")

    def test_by_items(self):
        line = self.render(self.progress(True, False, by_bytes=False), 200)
        self.assertIn(" 40% 400/1000 40/s 0:15 left", line)
        self.assertNotIn("GiB", line)

    def test_done(self):
        line = self.render(self.progress(True, True, done=1), 200)
        self.assertIn("100% 1000/1000", line)
        self.assertNotIn("left", line)

    def test_status_only(self):
        p = load_rrcc().Progress(stream=FakeTerminal(), unicode=True, color=False)
        p._reset(self.LABEL)
        p.status("reading metadata")
        self.assertEqual(self.render(p, 80), f"⠋ {self.LABEL}: reading metadata")

    def test_bars(self):
        rrcc = load_rrcc()
        ascii_bar = rrcc.Progress(unicode=False, color=False)._bar(0.5, 10)
        self.assertEqual(text_of(ascii_bar), "[#####-----]")
        block_bar = rrcc.Progress(unicode=True, color=False)._bar(0.55, 10)
        self.assertEqual(text_of(block_bar), "│█████▌    │")
        line_bar = rrcc.Progress(unicode=True, color=True)._bar(0.55, 10)
        self.assertEqual(text_of(line_bar), "━" * 5 + "╸" + "━" * 4)


class DrawTest(unittest.TestCase):
    def test_disabled_writes_nothing(self):
        rrcc = load_rrcc()
        term = FakeTerminal()
        p = rrcc.Progress(stream=term, enabled=False, delay=0)
        p.start("repo")
        p.count("packages", 10)
        p.advance(5)
        p.stop()
        self.assertEqual(term.getvalue(), "")

    def test_quick_repo_writes_nothing(self):
        rrcc = load_rrcc()
        term = FakeTerminal()
        p = rrcc.Progress(stream=term, delay=60)
        p.start("repo")
        p.stop()
        self.assertEqual(term.getvalue(), "")

    def test_draw_and_clear(self):
        rrcc = load_rrcc()
        shown = len("| repo: reading metadata")
        for color, cleared in ((True, "\r\x1b[K"), (False, "\r" + " " * shown + "\r")):
            with self.subTest(color=color):
                term = FakeTerminal()
                p = rrcc.Progress(stream=term, unicode=False, color=color, delay=0)
                p.start("repo")
                p._draw()
                p.stop()
                out = term.getvalue()
                self.assertIn("repo: reading metadata", out)
                self.assertEqual("\x1b[" in out, color)
                self.assertTrue(out.endswith(cleared), repr(out))

    def test_verify_checksum_reports_bytes(self):
        rrcc = load_rrcc()
        data = b"a" * (3 * 1024 * 1024 + 5)
        good = rrcc.hashlib.sha256(data).hexdigest()
        read = []
        self.assertTrue(rrcc.verify_checksum(io.BytesIO(data), "sha256", good, on_read=read.append))
        self.assertEqual(sum(read), len(data))


class TerminalTest(TestCase):
    """rrcc run with stderr on a pseudo-terminal, checking a slow server so
    that the line appears."""

    def run_on_terminal(self, *args):
        master, slave = pty.openpty()
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        env.update(TERM="xterm", COLUMNS="120", PYTHONDONTWRITEBYTECODE="1")
        env.pop("NO_COLOR", None)
        proc = subprocess.Popen([sys.executable, RRCC] + list(args), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=slave, env=env,
                                universal_newlines=True)
        os.close(slave)
        chunks = []

        def read_terminal():
            while True:
                try:
                    data = os.read(master, 4096)
                except OSError:  # EIO once rrcc has exited
                    break
                if not data:
                    break
                chunks.append(data)

        reader = threading.Thread(target=read_terminal)
        reader.start()
        out = proc.communicate(timeout=120)[0]
        reader.join()
        os.close(master)
        return proc.returncode, out, b"".join(chunks).decode("utf-8", "replace")

    def setUp(self):
        super().setUp()
        standard_repo(self.tmp)
        self.server = StaticServer(self.tmp, delay=0.1).start()
        self.addCleanup(self.server.stop)

    def test_progress_shown_and_cleared(self):
        rc, out, term = self.run_on_terminal("-j", "1", self.server.url)
        self.assertEqual(rc, 0, out + term)
        self.assertIn("Result: all checked repos are consistent.", out)
        self.assertIn(": packages", term)
        self.assertIn("/6", term)
        self.assertIn("\x1b[36m", term)
        self.assertTrue(term.endswith("\r\x1b[K"), repr(term[-200:]))

    def test_not_with_no_progress_quiet_or_verbose(self):
        for option in ("--no-progress", "-q", "-v"):
            with self.subTest(option=option):
                rc, out, term = self.run_on_terminal(option, "-j", "1", self.server.url)
                self.assertEqual(rc, 0, out + term)
                self.assertEqual(term, "")


if __name__ == "__main__":
    unittest.main()
