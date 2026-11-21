// 在官方 AIGO 主进程启动之前就把 node-hid 拦住。
// 用法：设 NODE_OPTIONS=--require <本文件> 再启动 AIGO.exe，
// 这样连「打开设备 → 第一次握手」的写入都能录到，不依赖事后挂钩。
const fs = require('fs');
const Module = require('module');
const OUT = 'D:\\Code\\lcdmuse-verify\\hid_boot.log';
let count = 0;

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
          count++;
          fs.appendFileSync(OUT, 'W ' + b.length + ' ' + b.toString('hex') + '\n');
        } catch (e) {}
        return origWrite.call(this, buf, ...rest);
      };
      const origOpen = mod.HID.prototype.constructor;
      fs.appendFileSync(OUT, '# hooked node-hid at ' + new Date().toISOString() + '\n');
    }
  } catch (e) {
    try { fs.appendFileSync(OUT, '# hook error ' + e.message + '\n'); } catch (_) {}
  }
  return mod;
};
