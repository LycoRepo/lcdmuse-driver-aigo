# lcdmuse-driver-aigo — 爱国者「星璨岚」屏显版驱动

给 [lcdmuse](https://github.com/Phenix704/lcdmuse) 用的驱动插件：驱动**爱国者星璨岚屏显版**机箱副屏
（USB HID 厂商自定义，1024 字节报文，面板 **1480×720**，设备自报 `degree=270`）。

```
lcdmuse plugin add <本仓库目录或 zip>
lcdmuse probe                      # 应看到 aigo_xingcanlan
lcdmuse render --plugin aigo_xingcanlan --out out/aigo.png
```

## 协议（实测，2026-11-21）

设备是一台 Linux 小主机（`OS: Linux`，app/firmware `V1.0.8`，sdk `V1.2.7`），
和 ASUS ROG Ryuo IV 同一套 **Baiyi（百一）** 方案 —— 帧格式来自公开逆向文档
[ryuo-iv-screen](https://github.com/FoxPodZ/ryuo-iv-screen)（GPL-3.0-or-later，作者 **FoxPodZ (foxpodz.de)**，
带 GPLv3 §7(b) 署名附加条款），**命令词表则来自 AIGO.exe 自己的日志**（`%APPDATA%\AIGO\logs\*.log`
会把每条请求的完整 hex 打出来，比抓包省事得多）。

```
帧：5A | stuffed( LEN(2 BE) | BODY | CKSUM(1) ) | 5A
    LEN = len(BODY)+5，CKSUM = sum(LEN+BODY) & 0xFF，填充 5A→5B01 / 5B→5B02
    整帧装进一个 1024 字节报文（不足补零）
BODY："<METHOD> <resource> 1\r\n" + Key=Value... + "\r\n" + payload
      METHOD = GET / POST / DELETE / STATE
```

必需头：`SeqNumber`、`Date`（毫秒时间戳）、`ContentType`、`ContentLength`（无负载时省略）。
**缺头会 400** —— 这也是验证手段：故意少头发一个 transport，设备回 400，说明 200 不是无脑通过。

### 命令

| 请求 | 作用 | 实测 |
|---|---|---|
| `POST conn` | 握手，回设备 JSON | 200，`{"OS":"Linux","version":{...},"space":70244,"brightness":100,"degree":270,"sn":"BY…","osdState":0,"mode":0,"timeout":60,...}` |
| `STATE all` + `{"heartbeat":1}` | 心跳 | 200 |
| `POST power` + `{"event":"resume"/"suspend"}` | 唤醒 / 休眠屏 | 日志里有 |
| `POST realtimeDisplay` + `{"enable":true}` | 进实时显示模式 | 200 |
| `POST transport` | 传文件：起始帧带 `FileName/FileSize/ContentType/blockCount`，数据帧带 `currentBlockIndex/ContentRange`，最后一块带 `isEnded=1` | 每块 200 |

推一帧 1480×720 PNG（约 100 KB，150 块 × 700 B）约 **1–2 秒**，即 **0.5–1 fps**。
块不能超过 ~800 B：HTTP 头本身要 ~200 B，超出 1024 会被驱动当场拒绝。

## 设备自报

```
OS Linux | app V1.0.8 | firmware V1.0.8 | sdk V1.2.7 | hardware V1.0
space 70244 KB | brightness 100 | degree 270 | osdState 0 | mode 0 | timeout 60
sn BYZL2626WC01CM000133
```

`degree=270` 是设备自己的旋转设置，所以驱动默认**不预旋转**；要手工转就设 `rotation`。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `conn_resource` | `conn` | 握手 resource |
| `file_name` | `lcdmuse.png` | 传输时上报的文件名 |
| `rotation` | `0` | 额外旋转（设备已自带 270） |
| `chunk_timeout` | `3.0` | 单块超时（秒） |
| `timeout` | `1.5` | 请求超时（秒） |

## 注意

- 官方软件 `AIGO.exe` 以管理员权限运行。HID 是共享打开，所以本驱动**可以**在它运行的同时推帧，
  但两边的 `SeqNumber` 会互相干扰（AIGO 日志里会出现 `AckNumber error`）。
  要干净地接管，先关掉它（需要管理员）。
- 驱动不改官方软件的任何文件，只读它的日志。

## 测试

```bash
PYTHONPATH=/path/to/lcdmuse pytest tests -q      # 9 passed
```

覆盖：填充/还原往返、帧布局与校验、超长拒绝、HTTP 风格 body、回复解析（含坏校验和）、
推帧的分块顺序与 `isEnded`、起始被拒时抛 `DeviceOffline`、无设备时 probe 返回 None。

## 许可

GPL-3.0-or-later。帧格式来自 ryuo-iv-screen，保留其作者署名与链接（见上）。
