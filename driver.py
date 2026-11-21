# SPDX-License-Identifier: GPL-3.0-or-later
"""爱国者「星璨岚」屏显版驱动（USB HID，Baiyi／百一 平台，PID 0x0113）。

这块屏走两条完全不同的通道，都是实测出来的：

1) 命令通道 —— 5A 包裹的「HTTP」报文，**一帧必须装进一个 1024 字节报文**：

       5A | stuffed( LEN(2 BE) | BODY | CKSUM(1) ) | 5A
       LEN = len(BODY) + 5      CKSUM = sum(LEN + BODY) & 0xFF
       填充 0x5A→5B 01、0x5B→5B 02（只填 LEN/BODY/CKSUM，不碰两端 5A）
       BODY = "<METHOD> <resource> 1\r\n" + "Key=Value\r\n"… + "\r\n" + payload

   用于 conn（取设备信息）、brightness、mode、realtimeDisplay。

2) 图像通道 —— 0x5C 报头 + 原始 JPEG，每包 ≤1025 字节：

       00 | 5C | LEN(2 BE) | 21 | 00 | 包数 | 00 | seq(2 LE) | 13×00 | ≤1000B JPEG
       LEN = 本包长度 - 4

   三条实测约束：
   - seq 是**帧内**序号，每帧从 256 重新开始；跨帧累加会被当成同一条流的尾巴丢掉，
     只有第一帧能显示（症状：第一次出图，之后画面永远不动）
   - 设备必须处在 `mode 2`（可传输）；`mode 0` 时帧写得进、回包也 200，但屏上不显示
   - 固件 60 秒无流量把亮度调回 0；唤醒只在 open 时做，之后不要再往流里插命令帧

5A 信封格式参考 ryuo-iv-screen（GPL-3.0-or-later，作者 FoxPodZ，
https://github.com/FoxPodZ/ryuo-iv-screen，带 GPLv3 §7(b) 署名要求，再分发请保留）。
0x5C 图像报头来自对官方软件 HID 写入的抓包分析。
"""
from __future__ import annotations

import contextlib
import io
import json
import time
from collections.abc import Mapping
from typing import Any, ClassVar

from lcdmuse.lcd_comm import (
    Capability,
    DeviceBusy,
    DeviceInfo,
    DeviceNotFound,
    DeviceOffline,
    Frame,
    InvalidArgument,
    LcdComm,
    Unsupported,
)

PID = 0x0113
REPORT_SIZE = 1024                  # 命令通道报文长度
DEFAULT_SIZE = (1480, 720)
DELIM = 0x5A
METHODS = ("GET", "POST", "DELETE", "STATE")

PKT_MAGIC = 0x5C
PKT_HEADER = 25                     # 图像报头长度（含 hidapi 的 report id）
PKT_CHUNK = 1000                    # 每包携带的 JPEG 字节
PKT_KIND_IMAGE = 0x21
PKT_SEQ_START = 256                 # 每帧的起始包序号（厂商实测值）


