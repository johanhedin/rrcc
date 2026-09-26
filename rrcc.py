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
    directory listings ("Index of ...") that the web server generates.

Options:
    --top-level          Treat the path as a parent directory containing
                         multiple repos; auto-discover and check each one.
    --checksum           Verify checksums too (slow: reads every RPM).
                         Without this flag, only presence + size are checked.
                         For a remote repo this also verifies the checksum of
                         every metadata file, which downloads all of them.
    --extra              Also report *.rpm files on disk that are NOT
                         referenced by primary.xml (orphans / stale files).
                         For a URL this needs directory listings enabled on
                         the web server.
    -n, --newest-only    Only check the latest version of each package (per name
                         and architecture) listed in primary.xml, and skip the
                         older ones. Same as 'dnf reposync --newest-only', so
                         use it to check a mirror made that way. Modular
                         repos are handled like dnf 4 does it (the module
                         metadata in modules.yaml is read).
    --ignore-modules     With --newest-only, ignore the module metadata and
                         just keep the latest version of each package, like
                         dnf 5 (Fedora 41+) does. Use it for a mirror of a
                         modular repo made with dnf 5's reposync.
    -j, --jobs N         Check N packages in parallel for a remote repo
                         (default 8, 1 disables parallelism). Each worker
                         keeps its own connection to the server alive, so
                         this is also how many connections are used.
                         Ignored for directories.
    -v, --verbose        Print a line for every package checked.

TLS options for https:// repos (named after the dnf repo settings sslcacert,
sslclientcert, sslclientkey and sslverify):
    --ca-cert FILE       Trust only the CA certificate(s) in this PEM file (or
                         directory hashed by 'openssl rehash') instead of the
                         system CAs. For the RHEL CDN: /etc/rhsm/ca/redhat-uep.pem
    --client-cert FILE   Authenticate with this client certificate (PEM). For
                         the RHEL CDN: /etc/pki/entitlement/<serial>.pem
    --client-key FILE    Private key for --client-cert, unless it is in the
                         same file. Encrypted keys are not supported. For the
                         RHEL CDN: /etc/pki/entitlement/<serial>-key.pem
    -k, --insecure       Don't verify the server certificate (sslverify=0).
    --version            Print the version and exit.

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
import threading
import xml.etree.ElementTree as ET
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit, urlunsplit

__version__ = "1.4.0"

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
MAX_REPOMD_SIZE = 16 * 1024**2      # repomd.xml
MAX_METADATA_SIZE = 4 * 1024**3     # a metadata file that repomd.xml gives no size for
MAX_MODULES_SIZE = 1024**3          # modules.yaml, decompressed
MAX_LISTING_DEPTH = 32              # directory levels followed in a web server's listings


def positive_int(value):
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"invalid value '{value}': must be an integer >= 1")
    return n


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


_CONTROL_CHARS = re.compile("[\x00-\x1f\x7f-\x9f]")


def printable(text):
    """text with control characters (newlines, escape sequences, ...) made
    visible, so that names from the metadata or the server can't fake lines
    of the report or drive the terminal."""
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


