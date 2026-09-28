#!/usr/bin/env python3
"""
RPM Repository Consistency Checker, or rrcc for short.

Verify that a local RPM repository mirror is consistent with its own
repodata: every package listed in primary.xml actually exists on disk
(and, optionally, matches the recorded size/checksum).

Every metadata file listed in repodata/repomd.xml (primary, filelists,
other, updateinfo, comps, modules, the sqlite and zchunk variants, ...) is
checked too: it must exist and match the size and checksum in repomd.xml.
For a remote repository, files that rrcc doesn't read itself are only
checked for presence and size, unless --checksum is given.

Single repo:
    rrcc.py [options] /path/to/repo/root

    /path/to/repo/root is the directory that CONTAINS "repodata/"
    (i.e. the same directory you'd point a baseurl at).

    A remote repository served over HTTP(S) can be checked the same way by
    giving its URL instead of a directory:
        rrcc.py [options] http://host/path/to/repo/root

    For a remote repository the metadata is downloaded to a temporary
    directory, the presence and size of each package is checked with a HEAD
    request, and (with --checksum) each package is downloaded and hashed.

Multiple repos under a common parent:
    rrcc.py --top-level [options] /path/to/mirrors

    Recursively finds every directory under the given path that has a
    "repodata/repomd.xml" in it, and checks each one as a separate repo.
    Useful when you mirror several release/arch trees (or os/debug/source
    variants) under one parent directory. For a URL this crawls the
    directory listings ("Index of ...") that the web server generates, or
    generated index pages that link directories as dir/index.html or
    without a trailing slash.
"""

# The end of --help, after the options. -h leaves it out.
HELP_NOTES = """\
If repomd.xml also lists a "primary_zck" entry (the zchunk-compressed copy
of primary.xml that createrepo_c --zck produces), it is cross-checked against
the plain primary: it must exist, be readable, and list exactly the same
packages with the same sizes and checksums. This needs 'unzck' (see below),
and is reported as a problem if the file can't be read.

Proxy:
    Remote repos are fetched through the proxy in the http_proxy and
    https_proxy environment variables (or HTTP_PROXY/HTTPS_PROXY), except for
    hosts in no_proxy. Only http:// proxies are supported, with optional
    basic authentication given as http://user:password@proxy:port.

Exit status:
    0   everything checked is consistent
    1   at least one problem found in at least one repo
    2   couldn't even read repodata for one or more repos (or none found
        under --top-level)

Uses only the Python standard library, with two exceptions for
primary.xml compression formats that Python can't read by itself:

    .zst   (the default on modern Fedora/createrepo_c repos) needs either
           Python 3.14+ (stdlib compression.zstd) or the third-party
           'zstandard' package.
    .zck   (zchunk) needs the external 'unzck' command from the 'zchunk'
           package, which must be in PATH. On Rocky/RHEL 9 this is
           "dnf install zchunk" and requires EPEL to be enabled. This works
           on any Python version.

.gz and .xz are always handled via the stdlib.
"""

import argparse
import base64
import contextlib
import functools
import gzip
import hashlib
import http.client
import lzma
import os
import posixpath
import re
import shutil
import ssl
import string
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import xml.etree.ElementTree as ET
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit, urlunsplit

__version__ = "1.5.2"

NS_REPO = "{http://linux.duke.edu/metadata/repo}"
NS_COMMON = "{http://linux.duke.edu/metadata/common}"
NS_RPM = "{http://linux.duke.edu/metadata/rpm}"

CHECKSUM_ALGO_MAP = {
    "sha256": hashlib.sha256,
    "sha1": hashlib.sha1,
    "sha": hashlib.sha1,
    "sha512": hashlib.sha512,
    "md5": hashlib.md5,
}


def open_maybe_compressed(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    if path.endswith(".xz"):
        return lzma.open(path, "rb")
    if path.endswith(".zst"):
        return open_zstd(path)
    if path.endswith(".zck"):
        return ZckStream(path)
    return open(path, "rb")


class ZckStream:
    """Read-only file-like object yielding the decompressed contents of a
    .zck (zchunk) file. Python has no zchunk support, so this streams the
    output of the external 'unzck --stdout' command (package 'zchunk',
    from EPEL on Rocky/RHEL 8+). If unzck fails, the error is raised from
    read() when the end of the stream is reached."""

    def __init__(self, path):
        self.path = path
        unzck = shutil.which("unzck")
        if unzck is None:
            raise RuntimeError(
                f"{path}: .zck primary metadata requires the 'unzck' command "
                "(dnf install zchunk; on Rocky/RHEL 8+ this needs EPEL)"
            )
        self._stderr = tempfile.TemporaryFile()
        try:
            self._proc = subprocess.Popen([unzck, "--stdout", path],
                                          stdout=subprocess.PIPE, stderr=self._stderr)
        except BaseException:
            self._stderr.close()
            raise

    def read(self, size=-1):
        data = self._proc.stdout.read(size)
        if not data or size is None or size < 0:
            self._check_exit()
        return data

    def _check_exit(self):
        rc = self._proc.wait()
        if rc != 0:
            self._stderr.seek(0)
            msg = self._stderr.read().decode(errors="replace").strip()
            raise RuntimeError(f"{self.path}: unzck failed (exit status {rc})"
                               + (f": {msg}" if msg else ""))

    def close(self):
        if self._proc.poll() is None:
            self._proc.kill()  # abandoned before EOF
        self._proc.stdout.close()
        self._proc.wait()
        self._stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_zstd(path):
    """Open a .zst file for reading, using the stdlib compression.zstd
    module (Python 3.14+) if available, else falling back to the
    third-party 'zstandard' package."""
    try:
        from compression import zstd
    except ImportError:
        pass
    else:
        return zstd.open(path, "rb")

    try:
        import zstandard
    except ImportError as e:
        raise RuntimeError(
            f"{path}: .zst primary metadata requires either Python 3.14+ "
            "(stdlib compression.zstd) or the 'zstandard' package "
            "(pip install zstandard / dnf install python3-zstandard)"
        ) from e

    fh = open(path, "rb")
    return zstandard.ZstdDecompressor().stream_reader(fh, closefd=True)


HTTP_TIMEOUT = 60  # seconds
DEFAULT_JOBS = 8   # parallel package checks for remote repos

# The repository is untrusted input, so what is read from it is bounded.
MAX_REPOMD_SIZE = 16 * 1024**2       # repomd.xml
MAX_METADATA_SIZE = 4 * 1024**3      # a metadata file that repomd.xml gives no size for
MAX_MODULES_SIZE = 256 * 1024**2     # modules.yaml, decompressed
MAX_LISTING_DEPTH = 32               # directory levels followed in a web server's listings
MAX_LISTED_DIRS = 100_000            # total directories followed in a web server's listings
MAX_LISTING_SIZE = 16 * 1024**2      # a single directory listing page
MAX_LISTING_PROBES = 1000            # links on one listing page probed for being a directory
DRAIN_LIMIT = 1024**2                # a redirect/error response body that is read to reuse the connection
PROLOG_PEEK = 64 * 1024              # read upfront to look for a <!DOCTYPE before parsing untrusted XML


def positive_int(value):
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"invalid value '{value}': must be an integer >= 1")
    return n


def positive_float(value):
    try:
        x = float(value)
    except ValueError:
        x = 0
    if not x > 0 or x == float("inf"):
        raise argparse.ArgumentTypeError(f"invalid value '{value}': must be a number > 0")
    return x


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keeps the layout of the description and epilog, and doesn't split
    paths like /etc/rhsm/ca/redhat-uep.pem at a hyphen."""

    def _split_lines(self, text, width):
        return textwrap.wrap(" ".join(text.split()), width, break_on_hyphens=False)


class FullHelpAction(argparse.Action):
    """--help: like -h, but with the whole description, the long help of
    each option (its long_help attribute, if it has one) and HELP_NOTES."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        for action in parser._actions:
            action.help = getattr(action, "long_help", action.help)
        parser.description = __doc__
        parser.epilog = HELP_NOTES
        parser.print_help()
        parser.exit()


def is_url(path):
    return path.startswith(("http://", "https://"))


def normalize_url(url):
    """Drop any query/fragment and make sure the URL ends with a slash."""
    parts = urlsplit(url)
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class UnsafeHrefError(OSError):
    """A location in the metadata that points outside of the repository."""


def check_href(href):
    """Return href if it is a relative path that stays below the repo root,
    else raise UnsafeHrefError. The metadata is untrusted: without this an
    href like ../../etc/shadow or //other.host/x.rpm would make rrcc look at
    files, or send requests, elsewhere."""
    if (not href or "\0" in href or href.startswith("/")
            or posixpath.normpath(href) in (".", "..")
            or posixpath.normpath(href).startswith("../")):
        raise UnsafeHrefError(f"refusing location outside of the repository: {href!r}")
    return href


_CONTROL_CHARS = re.compile(
    "[\x00-\x1f\x7f-\x9f"        # C0 and C1 control characters
    "\u200b-\u200f"              # zero-width space/joiners and directional marks
    "\u202a-\u202e"              # bidi embedding/override
    "\u2060-\u2069"              # word joiner and bidi isolates
    "\ufeff]")                   # BOM / zero-width no-break space


