"""HTTP(S) servers and an HTTP proxy that run in a background thread of the
test process, on a free port of 127.0.0.1.

    with StaticServer(repo_dir) as srv:
        run_rrcc(srv.url)
        srv.stats["connections"]
"""

import base64
import http.client
import http.server
import os
import posixpath
import select
import socket
import socketserver
import ssl
import threading
from urllib.parse import unquote, urlsplit


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # clients hanging up early is expected, don't print tracebacks


class _BaseServer:
    """Runs handler_class in a thread. Subclasses set up self.stats."""

    scheme = "http"
    handler_class = None

    def __init__(self):
        self.stats = {}
        self._lock = threading.Lock()
        self._server = _ThreadingServer(("127.0.0.1", 0), self._make_handler())
        self._server.owner = self
        self.port = self._server.server_address[1]
        self._thread = None

    def _make_handler(self):
        return self.handler_class

    def bump(self, key, n=1):
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + n

    @property
    def url(self):
        return f"{self.scheme}://127.0.0.1:{self.port}/"

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.05,), daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


class _StaticHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive
    # Headers and body are written separately; with Nagle and delayed ACKs
    # every response on a kept-alive connection would take ~40 ms.
    disable_nagle_algorithm = True

    def log_message(self, *args):
        pass

    @property
    def owner(self):
        return self.server.owner

    def setup(self):
        super().setup()
        self.owner.bump("connections")

    def handle(self):
        if self.owner.drop_after_request:
            # Answer one request as if the connection stays open (HTTP/1.1
            # without 'Connection: close'), then drop it, like a server
            # whose keep-alive timeout ran out.
            self.handle_one_request()
        else:
            super().handle()

    def translate_path(self, path):
        # Like the stdlib version, but serving owner.root (Python 3.6 has no
        # 'directory' argument).
        path = unquote(urlsplit(path).path)
        trailing = path.endswith("/")
        parts = [p for p in posixpath.normpath(path).split("/") if p not in ("", ".", "..")]
        result = os.path.join(self.owner.root, *parts)
        return result + "/" if trailing else result

    def list_directory(self, path):
        if not self.owner.listings:
            self.send_error(403, "Directory listing disabled")
            return None
        return super().list_directory(path)

    def _count(self):
        self.owner.bump(self.command)
        self.owner.requests.append((self.command, self.path))

    def do_GET(self):
        self._count()
        if self._redirect():
            return
        super().do_GET()

    def do_HEAD(self):
        self._count()
        if self._redirect():
            return
        super().do_HEAD()

    def _redirect(self):
        prefix = self.owner.redirect_prefix
        if prefix and self.path.startswith(prefix):
            self.send_response(302)
            self.send_header("Location", (self.owner.redirect_target or "/") + self.path[len(prefix):])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False


class StaticServer(_BaseServer):
    """Serves the directory root, with directory listings like Apache's
    'Index of'. stats counts connections and requests per method, and
    requests lists (method, path) of every request.

    drop_after_request: close every connection after one request, without
        telling the client (tests the retry on a stale keep-alive connection).
    redirect_prefix: e.g. "/old/": requests below it get a 302 to the same
        path without the prefix.
    redirect_target: with redirect_prefix, where the redirect goes instead of
        this server, e.g. "http://127.0.0.1:1234/" (ends with a slash).
    listings: set to False to answer directory requests with 403.
    """

    handler_class = _StaticHandler

    def __init__(self, root, drop_after_request=False, redirect_prefix=None, listings=True,
                 redirect_target=None):
        self.root = root
        self.drop_after_request = drop_after_request
        self.redirect_prefix = redirect_prefix
        self.redirect_target = redirect_target
        self.listings = listings
        self.requests = []
        super().__init__()


class TLSServer(StaticServer):
    """StaticServer over HTTPS, with the test certificate for 'localhost'
    from tests/support/pki/. With require_client_cert, clients must present
    a certificate signed by the test CA."""

    scheme = "https"

    def __init__(self, root, pki_dir, require_client_cert=False, **kwargs):
        super().__init__(root, **kwargs)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(os.path.join(pki_dir, "server.pem"), os.path.join(pki_dir, "server.key"))
        if require_client_cert:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(os.path.join(pki_dir, "ca.pem"))
        self._server.socket = ctx.wrap_socket(self._server.socket, server_side=True)

    @property
    def url(self):
        # the name in the certificate
        return f"https://localhost:{self.port}/"


# The proxy only forwards to the test servers, never anywhere else.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def log_message(self, *args):
        pass

    @property
    def owner(self):
        return self.server.owner

    def setup(self):
        super().setup()
        self.owner.bump("connections")

    def _authorized(self):
        auth = self.owner.auth
        if auth is None:
            return True
        expected = "Basic " + base64.b64encode(auth.encode()).decode()
        if self.headers.get("Proxy-Authorization") == expected:
            return True
        self.owner.bump("denied")
        self.send_response(407)
        self.send_header("Proxy-Authenticate", 'Basic realm="test"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _forward(self):
        if not self.path.startswith("http://"):
            self.send_error(400, "Not a proxy request")
            return
        if not self._authorized():
            return
        target = urlsplit(self.path)
        host = target.hostname
        if host not in _LOOPBACK_HOSTS:
            self.send_error(403, "Only loopback targets are allowed")
            return
        self.owner.bump(self.command)
        conn = http.client.HTTPConnection(host, target.port or 80, timeout=30)
        try:
            conn.request(self.command, (target.path or "/") + ("?" + target.query if target.query else ""))
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() not in ("connection", "keep-alive", "transfer-encoding", "content-length"):
                self.send_header(key, value)
        length = resp.getheader("Content-Length") if self.command == "HEAD" else None
        self.send_header("Content-Length", length or str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = _forward
    do_HEAD = _forward

    def do_CONNECT(self):
        if not self._authorized():
            return
        host, port = self.path.rsplit(":", 1)
        if host not in _LOOPBACK_HOSTS:
            self.send_error(403, "Only loopback targets are allowed")
            return
        self.owner.bump("CONNECT")
        upstream = socket.create_connection((host, int(port)), timeout=30)
        self.send_response(200, "Connection established")
        self.end_headers()
        socks = [self.connection, upstream]
        try:
            while True:
                readable, _, _ = select.select(socks, [], [], 30)
                if not readable:
                    return
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    (upstream if s is self.connection else self.connection).sendall(data)
        finally:
            upstream.close()
            self.close_connection = True


class ProxyServer(_BaseServer):
    """A forwarding HTTP proxy that also supports CONNECT tunnels. stats
    counts client connections, forwarded requests per method, CONNECTs and
    requests denied for lack of credentials. With auth="user:password",
    basic proxy authentication is required."""

    handler_class = _ProxyHandler

    def __init__(self, auth=None):
        self.auth = auth
        super().__init__()

    @property
    def address(self):
        return f"127.0.0.1:{self.port}"
