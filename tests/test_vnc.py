"""Offline checks for the built-in VNC pieces."""
import struct

import pytest

from kasm_use import vnc


def test_des_known_answer():
    # Classic DES test vector (FIPS 46 worked example).
    key, block = bytes.fromhex("133457799BBCDFF1"), bytes.fromhex("0123456789ABCDEF")
    assert vnc._des_encrypt(key, block).hex().upper() == "85E813540F0AB405"


def test_vnc_auth_response_is_two_blocks():
    assert len(vnc.vnc_auth_response(bytes(16))) == 16


def test_zrle_solid_and_raw_tiles():
    # 2x1 rect: one tile, raw subencoding, pixels B,G,R little-endian CPIXELs.
    data = bytes([0]) + bytes([3, 2, 1]) + bytes([6, 5, 4])
    assert bytes(vnc._zrle_decode(data, 2, 1)) == bytes([1, 2, 3, 4, 5, 6])
    solid = bytes([1, 30, 20, 10])
    assert bytes(vnc._zrle_decode(solid, 2, 2)) == bytes([10, 20, 30]) * 4


def test_zrle_palette_rle():
    # palette of 2 colours (sub 130); index 0 run of 3 (128|0, run byte 2), then index 1 single.
    data = bytes([130, 0, 0, 255, 255, 0, 0, 128, 2, 1])
    assert bytes(vnc._zrle_decode(data, 4, 1)) == bytes([255, 0, 0]) * 3 + bytes([0, 0, 255])


def test_keysyms():
    assert vnc.keysym("ctrl") == 0xFFE3 and vnc.keysym("Return") == 0xFF0D and vnc.keysym("a") == ord("a")
    assert vnc.char_keysym("é") == 0xE9 and vnc.char_keysym("€") == 0x010020AC


def test_pointer_message_is_kasmvnc_11_bytes():
    sent = []
    s = vnc.Session.__new__(vnc.Session)
    s.width, s.height = 100, 100
    s.ws = type("W", (), {"send": lambda self, b: sent.append(b)})()
    s.pointer(5, 6, 1)
    assert len(sent[0]) == 11 and struct.unpack(">BHHHhh", sent[0]) == (5, 1, 5, 6, 0, 0)


def test_failed_authentication_closes_websocket(monkeypatch):
    class RejectingWebSocket:
        def __init__(self, *_args):
            self.responses = [b"RFB 003.008\n", b"\x01", b"\x01", b"\0\0\0\1"]
            self.closed = False

        def read(self, _length):
            return self.responses.pop(0)

        def send(self, _data):
            pass

        def close(self):
            self.closed = True

    socket = RejectingWebSocket()
    monkeypatch.setattr(vnc, "_WebSocket", lambda *_args: socket)

    with pytest.raises(PermissionError, match="authentication rejected"):
        vnc.Session("https://kasm.invalid", "desktop/sid/vnc", "alice", "stale", None)

    assert socket.closed


@pytest.mark.parametrize("status_code", [401, 403])
def test_http_auth_rejection_closes_websocket(monkeypatch, status_code):
    class HandshakeSocket:
        def __init__(self):
            self.response = f"HTTP/1.1 {status_code} Rejected\r\nContent-Length: 0\r\n\r\n".encode()
            self.closed = False

        def sendall(self, _data):
            pass

        def recv(self, _size):
            response, self.response = self.response, b""
            return response

        def close(self):
            self.closed = True

    sock = HandshakeSocket()
    monkeypatch.setattr(vnc.socket, "create_connection", lambda *_args, **_kwargs: sock)

    with pytest.raises(vnc.AuthenticationError, match=str(status_code)):
        vnc._WebSocket("ws://kasm.invalid/desktop/sid/vnc/websockify", {}, None)

    assert sock.closed


def test_drag_holds_button_while_moving_then_releases(monkeypatch):
    monkeypatch.setattr(vnc.time, "sleep", lambda s: None)
    sent = []
    s = vnc.Session.__new__(vnc.Session)
    s.width, s.height = 100, 100
    s.ws = type("W", (), {"send": lambda self, b: sent.append(struct.unpack(">BHHHhh", b))})()
    s.drag(10, 10, 50, 30, steps=4)
    assert sent[0][1:4] == (0, 10, 10)                       # move to start, no button
    assert sent[1][1:4] == (1, 10, 10)                       # press at start
    assert [m[1] for m in sent[2:-1]] == [1, 1, 1, 1]        # held through every step
    assert sent[-2][2:4] == (50, 30)                         # last step reaches the target
    assert sent[-1][1:4] == (0, 50, 30)                      # release at the target


# ------------------------------------------------------------------------------ desktop size
class _Wire:
    def __init__(self, incoming=b""):
        self.sent, self.incoming = [], bytearray(incoming)

    def send(self, data):
        self.sent.append(bytes(data))

    def read(self, n):
        out = bytes(self.incoming[:n])
        del self.incoming[:n]
        assert len(out) == n, "read past the end of the scripted server data"
        return out


