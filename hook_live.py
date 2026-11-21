"""录官方 AIGO 一次完整会话：命令包和图像包分开标记。

AIGO.exe 用 --inspect-brk 启动后，主进程停在第一行，我们先把 node-hid.write 拦住
再放行，所以从「打开设备」开始的每一笔写入都会落盘。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

import websocket

OUT = r"D:\Code\lcdmuse-verify\hid_live.log"
INSPECT = "http://127.0.0.1:9229/json/list"

PATCH = r"""
(() => {
  const req = process.mainModule.require.bind(process.mainModule);
  const fs = req('fs');
  fs.writeFileSync(OUT, '');
  const Module = req('module');
  let n = 0;
  const origLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    const mod = origLoad.apply(this, arguments);
    try {
      if ((request === 'node-hid' || request === 'hid') && mod && mod.HID && !mod.__lcd) {
        mod.__lcd = true;
        const proto = mod.HID.prototype;
        const W = proto.write;
        proto.write = function (buf, ...rest) {
          try {
            const b = Buffer.from(buf);
            n++; globalThis.__lcdCount = n;
            const tag = (b[0] === 0x00 && b[1] === 0x5c) ? 'IMG'
                      : (b[0] === 0x5a || b[1] === 0x5a) ? 'CMD' : 'OTH';
            fs.appendFileSync(OUT, tag + ' ' + b.length + ' ' + Date.now() + ' ' + b.toString('hex') + '\n');
          } catch (e) {}
          return W.call(this, buf, ...rest);
        };
        fs.appendFileSync(OUT, 'HOOKED\n');
      }
    } catch (e) {}
    return mod;
  };
  return 'patched';
})()
""".replace("OUT", json.dumps(OUT))


def main() -> int:
    seconds = int(sys.argv[sys.argv.index("--seconds") + 1]) if "--seconds" in sys.argv else 45
    deadline = time.time() + 120
    target = None
    while time.time() < deadline:
        try:
            listing = json.loads(urllib.request.urlopen(INSPECT, timeout=2).read())
            target = next((x for x in listing if x.get("type") == "node"), None)
            if target:
                break
        except Exception:
            pass
        time.sleep(0.25)
    if not target:
        print("没等到 inspector"); return 1
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=25, max_size=None)
    seq = 0

    def call(method, **params):
        nonlocal seq
        seq += 1
        ws.send(json.dumps({"id": seq, "method": method, "params": params}))
        return seq

    def wait(want, timeout=25.0):
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(ws.recv())
            if msg.get("id") == want:
                return msg
        return {}

    call("Runtime.enable")
    call("Debugger.enable")

    # --inspect-brk 会把主进程停在第一行，这时全局作用域里没有 require、也没有
    # process.mainModule（还没赋值），所以必须在「暂停的那个调用帧」里求值，
    # 那里才能拿到模块的 require。这是上次「打钩: None」的原因。
    paused = None
    end = time.time() + 30
    while time.time() < end and paused is None:
        msg = json.loads(ws.recv())
        if msg.get("method") == "Debugger.paused":
            paused = msg["params"]
    if paused is None:
        print("没收到 Debugger.paused（可能没有用 --inspect-brk 启动）")
    else:
        frame_id = paused["callFrames"][0]["callFrameId"]
        call("Debugger.evaluateOnCallFrame", callFrameId=frame_id, expression=PATCH,
             returnByValue=True)
        r = wait(seq)
        value = ((r.get("result") or {}).get("result") or {}).get("value")
        print("打钩:", value, (r.get("result") or {}).get("exceptionDetails", {}).get("text", ""))
        if value != "patched":
            print("在调用帧里也失败，先不继续")
            return 2
        call("Debugger.resume")
    print(f"放行，录 {seconds} 秒 → {OUT}")
    time.sleep(seconds)
    r = wait(call("Runtime.evaluate", expression="globalThis.__lcdCount || 0", returnByValue=True))
    print("写入次数:", ((r.get("result") or {}).get("result") or {}).get("value"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
