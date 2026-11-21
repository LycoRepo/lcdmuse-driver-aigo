# SPDX-License-Identifier: GPL-3.0-or-later
"""帧编码/解码的单测（不碰设备）。格式依据 ryuo-iv-screen 的逆向文档。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import driver  # noqa: E402
from lcdmuse.lcd_comm import Frame, InvalidArgument, Unsupported  # noqa: E402


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


def test_push_frame_refuses_until_resource_configured():
    drv = driver.AigoXingcanlan({"image_resource": ""})
    drv._size = (1480, 720)
    with pytest.raises(Unsupported):
        drv.push_frame(Frame(width=1480, height=720, data=b"\x00" * (1480 * 720 * 3)))


def test_probe_returns_none_without_device(monkeypatch):
    monkeypatch.setattr(driver.AigoXingcanlan, "_find_device", staticmethod(lambda: None))
    assert driver.AigoXingcanlan.probe({}) is None
