"""The HTTPS server must keep answering while a client sits on an unfinished TLS handshake."""
import http.client
import shutil
import socket
import ssl
import subprocess
import threading

import pytest
from flask import Flask
from werkzeug.serving import make_server

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl CLI needed to make a test certificate")


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    crt, key = d / "cert.pem", d / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                    "-keyout", str(key), "-out", str(crt)], check=True, capture_output=True)
    return str(crt), str(key)


def serve(ssl_context, request_handler=None):
    tiny = Flask("tiny")
    tiny.add_url_rule("/ping", "ping", lambda: "pong")
    kw = {"request_handler": request_handler} if request_handler else {}
    srv = make_server("127.0.0.1", 0, tiny, threaded=True, ssl_context=ssl_context, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def ping(port, timeout):
    ctx = ssl._create_unverified_context()
    c = http.client.HTTPSConnection("127.0.0.1", port, timeout=timeout, context=ctx)
    try:
        c.request("GET", "/ping")
        return c.getresponse().read()
    finally:
        c.close()


def test_silent_client_does_not_block_other_requests(app_mod, cert):
    srv = serve(app_mod.tls_context(*cert), app_mod.TimeoutRequestHandler)
    silent = socket.create_connection(("127.0.0.1", srv.server_port))   # connects, never says hello
    try:
        assert ping(srv.server_port, timeout=5) == b"pong"
        assert ping(srv.server_port, timeout=5) == b"pong"
    finally:
        silent.close()
        srv.shutdown()


def test_plain_context_reproduces_the_hang(cert):
    """What the dashboard did before: the handshake in the accept loop blocks everyone behind a silent client."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(*cert)
    srv = serve(ctx)
    silent = socket.create_connection(("127.0.0.1", srv.server_port))
    try:
        with pytest.raises((socket.timeout, TimeoutError, ssl.SSLError, ConnectionError)):
            ping(srv.server_port, timeout=2)
    finally:
        silent.close()
        srv.shutdown()


def test_silent_client_is_dropped_after_timeout(app_mod, cert, monkeypatch):
    monkeypatch.setattr(app_mod.TimeoutRequestHandler, "timeout", 1)
    srv = serve(app_mod.tls_context(*cert), app_mod.TimeoutRequestHandler)
    silent = socket.create_connection(("127.0.0.1", srv.server_port))
    silent.settimeout(5)
    try:
        assert silent.recv(1) == b""                                      # server closed it
    except ConnectionError:
        pass                                                              # or reset it: also closed
    finally:
        silent.close()
        srv.shutdown()
