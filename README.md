# 小智AI on MaixCAM2

把 Sipeed MaixCAM2 变成一台真正可用的小智（XiaoZhi）语音助手：**语音唤醒 → 对话 → 连续多轮 → 屏幕弹窗**。
协议实现对齐 [py-xiaozhi](https://github.com/huangjunsen0406/py-xiaozhi) v2.1.2。

设备：MaixCAM2（`maixcam2-40d4`，Ubuntu 22.04 / MaixPy 4.12.5 / Python 3.13 / 640×480 屏）

```
root@maixcam2-40d4:~# journalctl -u xiaozhi | tail -4
语音唤醒: 你好小智
→ listen start (第 1 轮)
← stt '你好，小智，今天天气怎么样？请用一句话告诉我。'
← tts sentence_start '江门今天阴天，31度，南风4级，湿度76%。'
```

---

## 一、这套东西由两部分组成

| 部分 | 位置 | 职责 |
|---|---|---|
| **设置 App** | `/maixapp/apps/xiaozhi/` | 桌面图标「小智AI」。只做显示状态 + 改配置，写 `config.json` |
| **后台服务** | `/root/xiaozhi/`（systemd 开机自启） | 唤醒词常驻监听、连服务器、本地 VAD 断句、TTS 播放、状态栏/弹窗渲染 |

两边通过 `/root/xiaozhi/config.json` 和 `state.json` 交换数据，服务每 0.25s 轮询配置，改动即时生效。

弹窗靠 `LD_PRELOAD` 注入 launcher，在 `VO::push` 里把 RGBA 图层 alpha 混合进 640×480 帧；
弹窗可见时用 `EVIOCGRAB` 独占触摸设备，桌面不会被误触或滑动。

---

## 二、目录

```
xiaozhi/                交付物（部署到设备的就是这个目录）
├── service/
│   ├── xiaozhi_service.py   后台服务主程序（协议 / VAD / MCP / 音频 / 主循环）
│   ├── ui_render.py         状态栏 + 聊天气泡渲染 → popup.rgba / status.rgba
│   └── xiaozhi.service      systemd unit
├── app/                     设置 App（main.py / app.yaml / icon）
├── uishim/                  libmaixcam2_overlay.so 的源码（VO::push 注入 + 触摸接管）
├── deploy.sh                设备端一键部署
└── README.md                详细设计、排障、协议说明  ← 先看这个

diag/                   排查与标定脚本（在设备上跑）
├── proto_test.py / proto_test2.py   WS 协议端到端对照试验
├── xz_bisect.py                     定位「有 stt 无 tts」的变量对照
├── mic_cal.py                       麦克风增益标定
└── dl_probe.py                      最小下行客户端（关键排障工具，见下）

ref/                    参考图与透视校正结果
shots/                  实机截图 / 帧抓取结果
device_snapshot/        改动前的原始程序（仅源码，模型与 .so 已排除）
```

---

## 三、部署

```sh
scp -r xiaozhi/ root@<设备IP>:/root/xiaozhi_src/
ssh root@<设备IP> 'cd /root/xiaozhi_src && sh deploy.sh'
```

`deploy.sh` 会：拷贝服务与 App → 编译注入库 → 写 systemd unit 与 launcher drop-in → 重启服务。
旧版本请先自行备份（本项目在设备上保留了 `*_legacy_<日期>` 快照）。

> **动手前务必读**：`xiaozhi/README.md` 第四节。服务必须排在 launcher **之后**启动，
> 否则会开机黑屏（详见下）。

---

## 四、踩过的坑（都已修，但值得知道）

| 症状 | 根因 | 修复 |
|---|---|---|
| 唤醒后一直「聆听中」，服务器从不回话 | 旧版发 `listen start mode="auto"` 后只上行、从不断句。实测 `api.tenclass.net` 的 WS 端**不会**自己做 VAD | 改 `mode="manual"` + **本地能量 VAD**，检测到说话结束发 `listen stop` |
| **开机黑屏**，launcher 杀不掉 | `xiaozhi.service` 抢在 `launcher.service` 前初始化 maix 多媒体栈，launcher 首帧 DRAM commit 永久阻塞在 `drm_atomic_helper_wait_for_vblanks`（D 状态不可中断） | unit 加 `After=launcher.service`，并等 launcher 打开 `/dev/dri/card0` 后再启动 |
| **TTS 播放一顿一顿** | `maix.audio.Recorder` 阻塞模式的 pybind 绑定**持 GIL 约 20ms**，把整个进程量化成 20ms 一跳 → 事件循环收包成簇 | `Recorder(..., block=False)`；播放线程成块写 100ms；自适应抖动缓冲 |
| 叉掉弹窗后又自己弹出来 | 关闭弹窗的那次触摸同时被 overlay 和 `maix.touchscreen` 看到，后者当成「触摸唤醒」 | 唤醒事件带来源标记；弹窗期间的触摸由 overlay 独占 |
| 弹窗盖住桌面却还能滑动 | overlay 只拦截自己 `open()` 的 fd，launcher 自己开的 fd 号不同 | 按 `fstat` 设备号识别，并加 `EVIOCGRAB` |
| TTS 无声 | 服务器固定 24k 输出，codec 只支持 16k/48k | 按 **Opus TOC 字节**解析帧长，再重采样到 48k |

### 一条排障教训

播放卡顿排查时，我在服务进程内测得「服务端下发只有 0.61× 实时」，差点得出"服务端不行"的结论。
后来写了个**最小客户端**（不跑麦克风/唤醒/界面渲染，只连 WS 收音频）复测：

```
帧间隔: 平均19.9ms 中位20.0ms p95=23.3ms 最大49.0ms | >60ms 停顿 0 次 | 0.97× 实时
```

服务端**完全正常**——那个 0.61× 是被自身的 GIL 阻塞污染的测量结果。

> **怀疑链路问题时，先用最小复现端对照，别在复杂进程里量。**

`diag/dl_probe.py` 就是那个最小客户端，留在仓库里备用。

---

## 五、协议要点（对齐 py-xiaozhi v2.1.2）

* **OTA** `POST {ota_url}`，头 `Device-Id / Client-Id / Activation-Version: 2`；
  激活 `POST {ota_url}/activate`，`hmac-sha256(key=hmac_key, msg=challenge)`，200 成功 / 202 等用户输码。
* **WS** `Authorization: Bearer <token>` / `Protocol-Version: 1`；
  `ping_interval=20 ping_timeout=20 max_size=10MB compression=None`。
* **上行** Opus 16k 单声道 20ms（320 样点），`APPLICATION_VOIP`。
* **下行** Opus 24k 单声道，帧长由每包 TOC 字节解析。
* **MCP** JSON-RPC 2.0：`initialize` / `tools/list`（按 name 游标分页，8000B 预算）/ `tools/call`。

---

## 六、仓库里有什么 / 没有什么

**包含**（约 58MB）：

- 全部自写代码与排障脚本
- 唤醒模型 `models/zh/`（sherpa-onnx KWS zipformer-wenetspeech-3.3M，12MB）
- 预编译运行库 `sherpa_rt/lib/`（libsherpa-onnx + libonnxruntime，38MB）

**不包含**：

- `config.json` / `state.json` —— 含设备凭据（`hmac_key` / `client_id` / `serial_number`）。
  首次启动会自动生成，模板见 `device_snapshot/root_xiaozhi/config.example.json`
- `*.raw` / `*.rgba` 原始帧转储、`__pycache__`

第三方组件的来源与许可见 [THIRD_PARTY.md](THIRD_PARTY.md)。

