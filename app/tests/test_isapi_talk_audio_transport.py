"""ISAPI two-way-audio transport (2026-09-27, found on the real Front Door).

The camera accepted /open and a chunked-transfer audioData upload of the
whole greeting, yet played nothing; the Videoloft app talked through the
same camera fine. Cameras expect PUT audioData with Content-Length: 0,
answer 200, and then read raw G.711 bytes from that TCP connection. This
fake camera speaks real HTTP over a real socket, with digest auth, and
records exactly what arrives."""
import hashlib
import socket
import threading
import time

import pytest

import talk_audio_relay

REALM, NONCE = "IP Camera", "abc123nonce"


def _ha(text):
    return hashlib.md5(text.encode()).hexdigest()


class FakeIsapiCamera:
    def __init__(self, *, audio_status=200, password="p"):
        self.audio_status = audio_status
        self.password = password
        self.requests = []  # (method, path, headers)
        self.audio = b""
        self.audio_done = threading.Event()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._stop = True
        self.server.close()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _authorized(self, method, path, header):
        if not header.startswith("Digest "):
            return False
        fields = dict(part.strip().split("=", 1) for part in header[7:].split(","))
        fields = {k: v.strip('"') for k, v in fields.items()}
        ha1 = _ha(f"{fields['username']}:{REALM}:{self.password}")
        ha2 = _ha(f"{method}:{path}")
        expected = _ha(f"{ha1}:{NONCE}:{fields['nc']}:{fields['cnonce']}:auth:{ha2}")
        return fields.get("response") == expected

    def _serve(self, conn):
        buffer = b""
        try:
            while True:
                while b"\r\n\r\n" not in buffer:
                    piece = conn.recv(4096)
                    if not piece:
                        return
                    buffer += piece
                head, _, buffer = buffer.partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                method, path, _ = lines[0].split(" ", 2)
                headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
                length = int(headers.get("content-length") or 0)
                while len(buffer) < length:
                    buffer += conn.recv(4096)
                buffer = buffer[length:]
                self.requests.append((method, path, headers))
                if not self._authorized(method, path, headers.get("authorization", "")):
                    challenge = f'Digest realm="{REALM}", nonce="{NONCE}", qop="auth"'
                    conn.sendall(f"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: {challenge}\r\nContent-Length: 0\r\n\r\n".encode())
                    continue
                if path.endswith("/audioData"):
                    conn.sendall(f"HTTP/1.1 {self.audio_status} OK\r\nContent-Length: 0\r\n\r\n".encode())
                    if self.audio_status != 200:
                        continue
                    self.audio = buffer
                    while True:  # raw audio until the client closes
                        piece = conn.recv(4096)
                        if not piece:
                            break
                        self.audio += piece
                    self.audio_done.set()
                    return
                body = b"<ResponseStatus><statusCode>1</statusCode></ResponseStatus>"
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        except OSError:
            return
        finally:
            conn.close()


@pytest.fixture()
def camera():
    fake = FakeIsapiCamera()
    yield fake
    fake.close()


def _relay(monkeypatch, port, camera_id="cam-transport"):
    monkeypatch.setattr(talk_audio_relay._LocalIsapiTalkRelay, "_target", lambda self: {
        "host": f"127.0.0.1:{port}", "username": "u", "password": "p", "channel_id": "1",
    })
    talk_audio_relay._camera_auth_failures.pop(camera_id, None)
    return talk_audio_relay._LocalIsapiTalkRelay({"id": camera_id, "name": "Front Door"}, 8000, "sess-transport")


def test_audio_reaches_the_camera_as_raw_bytes_after_a_content_length_zero_put(camera, monkeypatch):
    relay = _relay(monkeypatch, camera.port)
    assert relay.start() is True
    payload = bytes(range(256)) * 20  # stands in for G.711 mu-law chunks
    for offset in range(0, len(payload), 800):
        relay.queue.put(payload[offset:offset + 800])
    relay.stop()
    relay.thread.join(timeout=10)
    assert camera.audio_done.wait(5)

    assert camera.audio == payload  # every byte, in order, with no chunk framing mixed in
    audio_requests = [h for m, p, h in camera.requests if p.endswith("/audioData")]
    authed = [h for h in audio_requests if h.get("authorization")]
    assert len(authed) == 1
    assert authed[0]["content-length"] == "0"
    assert "transfer-encoding" not in authed[0]
    assert authed[0]["content-type"] == "application/octet-stream"
    paths = [p.rsplit("/", 1)[-1] for m, p, h in camera.requests if h.get("authorization")]
    assert paths == ["open", "audioData", "close"]  # opened, streamed, then closed
    assert relay.error is None


def test_audio_upload_rejected_by_the_camera_is_reported_and_starts_the_login_cooldown(monkeypatch):
    fake = FakeIsapiCamera(audio_status=403)
    try:
        relay = _relay(monkeypatch, fake.port, camera_id="cam-reject")
        relay.start()
        relay.stop()
        relay.thread.join(timeout=10)
        assert relay.error_reason == "camera_auth_failed"
        assert talk_audio_relay.camera_auth_cooldown_remaining("cam-reject") > 0
        # no /close digest retry after the camera refused the upload
        assert not [p for m, p, h in fake.requests if p.endswith("/close")]
    finally:
        fake.close()
        talk_audio_relay._camera_auth_failures.pop("cam-reject", None)


def test_wrong_password_never_streams_and_is_not_retried(monkeypatch):
    fake = FakeIsapiCamera(password="other")
    try:
        relay = _relay(monkeypatch, fake.port, camera_id="cam-badpw")
        assert relay.start() is False
        relay.thread.join(timeout=10)
        assert relay.error_reason == "camera_auth_failed"
        assert fake.audio == b""
        authed_opens = [p for m, p, h in fake.requests if p.endswith("/open") and h.get("authorization")]
        assert len(authed_opens) == 1  # one login attempt, then the cooldown
    finally:
        fake.close()
        talk_audio_relay._camera_auth_failures.pop("cam-badpw", None)


def test_upload_helper_streams_even_when_the_camera_sends_no_immediate_reply(monkeypatch):
    """Some firmware only answers audioData when the upload ends."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    received = {}

    def serve():
        conn, _ = server.accept()
        data = b""
        while True:
            piece = conn.recv(4096)
            if not piece:
                break
            data += piece
        received["data"] = data
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    started = time.monotonic()
    upload = talk_audio_relay.open_isapi_audio_upload(f"127.0.0.1:{port}", "/ISAPI/System/TwoWayAudio/channels/1/audioData", "u", "p", timeout=0.5)
    assert upload.status_code is None
    upload.sendall(b"\x7f" * 400)
    upload.close()
    thread.join(timeout=5)
    server.close()
    head, _, audio = received["data"].partition(b"\r\n\r\n")
    assert b"Content-Length: 0" in head and audio == b"\x7f" * 400
    assert time.monotonic() - started < 5
