# SPDX-License-Identifier: GPL-3.0-or-later
"""爱国者「星璨岚」屏显版驱动（USB HID 厂商自定义，1024 字节报文）。

这块屏和 ASUS ROG Ryuo IV 是同一套 Baiyi（百一）方案：
厂商自定义 HID（usage page 0xFF00）、1024 字节报文、帧用 0x5A 包裹并做字节填充。

帧格式来自 ryuo-iv-screen 的逆向文档与实现
（https://github.com/FoxPodZ/ryuo-iv-screen，GPL-3.0-or-later，作者 FoxPodZ (foxpodz.de)）。
该仓库带 GPLv3 §7(b) 附加条款，要求保留作者署名与本链接；本文件与 README 均已保留，
再分发时也请一并保留。

    5A | stuffed( LEN(2 BE) | BODY | CKSUM(1) ) | 5A
    LEN   = len(BODY) + 5
    CKSUM = sum(LEN + BODY) & 0xFF
    填充  0x5A -> 5B 01，0x5B -> 5B 02（只对 LEN/BODY/CKSUM 做，不碰两端的 5A）
    整帧必须装进一个 1024 字节报文，不足补零

BODY 是「重新发明的 HTTP」：

    "<METHOD> <resource> 1\\r\\n" + 若干 "Key=Value\\r\\n" + "\\r\\n" + payload
    方法：GET / POST / DELETE / STATE

本驱动目前做到「能把请求发出去、能把回复解出来」；具体哪个 resource 负责推图
（画面是整帧 JPEG 还是别的编码）还没有实测样本，所以在 `image_resource` 没配置之前
push_frame 会明确拒绝，而不是假装成功。
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, ClassVar

from lcdmuse.lcd_comm import (Capability, DeviceBusy, DeviceInfo, DeviceNotFound, DeviceOffline, Frame,
                              InvalidArgument, LcdComm, Unsupported)

PID = 0x0113
VID_HINTS = (0x1D6B, 0x1C75, 0x0B05)     # 固件换过厂商 id，认 PID 更稳
REPORT_SIZE = 1024
DELIM = 0x5A
DEFAULT_SIZE = (1480, 720)

METHODS = ("GET", "POST", "DELETE", "STATE")


# ── 帧编码/解码（纯函数，可单测）─────────────────────────────────────────
def stuff(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == 0x5A:
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
            out.append(0x5A if nxt == 0x01 else 0x5B if nxt == 0x02 else nxt)
            i += 2
            continue
        out.append(b)
        i += 1
    return bytes(out)


def build_frame(body: bytes) -> bytes:
    """组一个 1024 字节报文载荷（不含 report id）。"""
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
    """从若干报文拼出的字节流里解一帧回复。"""
    if DELIM not in raw:
        return None
    start = raw.index(DELIM)
    end = raw.find(DELIM, start + 1)
    if end < 0:
        return None
    body_bytes = unstuff(raw[start + 1:end])
    if len(body_bytes) < 3:
        return None
    length = int.from_bytes(body_bytes[0:2], "big")
    body = body_bytes[2:-1]
    if length != len(body) + 5:
        return None
    if sum(body_bytes[0:2] + body) & 0xFF != body_bytes[-1]:
        return None

    text = body.decode("utf-8", "replace")
    line, _, rest = text.partition("\r\n")
    parts = line.split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    headers: dict[str, str] = {}
    payload = b""
    if "\r\n\r\n" in text:
        head, _, payload_text = text.partition("\r\n\r\n")
        for ln in head.split("\r\n")[1:]:
            k, _, v = ln.partition("=")
            if k:
                headers[k] = v
        payload = payload_text.encode()
    return {"status": status, "headers": headers, "payload": payload, "raw": body}


class AigoXingcanlan(LcdComm):
    driver_id: ClassVar[str] = "aigo_xingcanlan"
    display_name: ClassVar[str] = "爱国者 星璨岚 屏显版"

    def __init__(self, config: Mapping[str, Any] | None = None):
        super().__init__(config)
        self._dev = None
        self._size = DEFAULT_SIZE
        self._spec: dict[str, Any] = {}
        self._seq = 0

    # ── 探测 ────────────────────────────────────────────────────────────
    @classmethod
    def probe(cls, config: Mapping[str, Any] | None = None) -> DeviceInfo | None:
        dev = cls._find_device()
        if dev is None:
            return None
        width, height = DEFAULT_SIZE
        return DeviceInfo(
            id=str(dev.get("serial_number") or "aigo-0113"),
            name="爱国者 星璨岚 屏显版",
            width=width,
            height=height,
            shape="rect",
            native_rotation=0,
            capabilities=Capability.PUSH_FULL,     # 推图通路待实测后再加别的
            firmware="",
            extra={"vendor_id": f"{dev['vendor_id']:04x}", "product_id": f"{dev['product_id']:04x}",
                   "path": dev["path"].decode("utf-8", "replace") if isinstance(dev.get("path"), bytes)
                   else str(dev.get("path")), "note": "分辨率来自公开参数，未由设备确认"},
        )

    @staticmethod
    def _find_device() -> dict[str, Any] | None:
        try:
            import hid
        except Exception:
            return None
        try:
            devices = hid.enumerate()
        except Exception:
            return None
        for dev in devices:
            if dev.get("product_id") != PID:
                continue
            if dev.get("usage_page") not in (0xFF00, None):
                continue
            return dev
        return None

    # ── 打开 ────────────────────────────────────────────────────────────
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
            raise DeviceBusy(f"HID 打不开（多半被 AIGO.exe 占着）: {exc}") from exc

        # 问一句设备规格；答不上来不算失败，只是分辨率按公开参数走
        spec = self.request("GET", self.config.get("spec_resource") or "spec")
        if spec and spec.get("payload"):
            try:
                import json
                data = json.loads(spec["payload"].decode("utf-8", "replace"))
                self._spec = data if isinstance(data, dict) else {}
                w = int(self._spec.get("width") or self._spec.get("w") or 0)
                h = int(self._spec.get("height") or self._spec.get("h") or 0)
                if w and h:
                    self._size = (w, h)
            except Exception:
                self._spec = {}

        return self.bind(DeviceInfo(
            id=info.id, name=info.name, width=self._size[0], height=self._size[1],
            shape="rect", native_rotation=int(self.config.get("rotation") or 0),
            capabilities=Capability.PUSH_FULL,
            firmware=str(self._spec.get("version") or ""),
            extra={**dict(info.extra), "spec": self._spec or "未取到"},
        ))

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None

    # ── 请求/回复 ───────────────────────────────────────────────────────
    def request(self, method: str, resource: str, headers: Mapping[str, Any] | None = None,
                payload: bytes = b"", timeout: float | None = None) -> dict[str, Any] | None:
        if self._dev is None:
            raise DeviceOffline("设备还没 open")
        self._seq += 1
        hdrs = {"SeqNumber": self._seq, "ContentLength": len(payload) if payload else -1}
        hdrs.update(dict(headers or {}))
        frame = build_frame(build_body(method, resource, hdrs, payload))
        try:
            self._dev.write(b"\x00" + frame)
        except Exception as exc:
            raise DeviceOffline(f"HID 写入失败: {exc}") from exc

        deadline = time.time() + (timeout if timeout is not None else float(self.config.get("timeout") or 1.5))
        buf = bytearray()
        while time.time() < deadline:
            got = self._dev.read(REPORT_SIZE)
            if got:
                buf += bytes(got)
                if buf.count(DELIM) >= 2:
                    reply = parse_reply(bytes(buf))
                    if reply:
                        return reply
            else:
                time.sleep(0.01)
        return None

    # ── 送帧 ────────────────────────────────────────────────────────────
    def push_frame(self, frame: Frame) -> None:
        resource = str(self.config.get("image_resource") or "").strip()
        if not resource:
            raise Unsupported(
                "这块屏的推图 resource 还没实测确认（画面编码方式未知）。"
                "拿到一次 AIGO.exe 的 HID 抓包后，把资源名填进 image_resource 即可启用。")
        if (frame.width, frame.height) != self._size:
            raise InvalidArgument(f"帧尺寸 {frame.size} 与设备 {self._size} 不一致")

        import io
        img = frame.to_image()
        quality = int(self.config.get("jpeg_quality") or 85)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        jpeg = buf.getvalue()

        # 一整帧通常放不进一个报文，按块续传；块序号放进 SeqNumber
        chunk = REPORT_SIZE - 9
        total = (len(jpeg) + chunk - 1) // chunk
        for index in range(total):
            piece = jpeg[index * chunk:(index + 1) * chunk]
            self.request("POST", resource,
                         {"ChunkIndex": index, "ChunkTotal": total, "ContentLength": len(piece)},
                         piece, timeout=float(self.config.get("chunk_timeout") or 2.0))

    def push_region(self, frame: Frame, x: int, y: int) -> None:
        raise Unsupported("爱国者 星璨岚 目前只按整帧推")

    def status(self) -> dict[str, Any]:
        return {"driver": self.driver_id, "size": list(self._size), "seq": self._seq,
                "open": self._dev is not None, "spec": self._spec or None}