class LocalRepo:
    """A repository in a directory on disk. The methods below are what the
    checker needs from a repo, and are mirrored by HttpRepo. hrefs are the
    relative paths used in the metadata."""

    def __init__(self, root):
        self.root = root

    def close(self):
        pass

    def locate(self, href):
        """Where href lives, for use in messages. Raises UnsafeHrefError
        for an href that is not inside the repository."""
        return os.path.join(self.root, check_href(href))

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

    def package_checksum_ok(self, href, algo_name, expected_hex, max_size=None):
        """True/False if the checksum of href matches, None if algo_name is
        not supported."""
        with open(self.locate(href), "rb") as f:
            return verify_checksum(f, algo_name, expected_hex)

    def iter_rpms(self):
        """Yield the normalized href of every *.rpm below the repo root,
        except those under repodata/."""
        for dirpath, _, filenames in os.walk(self.root):
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

    def __init__(self, ssl_context=None):
        self._ssl_context = ssl_context  # None: the defaults of http.client
        self._local = threading.local()
        self._all = []  # every connection ever opened, so close() can reach them
        self._lock = threading.Lock()
        self._proxies = urllib.request.getproxies()

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
                cls = functools.partial(http.client.HTTPSConnection, context=self._ssl_context)
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
                resp.read()  # drain, so the connection can be reused
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
                resp.read()
                raise HttpStatusError(resp.status, resp.reason, url)
            if method == "HEAD":
                resp.read()  # no body, but http.client only frees the connection after a read
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

    def __init__(self, url, ssl_context=None):
        self.root = normalize_url(url)
        self.client = HttpClient(ssl_context)
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
        than max_size + 1 bytes (when max_size is given)."""
        if self.has_local_copy(href):
            return os.path.getsize(self._downloads[href])
        try:
            with self.client.open(self.locate(href), "HEAD") as resp:
                length = resp.getheader("Content-Length")
            if length is not None:
                return int(length)
            # No Content-Length (e.g. chunked): count the bytes instead
            with self.client.open(self.locate(href)) as resp:
                remaining = None if max_size is None else max_size + 1
                total = 0
                while remaining is None or remaining > 0:
                    chunk = resp.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    total += len(chunk)
                    if remaining is not None:
                        remaining -= len(chunk)
                return total
        except HttpStatusError as e:
            if e.code == 404:
                return None
            raise

    def package_checksum_ok(self, href, algo_name, expected_hex, max_size=None):
        if self.has_local_copy(href):
            with open(self._downloads[href], "rb") as f:
                return verify_checksum(f, algo_name, expected_hex, max_size)
        with self.client.open(self.locate(href)) as resp:
            return verify_checksum(resp, algo_name, expected_hex, max_size)

    def iter_rpms(self):
        for url in walk_http(self.client, self.root, skip_repodata=True):
            if url.endswith(".rpm"):
                yield os.path.normpath(unquote(url[len(self.root):]))


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.hrefs.append(value)


def list_http_dir(client, url):
    """Return (dirs, files) as absolute URLs for the entries linked from the
    directory listing at url. Sort links, the parent directory and anything
    outside of url are ignored."""
    with client.open(url) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    parser = _LinkParser()
    parser.feed(html)
    dirs, files = [], []
    for href in parser.hrefs:
        full = urldefrag(urljoin(url, href))[0]
        if "?" in full or full == url or not full.startswith(url):
            continue
        target = dirs if full.endswith("/") else files
        if full not in target:
            target.append(full)
    return dirs, files


def walk_http(client, top, skip_repodata=False):
    """Yield the URL of every file below the directory URL top, by following
    the web server's directory listings. Each directory is listed once, and
    directories nested deeper than MAX_LISTING_DEPTH (a symlink loop on the
    server, say) raise OSError."""
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
            seen.add(d)
            todo.append((d, depth + 1))


def open_repo(root, ssl_context=None):
    return HttpRepo(root, ssl_context) if is_url(root) else LocalRepo(root)


def make_ssl_context(args):
    """Build the TLS settings for https:// repos from the command line: by
    default the system CAs and no client certificate. The context is always
    made here, so that verification of the server does not depend on how the
    platform's Python is configured (as on RHEL 8, where a system file can
    turn it off). Raises ValueError with a message for unusable files."""
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
    if args.client_cert:
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


def read_repomd(repo):
    """Return the <data> entries of repomd.xml as dicts {type, href, size,
    checksum_type, checksum}, with the same keys as iter_packages() uses
    for the fields that describe a file. size and the checksum fields are
    None if not given."""
    repomd_path = repo.metadata_path("repodata/repomd.xml", MAX_REPOMD_SIZE)
    if repomd_path is None:
        raise RuntimeError(f"missing {repo.locate('repodata/repomd.xml')}")

    entries = []
    for data_el in ET.parse(repomd_path).getroot().findall(f"{NS_REPO}data"):
        data_type = data_el.get("type")
        loc = data_el.find(f"{NS_REPO}location")
        if loc is None or "href" not in loc.attrib:
            raise RuntimeError(f"{data_type} <location> missing href in repomd.xml")
        csum_el = data_el.find(f"{NS_REPO}checksum")
        size_el = data_el.find(f"{NS_REPO}size")
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
        for event, elem in ET.iterparse(fh, events=("end",)):
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


def verify_checksum(f, algo_name, expected_hex, max_size=None):
    """Hash the file-like object f. Returns None if the algo is unsupported.
    With max_size, no more than max_size + 1 bytes are read, and a longer
    file is a mismatch."""
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
        if remaining is not None:
            remaining -= len(chunk)
    if remaining is not None and remaining <= 0:
        return False  # more data than the metadata says
    return h.hexdigest() == expected_hex


def find_http_repos(top_url, ssl_context=None):
    """Like find_repos(), but for a directory URL: follows the web server's
    directory listings and returns the URLs (with trailing slash) of the
    repos found."""
    repos = []
    top_url = normalize_url(top_url)
    todo = [(top_url, 0)]
    seen = {top_url}
    client = HttpClient(ssl_context)
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
                seen.add(d)
                todo.append((d, depth + 1))
    finally:
        client.close()
    return sorted(repos)


def find_repos(top_level, ssl_context=None):
    """Recursively find directories under top_level that contain a valid
    repodata/repomd.xml. Does not descend into repodata/ itself."""
    if is_url(top_level):
        return find_http_repos(top_level, ssl_context)
    repos = []
    for dirpath, dirnames, _filenames in os.walk(top_level):
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
               lzma.LZMAError, zlib.error, ValueError)


def check_one_repo(repo_root, args):
    """Returns (status, count, problems, notes).
    status is one of 'ok', 'problems', 'error'.
    On 'error', problems is a single-element list with the error message.
    notes is a list of informational lines about what else was checked.
    """
    repo = open_repo(repo_root, args.ssl_context)
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


def check_package(repo, pkg, verify):
    """Check one package from primary.xml against the repo. Returns
    (pkg, problem, is_ok): problem is a message or None, and is_ok says
    whether the package should be reported as OK (a skipped checksum
    still counts as OK, but comes with a warning as the problem)."""
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
                result = repo.package_checksum_ok(href, pkg["checksum_type"], pkg["checksum"], pkg["size"])
            else:
                no_checksum = True
    except OSError as e:
        return pkg, f"UNREADABLE: {href}: {type(e).__name__}: {e}", False

    if no_checksum:
        return pkg, f"WARNING: no checksum in the metadata for {href}, skipped", True
    if result is False:
        return pkg, f"CHECKSUM MISMATCH: {href}", False
    if result is None:
        return pkg, f"WARNING: unsupported checksum algo '{pkg['checksum_type']}' for {href}, skipped", True
    return pkg, None, True


def check_packages(repo, packages, args, verify=None):
    """Yield check_package() results for the given packages, in order.
    verify(pkg) says whether to verify the checksum of a package; by
    default args.checksum decides. Remote repos are checked by args.jobs
    threads at a time; a local repo is checked in the calling thread."""
    if verify is None:
        def verify(pkg):
            return args.checksum

    def check(pkg):
        return check_package(repo, pkg, verify(pkg))

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
    for entry, problem, is_ok in check_packages(repo, files, args, verify):
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

    if zck_href is not None:
        zck_problems, note = _compare_primary_zck(repo, zck_href, sizes[zck_href], plain)
        problems.extend(zck_problems)
        if note:
            notes.append(note)

    # Last, so that a remote repo has downloaded what it needs by now and
    # the checksums of those files come for free. Listed first, though.
    meta_problems, note = verify_metadata(repo, entries, args)
    problems[:0] = meta_problems
    notes.insert(0, note)

    if args.extra:
        try:
            for rel in repo.iter_rpms():
                if rel not in seen_hrefs:
                    problems.append(f"ORPHAN (on disk, not in metadata): {rel}")
        except OSError as e:
            problems.append(f"CANNOT LIST FILES for --extra: {type(e).__name__}: {e}")

    status = "problems" if problems else "ok"
    return status, count, problems, notes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", metavar="path",
                    help="repo root(s), or (with --top-level) parent directories of multiple repos; "
                         "a directory or an http(s):// URL")
    ap.add_argument("--top-level", action="store_true",
                     help="treat each 'path' as a parent dir; auto-discover repos under it")
    ap.add_argument("--checksum", action="store_true", help="also verify checksums (slow)")
    ap.add_argument("--extra", action="store_true", help="report on-disk RPMs not in metadata")
    ap.add_argument("-n", "--newest-only", action="store_true",
                    help="only check the latest version of each package (like dnf reposync --newest-only)")
    ap.add_argument("--ignore-modules", action="store_true",
                    help="with --newest-only: ignore module metadata, like dnf 5's reposync does")
    ap.add_argument("-j", "--jobs", type=positive_int, default=DEFAULT_JOBS, metavar="N",
                    help=f"number of packages to check in parallel for http(s):// repos "
                         f"(default: {DEFAULT_JOBS}; 1 disables parallelism); ignored for directories")
    tls = ap.add_argument_group("TLS options for https:// repos (like sslcacert, sslclientcert, "
                                "sslclientkey and sslverify in dnf)")
    tls.add_argument("--ca-cert", metavar="FILE",
                     help="trust only the CA certificate(s) in this PEM file, or in this directory "
                          "(hashed as by openssl rehash), instead of the system CAs")
    tls.add_argument("--client-cert", metavar="FILE",
                     help="authenticate with this client certificate (PEM), e.g. an entitlement "
                          "certificate in /etc/pki/entitlement/ for the RHEL CDN")
    tls.add_argument("--client-key", metavar="FILE",
                     help="private key for --client-cert (PEM, not encrypted), if it isn't "
                          "in the --client-cert file")
    tls.add_argument("-k", "--insecure", action="store_true",
                     help="don't verify the server certificate (like sslverify=0)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"rrcc {__version__}",
                    help="print the version and exit")
    args = ap.parse_args()
    if args.ignore_modules and not args.newest_only:
        ap.error("--ignore-modules needs --newest-only")
    try:
        args.ssl_context = make_ssl_context(args)
    except ValueError as e:
        ap.error(str(e))
    if args.insecure:
        print("WARNING: --insecure: the identity of https:// servers is not verified", file=sys.stderr)

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
            try:
                found = find_repos(top_path, args.ssl_context)
            except (OSError, RuntimeError) as e:
                print(f"ERROR: cannot search {top_path}: {type(e).__name__}: {printable(e)}", file=sys.stderr)
                overall_error = True
                continue
            if not found:
                print(f"ERROR: no repos found under {printable(top_path)} (looked for */repodata/repomd.xml)", file=sys.stderr)
                overall_error = True
                continue

            def rel(r):
                return r[len(top_path):] or "." if is_url(top_path) else os.path.relpath(r, top_path)

            print(f"Found {len(found)} repo(s) under {printable(top_path)}:")
            for r in found:
                print(f"  {printable(rel(r))}")
                add_repo(r if len(args.paths) > 1 else rel(r), r)
            print()
        else:
            add_repo(top_path, top_path)

    for label, repo_root in repos:
        print(f"=== {printable(label)} ===")
        status, count, problems, notes = check_one_repo(repo_root, args)
        total_packages += count

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

    print(f"Summary: {len(repos)} repo(s), {total_packages} package(s) checked total.")
    if overall_error:
        print("Result: one or more repos could not be read.")
        return 2
    if overall_problem:
        print("Result: inconsistencies found.")
        return 1
    print("Result: all checked repos are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