# ── 命令通道：编码 / 解码（纯函数，可单测）──────────────────────────────
def stuff(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == DELIM:
            out += b"\x5b\x01"
        elif b == 0x5B:
            out += b"\x5b\x02"
        else:
            out.append(b)
    return bytes(out)


def unstuff(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == 0x5B and i + 1 < len(data):
            nxt = data[i + 1]
            out.append(DELIM if nxt == 0x01 else 0x5B if nxt == 0x02 else nxt)
            i += 2
            continue
        out.append(b)
        i += 1
    return bytes(out)


def build_frame(body: bytes) -> bytes:
    """组一个 1024 字节报文（不含 hidapi 的 report id 前缀）。"""
    if len(body) > REPORT_SIZE - 9:
        raise InvalidArgument(f"body 太长：{len(body)} 字节，单帧上限 {REPORT_SIZE - 9}")
    raw = (len(body) + 5).to_bytes(2, "big") + body
    framed = bytes([DELIM]) + stuff(raw + bytes([sum(raw) & 0xFF])) + bytes([DELIM])
    if len(framed) > REPORT_SIZE:
        raise InvalidArgument(f"填充后超出单报文：{len(framed)} > {REPORT_SIZE}")
    return framed.ljust(REPORT_SIZE, b"\x00")


def build_body(method: str, resource: str, headers: Mapping[str, Any] | None = None,
               payload: bytes = b"") -> bytes:
    method = method.upper()
    if method not in METHODS:
        raise InvalidArgument(f"不支持的方法 {method}")
    head = bytearray(f"{method} {resource} 1\r\n".encode())
    for key, value in (headers or {}).items():
        if value is None or value == -1:
            continue
        head += f"{key}={value}\r\n".encode()
    head += b"\r\n"
    return bytes(head) + payload


def parse_reply(raw: bytes) -> dict[str, Any] | None:
    """从字节流里解一帧回复；不合法返回 None。"""
    if DELIM not in raw:
        return None
    start = raw.index(DELIM)
    end = raw.find(DELIM, start + 1)
    if end < 0:
        return None
    body_bytes = unstuff(raw[start + 1:end])
    if len(body_bytes) < 3:
        return None
    body = body_bytes[2:-1]
    if int.from_bytes(body_bytes[0:2], "big") != len(body) + 5:
        return None
    if sum(body_bytes[0:2] + body) & 0xFF != body_bytes[-1]:
        return None

    text = body.decode("utf-8", "replace")
    line = text.partition("\r\n")[0]
    parts = line.split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    headers: dict[str, str] = {}
    payload = b""
    if "\r\n\r\n" in text:
        head, _, payload_text = text.partition("\r\n\r\n")
        for ln in head.split("\r\n")[1:]:
            key, _, value = ln.partition("=")
            if key:
                headers[key] = value
        payload = payload_text.encode()
    return {"status": status, "headers": headers, "payload": payload, "raw": body}


# ── 图像通道：切包（纯函数，可单测）─────────────────────────────────────
def image_packets(jpeg: bytes, seq: int = PKT_SEQ_START, chunk: int = PKT_CHUNK,
                  kind: int = PKT_KIND_IMAGE) -> list[bytes]:
    """把一张 JPEG 切成厂商格式的报文列表。

    ``seq`` 是帧内序号：每帧都从 256 重新开始、逐包 +1（见模块 docstring）。
    """
    if not jpeg:
        raise InvalidArgument("JPEG 为空")
    chunks = [jpeg[i:i + chunk] for i in range(0, len(jpeg), chunk)]
    total = len(chunks)
    if total > 255:
        raise InvalidArgument(f"单帧 {total} 包超出报头「包数」字段（≤255）")

    packets: list[bytes] = []
    for index, piece in enumerate(chunks):
        head = bytearray(PKT_HEADER)
        head[0] = 0x00                                    # report id（hidapi 要求）
        head[1] = PKT_MAGIC
        head[2:4] = (21 + len(piece)).to_bytes(2, "big")   # LEN = 包长 - 4
        head[4] = kind
        head[6] = total
        head[8:10] = ((seq + index) & 0xFFFF).to_bytes(2, "little")
        packets.append(bytes(head) + piece)
    return packets


class AigoXingcanlan(LcdComm):
    driver_id: ClassVar[str] = "aigo_xingcanlan"
    display_name: ClassVar[str] = "爱国者 星璨岚 屏显版"

    def __init__(self, config: Mapping[str, Any] | None = None):
        super().__init__(config)
        self._dev = None
        self._size = DEFAULT_SIZE
        self._spec: dict[str, Any] = {}
        self._cmd_seq = 0
        self._frames = 0

    # ── 探测 / 打开 ─────────────────────────────────────────────────────
    @classmethod
    def probe(cls, config: Mapping[str, Any] | None = None) -> DeviceInfo | None:
        dev = cls._find_device()
        if dev is None:
            return None
        width, height = DEFAULT_SIZE
        return DeviceInfo(
            id=str(dev.get("serial_number") or "aigo-0113"),
            name=cls.display_name,
            width=width,
            height=height,
            shape="rect",
            native_rotation=0,
            capabilities=Capability.PUSH_FULL,
            firmware="",
            extra={"path": cls._path_str(dev["path"]),
                   "vendor_id": f"{dev['vendor_id']:04x}", "product_id": f"{dev['product_id']:04x}"},
        )

    @staticmethod
    def _path_str(path: Any) -> str:
        return path.decode("utf-8", "replace") if isinstance(path, bytes) else str(path)

    @staticmethod
    def _find_device() -> dict[str, Any] | None:
        try:
            import hid
            devices = hid.enumerate()
        except (ImportError, OSError):
            return None
        for dev in devices:
            if dev.get("product_id") != PID:
                continue
            if dev.get("usage_page") not in (0xFF00, None):
                continue
            return dev
        return None

    def open(self) -> DeviceInfo:
        info = self.probe(self.config)
        if info is None:
            raise DeviceNotFound("没找到爱国者星璨岚屏（USB PID 0113）")
        try:
            import hid
            self._dev = hid.device()
            self._dev.open_path(info.extra["path"].encode())
            self._dev.set_nonblocking(1)
        except Exception as exc:
            self.close()
            raise DeviceBusy(f"HID 打不开（多半被官方软件占着）: {exc}") from exc

        reply = self.request("POST", "conn")
        if reply and reply.get("payload"):
            with contextlib.suppress(Exception):
                self._spec = json.loads(reply["payload"].decode("utf-8", "replace"))
        self._wake()                     # 之后不要再插命令帧，见 tick()

        return self.bind(DeviceInfo(
            id=info.id, name=info.name, width=self._size[0], height=self._size[1],
            shape="rect", native_rotation=int(self.config.get("rotation") or 0),
            capabilities=Capability.PUSH_FULL,
            firmware=str(self._spec.get("version") or ""),
            extra={**info.extra, "spec": self._spec or "未取到"},
        ))

    def close(self) -> None:
        if self._dev is not None:
            with contextlib.suppress(Exception):
                self._dev.close()
            self._dev = None

    # ── 命令通道 ────────────────────────────────────────────────────────
    def request(self, method: str, resource: str, headers: Mapping[str, Any] | None = None,
                payload: bytes = b"", timeout: float | None = None) -> dict[str, Any] | None:
        """发一条命令，返回匹配 AckNumber 的回复；超时返回 None。"""
        if self._dev is None:
            raise DeviceOffline("设备还没 open")
        self._cmd_seq += 1
        hdrs = {"SeqNumber": self._cmd_seq, "ContentLength": len(payload) if payload else -1}
        hdrs.update(dict(headers or {}))
        frame = build_frame(build_body(method, resource, hdrs, payload))
        try:
            self._dev.write(b"\x00" + frame)
        except Exception as exc:
            raise DeviceOffline(f"HID 写入失败: {exc}") from exc

        deadline = time.time() + (timeout if timeout is not None
                                  else float(self.config.get("timeout") or 1.5))
        buf = bytearray()
        while time.time() < deadline:
            got = self._dev.read(REPORT_SIZE)
            if not got:
                time.sleep(0.01)
                continue
            buf += bytes(got)
            while buf.count(DELIM) >= 2:
                reply = parse_reply(bytes(buf))
                if reply is None:
                    buf.clear()
                    break
                buf = buf[buf.index(DELIM, buf.index(DELIM) + 1) + 1:]
                ack = (reply.get("headers") or {}).get("AckNumber")
                if ack is not None:
                    try:
                        if int(ack) != self._cmd_seq + 1:
                            continue          # 上一条请求的残留回复
                    except ValueError:
                        pass
                return reply
        return None

    def _post_json(self, resource: str, obj: Mapping[str, Any],
                   timeout: float = 1.5) -> dict[str, Any] | None:
        body = json.dumps(obj).encode()
        return self.request("POST", resource,
                            {"ContentType": "json", "ContentLength": len(body)}, body, timeout=timeout)

    def set_brightness(self, percent: int) -> None:
        percent = int(percent)
        if not 0 <= percent <= 100:
            raise InvalidArgument(f"亮度 {percent} 超出 0-100")
        if not (self._post_json("brightness", {"value": percent}) or {}).get("status") == 200:
            raise DeviceOffline("设置亮度没得到 200")

    def set_mode(self, value: int) -> None:
        """0=默认 UI、2=可传输。推帧必须 2，否则帧写得进但不显示。"""
        if not (self._post_json("mode", {"value": int(value)}) or {}).get("status") == 200:
            raise DeviceOffline("切换模式没得到 200")

    def start_realtime(self, enable: bool = True) -> None:
        if not (self._post_json("realtimeDisplay", {"enable": bool(enable)}) or {}).get("status") == 200:
            raise DeviceOffline("切换实时显示没得到 200")

    # ── 图像通道 ────────────────────────────────────────────────────────
    def push_frame(self, frame: Frame) -> None:
        if self._dev is None:
            raise DeviceOffline("设备未打开")
        if frame.size != self._size:
            raise InvalidArgument(f"帧尺寸 {frame.size} 与设备 {self._size} 不一致")

        img = frame.to_image()
        rotation = int(self.config.get("rotation") or 0)
        if rotation % 360:
            img = img.rotate(-rotation % 360, expand=False)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG",
                                quality=int(self.config.get("jpeg_quality") or 70), optimize=False)
        packets = image_packets(buf.getvalue())

        gap = float(self.config.get("packet_gap", 0.002))
        for packet in packets:
            self._dev.write(packet)
            if gap > 0:
                time.sleep(gap)
        self._frames += 1

    def push_region(self, frame: Frame, x: int, y: int) -> None:
        raise Unsupported("爱国者 星璨岚 目前只按整帧推")

    def _wake(self) -> None:
        """把设备调到「能显示」的状态：亮度 → mode 2 → 实时通道，每步都要 200。"""
        steps = (
            ("brightness", lambda: self.set_brightness(int(self.config.get("brightness") or 100))),
            ("mode", lambda: self.set_mode(int(self.config.get("mode") if self.config.get("mode")
                                              is not None else 2))),
            ("realtimeDisplay", lambda: self.start_realtime(True)),
        )
        for name, action in steps:
            for _ in range(3):
                try:
                    action()
                    break
                except (DeviceOffline, DeviceBusy, InvalidArgument, OSError):
                    time.sleep(0.3)
            else:
                raise DeviceBusy(f"唤醒失败：{name} 三次都没成功，屏可能卡在休眠/错误状态")

    def tick(self, now: float) -> None:
        """刻意留空：这台固件把 HID 当一条图像流，中途插入命令帧会让流断开、整屏黑。
        需要唤醒时重新 open 设备即可（见 _wake）。"""

    def status(self) -> dict[str, Any]:
        return {"driver": self.driver_id, "size": list(self._size), "frames": self._frames,
                "open": self._dev is not None, "spec": self._spec or None}
