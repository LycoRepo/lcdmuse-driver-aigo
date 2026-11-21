"""用 --inspect-brk 把官方 AIGO 主进程挂在第一行，先打好 node-hid 的钩子再放行。

这样能录到从「打开设备」开始的第一笔写入，包括 conn / power / realtime 这些
握手命令——这是事后挂钩录不到的部分。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

import websocket

OUT = r"D:\Code\lcdmuse-verify\hid_boot.log"
INSPECT = "http://127.0.0.1:9229/json/list"

PATCH = r"""
(() => {
  const req = (globalThis.process && process.mainModule && process.mainModule.require)
      ? process.mainModule.require.bind(process.mainModule)
      : globalThis.require;
  if (!req) return 'no-require';
  const fs = req('fs');
  const Module = req('module');
  fs.writeFileSync(OUT, '# hooked before app code\n');
  let n = 0;
  const origLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    const mod = origLoad.apply(this, arguments);
    try {
      if ((request === 'node-hid' || request === 'hid') && mod && mod.HID && !mod.__lcdPatched) {
        mod.__lcdPatched = true;
        const proto = mod.HID.prototype;
        const origWrite = proto.write;
        proto.write = function (buf, ...rest) {
          try {
            const b = Buffer.from(buf);
            n++;
            globalThis.__lcdCount = n;
            fs.appendFileSync(OUT, 'W ' + b.length + ' ' + b.toString('hex') + '\n');
          } catch (e) {}
          return origWrite.call(this, buf, ...rest);
        };
        fs.appendFileSync(OUT, '# node-hid patched\n');
      }
    } catch (e) {}
    return mod;
  };
  return 'patched';
})()
""".replace("OUT", json.dumps(OUT))


def main() -> int:
    seconds = int(sys.argv[sys.argv.index("--seconds") + 1]) if "--seconds" in sys.argv else 40
    deadline = time.time() + 90
    target = None
    while time.time() < deadline:
        try:
            listing = json.loads(urllib.request.urlopen(INSPECT, timeout=2).read())
            target = next((x for x in listing if x.get("type") == "node"), None)
            if target:
                break
        except Exception:
            pass
        time.sleep(0.3)
    if not target:
        print("没等到 inspector"); return 1

    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=20, max_size=None)
    seq = 0

    def call(method: str, **params):
        nonlocal seq
        seq += 1
        ws.send(json.dumps({"id": seq, "method": method, "params": params}))
        return seq

    def wait(want: int, timeout: float = 20.0):
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(ws.recv())
            if msg.get("id") == want:
                return msg
        return {}

    call("Runtime.enable")
    res = wait(call("Runtime.evaluate", expression=PATCH, returnByValue=True,
                    includeCommandLineAPI=True))
    print("打钩:", ((res.get("result") or {}).get("result") or {}).get("value"),
          (res.get("result") or {}).get("exceptionDetails"))
    call("Runtime.runIfWaitingForDebugger")
    print(f"已放行，录 {seconds} 秒 → {OUT}")
    time.sleep(seconds)
    res = wait(call("Runtime.evaluate", expression="globalThis.__lcdCount || 0", returnByValue=True))
    print("写入次数:", ((res.get("result") or {}).get("result") or {}).get("value"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