def _bare_session(incoming=b"", width=4, height=2):
    session = object.__new__(vnc.Session)
    session.ws, session.width, session.height, session._bpp = _Wire(incoming), width, height, 4
    return session


def _extended_desktop_size(width, height, status=0, reason=1):
    # pseudo-rectangle -308: x = reason, y = status, w/h = size; then U8 screens, 3 pad, 16 bytes per screen
    return (struct.pack(">HHHHi", reason, status, width, height, -308)
            + struct.pack(">B3x", 1) + struct.pack(">IHHHHI", 0, 0, 0, width, height, 0))


def _raw_rect(width, height, pixel=b"\x01\x02\x03\x00"):
    return struct.pack(">HHHHi", 0, 0, width, height, 0) + pixel * (width * height)


def test_set_desktop_size_message():
    session = _bare_session()
    session.set_desktop_size(1280, 800)
    assert session.ws.sent == [
        bytes([251, 0]) + struct.pack(">HH", 1280, 800) + bytes([1, 0])
        + struct.pack(">IHHHHI", 0, 0, 0, 1280, 800, 0)]


def test_extended_desktop_size_is_announced_with_the_encodings(monkeypatch):
    sent = []

    class Handshake:
        def __init__(self, *_args):
            self.incoming = bytearray(
                b"RFB 003.008\n" + bytes([1, 1]) + struct.pack(">I", 0)
                + struct.pack(">HH", 1024, 768) + bytes(16) + struct.pack(">I", 0))

        def read(self, n):
            out = bytes(self.incoming[:n])
            del self.incoming[:n]
            return out

        def send(self, data):
            sent.append(bytes(data))

        def close(self):
            pass

    monkeypatch.setattr(vnc, "_WebSocket", Handshake)
    vnc.Session("https://kasm.invalid", "desktop/x/vnc", "u", "t", None)
    assert sent[-1] == struct.pack(">BxHiii", 2, 3, 16, 0, -308)


def _update_message(*rects):
    return bytes([0, 0]) + struct.pack(">H", len(rects)) + b"".join(rects)


def test_an_accepted_resize_updates_the_size():
    session = _bare_session(_update_message(_extended_desktop_size(6, 3)))
    assert session._update(0, 0, 1, 1) == []  # a size-only update is the whole answer; nothing else follows
    assert (session.width, session.height) == (6, 3)


def test_a_refused_resize_keeps_the_size():
    session = _bare_session(_update_message(_extended_desktop_size(6, 3, status=1), _raw_rect(4, 2)))
    session._update(0, 0, 4, 2)
    assert (session.width, session.height) == (4, 2)


def test_messages_about_other_viewers_joining_and_leaving_are_skipped():
    joined = bytes([253]) + struct.pack(">I", 9) + b"kasm_user"
    left = bytes([254]) + struct.pack(">I", 9) + b"kasm_user"
    session = _bare_session(joined + left + _update_message(_raw_rect(4, 2)))
    assert len(session._update(0, 0, 4, 2)) == 1
    assert not session.ws.incoming


FULL, INCREMENTAL = 0, 1


def _request(incremental, width, height):
    return struct.pack(">BBHHHH", 3, incremental, 0, 0, width, height)


def test_screenshot_reads_pixels_sent_straight_away():
    session = _bare_session(_update_message(_raw_rect(4, 2)))
    assert struct.unpack(">II", session.screenshot()[16:24]) == (4, 2)
    assert session.ws.sent == [_request(FULL, 4, 2)]


def test_screenshot_follows_a_size_only_answer_with_an_incremental_request():
    # KasmVNC, once resize support is announced: a full request is answered with the size alone,
    # and the pixels only come with the next incremental request.
    session = _bare_session(_update_message(_extended_desktop_size(4, 2)) + _update_message(_raw_rect(4, 2)))
    png = session.screenshot()
    assert struct.unpack(">II", png[16:24]) == (4, 2) and session.blank is True
    assert session.ws.sent == [_request(FULL, 4, 2), _request(INCREMENTAL, 4, 2)]
    assert not session.ws.incoming


def test_screenshot_asks_again_at_the_new_size_when_the_desktop_changed_under_it():
    session = _bare_session(
        _update_message(_extended_desktop_size(6, 3))
        + _update_message(_extended_desktop_size(6, 3)) + _update_message(_raw_rect(6, 3)))
    assert struct.unpack(">II", session.screenshot()[16:24]) == (6, 3)
    assert session.ws.sent == [_request(FULL, 4, 2), _request(FULL, 6, 3), _request(INCREMENTAL, 6, 3)]
    assert not session.ws.incoming


def test_screenshot_gives_up_when_the_desktop_never_settles():
    sizes = [(6, 3), (4, 2), (6, 3), (4, 2)]
    session = _bare_session(b"".join(_update_message(_extended_desktop_size(*size)) for size in sizes))
    with pytest.raises(ConnectionError, match="changing size"):
        session.screenshot()