def printable(text):
    """text with control and Unicode bidi/format characters (newlines,
    escape sequences, characters that reorder or hide following text, ...)
    made visible, so that names from the metadata or the server can't fake
    lines of the report, drive the terminal, or make part of a name display
    misleadingly."""
    return _CONTROL_CHARS.sub(lambda m: "\\x%02x" % ord(m.group()), str(text))


def redact_url(url):
    """url without any user name and password."""
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2],
                       parts.path, parts.query, parts.fragment))


def copy_limited(src, dst, limit):
    """Copy at most limit bytes from src to dst, returning how many."""
    copied = 0
    while copied < limit:
        chunk = src.read(min(1024 * 1024, limit - copied))
        if not chunk:
            break
        dst.write(chunk)
        copied += len(chunk)
    return copied


def discard_limited(src, limit):
    """Read and discard at most limit + 1 bytes from src, returning how many
    were read (so the caller can tell whether more than limit was there)."""
    remaining = limit + 1
    total = 0
    while remaining > 0:
        chunk = src.read(min(1024 * 1024, remaining))
        if not chunk:
            break
        total += len(chunk)
        remaining -= len(chunk)
    return total


def read_limited(src, limit):
    """The whole body of src, raising OSError if it is more than limit bytes.
    Used for things read fully into memory (a directory listing page), so
    that a hostile server can't exhaust memory by sending an oversized one."""
    chunks = []
    total = 0
    while total <= limit:
        chunk = src.read(min(1024 * 1024, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > limit:
        raise OSError(f"response is larger than {limit} bytes")
    return b"".join(chunks)


def format_size(n):
    """n bytes as a short human readable size, like 1.5 GiB."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def format_duration(seconds):
    """seconds as m:ss or h:mm:ss."""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def terminal_can_color(environ=os.environ):
    """Whether to use colors on a terminal, going by the same environment
    variables as Python 3.13+ (NO_COLOR, FORCE_COLOR, PYTHON_COLORS and
    TERM=dumb)."""
    if environ.get("PYTHON_COLORS") in ("0", "1"):
        return environ["PYTHON_COLORS"] == "1"
    if environ.get("NO_COLOR"):
        return False
    if environ.get("FORCE_COLOR"):
        return True
    return environ.get("TERM", "dumb") != "dumb"


class Progress:
    """A progress line on a terminal, meant for stderr: which repo is being
    checked and what is being done, and while files are checked a bar with
    their number, the bytes hashed, the speed and the time left.

    The line is redrawn by a background thread a few times a second, so it
    keeps moving while one big package is hashed; the counters may be
    updated from any thread. It only appears once a repo has taken longer
    than delay seconds, so that quick runs don't flicker, and is cleared by
    stop(), before the report is printed. With colors it looks like the
    progress bars of pip, without them it uses Unicode block characters, and
    it falls back to plain ASCII when the terminal's encoding is not UTF-8
    (as with the C locale on Python 3.6). A disabled Progress does nothing."""

    SPINNER_UNICODE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    SPINNER_ASCII = "|/-\\"
    EIGHTHS = " ▏▎▍▌▋▊▉"  # partial blocks, 0/8 to 7/8
    BAR_WIDTH = (10, 30)  # least and most
    INTERVAL = 0.1  # seconds between redraws

    # SGR codes
    DIM, BOLD, GREEN, CYAN, GREY = "2", "1", "32", "36", "90"

    def __init__(self, stream=None, enabled=True, unicode=None, color=None, delay=0.5):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        if unicode is None:
            encoding = (getattr(self.stream, "encoding", None) or "").lower().replace("-", "")
            unicode = encoding == "utf8"
        self.unicode = unicode
        self.color = terminal_can_color() if color is None else color
        self.delay = delay
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._shown = 0  # width of what is on the line now
        self._tick = 0
        self._reset()

    def _reset(self, label=""):
        self.label = label
        self.text = ""
        self.total_items = None  # None while there is nothing to count
        self.total_bytes = None
        self.done_items = 0
        self.done_bytes = 0
        self.started = self.counting_since = time.monotonic()

    # The API used by the checker

    def start(self, label, text="reading metadata"):
        """Begin showing progress for label, e.g. a repo."""
        if not self.enabled:
            return
        sys.stdout.flush()  # the report so far must come before the line
        with self._lock:
            self._reset(label)
            self.text = text
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rrcc-progress", daemon=True)
        self._thread.start()

    def status(self, text):
        """Show what is being done, without a bar."""
        with self._lock:
            self.text = text
            self.total_items = self.total_bytes = None

    def count(self, text, total_items, total_bytes=None):
        """Show a bar for checking total_items files. With total_bytes, the
        bar goes by bytes (for checksums), else by files."""
        with self._lock:
            self.text = text
            self.total_items = total_items
            self.total_bytes = total_bytes or None
            self.done_items = self.done_bytes = 0
            self.counting_since = time.monotonic()

    def advance(self, items=0, nbytes=0):
        with self._lock:
            self.done_items += items
            self.done_bytes += nbytes

    def stop(self):
        """Clear the line and stop the redrawing."""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        with self._lock:
            self._clear()

    # Drawing

    def _run(self):
        while not self._stop.wait(self.INTERVAL):
            with self._lock:
                if time.monotonic() - self.started >= self.delay:
                    self._draw()

    def _write(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            self.enabled = False  # e.g. the terminal went away

    def _clear(self):
        if self._shown:
            self._write("\r\x1b[K" if self.color else "\r" + " " * self._shown + "\r")
            self._shown = 0

    def _draw(self):
        if not self.enabled:
            return
        width = shutil.get_terminal_size().columns - 1  # the last column would wrap on some terminals
        self._tick += 1
        segments = self.render(width, time.monotonic())
        visible = sum(len(text) for text, _ in segments)
        if self.color:
            line = "".join(f"\x1b[{sgr}m{text}\x1b[0m" if sgr else text for text, sgr in segments)
            self._write("\r" + line + "\x1b[K")
        else:
            line = "".join(text for text, _ in segments)
            self._write("\r" + line + " " * max(0, self._shown - visible))
        self._shown = visible

    # Fields after the bar, in the order shown, with the one that goes first
    # when the line doesn't fit the terminal last
    FIELDS = ("percent", "count", "bytes", "speed", "eta")
    DROP_ORDER = ("speed", "bytes", "bar", "eta", "count", "percent")
    MIN_LABEL = 20  # the label is shortened to this before fields are dropped

    def render(self, width, now):
        """The line as a list of (text, SGR code or None) segments that are
        at most width characters together."""
        spinner = self.SPINNER_UNICODE if self.unicode else self.SPINNER_ASCII
        head = [(spinner[self._tick % len(spinner)], self.CYAN), (" ", None)]
        fields = {}
        if self.total_items is not None:
            by_bytes = self.total_bytes is not None
            done, total = (self.done_bytes, self.total_bytes) if by_bytes else (self.done_items, self.total_items)
            fraction = min(1.0, done / total) if total else 1.0
            elapsed = now - self.counting_since
            rate = done / elapsed if elapsed > 0 else 0
            fields["bar"] = [("  ", None)] + self._bar(fraction, self.BAR_WIDTH[0])
            fields["percent"] = [(" ", None), (f"{fraction * 100:3.0f}%", self.BOLD)]
            fields["count"] = [(f" {self.done_items}/{self.total_items}", None)]
            if by_bytes:
                fields["bytes"] = [(f" {format_size(self.done_bytes)}/{format_size(self.total_bytes)}", None)]
            if elapsed >= 1 and rate > 0:  # too early to tell before that
                speed = f"{format_size(rate)}/s" if by_bytes else f"{rate:.0f}/s"
                fields["speed"] = [(f" {speed}", self.GREY)]
                if done < total:
                    fields["eta"] = [(f" {format_duration((total - done) / rate)} left", self.CYAN)]
        label = self.label
        text = f": {self.text}" if self.text and label else self.text

        def length(segs):
            return sum(len(t) for t, _ in segs)

        def tail():
            return [s for name in ("bar",) + self.FIELDS for s in fields.get(name, ())]

        def overflow():
            return length(head) + len(label) + len(text) + length(tail()) - width

        # Shorten the label from the left, where a path is least telling,
        # then drop fields, then shorten the label (and text) further.
        ellipsis = "\u2026" if self.unicode else "..."

        def shorten(s, by):
            keep = len(s) - by - len(ellipsis)
            return ellipsis + s[len(s) - keep:] if keep > 0 else ""

        if overflow() > 0 and len(label) > self.MIN_LABEL:
            label = shorten(label, min(overflow(), len(label) - self.MIN_LABEL))
        for name in self.DROP_ORDER:
            if overflow() <= 0:
                break
            fields.pop(name, None)
        if overflow() > 0:
            label = shorten(label, overflow())
            if not label:
                text = self.text
        if overflow() > 0:
            text = text[:max(0, len(text) - overflow())]
        if "bar" in fields and overflow() < 0:  # widen the bar into the room left
            least, most = self.BAR_WIDTH
            fields["bar"][1:] = self._bar(fraction, min(most, least - overflow()))
        return [s for s in head + [(label + text, None)] + tail() if s[0]]

    def _bar(self, fraction, width):
        if self.color and self.unicode:
            # like pip and rich: a heavy line, with a half cell at the head
            halves = int(fraction * width * 2)
            full, half = divmod(halves, 2)
            done = "━" * full + ("╸" if half else "")
            rest = "━" * (width - len(done))
            color = self.GREEN if fraction >= 1 else "35"  # magenta while running
            return [(done, color), (rest, self.GREY)]
        if self.unicode:
            eighths = int(fraction * width * 8)
            full, part = divmod(eighths, 8)
            cells = "█" * full + (self.EIGHTHS[part] if full < width else "")
            return [("│", None), (cells.ljust(width), self.GREEN), ("│", None)]
        full = int(fraction * width)
        return [("[" + "#" * full + "-" * (width - full) + "]", self.GREEN)]


NO_PROGRESS = Progress(enabled=False)


class LocalRepo:
    """A repository in a directory on disk. The methods below are what the
    checker needs from a repo, and are mirrored by HttpRepo. hrefs are the
    relative paths used in the metadata."""

    def __init__(self, root, follow_symlinks=False):
        self.root = root
        self.follow_symlinks = follow_symlinks
        self._real_root = os.path.realpath(root)

    def close(self):
        pass

    def locate(self, href):
        """Where href lives, for use in messages. Raises UnsafeHrefError
        for an href that is not inside the repository, lexically (check_href)
        or, unless follow_symlinks, because a symlink resolves
        outside of it: a mirror is untrusted input too, and a package or
        metadata file that is really a symlink to, say, /etc/shadow must not
        be opened, hashed (an oracle for its contents) or have its size
        reported (which would leak that size)."""
        path = os.path.join(self.root, check_href(href))
        if not self.follow_symlinks:
            real = os.path.realpath(path)
            if real != self._real_root and not real.startswith(self._real_root + os.sep):
                raise UnsafeHrefError(
                    f"refusing location that resolves outside of the repository: {href!r}")
        return path

    def metadata_path(self, href, max_size=None):
        """Path of a local file with the contents of href, or None if the
        file does not exist. max_size is only used for remote repos."""
        path = self.locate(href)
        return path if os.path.isfile(path) else None

    def has_local_copy(self, href):
        """True if href can be read without downloading it."""
        return True

    def package_size(self, href, max_size=None):
        """Size of the file href, or None if it does not exist."""
        path = self.locate(href)
        return os.path.getsize(path) if os.path.isfile(path) else None

    def package_checksum_ok(self, href, algo_name, expected_hex, max_size=None, on_read=None):
        """True/False if the checksum of href matches, None if algo_name is
        not supported. on_read(n) is called for every n bytes hashed."""
        with open(self.locate(href), "rb") as f:
            return verify_checksum(f, algo_name, expected_hex, on_read=on_read)

    def iter_rpms(self):
        """Yield the normalized href of every *.rpm below the repo root,
        except those under repodata/."""
        for dirpath, _, filenames in os.walk(self.root, followlinks=self.follow_symlinks):
            if os.sep + "repodata" in dirpath + os.sep:
                continue
            for fn in filenames:
                if fn.endswith(".rpm"):
                    yield os.path.normpath(os.path.relpath(os.path.join(dirpath, fn), self.root))


class HttpStatusError(OSError):
    """The server answered with an HTTP error status."""

    def __init__(self, code, reason, url):
        super().__init__(f"HTTP Error {code}: {reason} ({url})")
        self.code = code


class HttpClient:
    """Minimal HTTP(S) client that keeps connections alive and can be used
    from several threads at once: every thread gets its own connection per
    server. Follows redirects and retries once on a connection that the
    server closed while it was idle.

    Honors the http_proxy, https_proxy and no_proxy environment variables
    (and their upper-case forms) like urllib and curl do. HTTP requests are
    sent to the proxy with the full URL, HTTPS goes through a CONNECT tunnel.
    User and password in the proxy URL are sent as basic authentication."""

    MAX_REDIRECTS = 5

    def __init__(self, ssl_context=None, ssl_context_bare=None, home_netloc=None):
        """ssl_context is used for home_netloc (the host rrcc was pointed
        at); any other host, reached via a redirect, gets ssl_context_bare
        instead (or ssl_context, if no bare one is given). This keeps a
        --client-cert from being handed to a host the user didn't name,
        which could otherwise read it off a redirect (it can identify the
        holder, e.g. list their RHEL subscriptions)."""
        self._ssl_context = ssl_context  # None: the defaults of http.client
        self._ssl_context_bare = ssl_context_bare
        self._home_netloc = home_netloc
        self._local = threading.local()
        self._all = []  # every connection ever opened, so close() can reach them
        self._lock = threading.Lock()
        self._proxies = urllib.request.getproxies()

    def _context_for(self, netloc):
        if self._ssl_context_bare is None or netloc == self._home_netloc:
            return self._ssl_context
        return self._ssl_context_bare

    def _proxy_for(self, scheme, netloc):
        """Return (proxy host:port, extra headers) for a server, or None
        when it should be contacted directly."""
        proxy = self._proxies.get(scheme)
        if not proxy or urllib.request.proxy_bypass(netloc):
            return None
        if "://" not in proxy:
            proxy = "http://" + proxy  # e.g. http_proxy=proxy.example.com:3128
        parts = urlsplit(proxy)
        if parts.scheme != "http":
            raise RuntimeError(f"{scheme}_proxy={redact_url(proxy)}: only http:// proxies are supported")
        if not parts.hostname:
            raise RuntimeError(f"{scheme}_proxy={redact_url(proxy)}: no proxy host")
        headers = {}
        if parts.username is not None:
            cred = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
            headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        return f"{host}:{parts.port or 80}", headers

    def close(self):
        with self._lock:
            for conn in self._all:
                conn.close()
            self._all.clear()

    def _connection(self, scheme, netloc):
        """Return (connection, headers) for a server. headers are the extra
        headers that each request must carry: the proxy credentials when
        talking to an HTTP proxy, else none. A connection that has
        _rrcc_via_proxy set takes the full URL as request target."""
        conns = self._local.__dict__.setdefault("conns", {})
        entry = conns.get((scheme, netloc))
        if entry is None:
            if scheme == "https":
                cls = functools.partial(http.client.HTTPSConnection, context=self._context_for(netloc))
            else:
                cls = http.client.HTTPConnection
            proxy = self._proxy_for(scheme, netloc)
            headers = {}
            if proxy is None:
                conn = cls(netloc, timeout=HTTP_TIMEOUT)
            elif scheme == "https":
                proxy_hostport, proxy_headers = proxy
                conn = cls(proxy_hostport, timeout=HTTP_TIMEOUT)
                conn.set_tunnel(netloc, headers=proxy_headers)
            else:
                proxy_hostport, headers = proxy
                conn = cls(proxy_hostport, timeout=HTTP_TIMEOUT)
            conn._rrcc_via_proxy = proxy is not None and scheme == "http"
            entry = conns[(scheme, netloc)] = (conn, headers)
            with self._lock:
                self._all.append(conn)
        return entry

    @contextlib.contextmanager
    def open(self, url, method="GET"):
        """Context manager yielding the response for url (a 2xx one; any
        other status raises HttpStatusError). The body must be read within
        the with block."""
        for _ in range(self.MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            conn, headers = self._connection(parts.scheme, parts.netloc)
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            if conn._rrcc_via_proxy:
                target = f"{parts.scheme}://{parts.netloc}{target}"
            headers = dict(headers, **{"User-Agent": f"rrcc/{__version__}"})
            for attempt in (0, 1):
                try:
                    conn.request(method, target, headers=headers)
                    resp = conn.getresponse()
                    break
                except (http.client.BadStatusLine, ConnectionError):
                    conn.close()  # reconnects automatically on the next request
                    if attempt:
                        raise
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                target_url = urljoin(url, resp.getheader("Location"))
                # Drain to reuse the connection, but a hostile server
                # shouldn't be able to make this read an unbounded body:
                # close the connection instead, past DRAIN_LIMIT.
                if discard_limited(resp, DRAIN_LIMIT) > DRAIN_LIMIT:
                    conn.close()
                new_scheme = urlsplit(target_url).scheme
                if new_scheme not in ("http", "https") or (parts.scheme == "https" and new_scheme != "https"):
                    raise HttpStatusError(resp.status, f"refusing redirect to {target_url}", url)
                url = target_url
                continue
            break
        else:
            raise HttpStatusError(resp.status, "too many redirects", url)

        try:
            if resp.status >= 300:
                if discard_limited(resp, DRAIN_LIMIT) > DRAIN_LIMIT:
                    conn.close()
                raise HttpStatusError(resp.status, resp.reason, url)
            if method == "HEAD":
                resp.read()  # no body, but http.client only frees the connection after a read
            resp.rrcc_url = url  # where the redirects, if any, ended up
            yield resp
        finally:
            if not resp.isclosed():
                conn.close()  # body not fully read; the connection can't be reused


class HttpRepo:
    """A repository served over HTTP(S). Metadata files are downloaded to a
    temporary directory (call close() to remove it). Packages are never
    stored: their size comes from a HEAD request and their checksum from
    hashing a streamed GET. Safe to use from several threads, except for
    metadata_path()."""

    def __init__(self, url, ssl_context=None, ssl_context_bare=None):
        self.root = normalize_url(url)
        self.client = HttpClient(ssl_context, ssl_context_bare, urlsplit(self.root).netloc)
        self._pool = None
        self._tmpdir = None
        self._downloads = {}  # href -> local path (None if missing on the server)

    def pool(self, jobs):
        """The worker threads for checking files in parallel. The same pool
        is used for the whole repo, as every thread keeps its own
        connections to the server."""
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=jobs)
        return self._pool

    def close(self):
        if self._pool is not None:
            self._pool.shutdown()
            self._pool = None
        self.client.close()
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def locate(self, href):
        return urljoin(self.root, quote(check_href(href), safe="/"))

    def metadata_path(self, href, max_size=None):
        """Download href to the temporary directory. At most max_size bytes
        (default MAX_METADATA_SIZE) plus one are stored, so that a file that
        is larger than it should be is noticed but can't fill the disk."""
        if href in self._downloads:
            return self._downloads[href]
        url = self.locate(href)
        if self._tmpdir is None:
            self._tmpdir = tempfile.mkdtemp(prefix="rrcc-")
        path = os.path.join(self._tmpdir, f"{len(self._downloads)}-{os.path.basename(href)}")
        limit = (MAX_METADATA_SIZE if max_size is None else max_size) + 1
        try:
            with self.client.open(url) as resp, open(path, "wb") as out:
                copy_limited(resp, out, limit)
        except HttpStatusError as e:
            if e.code != 404:
                raise
            path = None
        self._downloads[href] = path
        return path

    def has_local_copy(self, href):
        return self._downloads.get(href) is not None

    def package_size(self, href, max_size=None):
        """Size of href, or None if the server doesn't have it. If the
        server sends no Content-Length, the body is counted, but no further
        than max_size + 1 bytes (max_size defaults to MAX_METADATA_SIZE, so
        that this is always bounded even for a file the metadata gives no
        size for)."""
        if self.has_local_copy(href):
            return os.path.getsize(self._downloads[href])
        limit = MAX_METADATA_SIZE if max_size is None else max_size
        try:
            with self.client.open(self.locate(href), "HEAD") as resp:
                length = resp.getheader("Content-Length")
            if length is not None:
                return int(length)
            # No Content-Length (e.g. chunked): count the bytes instead
            with self.client.open(self.locate(href)) as resp:
                remaining = limit + 1
                total = 0
                while remaining > 0:
                    chunk = resp.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    total += len(chunk)
                    remaining -= len(chunk)
                return total
        except HttpStatusError as e:
            if e.code == 404:
                return None
            raise

    def package_checksum_ok(self, href, algo_name, expected_hex, max_size=None, on_read=None):
        if self.has_local_copy(href):
            with open(self._downloads[href], "rb") as f:
                return verify_checksum(f, algo_name, expected_hex, max_size, on_read)
        limit = MAX_METADATA_SIZE if max_size is None else max_size
        with self.client.open(self.locate(href)) as resp:
            return verify_checksum(resp, algo_name, expected_hex, limit, on_read)

    def iter_rpms(self):
        for url in walk_http(self.client, self.root, skip_repodata=True):
            if url.endswith(".rpm"):
                yield os.path.normpath(unquote(url[len(self.root):]))


class _LinkParser(HTMLParser):
    """Collects [href, text] of every <a href=...> in a page."""

    def __init__(self):
        super().__init__()
        self.links = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            self._current = [href, ""] if href else None
            if self._current:
                self.links.append(self._current)

    def handle_endtag(self, tag):
        if tag == "a":
            self._current = None

    def handle_data(self, data):
        if self._current is not None:
            self._current[1] += data


# A last path segment ending like this names a file (x.rpm, repomd.xml), not
# a directory. 10.0 or RPM-GPG-KEY-EPEL-8 could be either.
_FILE_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]*$")


def _is_http_dir(client, url):
    """True if the server redirects url (without a trailing slash) to url + "/",
    as web servers do for a directory."""
    try:
        with client.open(url, "HEAD") as resp:
            return resp.rrcc_url == url + "/"
    except HttpStatusError:
        return False


def list_http_dir(client, url):
    """Return (dirs, files) as absolute URLs for the entries linked from the
    directory listing at url. Sort links, the parent directory and anything
    outside of url are ignored.

    A link ending in a slash is a directory. Generated ("fancy") listings
    don't always do that, so a link is also a directory if it goes to
    dir/index.html (e.g. the static indexes of an S3 bucket), if its text
    ends in a slash, or if it has no file extension and the server
    redirects it to the same URL with a slash (a HEAD request, for at most
    MAX_LISTING_PROBES links on the page)."""
    with client.open(url) as resp:
        html = read_limited(resp, MAX_LISTING_SIZE).decode("utf-8", errors="replace")
    parser = _LinkParser()
    parser.feed(html)
    dirs, files = [], []
    probes = 0
    for href, text in parser.links:
        full = urldefrag(urljoin(url, href.strip()))[0]
        if "?" in full:
            continue
        name = full.rsplit("/", 1)[-1]  # "" for a URL ending in a slash
        if name in ("index.html", "index.htm"):
            full = full[:-len(name)]
        elif name and text.strip().endswith("/"):
            full += "/"
        if full == url or not full.startswith(url):
            continue
        if (not full.endswith("/") and not _FILE_EXTENSION.search(full)
                and full not in files and full + "/" not in dirs
                and probes < MAX_LISTING_PROBES):
            probes += 1
            if _is_http_dir(client, full):
                full += "/"
        target = dirs if full.endswith("/") else files
        if full not in target:
            target.append(full)
    return dirs, files


def walk_http(client, top, skip_repodata=False):
    """Yield the URL of every file below the directory URL top, by following
    the web server's directory listings. Each directory is listed once,
    directories nested deeper than MAX_LISTING_DEPTH (a symlink loop on the
    server, say) raise OSError, and so does a tree with more than
    MAX_LISTED_DIRS directories (a loop that stays within the depth limit,
    e.g. by cycling through a handful of directories)."""
    todo = [(top, 0)]
    seen = {top}
    while todo:
        url, depth = todo.pop()
        dirs, files = list_http_dir(client, url)
        yield from files
        for d in dirs:
            if skip_repodata and d.endswith("/repodata/"):
                continue
            if d in seen:
                continue
            if depth >= MAX_LISTING_DEPTH:
                raise OSError(f"directories nested more than {MAX_LISTING_DEPTH} levels deep "
                              f"below {top} (a loop?): {d}")
            if len(seen) >= MAX_LISTED_DIRS:
                raise OSError(f"more than {MAX_LISTED_DIRS} directories below {top} (a loop?): {d}")
            seen.add(d)
            todo.append((d, depth + 1))


def open_repo(root, ssl_context=None, ssl_context_bare=None, follow_symlinks=False):
    if is_url(root):
        return HttpRepo(root, ssl_context, ssl_context_bare)
    return LocalRepo(root, follow_symlinks)


def make_ssl_context(args, with_client_cert=True):
    """Build the TLS settings for https:// repos from the command line: by
    default the system CAs and no client certificate. The context is always
    made here, so that verification of the server does not depend on how the
    platform's Python is configured (as on RHEL 8, where a system file can
    turn it off). Raises ValueError with a message for unusable files.

    with_client_cert=False builds the same context but without loading
    --client-cert: used for any host other than the one the user pointed
    rrcc at (see HttpClient), so that a redirect can't make a client
    certificate, which can identify its holder, be sent to a host the user
    didn't name."""
    if args.client_key and not args.client_cert:
        raise ValueError("--client-key needs --client-cert")

    def no_password():
        raise ValueError(f"{args.client_key or args.client_cert}: "
                         "encrypted private keys are not supported")

    def load(option, filename, func, *func_args, **kwargs):
        """Call func, turning a failure into a message that names the file."""
        if not os.path.exists(filename):
            raise ValueError(f"{option} {filename}: no such file or directory")
        try:
            return func(*func_args, **kwargs)
        except (OSError, ssl.SSLError) as e:
            raise ValueError(f"{option} {filename}: {getattr(e, 'reason', None) or e}")

    if args.ca_cert is None:
        ctx = ssl.create_default_context()
    elif os.path.isdir(args.ca_cert):
        ctx = load("--ca-cert", args.ca_cert, ssl.create_default_context, capath=args.ca_cert)
    else:
        ctx = load("--ca-cert", args.ca_cert, ssl.create_default_context, cafile=args.ca_cert)
    if args.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if args.no_strict_x509:
        # Python 3.13+ sets this flag in create_default_context(); clearing it
        # is a no-op on older versions. The chain and host name are still checked.
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    if with_client_cert and args.client_cert:
        if args.client_key and not os.path.exists(args.client_key):
            raise ValueError(f"--client-key {args.client_key}: no such file or directory")
        try:
            load("--client-cert", args.client_cert, ctx.load_cert_chain,
                 args.client_cert, args.client_key, password=no_password)
        except ValueError as e:
            if "KEY_VALUES_MISMATCH" in str(e):
                raise ValueError(f"--client-key {args.client_key}: "
                                 f"does not belong to the certificate in {args.client_cert}")
            if "PEM lib" in str(e):
                raise ValueError(f"--client-cert {args.client_cert}: cannot read a certificate "
                                 + ("and private key from the file (give the key with --client-key "
                                    "if it is in a separate file)" if args.client_key is None else
                                    f"from it, or the key from {args.client_key}"))
            raise
    return ctx


class _NoDoctypeReader:
    """Wraps a binary file object opened on untrusted XML: read() raises
    ValueError if a "<!DOCTYPE" appears in the first PROLOG_PEEK bytes (a
    DOCTYPE, if present at all, always precedes the root element). RPM
    metadata never has one, and rejecting it blocks internal-entity expansion
    ("billion laughs") on Python/expat versions old enough to lack expat's
    own amplification limit (added in expat 2.4.0), without depending on
    ElementTree/expat internals that differ between Python's C accelerator
    and its pure-Python fallback. Used through parse_xml()/iterparse_xml(),
    which pass an explicit XMLParser so that ElementTree always reads this
    wrapper in chunks, instead of an accelerator reading the raw file
    directly as an optimization."""

    def __init__(self, fh):
        self._fh = fh
        self._buf = fh.read(PROLOG_PEEK)
        if b"<!DOCTYPE" in self._buf:
            raise ValueError("refusing metadata with a <!DOCTYPE declaration")

    def read(self, size=-1):
        if size is None or size < 0:
            data, self._buf = self._buf + self._fh.read(), b""
            return data
        if len(self._buf) >= size:
            data, self._buf = self._buf[:size], self._buf[size:]
            return data
        data, self._buf = self._buf + self._fh.read(size - len(self._buf)), b""
        return data


def parse_xml(fh):
    """Like ET.parse(fh), refusing a <!DOCTYPE (see _NoDoctypeReader)."""
    return ET.parse(_NoDoctypeReader(fh), parser=ET.XMLParser())


def iterparse_xml(fh, events):
    """Like ET.iterparse(fh, events=events), refusing a <!DOCTYPE."""
    return ET.iterparse(_NoDoctypeReader(fh), events=events, parser=ET.XMLParser())


def read_repomd(repo):
    """Return the <data> entries of repomd.xml as dicts {type, href, size,
    checksum_type, checksum, timestamp}, with the same keys as
    iter_packages() uses for the fields that describe a file. size, the
    checksum fields and timestamp (seconds since the epoch) are None if not
    given; an invalid timestamp is None too."""
    repomd_path = repo.metadata_path("repodata/repomd.xml", MAX_REPOMD_SIZE)
    if repomd_path is None:
        raise RuntimeError(f"missing {repo.locate('repodata/repomd.xml')}")

    with open(repomd_path, "rb") as fh:
        root = parse_xml(fh).getroot()
    entries = []
    for data_el in root.findall(f"{NS_REPO}data"):
        data_type = data_el.get("type")
        loc = data_el.find(f"{NS_REPO}location")
        if loc is None or "href" not in loc.attrib:
            raise RuntimeError(f"{data_type} <location> missing href in repomd.xml")
        csum_el = data_el.find(f"{NS_REPO}checksum")
        size_el = data_el.find(f"{NS_REPO}size")
        ts_el = data_el.find(f"{NS_REPO}timestamp")
        try:
            timestamp = int(float(ts_el.text))
        except (AttributeError, TypeError, ValueError, OverflowError):
            timestamp = None
        try:
            size = int(size_el.text) if size_el is not None else None
        except (TypeError, ValueError):
            raise RuntimeError(f"{data_type} has an invalid <size> in repomd.xml")
        entries.append({
            "type": data_type,
            "href": loc.attrib["href"],
            "size": size,
            "checksum_type": csum_el.get("type") if csum_el is not None else None,
            "checksum": csum_el.text.strip() if csum_el is not None and csum_el.text else None,
            "timestamp": timestamp,
        })
    return entries


def find_data_location(entries, data_type, required=True):
    """Return the href of the entry of the given type in read_repomd()'s
    entries. If there is no such entry, raise RuntimeError, or return None
    when required is False."""
    for entry in entries:
        if entry["type"] == data_type:
            return entry["href"]
    if required:
        raise RuntimeError(f'no <data type="{data_type}"> entry found in repomd.xml')
    return None


def iter_packages(primary_path):
    """Yield dicts: {href, size, checksum_type, checksum, name, arch,
    epoch, version, release}"""
    with open_maybe_compressed(primary_path) as fh:
        for event, elem in iterparse_xml(fh, events=("end",)):
            if elem.tag != f"{NS_COMMON}package":
                continue
            loc = elem.find(f"{NS_COMMON}location")
            name_el = elem.find(f"{NS_COMMON}name")
            arch_el = elem.find(f"{NS_COMMON}arch")
            ver_el = elem.find(f"{NS_COMMON}version")
            size_el = elem.find(f"{NS_COMMON}size")
            csum_el = elem.find(f"{NS_COMMON}checksum")
            if loc is None or "href" not in loc.attrib:
                elem.clear()
                continue
            try:
                size = int(size_el.attrib["package"]) if size_el is not None else None
            except (KeyError, ValueError):
                raise RuntimeError(f"{loc.attrib['href']}: missing or invalid package size "
                                   "in primary metadata")
            yield {
                "href": loc.attrib["href"],
                "size": size,
                "checksum_type": csum_el.attrib.get("type") if csum_el is not None else None,
                "checksum": csum_el.text if csum_el is not None else None,
                "name": name_el.text if name_el is not None else None,
                "arch": arch_el.text if arch_el is not None else None,
                "epoch": ver_el.attrib.get("epoch") if ver_el is not None else None,
                "version": ver_el.attrib.get("ver") if ver_el is not None else None,
                "release": ver_el.attrib.get("rel") if ver_el is not None else None,
            }
            elem.clear()


ALNUM = frozenset(string.ascii_letters + string.digits)
DIGITS = frozenset(string.digits)


def rpmvercmp(a, b):
    """Compare two version (or release) strings the way rpm does
    (rpmvercmp() in rpm's rpmio/rpmvercmp.c). Returns -1, 0 or 1."""
    if a == b:
        return 0
    i = j = 0
    while i < len(a) or j < len(b):
        # skip separators, i.e. everything but alphanumerics, ~ and ^
        while i < len(a) and a[i] not in ALNUM and a[i] not in "~^":
            i += 1
        while j < len(b) and b[j] not in ALNUM and b[j] not in "~^":
            j += 1
        ca = a[i] if i < len(a) else ""
        cb = b[j] if j < len(b) else ""

        # ~ sorts before everything, even the end of the string
        if ca == "~" or cb == "~":
            if ca != "~":
                return 1
            if cb != "~":
                return -1
            i += 1
            j += 1
            continue

        # ^ sorts after the end of the string, but before anything else
        if ca == "^" or cb == "^":
            if not ca:
                return -1
            if not cb:
                return 1
            if ca != "^":
                return 1
            if cb != "^":
                return -1
            i += 1
            j += 1
            continue

        if not (ca and cb):
            break

        # Compare one segment: all digits or all letters, decided by a
        isnum = ca in DIGITS
        charset = DIGITS if isnum else ALNUM - DIGITS
        ei, ej = i, j
        while ei < len(a) and a[ei] in charset:
            ei += 1
        while ej < len(b) and b[ej] in charset:
            ej += 1
        seg_a, seg_b = a[i:ei], b[j:ej]
        if not seg_b:
            # segments of different kinds: numeric is newer than alpha
            return 1 if isnum else -1
        if isnum:
            seg_a, seg_b = seg_a.lstrip("0"), seg_b.lstrip("0")
            if len(seg_a) != len(seg_b):
                return 1 if len(seg_a) > len(seg_b) else -1
        if seg_a != seg_b:
            return 1 if seg_a > seg_b else -1
        i, j = ei, ej

    if i >= len(a) and j >= len(b):
        return 0
    return 1 if i < len(a) else -1


def evr_cmp(pkg1, pkg2):
    """Compare two packages by epoch, version and release."""
    for key in ("epoch", "version", "release"):
        v1, v2 = pkg1[key] or "0", pkg2[key] or "0"
        if key == "epoch":
            v1, v2 = int(v1), int(v2)
            rc = (v1 > v2) - (v1 < v2)
        else:
            rc = rpmvercmp(v1, v2)
        if rc:
            return rc
    return 0


def latest_packages(packages):
    """Return the packages that have the highest version of their name and
    architecture. Several packages that share the highest version are all
    kept, and so is any package that has no name or version in the
    metadata. The order of packages is kept."""
    best = {}  # (name, arch) -> list of packages with the highest version so far
    keep = []
    for pkg in packages:
        if pkg["name"] is None or pkg["version"] is None:
            keep.append(pkg)
            continue
        group = best.setdefault((pkg["name"], pkg["arch"]), [])
        rc = evr_cmp(pkg, group[0]) if group else 1
        if rc > 0:
            group[:] = [pkg]
        elif rc == 0:
            group.append(pkg)
    for group in best.values():
        keep.extend(group)
    keep_ids = {id(p) for p in keep}
    return [p for p in packages if id(p) in keep_ids]


def nevra(pkg):
    """name-epoch:version-release.arch, as in the artifacts of a module."""
    if pkg["name"] is None or pkg["version"] is None:
        return None
    return f"{pkg['name']}-{pkg['epoch'] or '0'}:{pkg['version']}-{pkg['release']}.{pkg['arch']}"


def read_modules(path):
    """Read modules.yaml (modulemd) and return a list of dicts
    {name, stream, version, artifacts} with one entry per module build,
    where artifacts is the set of NEVRAs of the module's rpms.

    There is no YAML parser in the standard library, so this understands
    just enough of the fixed layout that libmodulemd writes: a stream of
    documents in which 'document: modulemd' has name, stream and version
    directly under 'data:' and NEVRAs listed under 'artifacts:' 'rpms:'."""
    with open_maybe_compressed(path) as fh:
        data = fh.read(MAX_MODULES_SIZE + 1)
    if len(data) > MAX_MODULES_SIZE:
        raise RuntimeError(f"{path}: module metadata is larger than {MAX_MODULES_SIZE} bytes")
    text = data.decode("utf-8")

    def unquote_yaml(value):
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value

    modules = []
    doc = None
    for line in text.splitlines():
        line = line.rstrip()
        if line == "---":
            doc = {"type": None, "section": None, "in_rpms": False, "artifacts": set()}
        elif line == "..." and doc is not None:
            if doc["type"] == "modulemd":
                try:
                    modules.append({"name": doc["name"], "stream": doc["stream"],
                                    "version": int(doc["version"]), "artifacts": doc["artifacts"]})
                except (KeyError, ValueError) as e:
                    raise RuntimeError(f"{path}: cannot understand module metadata (missing or bad {e})")
            doc = None
        elif doc is None:
            continue
        elif line.startswith("document: "):
            doc["type"] = line[len("document: "):].strip()
        elif line.startswith("  ") and not line.startswith("   "):  # a key directly under data:
            doc["section"] = "artifacts" if line == "  artifacts:" else None
            for key in ("name", "stream", "version"):
                if line.startswith(f"  {key}: "):
                    doc[key] = unquote_yaml(line[len(f"  {key}: "):])
        elif doc["section"] == "artifacts":
            if line.startswith("    - "):
                if doc["in_rpms"]:
                    doc["artifacts"].add(unquote_yaml(line[6:]))
            elif line.startswith("    ") and not line.startswith("     "):
                doc["in_rpms"] = line == "    rpms:"
    return modules


def newest_packages(packages, modules=()):
    """Return the packages that 'dnf reposync --newest-only' of dnf 4 would
    download (dnf 5 ignores the modules, which is newest_packages(packages)):

    - the latest version of each non-modular package (per name and arch),
    - all rpms of the newest build of each module stream, and
    - all rpms of any older build of a stream that holds the latest
      version of one of the stream's packages.

    Without modules this is just the latest version of each package."""
    all_artifacts = set()
    streams = {}           # (name, stream) -> {version: [modules]}
    artifact_versions = {}  # NEVRA -> {(name, stream): [versions]}
    for m in modules:
        all_artifacts |= m["artifacts"]
        key = (m["name"], m["stream"])
        streams.setdefault(key, {}).setdefault(m["version"], []).append(m)
        for artifact in m["artifacts"]:
            artifact_versions.setdefault(artifact, {}).setdefault(key, []).append(m["version"])

    by_nevra = {}
    for pkg in packages:
        by_nevra.setdefault(nevra(pkg), []).append(pkg)

    keep = latest_packages([p for p in packages if nevra(p) not in all_artifacts])

    keep_artifacts = set()
    for key, versions_dict in streams.items():
        versions = {max(versions_dict)}
        stream_artifacts = set()
        for ms in versions_dict.values():
            for m in ms:
                stream_artifacts |= m["artifacts"]
        in_stream = [p for a in stream_artifacts for p in by_nevra.get(a, ())]
        for pkg in latest_packages(in_stream):
            versions.add(max(artifact_versions[nevra(pkg)][key]))
        for version in versions:
            for m in versions_dict[version]:
                keep_artifacts |= m["artifacts"]
    for artifact in keep_artifacts:
        keep.extend(by_nevra.get(artifact, ()))

    keep_ids = {id(p) for p in keep}
    return [p for p in packages if id(p) in keep_ids]  # keep the metadata order


def verify_checksum(f, algo_name, expected_hex, max_size=None, on_read=None):
    """Hash the file-like object f. Returns None if the algo is unsupported.
    With max_size, no more than max_size + 1 bytes are read, and a longer
    file is a mismatch. on_read(n) is called for every n bytes hashed."""
    hasher = CHECKSUM_ALGO_MAP.get(algo_name.lower())
    if hasher is None:
        return None  # unknown algo, skip
    h = hasher()
    remaining = None if max_size is None else max_size + 1
    while remaining is None or remaining > 0:
        chunk = f.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
        if not chunk:
            break
        h.update(chunk)
        if on_read is not None:
            on_read(len(chunk))
        if remaining is not None:
            remaining -= len(chunk)
    if remaining is not None and remaining <= 0:
        return False  # more data than the metadata says
    return h.hexdigest() == expected_hex


def find_http_repos(top_url, ssl_context=None, ssl_context_bare=None):
    """Like find_repos(), but for a directory URL: follows the web server's
    directory listings and returns the URLs (with trailing slash) of the
    repos found."""
    repos = []
    top_url = normalize_url(top_url)
    todo = [(top_url, 0)]
    seen = {top_url}
    client = HttpClient(ssl_context, ssl_context_bare, urlsplit(top_url).netloc)
    try:
        while todo:
            url, depth = todo.pop()
            dirs, _files = list_http_dir(client, url)
            if url + "repodata/" in dirs:
                try:
                    with client.open(url + "repodata/repomd.xml", "HEAD"):
                        repos.append(url)
                except HttpStatusError as e:
                    if e.code != 404:
                        raise
                dirs.remove(url + "repodata/")
            for d in dirs:
                if d in seen:
                    continue
                if depth >= MAX_LISTING_DEPTH:
                    raise OSError(f"directories nested more than {MAX_LISTING_DEPTH} levels deep "
                                  f"below {top_url} (a loop?): {d}")
                if len(seen) >= MAX_LISTED_DIRS:
                    raise OSError(f"more than {MAX_LISTED_DIRS} directories below {top_url} (a loop?): {d}")
                seen.add(d)
                todo.append((d, depth + 1))
    finally:
        client.close()
    return sorted(repos)


def find_repos(top_level, ssl_context=None, ssl_context_bare=None, follow_symlinks=False):
    """Recursively find directories under top_level that contain a valid
    repodata/repomd.xml. Does not descend into repodata/ itself. By default,
    a symlinked directory is not followed (matching LocalRepo's refusal of
    hrefs that resolve outside of a repo); follow_symlinks follows it,
    for a parent directory that itself uses symlinks to lay out its repos."""
    if is_url(top_level):
        return find_http_repos(top_level, ssl_context, ssl_context_bare)
    repos = []
    for dirpath, dirnames, _filenames in os.walk(top_level, followlinks=follow_symlinks):
        if "repodata" in dirnames:
            if os.path.isfile(os.path.join(dirpath, "repodata", "repomd.xml")):
                repos.append(dirpath)
            # never need to walk into a repodata dir looking for more repos
            dirnames.remove("repodata")
    return sorted(repos)


# Failures that mean "this repo's metadata (or a file it points at) can't
# be read", as opposed to a package problem. Reported per repo so that one
# broken repo doesn't stop the others from being checked.
READ_ERRORS = (RuntimeError, ET.ParseError, OSError, EOFError,
               lzma.LZMAError, zlib.error, ValueError, http.client.HTTPException)


def check_one_repo(repo_root, args):
    """Returns (status, count, problems, notes).
    status is one of 'ok', 'problems', 'error'.
    On 'error', problems is a single-element list with the error message.
    notes is a list of informational lines about what else was checked.
    """
    repo = open_repo(repo_root, args.ssl_context, args.ssl_context_bare, args.follow_symlinks)
    try:
        return _check_one_repo(repo, args)
    except READ_ERRORS as e:
        return "error", 0, [f"{type(e).__name__}: {e}"], []
    finally:
        repo.close()


def _compare_primary_zck(repo, zck_href, zck_size, plain):
    """Cross-check the primary_zck metadata against the plain primary.
    zck_size is its size according to repomd.xml, or None. plain maps
    normalized href -> (size, checksum_type, checksum) as read from the plain
    primary. Returns (problems, note)."""
    zck_path = repo.metadata_path(zck_href, zck_size)
    if zck_path is None:
        return [], None  # reported by verify_metadata()

    problems = []
    zck_seen = set()
    try:
        for pkg in iter_packages(zck_path):
            href = os.path.normpath(pkg["href"])
            zck_seen.add(href)
            if href not in plain:
                problems.append(f"PRIMARY_ZCK EXTRA: in primary_zck but not in primary: {pkg['href']}")
            elif plain[href] != (pkg["size"], pkg["checksum_type"], pkg["checksum"]):
                problems.append(f"PRIMARY_ZCK MISMATCH: size/checksum differs from primary: {pkg['href']}")
    except READ_ERRORS as e:
        return [f"PRIMARY_ZCK UNREADABLE: {zck_href}: {type(e).__name__}: {e}"], None

    for href in sorted(set(plain) - zck_seen):
        problems.append(f"PRIMARY_ZCK MISSING PACKAGE: in primary but not in primary_zck: {href}")

    return problems, f"primary_zck cross-checked against primary ({len(zck_seen)} packages)"


def check_package(repo, pkg, verify, on_read=None):
    """Check one package from primary.xml against the repo. Returns
    (pkg, problem, is_ok): problem is a message or None, and is_ok says
    whether the package should be reported as OK (a skipped checksum
    still counts as OK, but comes with a warning as the problem).
    on_read(n) is called for every n bytes hashed."""
    href = pkg["href"]
    result = True  # checksum result: True/False, or None if the algo is unsupported
    no_checksum = False
    try:
        actual_size = repo.package_size(href, pkg["size"])
        if actual_size is None:
            return pkg, f"MISSING: {href}", False

        if pkg["size"] is not None and actual_size != pkg["size"]:
            # no point checksumming a truncated/corrupt file
            return pkg, f"SIZE MISMATCH: {href} (expected {pkg['size']}, got {actual_size})", False

        if verify:
            if pkg["checksum_type"] and pkg["checksum"]:
                result = repo.package_checksum_ok(href, pkg["checksum_type"], pkg["checksum"],
                                                  pkg["size"], on_read)
            else:
                no_checksum = True
    except (OSError, http.client.HTTPException) as e:
        return pkg, f"UNREADABLE: {href}: {type(e).__name__}: {e}", False

    if no_checksum:
        return pkg, f"WARNING: no checksum in the metadata for {href}, skipped", True
    if result is False:
        return pkg, f"CHECKSUM MISMATCH: {href}", False
    if result is None:
        return pkg, f"WARNING: unsupported checksum algo '{pkg['checksum_type']}' for {href}, skipped", True
    return pkg, None, True


def check_packages(repo, packages, args, verify=None, what="packages"):
    """Yield check_package() results for the given packages, in order.
    verify(pkg) says whether to verify the checksum of a package; by
    default args.checksum decides. Remote repos are checked by args.jobs
    threads at a time; a local repo is checked in the calling thread.
    Progress is shown as checking the given number of what."""
    if verify is None:
        def verify(pkg):
            return args.checksum

    progress = getattr(args, "progress", NO_PROGRESS)
    total_bytes = sum(p["size"] or 0 for p in packages) if args.checksum else None
    progress.count(what, len(packages), total_bytes)

    def check(pkg):
        hashed = [0]

        def on_read(n):
            hashed[0] += n
            progress.advance(nbytes=n)

        result = check_package(repo, pkg, verify(pkg), on_read if progress.enabled else None)
        # a package that wasn't hashed (or only partly) still counts as done
        progress.advance(1, max(0, (pkg["size"] or 0) - hashed[0]))
        return result

    if args.jobs <= 1 or not isinstance(repo, HttpRepo):
        yield from map(check, packages)
        return
    yield from repo.pool(args.jobs).map(check, packages)


def verify_metadata(repo, entries, args):
    """Check that every file listed in repomd.xml exists and has the
    recorded size and checksum. For a remote repo, only files that were
    downloaded anyway get their checksum verified, unless --checksum is
    given. Returns (problems, note)."""
    files = []
    seen = set()
    for entry in entries:
        if entry["href"] not in seen:  # a file could be listed twice
            seen.add(entry["href"])
            files.append(entry)

    verified = set()

    def verify(entry):
        if args.checksum or repo.has_local_copy(entry["href"]):
            verified.add(entry["href"])
            return True
        return False

    problems = []
    for entry, problem, is_ok in check_packages(repo, files, args, verify, "metadata files"):
        if problem:
            problems.append(f"METADATA {problem} ({entry['type']})")
        if is_ok and args.verbose:
            print(f"  OK: {printable(entry['href'])}")

    size_only = len(files) - len(verified)
    note = f"{len(files)} metadata file(s) in repomd.xml checked"
    if size_only:
        note += (f" ({len(verified)} with checksum, {size_only} size only; "
                 "--checksum verifies all)")
    else:
        note += " (size and checksum)"
    return problems, note


def check_age(entries, max_age):
    """Check that the newest <timestamp> in repomd.xml is at most max_age
    days old. Returns (problems, note)."""
    timestamps = [e["timestamp"] for e in entries if e["timestamp"] is not None]
    if not timestamps:
        return ["NO TIMESTAMP: repomd.xml has no <timestamp>, cannot check --max-age"], None
    newest = max(timestamps)
    try:
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(newest))
    except (OverflowError, OSError, ValueError):
        when = str(newest)
    days = (time.time() - newest) / 86400
    if days < 0:
        return [], f"repomd.xml timestamp is in the future ({when})"
    if days > max_age:
        return [f"STALE: repomd.xml is {days:.1f} days old (newest timestamp {when}, "
                f"--max-age {max_age:g})"], None
    return [], f"repomd.xml is {days:.1f} days old (newest timestamp {when})"


