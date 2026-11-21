"""对官方 AIGO 主进程挂钩 node-hid，把一次完整会话的写入全量录下来。

和上次的区别：这次从**进程刚起来**就挂上，所以能录到 conn/power/realtime 这些
握手命令，以及第一帧的完整字节；上次那份 hid.log 是从推流中间开始的。

用法：
    python hook_session.py            # 等 9229 出现，挂上去，录 90 秒
    python hook_session.py --seconds 30
输出：D:\\Code\\lcdmuse-verify\\hid_session.log
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

OUT = Path(r"D:\Code\lcdmuse-verify\hid_session.log")
INSPECT = "http://127.0.0.1:9229/json/list"


def _ws(url: str):
    import websocket  # websocket-client
    return websocket.create_connection(url, timeout=10, max_size=None)


def main() -> int:
    seconds = 90
    if "--seconds" in sys.argv:
        seconds = int(sys.argv[sys.argv.index("--seconds") + 1])

    # 1) 等主进程 inspector 起来
    target, ws_url = None, None
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(INSPECT, timeout=1) as resp:
                listing = json.load(resp)
            for t in listing:
                if t.get("type") == "node":
                    target, ws_url = t, t.get("webSocketDebuggerUrl")
                    break
            if ws_url:
                break
        except Exception:
            pass
        time.sleep(0.3)
    if not ws_url:
        print("120 秒内没等到 9229 的 node inspector（AIGO 可能没带 --inspect 启动）", flush=True)
        return 1
    print(f"已找到主进程 inspector: {target.get('title')}", flush=True)

    ws = _ws(ws_url)
    seq = 0

    def call(method: str, **params):
        nonlocal seq
        seq += 1
        ws.send(json.dumps({"id": seq, "method": method, "params": params}))
        return seq

    def wait_result(want: int, timeout: float = 15.0):
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(ws.recv())
            if msg.get("id") == want:
                return msg
        return None

    call("Runtime.enable")
    call("Debugger.enable")
    script = r"""
    (() => {
      const req = (globalThis.process && process.mainModule && process.mainModule.require)
          ? process.mainModule.require.bind(process.mainModule)
          : (typeof globalThis.require === 'function' ? globalThis.require : null);
      if (!req) return 'no-require';
      if (globalThis.__lcdHook) return 'already';
      const fs = req('fs');
      const path = 'D:\\Code\\lcdmuse-verify\\hid_session.log';
      const hid = req('node-hid');
      if (!hid || !hid.HID) return 'no node-hid';
      const proto = hid.HID.prototype;
      const orig = proto.write;
      let n = 0;
      proto.write = function (buf, ...rest) {
        try {
          const b = Buffer.from(buf);
          n++;
          const tag = b.length >= 2 && b[0] === 0x00 ? b.subarray(1, 3).toString('hex') : 'x';
          const line = `W ${b.length} ${tag} ${b.toString('hex')}\n`;
          fs.appendFileSync(path, line);
        } catch (e) {}
        return orig.call(this, buf, ...rest);
      };
      globalThis.__lcdHook = () => n;
      return 'hooked';
    })()
    """
    mid = call("Runtime.evaluate", expression=script, includeCommandLineAPI=True,
               returnByValue=True, awaitPromise=True)
    res = wait_result(mid)
    value = (((res or {}).get("result") or {}).get("result") or {}).get("value")
    print("挂钩结果:", value, flush=True)
    if value not in ("hooked", "already"):
        return 2

    print(f"开始录 {seconds} 秒 → {OUT}", flush=True)
    time.sleep(seconds)
    mid = call("Runtime.evaluate", expression="globalThis.__lcdHook && globalThis.__lcdHook()",
               returnByValue=True)
    res = wait_result(mid)
    count = (((res or {}).get("result") or {}).get("result") or {}).get("value")
    print("总共录到写入次数:", count, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
