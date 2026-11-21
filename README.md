# lcdmuse-driver-aigo — 爱国者「星璨岚」屏显版驱动

给 [lcdmuse](https://github.com/LycoRepo/lcdmuse) 用的驱动插件：驱动**爱国者星璨岚屏显版**机箱副屏
（USB HID，厂商自定义 usage page `0xFF00`，1024 字节报文，面板 1480×720）。

```
lcdmuse plugin add <本仓库目录或 zip>
lcdmuse probe            # 应看到 aigo_xingcanlan（默认不参与自动 probe，见下）
```

## 现在的状态（重要，先看这段）

| 环节 | 状态 |
|---|---|
| 找到设备（PID `0113`，忽略会变的 VID） | ✅ 实测通过 |
| HID 打开、报文收发 | ✅ 实测通过 |
| 帧编码（`0x5A` 包裹 + 字节填充 + 加和校验） | ✅ 实测通过 —— **设备会回包** |
| 设备对未知 resource 的回复 | ✅ 收到 `1 400\r\nAckNumber=N\r\n\r\n` |
| 正确 resource 名 / 推图通路 | ❌ **未知**，需要一次 `AIGO.exe` 的 HID 抓包 |

也就是说：**线是通的，命令词还没对上**。驱动在拿到 resource 之前会明确拒绝送帧
（`Unsupported`），不会假装成功、也不会往屏上乱推东西。

### 怎么补上最后一块

任选一条，抓到 `AIGO.exe` 正在发的那几帧 1024 字节报文：

1. `USBPcap` + Wireshark，过滤 `usb.idVendor == 0x1d6b`，抓 3–5 秒；
2. 或在能拿到日志的环境里打开 AIGO 的调试输出；
3. 把抓到的报文贴进 issue，或自己看 `1 <METHOD> <resource> 1\r\n` 里那个 resource，
   填进配置 `image_resource`。若推图是分块的，`ChunkIndex/ChunkTotal` 两个头就够用了。

## 协议（Baiyi / 百一方案）

这块屏与 ASUS ROG Ryuo IV 是同一套 ODM 方案，帧格式来自公开的逆向文档
**[ryuo-iv-screen](https://github.com/FoxPodZ/ryuo-iv-screen)**（GPL-3.0-or-later，作者 **FoxPodZ (foxpodz.de)**）：

```
5A | stuffed( LEN(2 BE) | BODY | CKSUM(1) ) | 5A
LEN   = len(BODY) + 5
CKSUM = sum(LEN + BODY) & 0xFF
填充  0x5A -> 5B 01，0x5B -> 5B 02（只处理 LEN/BODY/CKSUM，不动两端 5A）
整帧必须装进一个 1024 字节报文，不足补零
BODY = "<METHOD> <resource> 1\r\n" + "Key=Value\r\n"... + "\r\n" + payload
方法 = GET / POST / DELETE / STATE
```

> 该上游仓库带 GPLv3 §7(b) 附加条款，要求保留作者署名与链接。本驱动与本文档都已保留；
> 再分发同样保留。

本驱动做的实测（2026-11-21，设备 `BYZL2626WC01CM000133`）：

- `hidapi` 枚举得到 `VID=1D6B PID=0113`、usage page `0xFF00`、in/out 报文各 **1025** 字节（含 report id）
- 打开设备、写入一帧 1024 字节报文、读回设备回复：**成功**
- 41 个常见 resource 名 + POST/GET/STATE 组合全部回 `400`，故 command 词表待抓包

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `spec_resource` | `spec` | 用来问设备规格的 resource（可选） |
| `image_resource` | 空 | **必填才能送帧**：推图用的 resource 名 |
| `jpeg_quality` | `85` | 送帧前 JPEG 质量 |
| `rotation` | `0` | 额外旋转 |
| `timeout` | `1.5` | 单次请求超时（秒） |

`driver.yaml` 里 `enabled_by_default: false` —— 命令词没确认前不该参与自动探测，
免得它抢在别的驱动前面认领设备。

## 注意

- **同一时间只能有一个程序占设备**。官方软件 `AIGO.exe` 以管理员权限运行并占着它，
  跑本驱动前要先关掉（需要管理员权限）。
- 驱动不改官方软件的任何文件。

## 测试

```bash
PYTHONPATH=/path/to/lcdmuse pytest tests -q
```

8 个用例：填充/还原往返、帧布局与校验、超长拒绝、HTTP 风格 body、回复解析（含坏校验和）、
未配 resource 时拒绝送帧、无设备时 probe 返回 None。

## 许可

GPL-3.0-or-later。帧格式部分来自 ryuo-iv-screen，保留其作者署名与链接（见上）。
