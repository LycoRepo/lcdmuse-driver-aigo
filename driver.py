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

import contextlib
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
HEADER_BUDGET = REPORT_SIZE - 9          # 单帧可用字节（含 HTTP 头）
BLOCK_SIZE = 700                         # 头部约 300 字节，留余量

_RESOURCES = {"conn": "conn", "realtime": "realtimeDisplay", "transport": "transport"}

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


PACKET_HEADER = 21          # 报头里 LEN 之后的固定字段数（4..24 共 21 字节）
CHUNK = 1000                # 每包最多携带的 JPEG 字节
KIND_IMAGE = 0x21


def image_packets(jpeg: bytes, seq: int = 0, chunk: int = CHUNK,
                  kind: int = KIND_IMAGE) -> list[bytes]:
    """把一张 JPEG 切成厂商格式的报文列表（每个 ≤1025 字节）。

    实测报头（25 字节，含 hidapi 的 report id）::

        00        report id
        5C        magic
        LEN(2, 大端) = 本包长度 - 4
        kind      0x21 图片
        00
        N         整帧包数
        00
        seq(2, 小端) 逐包 +1
        00 + 13 个 0

    ``seq`` 由调用方跨帧递增；设备靠它按顺序拼帧。
    """
    if not jpeg:
        raise InvalidArgument("JPEG 为空")
    chunks = [jpeg[i:i + chunk] for i in range(0, len(jpeg), chunk)]
    total = len(chunks)
    if total > 255:
        raise InvalidArgument(f"单帧 {total} 包超出报头 blockCount 字段（≤255）")
    out: list[bytes] = []
    for index, piece in enumerate(chunks):
        head = bytearray(25)
        head[0] = 0x00                       # report id
        head[1] = 0x5C                       # magic
        head[2:4] = (PACKET_HEADER + len(piece)).to_bytes(2, "big")
        head[4] = kind
        head[5] = 0x00
        head[6] = total
        head[7] = 0x00
        head[8:10] = ((seq + index) & 0xFFFF).to_bytes(2, "little")
        out.append(bytes(head) + piece)
    return out


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
        self._realtime = False
        self._frames = 0
        self._connected = False
        self._missed_acks = 0

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

        # 握手：POST conn（实测回 200 + 设备 JSON：version/space/brightness/degree/sn）
        spec = self.request("POST", self.config.get("conn_resource") or "conn")
        # 厂商的顺序：conn → STATE all heartbeat → power resume → realtimeDisplay enable。
        # 少了 heartbeat / resume，设备会收下报文字节但**不换画面**（今天实测到的就是这个）。
        self.heartbeat()
        self.power_event("resume")
        with contextlib.suppress(Exception):
            self.start_realtime(True)
            self._realtime = True
        if spec and spec.get("payload"):
            try:
                self._connected = True
            except Exception:
                pass
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
                while buf.count(DELIM) >= 2:
                    reply = parse_reply(bytes(buf))
                    if not reply:
                        break
                    end = buf.index(DELIM, buf.index(DELIM) + 1) + 1
                    buf = buf[end:]
                    # 只认属于本次请求的回复：设备每个请求都回 AckNumber=SeqNumber+1。
                    # 不校验就会读到上一个请求的回复——实测曾因此把 400 看成 200。
                    ack = (reply.get("headers") or {}).get("AckNumber")
                    if ack is not None:
                        try:
                            if int(ack) != self._seq + 1:
                                continue
                        except ValueError:
                            pass
                    return reply
            else:
                time.sleep(0.01)
        return None

    def _write(self, method: str, resource: str, headers: Mapping[str, Any] | None = None,
               payload: bytes = b"") -> None:
        """只写不等回包（数据块用，厂商软件同时开时回包会被抢）。"""
        if self._dev is None:
            raise DeviceOffline("设备还没 open")
        self._seq += 1
        hdrs = {"SeqNumber": self._seq, "Date": int(time.time() * 1000),
                "ContentLength": len(payload) if payload else -1}
        hdrs.update(dict(headers or {}))
        try:
            self._dev.write(b"\x00" + build_frame(build_body(method, resource, hdrs, payload)))
        except Exception as exc:
            raise DeviceOffline(f"HID 写入失败: {exc}") from exc

    # ── 送帧 ────────────────────────────────────────────────────────────
    def set_mode(self, value: int) -> dict[str, Any] | None:
        """切设备模式：payload 是 {"value": N}（不是 {"mode": N}，后者会被当成 0x7FFFFFFF）。

        实测：0=默认 UI，2=可传输（推帧前设成 2）。
        """
        import json as _json
        body = _json.dumps({"value": int(value)}).encode()
        return self.request("POST", "mode",
                            {"ContentType": "json", "ContentLength": len(body)}, body, timeout=1.5)

    def heartbeat(self) -> dict[str, Any] | None:
        """STATE all + {"heartbeat":1}：设备靠它认为"对面还活着"。"""
        import json as _json
        body = _json.dumps({"heartbeat": 1}).encode()
        return self.request("STATE", "all",
                            {"ContentType": "json", "ContentLength": len(body)}, body, timeout=1.0)

    def power_event(self, event: str = "resume") -> dict[str, Any] | None:
        """power resume/suspend：不 resume 的话，传输成功但画面不刷新。"""
        import json as _json
        body = _json.dumps({"event": event}).encode()
        return self.request("POST", "power",
                            {"ContentType": "json", "ContentLength": len(body)}, body, timeout=1.5)

    def start_realtime(self, enable: bool = True) -> dict[str, Any] | None:
        """进/出实时显示模式（实测：{"enable":true} → 200）。"""
        body = b'{"enable":true}' if enable else b'{"enable":false}'
        return self.request("POST", _RESOURCES["realtime"],
                            {"ContentType": "json", "ContentLength": len(body)}, body)

    def push_frame(self, frame: Frame) -> None:
        """推一帧。

        格式是抓包实测出来的：25 字节报头 + 最多 1000 字节 JPEG，
        整帧切成若干 ≤1025 字节的 HID 报文连续写出，seq 跨帧递增。
        """
        import io

        if (frame.width, frame.height) != self._size:
            raise InvalidArgument(f"帧尺寸 {frame.size} 与设备 {self._size} 不一致")
        if self._dev is None:
            raise DeviceOffline("设备未打开")

        img = frame.to_image()
        rotation = int(self.config.get("rotation") or 0)
        if rotation % 360:
            img = img.rotate(-rotation % 360, expand=False)

        quality = int(self.config.get("jpeg_quality") or 70)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=False)
        jpeg = buf.getvalue()

        self._wake()

        packets = image_packets(jpeg, self._seq)
        gap = float(self.config.get("packet_gap") or 0.002)
        for packet in packets:
            self._dev.write(packet)
            if gap > 0:
                time.sleep(gap)
        self._seq = (self._seq + len(packets)) & 0xFFFF
        self._frames += 1

    def _wake(self) -> None:
        """背光会被固件按 60 秒无流量调暗到 0，推帧前确保亮着且处于实时模式。"""
        now = time.time()
        if now - getattr(self, "_last_wake", 0.0) < 4.0:
            return
        self._last_wake = now
        want = int(self.config.get("brightness") or 100)
        for action in (lambda: self._brightness(want), lambda: self.start_realtime(True)):
            with contextlib.suppress(Exception):
                action()

    def _brightness(self, value: int) -> dict[str, Any] | None:
        import json as _json
        body = _json.dumps({"value": int(value)}).encode()
        return self.request("POST", "brightness",
                            {"ContentType": "json", "ContentLength": len(body)}, body, timeout=1.5)

    def push_region(self, frame: Frame, x: int, y: int) -> None:
        raise Unsupported("爱国者 星璨岚 目前只按整帧推")

    def tick(self, now: float) -> None:
        """保活：这台固件 60 秒收不到主机流量就把亮度降到 0（屏变黑）。

        所以每 keepalive_seconds 秒发一次 conn；发现 brightness 被系统调没了
        就顺手拉回配置值（默认 100）。
        """
        interval = float(self.config.get("keepalive_seconds") or 20)
        last = getattr(self, "_last_tick", 0.0)
        if now - last < interval:
            return
        self._last_tick = now
        if self._dev is None:
            return
        try:
            resp = self.request("POST", "conn", {}, b"", timeout=1.0)
            payload = (resp or {}).get("payload") or b""
            if not payload:
                return
            import json
            state = json.loads(payload)
        except Exception:
            return
        if int(state.get("brightness") or 0) == 0:
            want = int(self.config.get("brightness") or 100)
            import json
            body = json.dumps({"value": want}).encode()
            with contextlib.suppress(Exception):
                self.request("POST", "brightness",
                             {"ContentType": "json", "ContentLength": len(body)}, body, timeout=1.0)

    def status(self) -> dict[str, Any]:
        return {"driver": self.driver_id, "size": list(self._size), "seq": self._seq,
                "frames": self._frames, "realtime": self._realtime, "missed_acks": self._missed_acks,
                "open": self._dev is not None, "spec": self._spec or None}
