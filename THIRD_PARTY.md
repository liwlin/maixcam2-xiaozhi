# 第三方组件

本仓库除自写代码外，`device_snapshot/root_xiaozhi/` 下随附了运行时依赖的模型与预编译库，
方便一键部署。这些组件版权归各自作者，遵循其原始许可：

| 组件 | 位置 | 大小 | 来源 | 许可 |
|---|---|---|---|---|
| `encoder.onnx` / `decoder.onnx` / `joiner.onnx` / `tokens.txt` | `models/zh/` | 12 MB | [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 预训练 KWS 模型 `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`（建模单元为拼音声母+韵母，WenetSpeech L 训练） | Apache-2.0 |
| `libsherpa-onnx-c-api.so` / `libsherpa-onnx-cxx-api.so` | `sherpa_rt/lib/` | 4.7 MB | sherpa-onnx 官方预编译 aarch64 运行库 | Apache-2.0 |
| `libonnxruntime.so` | `sherpa_rt/lib/` | 33 MB | [ONNX Runtime](https://github.com/microsoft/onnxruntime) 1.30.0 | MIT |
| `libonnxruntime.so`（软链） | `sherpa_lib/` | — | 指向设备上 `site-packages/onnxruntime/capi/libonnxruntime.so.1.30.0` | MIT |
| `SourceHanSansCN-Regular.otf`（未随仓库分发，设备自带） | `/maixapp/share/font/` | — | Adobe | SIL OFL 1.1 |

`keywords_user.txt` 与 `models/zh/keywords_user.txt` 是运行时按唤醒词生成的拼音关键词文件，
格式为 `n ǐ h ǎo x iǎo zh ì @你好小智`，属于本项目产物。

## 其它依赖

以下通过 pip 安装，不随仓库分发：

- `MaixPy` 4.12.5（Sipeed，设备固件自带）
- `websockets` / `opuslib` / `pyaudio` / `Pillow` / `numpy` / `requests`

## 关于 `device_snapshot/`

该目录是**改造前**设备上原有程序的快照，仅作对照留存，版权归原作者（`yizhen`）。当前可用版本在 `xiaozhi/`。