def _check_one_repo(repo, args):
    try:
        entries = read_repomd(repo)
        primary_href = find_data_location(entries, "primary")
    except RuntimeError as e:
        return "error", 0, [str(e)], []

    # repomd.xml says how big each file is, which bounds what is downloaded
    sizes = {e["href"]: e["size"] for e in entries}

    primary_path = repo.metadata_path(primary_href, sizes[primary_href])
    if primary_path is None:
        return "error", 0, [f"primary metadata file listed in repomd.xml is missing: {repo.locate(primary_href)}"], []

    # A primary that doesn't match repomd.xml can't be trusted to list the
    # packages, and would likely fail to decompress with a confusing error.
    primary_entry = next(e for e in entries if e["type"] == "primary")
    _, problem, is_ok = check_package(repo, primary_entry, True)
    if not is_ok:
        return "error", 0, [f"primary metadata file does not match repomd.xml: {problem}"], []

    zck_href = find_data_location(entries, "primary_zck", required=False)

    problems = []
    notes = []
    seen_hrefs = set()
    plain = {}  # normalized href -> (size, checksum_type, checksum), for the primary_zck cross-check
    count = 0

    # Everything in the metadata counts as referenced (for --extra and the
    # primary_zck cross-check), even packages skipped by --newest-only.
    packages = list(iter_packages(primary_path))
    for pkg in packages:
        seen_hrefs.add(os.path.normpath(pkg["href"]))
        plain[os.path.normpath(pkg["href"])] = (pkg["size"], pkg["checksum_type"], pkg["checksum"])

    if args.newest_only:
        modules = []
        modules_href = find_data_location(entries, "modules", required=False)
        if modules_href is not None and not args.ignore_modules:
            modules_path = repo.metadata_path(modules_href, sizes[modules_href])
            if modules_path is None:
                return "error", 0, [f"modules metadata file listed in repomd.xml is missing: "
                                    f"{repo.locate(modules_href)}"], []
            modules = read_modules(modules_path)
        to_check = newest_packages(packages, modules)
        notes.append(f"--newest-only: skipped {len(packages) - len(to_check)} older package(s), "
                     f"{len(packages)} in metadata"
                     + (", modules ignored" if args.ignore_modules and modules_href else ""))
    else:
        to_check = packages

    for pkg, problem, is_ok in check_packages(repo, to_check, args):
        count += 1
        href = pkg["href"]

        if problem:
            problems.append(problem)
        if is_ok and args.verbose:
            print(f"  OK: {printable(href)}")

    progress = getattr(args, "progress", NO_PROGRESS)
    if zck_href is not None:
        progress.status("cross-checking primary_zck")
        zck_problems, note = _compare_primary_zck(repo, zck_href, sizes[zck_href], plain)
        problems.extend(zck_problems)
        if note:
            notes.append(note)

    # Last, so that a remote repo has downloaded what it needs by now and
    # the checksums of those files come for free. Listed first, though.
    meta_problems, note = verify_metadata(repo, entries, args)
    problems[:0] = meta_problems
    notes.insert(0, note)

    # The age is what matters most when it is checked, so it goes first.
    if args.max_age is not None:
        age_problems, note = check_age(entries, args.max_age)
        problems[:0] = age_problems
        if note:
            notes.insert(0, note)

    if args.extra:
        progress.status("looking for files not in the metadata")
        try:
            for rel in repo.iter_rpms():
                if rel not in seen_hrefs:
                    problems.append(f"ORPHAN (on disk, not in metadata): {rel}")
        except (OSError, http.client.HTTPException) as e:
            problems.append(f"CANNOT LIST FILES for --extra: {type(e).__name__}: {e}")

    status = "problems" if problems else "ok"
    return status, count, problems, notes


