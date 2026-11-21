# SPDX-License-Identifier: GPL-3.0-or-later
"""帧编码/解码的单测（不碰设备）。格式依据 ryuo-iv-screen 的逆向文档。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import driver  # noqa: E402
from lcdmuse.lcd_comm import DeviceOffline, Frame, InvalidArgument  # noqa: E402


def test_stuff_and_unstuff_round_trip():
    raw = bytes([0x00, 0x5A, 0x11, 0x5B, 0xFF])
    assert driver.stuff(raw) == bytes([0x00, 0x5B, 0x01, 0x11, 0x5B, 0x02, 0xFF])
    assert driver.unstuff(driver.stuff(raw)) == raw


def test_build_frame_layout():
    body = b"GET spec 1\r\n\r\n"
    frame = driver.build_frame(body)
    assert len(frame) == driver.REPORT_SIZE          # 补零到 1024
    assert frame[0] == 0x5A
    inner = frame[1:frame.index(b"\x5a", 1)]
    raw = driver.unstuff(inner)
    assert int.from_bytes(raw[0:2], "big") == len(body) + 5
    assert raw[2:-1] == body
    assert raw[-1] == sum(raw[0:2] + body) & 0xFF


def test_build_frame_rejects_oversize_body():
    with pytest.raises(InvalidArgument):
        driver.build_frame(b"x" * (driver.REPORT_SIZE - 5))


def test_build_body_headers_and_blank_line():
    body = driver.build_body("post", "all", {"SeqNumber": 3, "Skip": -1}, b"{}")
    assert body.startswith(b"POST all 1\r\n")
    assert b"SeqNumber=3\r\n" in body
    assert b"Skip=" not in body
    assert body.endswith(b"\r\n\r\n{}")


def test_parse_reply_accepts_valid_frame():
    payload = b'{"width":1480}'
    body = b"1 200\r\nContentLength=" + str(len(payload)).encode() + b"\r\n\r\n" + payload
    frame = driver.build_frame(body)
    reply = driver.parse_reply(frame)
    assert reply["status"] == 200
    assert reply["payload"] == payload


def test_parse_reply_rejects_bad_checksum():
    body = b"1 200\r\n\r\n"
    frame = bytearray(driver.build_frame(body))
    frame[5] ^= 0xFF                                   # 破坏校验和
    assert driver.parse_reply(bytes(frame)) is None


class FakeHid:
    """记录写出的报文，模拟 hidapi 设备。"""

    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)


def test_image_packets_layout():
    """报头 25 字节：report id / 5C / LEN(大端) / kind / 00 / N / 00 / seq(小端)。"""
    jpeg = bytes(range(256)) * 9                    # 2304 字节 → 3 包
    packets = driver.image_packets(jpeg, seq=100)

    assert len(packets) == 3
    first = packets[0]
    assert first[0] == 0x00
    assert first[1] == 0x5C
    assert int.from_bytes(first[2:4], "big") == len(first) - 4
    assert first[4] == 0x21
    assert first[6] == 3                            # blockCount
    assert int.from_bytes(first[8:10], "little") == 100
    assert len(first) == 25 + 1000

    assert int.from_bytes(packets[2][8:10], "little") == 102
    assert len(packets[2]) == 25 + 304
    assert b"".join(p[25:] for p in packets) == jpeg


def test_image_packets_rejects_too_many_blocks():
    with pytest.raises(InvalidArgument):
        driver.image_packets(bytes(300 * 1000))


def test_push_frame_writes_packets_and_advances_seq(monkeypatch):
    drv = driver.AigoXingcanlan({"packet_gap": 0})
    drv._size = (16, 8)
    dev = FakeHid()

    def fake_req(*args, **kwargs):
        return {"status": 200, "headers": {}, "payload": b""}

    monkeypatch.setattr(drv, "request", fake_req)
    drv._dev = dev
    drv.push_frame(Frame(width=16, height=8, data=bytes(16 * 8 * 3)))

    assert len(dev.writes) >= 1
    assert all(len(w) <= 1025 for w in dev.writes)
    assert all(w[1] == 0x5C for w in dev.writes)
    assert all(w[6] == len(dev.writes) for w in dev.writes)
    assert drv.status()["frames"] == 1
    # 每帧 seq 从 256 开始（厂商实测），所以推完 = 256 + 包数
    assert drv.status()["seq"] == 256 + len(dev.writes)
    assert int.from_bytes(dev.writes[0][8:10], "little") == 256


def test_push_frame_requires_open_device():
    drv = driver.AigoXingcanlan({})
    drv._size = (16, 8)
    with pytest.raises(DeviceOffline):
        drv.push_frame(Frame(width=16, height=8, data=bytes(16 * 8 * 3)))


def test_probe_returns_none_without_device(monkeypatch):
    monkeypatch.setattr(driver.AigoXingcanlan, "_find_device", staticmethod(lambda: None))
    assert driver.AigoXingcanlan.probe({}) is None

def test_wake_sets_mode_two_and_brightness(monkeypatch):
    """推帧前必须切到 mode 2（mode 0 时帧不显示），并把亮度拉回来。"""
    drv = driver.AigoXingcanlan({})
    seen = []

    def fake_request(method, resource, headers=None, payload=b"", timeout=None):
        seen.append((resource, payload))
        return {"status": 200, "headers": {}, "payload": b""}

    monkeypatch.setattr(drv, "request", fake_request)
    drv._wake()

    resources = [r for r, _ in seen]
    assert "brightness" in resources
    assert "mode" in resources
    assert "realtimeDisplay" in resources
    mode_body = next(p for r, p in seen if r == "mode")
    assert b'"value":2' in mode_body.replace(b" ", b"")