def main():
    # The first two paragraphs of the docstring, for -h
    short_description = "\n\n".join(__doc__.strip().split("\n\n")[:2])
    ap = argparse.ArgumentParser(description=short_description, add_help=False,
                                 epilog="Use --help for the full help, or see rrcc(1).",
                                 formatter_class=HelpFormatter)
    # Python < 3.10 calls them "optional arguments"
    ap._optionals.title = "options"

    def add(group, *flags, long_help=None, **kwargs):
        """Add an option with help for -h, and optionally a longer one for --help."""
        action = group.add_argument(*flags, **kwargs)
        if long_help:
            action.long_help = long_help
        return action

    add(ap, "paths", nargs="+", metavar="path",
        help="repo root(s), or (with --top-level) parent directories of multiple repos; "
             "a directory or an http(s):// URL",
        long_help="the root of a repo, i.e. the directory or http(s):// URL that contains "
                  "\"repodata/\", or with --top-level a parent directory of several repos")
    ap.add_argument("-h", action="help", help="show this short help and exit")
    ap.add_argument("--help", action=FullHelpAction, help="show the full help and exit")
    add(ap, "--top-level", action="store_true",
        help="treat each 'path' as a parent dir; auto-discover repos under it",
        long_help="treat the path as a parent directory containing multiple repos; "
                  "auto-discover and check each one")
    add(ap, "--checksum", action="store_true", help="also verify checksums (slow)",
        long_help="verify checksums too (slow: reads every RPM); without this flag, only "
                  "presence and size are checked. For a remote repo this also verifies the "
                  "checksum of every metadata file, which downloads all of them.")
    add(ap, "--extra", action="store_true", help="report on-disk RPMs not in metadata",
        long_help="also report *.rpm files on disk that are NOT referenced by primary.xml "
                  "(orphans / stale files). For a URL this needs directory listings enabled "
                  "on the web server.")
    add(ap, "--follow-symlinks", action="store_true",
        help="follow symlinks leading outside a local repo (unsafe)",
        long_help="for a repo in a directory, follow symlinks that lead outside of it, instead "
                  "of refusing them like any other location outside of the repo. Only use this "
                  "for a mirror you made yourself, e.g. one that symlinks packages in from a "
                  "shared pool: a mirror from someone else could use a symlink to make rrcc "
                  "read (and report the size, or a checksum match, of) an arbitrary file.")
    add(ap, "--max-age", type=positive_float, metavar="DAYS",
        help="report a repo whose repomd.xml is older than DAYS days (a fraction works too)",
        long_help="report a repo whose repomd.xml is older than DAYS days (a fraction like "
                  "0.5 works too), going by the newest <timestamp> in it. Catches a mirror "
                  "that is consistent but no longer being synced.")
    add(ap, "-n", "--newest-only", action="store_true",
        help="only check the latest version of each package (like dnf reposync --newest-only)",
        long_help="only check the latest version of each package (per name and architecture) "
                  "listed in primary.xml, and skip the older ones. Same as 'dnf reposync "
                  "--newest-only', so use it to check a mirror made that way. Modular repos "
                  "are handled like dnf 4 does it (the module metadata in modules.yaml is read).")
    add(ap, "--ignore-modules", action="store_true",
        help="with --newest-only: ignore module metadata, like dnf 5's reposync does",
        long_help="with --newest-only, ignore the module metadata and just keep the latest "
                  "version of each package, like dnf 5 (Fedora 41+) does. Use it for a mirror "
                  "of a modular repo made with dnf 5's reposync.")
    add(ap, "-j", "--jobs", type=positive_int, default=DEFAULT_JOBS, metavar="N",
        help=f"number of packages to check in parallel for http(s):// repos "
             f"(default: {DEFAULT_JOBS}; 1 disables parallelism); ignored for directories",
        long_help=f"check N packages in parallel for a remote repo (default: {DEFAULT_JOBS}; "
                  f"1 disables parallelism). Each worker keeps its own connection to the "
                  f"server alive, so this is also how many connections are used. Ignored for "
                  f"directories.")
    output = ap.add_mutually_exclusive_group()
    add(output, "-q", "--quiet", action="store_true",
        help="print only repos with problems, and nothing if all are consistent",
        long_help="print only the repos that have problems, and nothing at all if every repo "
                  "is consistent (for cron)")
    add(output, "-v", "--verbose", action="store_true",
        help="print a line for every package checked")
    add(ap, "--no-progress", action="store_true",
        help="don't show the progress line that is shown when stderr is a terminal",
        long_help="don't show a progress line. By default it is shown on stderr when that is "
                  "a terminal (not with -q or -v), once a repo takes more than half a second.")
    add(ap, "--version", action="version", version=f"{__version__}",
        help="print the version and exit")
    tls = ap.add_argument_group("TLS options for https:// repos",
                                "Like sslcacert, sslclientcert, sslclientkey and sslverify in dnf.")
    add(tls, "--ca-cert", metavar="FILE",
        help="trust only the CA certificate(s) in this PEM file, or in this directory "
             "(hashed as by openssl rehash), instead of the system CAs",
        long_help="trust only the CA certificate(s) in this PEM file, or in this directory "
                  "(hashed by 'openssl rehash'), instead of the system CAs. For the RHEL CDN: "
                  "/etc/rhsm/ca/redhat-uep.pem")
    add(tls, "--client-cert", metavar="FILE",
        help="authenticate with this client certificate (PEM), e.g. an entitlement "
             "certificate in /etc/pki/entitlement/ for the RHEL CDN",
        long_help="authenticate with this client certificate (PEM). For the RHEL CDN: "
                  "/etc/pki/entitlement/<serial>.pem")
    add(tls, "--client-key", metavar="FILE",
        help="private key for --client-cert (PEM, not encrypted), if it isn't "
             "in the --client-cert file",
        long_help="private key for --client-cert, unless it is in the same file. Encrypted "
                  "keys are not supported. For the RHEL CDN: "
                  "/etc/pki/entitlement/<serial>-key.pem")
    add(tls, "-k", "--insecure", action="store_true",
        help="don't verify the server certificate (like sslverify=0)")
    add(tls, "--no-strict-x509", action="store_true",
        help="accept server certificates that verify but don't follow RFC 5280 "
             "strictly, e.g. from a home-made CA (Python 3.13+ is strict by default)",
        long_help="accept a server certificate that verifies but doesn't follow RFC 5280 "
                  "strictly, e.g. one from a home-made CA without an Authority Key "
                  "Identifier. Python 3.13+ rejects those by default, unlike curl and dnf.")
    args = ap.parse_args()
    if args.ignore_modules and not args.newest_only:
        ap.error("--ignore-modules needs --newest-only")
    try:
        args.ssl_context = make_ssl_context(args)
        args.ssl_context_bare = (make_ssl_context(args, with_client_cert=False)
                                 if args.client_cert else args.ssl_context)
    except ValueError as e:
        ap.error(str(e))
    if args.insecure:
        print("WARNING: --insecure: the identity of https:// servers is not verified", file=sys.stderr)
    # -v prints to the terminal while checking, and -q is for running unattended
    args.progress = progress = Progress(enabled=sys.stderr.isatty() and not (
        args.quiet or args.verbose or args.no_progress))

    overall_problem = False
    overall_error = False
    total_packages = 0

    # (label, repo_root) pairs, in command-line order, without duplicates
    repos = []
    seen_roots = set()

    def add_repo(label, root):
        if root not in seen_roots:
            seen_roots.add(root)
            repos.append((label, root))

    for path in args.paths:
        top_path = normalize_url(path) if is_url(path) else os.path.abspath(path)
        if args.top_level:
            progress.start(printable(top_path), "looking for repos")
            try:
                found = find_repos(top_path, args.ssl_context, args.ssl_context_bare,
                                    args.follow_symlinks)
            except (OSError, RuntimeError, http.client.HTTPException) as e:
                print(f"ERROR: cannot search {top_path}: {type(e).__name__}: {printable(e)}", file=sys.stderr)
                overall_error = True
                continue
            finally:
                progress.stop()
            if not found:
                print(f"ERROR: no repos found under {printable(top_path)} (looked for */repodata/repomd.xml)", file=sys.stderr)
                overall_error = True
                continue

            def rel(r):
                return r[len(top_path):] or "." if is_url(top_path) else os.path.relpath(r, top_path)

            if not args.quiet:
                print(f"Found {len(found)} repo(s) under {printable(top_path)}:")
            for r in found:
                if not args.quiet:
                    print(f"  {printable(rel(r))}")
                add_repo(r if len(args.paths) > 1 else rel(r), r)
            if not args.quiet:
                print()
        else:
            add_repo(top_path, top_path)

    for label, repo_root in repos:
        if not args.quiet:
            print(f"=== {printable(label)} ===")
        progress.start(printable(label))
        try:
            status, count, problems, notes = check_one_repo(repo_root, args)
        finally:
            progress.stop()
        total_packages += count
        if args.quiet:
            if status == "ok":
                continue
            print(f"=== {printable(label)} ===")
            notes = []

        if status == "error":
            overall_error = True
            print(f"  ERROR: {printable(problems[0])}")
        elif status == "problems":
            overall_problem = True
            print(f"  {count} packages checked, {len(problems)} problem(s):")
            for p in problems:
                print(f"    {printable(p)}")
        else:
            print(f"  {count} packages checked, consistent"
                  + (" (checksums verified)." if args.checksum else " (size-checked)."))
        for note in notes:
            print(f"  {printable(note)}")
        print()

    if not args.quiet:
        print(f"Summary: {len(repos)} repo(s), {total_packages} package(s) checked total.")
    if overall_error:
        print("Result: one or more repos could not be read.")
        return 2
    if overall_problem:
        print("Result: inconsistencies found.")
        return 1
    if not args.quiet:
        print("Result: all checked repos are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

