#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小智AI 后台服务 v2（MaixCAM2）—— 开机自启：状态栏 / 语音唤醒 / 聊天弹窗

与 py-xiaozhi (huangjunsen0406/py-xiaozhi v2.1.2) 对齐的协议实现：
  OTA      POST {ota_url}，头 Device-Id / Client-Id / Activation-Version=2，
           读 websocket.url/token 与 activation.challenge|code；
           激活 POST {ota_url}/activate，HMAC-SHA256(key=hmac_key, msg=challenge)，
           202 = 等待用户在控制台输码（5s 轮询，最多 60 次）。
  WS       wss，头 Authorization: Bearer <token> / Protocol-Version: 1 /
           Device-Id / Client-Id；ping_interval=20 ping_timeout=20
           close_timeout=10 open_timeout=5 max_size=10MB compression=None。
  hello    {"type":"hello","version":1,"transport":"websocket",
            "features":{"mcp":true},
            "audio_params":{"format":"opus","sample_rate":16000,
                            "channels":1,"frame_duration":20}}
  上行     Opus 16k 单声道 20ms(320 样点) APPLICATION_VOIP
  下行     Opus 24k 单声道，帧长由每包 TOC 字节解析（20/40/60ms 自适应），
           再重采样到硬件支持的 48k 播放。
  MCP      JSON-RPC 2.0：initialize / tools/list（按 name 游标分页，8000B 预算）/
           tools/call，结果包 {"content":[{"type":"text","text":...}],"isError":false}。

对话闭环（本机关键修复）：
  实测 api.tenclass.net 的 WS 端 **不会** 在 mode="auto" 下自行断句（连续上行
  数分钟也不返回 stt/tts）。因此这里做客户端断句：listen start mode="manual"
  → 本地能量 VAD 检测说话结束 → listen stop → 服务器立刻返回 stt/llm/tts。
  每一轮 tts stop 之后重新 listen start，实现连续多轮对话；静默超时后回到待唤醒。
"""
import asyncio
import copy
import hashlib
import hmac as hmac_mod
import json
import os
import queue
import shutil
import socket
import struct
import threading
import time
import uuid as uuidlib

import faulthandler
import sys

import numpy as np
import requests
import websockets
import pyaudio
import opuslib

import ui_render

# ---------------- 路径 ----------------
BASE = "/root/xiaozhi"
CFG_PATH = f"{BASE}/config.json"
STATE_PATH = f"{BASE}/state.json"
STATUS_RGBA = f"{BASE}/status.rgba"
POPUP_RGBA = f"{BASE}/popup.rgba"

SHERPA_LIB = f"{BASE}/sherpa_rt/lib"
KWS_MODEL_DIR = f"{BASE}/models/zh"
KWS_KEYWORDS = f"{KWS_MODEL_DIR}/keywords_user.txt"

CMD_PATH = "/tmp/xz_cmd"          # overlay shim 上报的触摸命令
UI_SNAPSHOT = "/tmp/xiaozhi_popup.png"

# ---------------- 音频常量 ----------------
SAMPLE_RATE = 16000               # 上行
FRAME_MS = 20
CHUNK = SAMPLE_RATE * FRAME_MS // 1000      # 320 样点
CHUNK_BYTES = CHUNK * 2                     # 640 字节
OUTPUT_SR = 24000                 # 下行（服务器固定 24k）

DEFAULT_CFG = {
    "enabled": True,
    "popup_enabled": True,
    "touch_wake": True,
    "wake_words": ["你好小智"],
    "ota_url": "https://api.tenclass.net/xiaozhi/ota/",
    "app_version": "2.0.0",
    "client_id": "",
    "serial_number": "",
    "hmac_key": "",
    "activated": False,
    "volume": 80,
    "mic_gain": 40,
    "vad_silence_ms": 650,        # 说话结束后静音多久判定一轮结束
    "idle_close_s": 20,           # 连续对话空闲多久后收尾
}

# ---------------- 全局状态 ----------------
class S:
    cfg = {}
    loop = None
    ws = None
    session_id = ""
    session_active = False
    session_state = "idle"        # idle/connecting/listening/thinking/speaking/activation
    detail = ""
    activated = False
    activation_code = ""
    network = "未知"
    bubbles = []                  # [(role, text)]
    emotion = ""
    wake_event_q = None
    live = []
    live_lock = threading.Lock()
    live_seq = 0
    play_q = []
    play_lock = threading.Lock()
    session_task = None
    mic_rms = 0.0
    mic_peak = 0.0
    mic_blocks = 0
    uplink_frames = 0
    out_sr = 48000
    rec = None
    tts_playing = False
    last_error = ""
    started_at = time.time()
    popup_visible = False
    popup_dismissed = False
    muted = False
    played = 0
    play_underrun = 0          # 说话途中播放缓冲见底的次数（卡顿直接指标）
    play_prime_ms = 400        # 自适应抖动缓冲目标（ms），350~1000 之间收敛
    # ---- 上行管线（mic 线程负责编码，sender 只发，避免事件循环被 opus/numpy 拖住） ----
    enc = None                 # opuslib.Encoder，会话期间有效
    uplink_on = False          # listen start 之后、listen stop 之前为 True
    up_q = []                  # [(pcm16, opus)]
    up_lock = threading.Lock()
    up_dropped = 0             # 因积压来不及发而丢的块（异常指标）
    up_discard = 0             # 主动丢弃（TTS 播放中 / 非聆听态，属正常）
    loop_lag_max = 0.0         # 事件循环最大卡顿（ms）
    loop_lag_n = 0             # 卡顿 >60ms 的次数
    last_sent_end = 0.0        # 最近一次 tts sentence_end 时刻（播放线程判「句尾排空」用）
    up_sent = 0
    kws_q = None               # 唤醒检测队列（独立线程消费，避免拖慢采集）
    last_play_ts = 0.0         # 最后一次播放时刻（播放尾音期间禁用唤醒，防自激）
    last_popup_cmd = 0.0       # 最后一次弹窗点击时刻（抑制触摸误唤醒）
    wake_src = ""


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def tune_gil():
    """本机 maix.audio.Recorder.record() 的 pybind 绑定会持 GIL 约 20ms，
    把整个进程量化成 20ms 一跳 —— 事件循环因此收包成簇，下行音频听起来一顿一顿。
    缩短切换间隔能让主线程更快重新拿到 GIL，把最坏延迟压下来。"""
    try:
        sys.setswitchinterval(0.001)
    except Exception:
        pass


def mono():
    """单调时钟：所有间隔/超时必须用它。
    本机有 rtc-time-sync 服务会调系统时钟，用 time.time() 做差会得到
    「事件循环卡顿 17 秒」这类假象（实测 40s 无跳变，但开机与校时点会跳）。"""
    return time.monotonic()


# ---------------- 配置 ----------------
def load_cfg():
    cfg = copy.deepcopy(DEFAULT_CFG)
    try:
        with open(CFG_PATH) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if not cfg.get("client_id"):
        cfg["client_id"] = str(uuidlib.uuid4())
    if not cfg.get("serial_number"):
        cfg["serial_number"] = uuidlib.uuid4().hex
    if not cfg.get("hmac_key"):
        cfg["hmac_key"] = os.urandom(32).hex()
    save_cfg(cfg)
    return cfg


def save_cfg(cfg=None):
    cfg = cfg or S.cfg
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG_PATH)


def cfg_mtime():
    try:
        return os.path.getmtime(CFG_PATH)
    except OSError:
        return 0


def wake_word():
    return (S.cfg.get("wake_words") or ["你好小智"])[0] or "你好小智"


# ---------------- 状态文件（设置 App 读它） ----------------
def write_state():
    _t0 = mono()
    st = {
        "service": "running",
        "state": S.session_state,
        "detail": S.detail,
        "activated": S.activated,
        "activation_code": S.activation_code,
        "network": S.network,
        "enabled": S.cfg.get("enabled", True),
        "popup_enabled": S.cfg.get("popup_enabled", True),
        "wake_word": wake_word(),
        "volume": S.cfg.get("volume", 80),
        "muted": S.muted,
        "mcp": True,
        "last_error": S.last_error,
        "uptime": int(time.time() - S.started_at),
    }
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)
    _d = (mono() - _t0) * 1000
    if _d > 80:
        log("!! write_state 耗时 %.0fms" % _d)


# ---------------- 弹窗渲染 ----------------
def render_popup(visible=None):
    _t0 = mono()
    if visible is None:
        visible = S.session_state != "idle"
    show = bool(visible) and S.cfg.get("enabled", True) \
        and S.cfg.get("popup_enabled", True) and not S.popup_dismissed
    S.popup_visible = show
    try:
        img = ui_render.render(
            POPUP_RGBA,
            state=S.session_state if show else "idle",
            detail=S.detail,
            bubbles=S.bubbles if show else [],
            emotion=S.emotion,
            wake_word=wake_word(),
            muted=S.muted,
            activation_code=S.activation_code,
        )
        _d = (mono() - _t0) * 1000
        if _d > 80:
            log("!! render_popup 耗时 %.0fms（state=%s 气泡=%d）"
                % (_d, S.session_state, len(S.bubbles)))
        return img
    except Exception as e:
        log("render err:", e)
        return None


def save_snapshot():
    img = render_popup()
    try:
        if img is not None:
            img.save(UI_SNAPSHOT)
    except Exception:
        pass


S.popup_dismissed = False


def set_status_icon(which):
    try:
        ui_render.render_status_icon(STATUS_RGBA, which)
    except Exception as e:
        log("status icon err:", e)


# ---------------- 触摸命令（overlay shim → /tmp/xz_cmd） ----------------
def in_btn(x, y, b, jitter=8):
    return (b[0] - jitter) <= x <= (b[0] + b[2] + jitter) and \
           (b[1] - jitter) <= y <= (b[1] + b[3] + jitter)


def poll_popup_cmds():
    if not os.path.exists(CMD_PATH):
        return
    try:
        with open(CMD_PATH) as f:
            lines = f.readlines()
        os.unlink(CMD_PATH)
    except Exception:
        return
    for ln in lines:
        p = ln.split()
        if len(p) != 3 or p[0] != "TAP":
            continue
        try:
            x, y = int(p[1]), int(p[2])
        except ValueError:
            continue
        if not S.popup_visible:
            continue
        S.last_popup_cmd = mono()
        if in_btn(x, y, ui_render.BTN_CLOSE):
            log("弹窗：关闭")
            S.popup_dismissed = True
            render_popup(visible=False)
        elif in_btn(x, y, ui_render.BTN_SPEAKER):
            S.muted = not S.muted
            apply_volume()
            log("弹窗：静音" if S.muted else "弹窗：取消静音")
            render_popup()
        else:
            log("弹窗：点击唤醒")
            if S.loop:
                S.loop.call_soon_threadsafe(S.wake_event_q.put_nowait, "cmd")


# ---------------- 网络 / OTA（对齐 py-xiaozhi ota.py + client.py） -------
def device_mac():
    try:
        return open("/sys/class/net/wlan0/address").read().strip().lower()
    except Exception:
        return "00:00:00:00:00:00"


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


ota_info = {"ws_url": "", "token": "", "need_activation": False, "challenge": ""}


def ota_poll():
    mac = device_mac()
    headers = {
        "Device-Id": mac,
        "Client-Id": S.cfg["client_id"],
        "Content-Type": "application/json",
        "User-Agent": f"maixcam2/xiaozhi-{S.cfg['app_version']}",
        "Accept-Language": "zh-CN",
        "Activation-Version": "2",
    }
    payload = {
        "application": {
            "version": S.cfg["app_version"],
            "elf_sha256": S.cfg.get("hmac_key", "unknown"),
        },
        "board": {
            "type": "maixcam2",
            "name": "xiaozhi-maixcam2",
            "ip": local_ip(),
            "mac": mac,
        },
    }
    r = requests.post(S.cfg["ota_url"], headers=headers,
                      json=payload, timeout=10, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"OTA HTTP {r.status_code}")
    data = r.json()
    ws = data.get("websocket") or {}
    if ws.get("url"):
        ota_info["ws_url"] = ws["url"]
    ota_info["token"] = ws.get("token") or ""
    S.network = "可用"
    act = data.get("activation")
    if act:
        ota_info["need_activation"] = True
        ota_info["challenge"] = act.get("challenge", "")
        S.activation_code = act.get("code", "")
        S.activated = False
    else:
        ota_info["need_activation"] = False
        S.activated = True
        S.activation_code = ""
    S.cfg["activated"] = S.activated
    save_cfg()
    return data


def try_activate():
    """POST /activate：200 已激活 / 202 等待用户输码"""
    sig = hmac_mod.new(S.cfg["hmac_key"].encode(),
                       ota_info["challenge"].encode(),
                       hashlib.sha256).hexdigest()
    url = S.cfg["ota_url"].rstrip("/") + "/activate"
    headers = {
        "Activation-Version": "2",
        "Device-Id": device_mac(),
        "Client-Id": S.cfg["client_id"],
        "Content-Type": "application/json",
    }
    payload = {"Payload": {
        "algorithm": "hmac-sha256",
        "serial_number": S.cfg["serial_number"],
        "challenge": ota_info["challenge"],
        "hmac": sig,
    }}
    r = requests.post(url, headers=headers, json=payload, timeout=10, verify=False)
    if r.status_code == 200:
        return True
    if r.status_code == 202:
        return False
    log("activate HTTP", r.status_code, r.text[:120])
    return False


# ---------------- 音量 ----------------
_PLAYBACK_CTLS = ["RX LEFT ANA GAIN", "RX RIGHT ANA GAIN",
                  "RX LEFT DIG GAIN", "RX RIGHT DIG GAIN"]


def _amixer_set(name, val):
    try:
        os.system(f'amixer sset "{name}" {int(val)}% >/dev/null 2>&1')
    except Exception:
        pass


def apply_volume():
    v = 0 if S.muted else int(S.cfg.get("volume", 80))
    for c in _PLAYBACK_CTLS:
        _amixer_set(c, 0 if v == 0 else min(100, int(v * 0.75)))


# ---------------- MCP（对齐 py-xiaozhi mcp_server.py / tooling.py） ------
class Property:
    def __init__(self, name, ptype, default=None, minimum=None, maximum=None):
        self.name, self.type = name, ptype
        self.default, self.minimum, self.maximum = default, minimum, maximum

    def to_json(self):
        j = {"type": self.type}
        if self.default is not None:
            j["default"] = self.default
        if self.minimum is not None:
            j["minimum"] = self.minimum
        if self.maximum is not None:
            j["maximum"] = self.maximum
        return j


class McpTool:
    def __init__(self, name, description, props, callback):
        self.name, self.description = name, description
        self.props = props              # list[Property]
        self.callback = callback

    def to_json(self):
        properties, required = {}, []
        for p in self.props:
            properties[p.name] = p.to_json()
            if p.default is None:
                required.append(p.name)
        schema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return {"name": self.name, "description": self.description,
                "inputSchema": schema}

    async def call(self, args):
        kw = {}
        for p in self.props:
            v = args.get(p.name, p.default)
            if v is None and p.default is None:
                raise RuntimeError(f"Missing required argument: {p.name}")
            if p.type == "integer" and v is not None:
                v = int(v)
                if p.minimum is not None and v < p.minimum:
                    raise RuntimeError(f"{p.name} out of range")
                if p.maximum is not None and v > p.maximum:
                    raise RuntimeError(f"{p.name} out of range")
            elif p.type == "boolean" and v is not None:
                v = bool(v)
            kw[p.name] = v
        try:
            res = self.callback(**kw)
        except Exception as e:
            return json.dumps({"content": [{"type": "text", "text": str(e)}],
                               "isError": True})
        if isinstance(res, bool):
            text = "true" if res else "false"
        elif isinstance(res, (int, float)):
            text = str(res)
        else:
            text = str(res)
        return json.dumps({"content": [{"type": "text", "text": text}],
                           "isError": False})


def _get_volume():
    try:
        from maix import audio as _a
        return S.rec.volume() if S.rec else int(S.cfg.get("volume", 80))
    except Exception:
        return int(S.cfg.get("volume", 80))


def _tool_set_volume(volume):
    S.cfg["volume"] = int(volume)
    S.muted = False
    save_cfg()
    apply_volume()
    return int(volume)


def _tool_get_volume():
    return int(S.cfg.get("volume", 80))


def _tool_volume_status():
    return json.dumps({"volume": int(S.cfg.get("volume", 80)),
                       "muted": bool(S.muted), "available": True},
                      ensure_ascii=False)


def _tool_get_device_status():
    return json.dumps({
        "state": S.session_state,
        "network": S.network,
        "activated": S.activated,
        "volume": int(S.cfg.get("volume", 80)),
        "muted": S.muted,
        "wake_word": wake_word(),
        "uptime": int(time.time() - S.started_at),
    }, ensure_ascii=False)


def _tool_show_message(message):
    S.bubbles.append(("sys", str(message)))
    del S.bubbles[:-6]
    render_popup(visible=True)
    return "ok"


MCP_TOOLS = [
    McpTool("self.audio_speaker.set_volume", "设置扬声器音量",
            [Property("volume", "integer", None, 0, 100)], _tool_set_volume),
    McpTool("self.audio_speaker.get_volume", "获取当前音量", [], _tool_get_volume),
    McpTool("self.audio_speaker.get_volume_status", "获取音量状态", [],
            _tool_volume_status),
    McpTool("self.get_device_status", "获取设备状态", [], _tool_get_device_status),
    McpTool("self.display.show_message", "在屏幕上显示一条消息",
            [Property("message", "string")], _tool_show_message),
]


class McpServer:
    def __init__(self):
        self.tools = list(MCP_TOOLS)

    async def parse_message(self, message):
        rid = None
        try:
            data = json.loads(message) if isinstance(message, str) else message
        except Exception as e:
            log("[mcp] json err:", e)
            return
        if data.get("jsonrpc") != "2.0":
            return
        method = data.get("method")
        if not method:
            return
        if method.startswith("notifications"):
            return
        rid = data.get("id")
        params = data.get("params") or {}
        if rid is None:
            return
        log(f"[mcp] {method} id={rid}")
        if method == "initialize":
            await self._reply(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "xiaozhi-maixcam2",
                               "version": S.cfg.get("app_version", "2.0.0")},
            })
        elif method == "tools/list":
            await self._tools_list(rid, params)
        elif method == "tools/call":
            await self._tool_call(rid, params)
        else:
            await self._error(rid, f"Method not implemented: {method}")

    async def _tools_list(self, rid, params):
        cursor = params.get("cursor", "")
        budget = 8000
        out, total, nxt = [], 0, ""
        found = not cursor
        for t in self.tools:
            if not found:
                if t.name == cursor:
                    found = True
                else:
                    continue
            tj = t.to_json()
            size = len(json.dumps(tj, ensure_ascii=False))
            if total + size + 100 > budget:
                nxt = t.name
                break
            out.append(tj)
            total += size
        result = {"tools": out}
        if nxt:
            result["nextCursor"] = nxt
        await self._reply(rid, result)

    async def _tool_call(self, rid, params):
        name = params.get("name")
        tool = next((t for t in self.tools if t.name == name), None)
        if not tool:
            await self._error(rid, f"Unknown tool: {name}")
            return
        args = params.get("arguments") or {}
        log(f"[mcp] call {name} {args}")
        await self._reply(rid, json.loads(await tool.call(args)))

    async def _send(self, payload):
        ws = S.ws
        if ws is None:
            log("[mcp] no ws, drop reply")
            return
        try:
            await ws.send(json.dumps({"session_id": S.session_id,
                                      "type": "mcp", "payload": payload},
                                     ensure_ascii=False))
        except Exception as e:
            log("[mcp] send err:", e)

    async def _reply(self, rid, result):
        await self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    async def _error(self, rid, msg):
        await self._send({"jsonrpc": "2.0", "id": rid,
                          "error": {"code": -32603, "message": msg}})


mcp_server = McpServer()


# ---------------- sherpa-onnx KWS 唤醒（对齐 py-xiaozhi） ----------------
KWS_PARAMS = dict(num_threads=2, feature_dim=80, max_active_paths=2,
                  keywords_score=1.8, keywords_threshold=0.2,
                  num_trailing_blanks=1, provider="cpu")
KWS_COOLDOWN = 1.5

KW_MAP = {
    "你好小智": "n ǐ h ǎo x iǎo zh ì",
    "小智": "x iǎo zh ì",
    "嗨小智": "h āi x iǎo zh ì",
    "你好小夏": "n ǐ h ǎo x iǎo x ià",
    "你好小哈": "n ǐ h ǎo x iǎo h ā",
}


class WakeWordDetector:
    def __init__(self):
        self.spotter = None
        self.stream = None
        self.lock = threading.Lock()
        self.last_hit = 0.0
        self.ok = False

    def build_keywords(self):
        kw = wake_word()
        phon = KW_MAP.get(kw) or KW_MAP["你好小智"]
        try:
            with open(KWS_KEYWORDS, "w") as f:
                f.write(f"{phon} @{kw}\n")
        except Exception as e:
            log("keywords write err:", e)
        return kw

    def load_model(self):
        if self.ok:
            return True
        try:
            import sherpa_onnx
        except Exception as e:
            log("sherpa_onnx import fail:", e)
            return False
        for f in ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
            if not os.path.exists(f"{KWS_MODEL_DIR}/{f}"):
                log("KWS model missing:", f)
                return False
        self.build_keywords()
        try:
            with self.lock:
                sp = sherpa_onnx.KeywordSpotter(
                    tokens=f"{KWS_MODEL_DIR}/tokens.txt",
                    encoder=f"{KWS_MODEL_DIR}/encoder.onnx",
                    decoder=f"{KWS_MODEL_DIR}/decoder.onnx",
                    joiner=f"{KWS_MODEL_DIR}/joiner.onnx",
                    keywords_file=KWS_KEYWORDS,
                    sample_rate=SAMPLE_RATE, **KWS_PARAMS)
                self.spotter = sp
                self.stream = sp.create_stream()
            self.ok = True
            log("唤醒引擎就绪 词=", wake_word())
            return True
        except Exception as e:
            log("KeywordSpotter load fail:", e)
            return False

    def reload_keyword(self):
        with self.lock:
            sp, st = self.spotter, self.stream
            self.spotter = self.stream = None
            self.ok = False
        try:
            if st is not None:
                del st
            if sp is not None:
                del sp
        except Exception:
            pass
        return self.load_model()

    def feed(self, pcm16):
        if not self.ok:
            return
        try:
            x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception:
            return
        with self.lock:
            if not self.ok:
                return
            try:
                self.stream.accept_waveform(SAMPLE_RATE, x)
                while self.spotter.is_ready(self.stream):
                    self.spotter.decode_stream(self.stream)
                res = self.spotter.get_result(self.stream)
            except Exception as e:
                log("kws err:", e)
                return
        if res and str(res).strip():
            now = mono()
            if now - self.last_hit >= KWS_COOLDOWN:
                self.last_hit = now
                log("语音唤醒:", res)
                if S.loop:
                    S.loop.call_soon_threadsafe(S.wake_event_q.put_nowait, "wake")

    def fresh_stream(self):
        if not self.ok:
            return
        with self.lock:
            try:
                self.stream = self.spotter.create_stream()
            except Exception:
                pass


kws = WakeWordDetector()


# ---------------- 麦克风采集 ----------------
class Mic:
    def __init__(self, rec=None):
        self.rec = rec
        self._acc = b""

    def open(self):
        if self.rec is None:
            from maix import audio as maix_audio
            self.rec = maix_audio.Recorder(
                sample_rate=SAMPLE_RATE,
                format=maix_audio.Format.FMT_S16_LE, channel=1, block=False)
        self.rec.volume(max(0, min(100, int(S.cfg.get("mic_gain", 40)))))
        log("麦克风就绪 sr=", self.rec.sample_rate())

    def read_chunk(self):
        try:
            data = self.rec.record(FRAME_MS)
            if data:
                self._acc += bytes(data)
        except Exception as e:
            log("mic read err:", e)
            time.sleep(0.2)
            return None
        try:
            grf = getattr(self.rec, "get_remaining_frames", None)
            if grf:
                guard = 0
                while grf() > SAMPLE_RATE // 5 and guard < 20:
                    guard += 1
                    extra = self.rec.record(FRAME_MS)
                    if not extra:
                        break
                    self._acc += bytes(extra)
        except Exception:
            pass
        if len(self._acc) < CHUNK_BYTES:
            return None
        chunk = self._acc[:CHUNK_BYTES]
        self._acc = self._acc[CHUNK_BYTES:]
        return chunk

    def close(self):
        try:
            self.rec.reset(False)
        except Exception:
            pass


def _mic_rebuild(mic):
    """已弃用：maix 多媒体栈为进程级单例，运行期重建会失败并扰动 launcher 显示。"""
    try:
        mic.close()
    except Exception:
        pass
    from maix import audio as maix_audio
    mic.rec = maix_audio.Recorder(sample_rate=SAMPLE_RATE,
                                  format=maix_audio.Format.FMT_S16_LE,
                                  channel=1, block=False)
    mic.rec.volume(max(0, min(100, int(S.cfg.get("mic_gain", 40)))))
    mic._acc = b""
    log("麦克风重建完成")


mic = None
S.inject = None          # 调试：用 WAV 替代麦克风（/tmp/xz_inwav 写入路径）


def load_wav_mono16(path):
    import wave
    w = wave.open(path)
    sw, nch, sr = w.getsampwidth(), w.getnchannels(), w.getframerate()
    data = w.readframes(w.getnframes())
    w.close()
    if sw != 2:
        raise RuntimeError("only 16-bit wav supported")
    if nch == 2:
        a = np.frombuffer(data, dtype=np.int16).reshape(-1, 2)
        data = a.mean(axis=1).astype(np.int16).tobytes()
    if sr != SAMPLE_RATE:
        data = resample_pcm(data, sr, SAMPLE_RATE)
    return data


def mic_thread():
    global mic
    try:
        mic = Mic(getattr(S, "rec", None))
        mic.open()
    except Exception as e:
        S.last_error = f"麦克风打开失败: {e}"
        log(S.last_error)
        write_state()
        return
    # 启动自检
    chk = b""
    t0 = mono()
    while mono() - t0 < 0.6 and len(chk) < CHUNK_BYTES * 10:
        c = mic.read_chunk()
        if c:
            chk += c
        else:
            time.sleep(0.003)
    if chk:
        a = np.frombuffer(chk[: len(chk) // CHUNK_BYTES * CHUNK_BYTES],
                          dtype=np.int16).astype(np.float32)
        r0 = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
        log("麦克风自检 rms=", round(r0, 1))
        if r0 < 30:
            # 同样不重建（会扰动共享的 maix 多媒体栈）；交给下面的 30s 看门狗重启
            log("警告：麦克风自检近乎全静音，若持续无数据将自动重启服务")
    last_ok = mono()
    stuck_log = 0.0
    stuck_since = 0.0
    inj_pos = 0
    inj_sil = 0
    while True:
        # ---- 调试注入：用 WAV 顶替麦克风，跑完后补 2s 静音（触发本地 VAD 断句）----
        if S.inject is not None:
            data = S.inject
            if inj_pos >= len(data):
                inj_sil += 1
                chunk = b"\x00" * CHUNK_BYTES
                if inj_sil > 100:
                    S.inject = None
                    inj_pos = inj_sil = 0
            else:
                chunk = data[inj_pos:inj_pos + CHUNK_BYTES]
                inj_pos += CHUNK_BYTES
                if len(chunk) < CHUNK_BYTES:
                    chunk = chunk + b"\x00" * (CHUNK_BYTES - len(chunk))
            time.sleep(FRAME_MS / 1000.0)
        else:
            chunk = mic.read_chunk()
        if chunk is None:
            # 运行期绝不重建 Recorder：maix 多媒体栈是进程级单例，重建会返回
            # "Busy: ai has been initialized"，并反复扰动与 launcher 共享的
            # AX/DRM 资源，把显示一起拖死（实测黑屏）。改为超时后交 systemd 重启。
            now = mono()
            if stuck_since == 0.0:
                stuck_since = now
            if now - stuck_since > 3 and now - stuck_log > 20:
                stuck_log = now
                log("麦克风连续无数据 %.0fs（超 30s 自动重启服务）"
                    % (now - stuck_since))
            if now - stuck_since > 30:
                log("麦克风连续 30s 无数据 → 退出，由 systemd 重启服务")
                write_state()
                time.sleep(0.2)
                os._exit(1)
            time.sleep(0.004)
            continue
        stuck_since = 0.0
        last_ok = mono()
        S.mic_blocks += 1
        if not S.session_active or (S.mic_blocks % 3 == 0):
            arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr)) + 1e-6)
            S.mic_rms = rms
            if rms > S.mic_peak:
                S.mic_peak = rms
        # 会话中：立刻编码成 opus 入队（sender 只负责 ws.send，不占事件循环）
        # 但 TTS 播放期间 sender 会丢弃上行（防自激），此时编码纯属浪费 CPU，
        # 还会和播放线程抢 GIL 造成卡顿 —— 直接跳过。
        if S.uplink_on and S.enc is not None and not S.tts_playing:
            try:
                opus = S.enc.encode(chunk, CHUNK)
            except Exception:
                opus = None
            if opus:
                with S.up_lock:
                    S.up_q.append((chunk, opus))
                    if len(S.up_q) > 250:          # 5s 上限，丢最旧
                        drop = len(S.up_q) - 250
                        del S.up_q[:drop]
                        S.up_dropped += drop
        else:
            # 空闲：交给唤醒线程（队列满则丢最旧，绝不阻塞采集）
            if S.cfg.get("enabled", True) and not S.session_active \
                    and mono() - S.last_play_ts > 0.8:
                try:
                    S.kws_q.put_nowait(chunk)
                except queue.Full:
                    try:
                        S.kws_q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        S.kws_q.put_nowait(chunk)
                    except queue.Full:
                        pass


def kws_thread():
    """独立唤醒线程：只做 sherpa-onnx 推理，不碰采集与网络"""
    while True:
        try:
            chunk = S.kws_q.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            kws.feed(chunk)
        except Exception as e:
            log("kws feed err:", e)


# ---------------- 播放 ----------------
def resample_pcm(pcm, sr_in, sr_out):
    if sr_in == sr_out or not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if x.size == 0:
        return pcm
    if sr_out == sr_in * 2:                      # 24k→48k：线性插值（快路径）
        y = np.empty(x.size * 2, dtype=np.float32)
        y[0::2] = x
        y[1::2] = (x + np.roll(x, -1)) * 0.5
        y[-1] = x[-1]
        return np.clip(y, -32768, 32767).astype(np.int16).tobytes()
    n_out = int(round(x.size * sr_out / sr_in))
    if n_out <= 0:
        return b""
    t = np.linspace(0.0, x.size - 1.0, n_out, endpoint=False, dtype=np.float32)
    i0 = t.astype(np.int32)
    i1 = np.minimum(i0 + 1, x.size - 1)
    frac = t - i0
    y = x[i0] * (1.0 - frac) + x[i1] * frac
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


def pick_output_rate(pa):
    for sr in (48000, 16000, 32000, 44100, 8000):
        try:
            s = pa.open(format=pyaudio.paInt16, channels=1, rate=sr,
                        output=True, frames_per_buffer=1024)
            s.close()
            log("扬声器支持采样率", sr)
            return sr
        except Exception:
            continue
    return 48000


def play_thread(pa=None):
    """播放线程：抖动缓冲 + 成块写入。

    旧实现每收到一个 20ms 帧就 write 一次（50 次/秒 Python 调用），任何一次
    被 GIL 抢占超过 20ms 都会让 ALSA 缓冲见底 → 声音一顿一顿。
    现在攒够 100ms 才写一次，且冷启动先攒 ~200ms 做抖动缓冲，明显抗抖动。
    """
    pa = pa or pyaudio.PyAudio()
    out_sr = pick_output_rate(pa)
    S.out_sr = out_sr
    per_buf = int(out_sr * 0.30)
    write_bytes = int(out_sr * 0.10) * 2      # 每次写 100ms
    prime_wait = 2.00                         # 攒不满也最多等 3.5s（服务端 ~0.8× 实时，
                                              # 攒够 2.5s 缓冲本身就要 3s 左右）
    log("播放：out_sr=%d 缓冲=%dms 写入块=100ms 抖动缓冲=自适应"
        % (out_sr, per_buf * 1000 // out_sr))

    stream = None
    acc = bytearray()
    acc_since = 0.0
    last_rx = 0.0
    last_write = 0.0
    idle = True
    while True:
        got = False
        while True:
            with S.play_lock:
                item = S.play_q.pop(0) if S.play_q else None
            if item is None:
                break
            pcm, sr = item
            if sr != out_sr:
                pcm = resample_pcm(pcm, sr, out_sr)
            if pcm:
                acc += pcm
            S.played += 1
            got = True
            if len(acc) >= write_bytes * 4:   # 单轮最多攒 400ms，防饥饿
                break
        now = mono()
        if got:
            S.last_play_ts = now
            last_rx = now
            if acc_since == 0.0:
                acc_since = now
            if S.played % 200 == 0:
                with S.play_lock:
                    qd = len(S.play_q)
                log("播放中", S.played, "块 队列", qd, "待写",
                    len(acc) // 2 * 1000 // out_sr, "ms")

        # 只有「停了 0.6s 以上」才算新一轮，需要重新预攒。
        # 漏掉这个判断会导致：缓冲一抽空就重新预攒 → 播 100ms、停 300ms、再播，
        # 听起来就是一顿一顿（比真的欠载还难听）。
        if not acc:
            if now - last_write > 0.6:
                idle = True
            time.sleep(0.005)
            continue

        # 冷启动预攒：攒够自适应阈值或等够 prime_wait 再开口。
        # 服务端下发实测只有 0.6~1.0× 实时（句子之间要等合成），固定 200ms
        # 预攒必然被抽干 → 自适应地在欠载后加大、顺畅后收回。
        if idle:
            prime_bytes = int(out_sr * (S.play_prime_ms / 1000.0)) * 2
            if len(acc) < prime_bytes and (now - acc_since) < prime_wait:
                time.sleep(0.005)
                continue
            wait_ms = int((now - acc_since) * 1000) if acc_since else 0
            log("开始播放：预攒 %dms（目标 %dms，等待 %dms）"
                % (len(acc) // 2 * 1000 // out_sr, S.play_prime_ms, wait_ms))
            idle = False

        # 队列空且 80ms 没有新数据 → 把手上剩下的全部写掉（收尾不拖沓）
        draining = (not got) and (now - last_rx) > 0.08
        with S.play_lock:
            _q_empty = not S.play_q
        # 句尾排空是正常收尾，不算欠载（否则每句都会误报一次并把缓冲顶到上限）
        if draining and S.tts_playing and _q_empty \
                and now - S.last_sent_end > 0.5:
            S.play_underrun += 1
            if S.play_prime_ms < 1000:            # 欠载 → 下次多攒一点
                S.play_prime_ms = min(1000, S.play_prime_ms + 200)
            if S.play_underrun % 5 == 1:
                log("播放欠载 ×%d（抖动缓冲 %dms）"
                    % (S.play_underrun, S.play_prime_ms))
        if len(acc) < write_bytes and not draining:
            time.sleep(0.005)
            continue

        buf = bytes(acc)
        acc.clear()
        acc_since = 0.0
        if os.path.exists("/tmp/xz_noplay"):     # 调试：只收不播
            continue
        try:
            if stream is None:
                stream = pa.open(format=pyaudio.paInt16, channels=1, rate=out_sr,
                                 output=True, frames_per_buffer=per_buf)
            stream.write(buf, exception_on_underflow=False)
            last_write = now
        except Exception as e:
            log("play err:", e)
            try:
                if stream:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            stream = None
            time.sleep(0.05)


# ---------------- 触摸唤醒线程 ----------------
def touch_thread(loop, trigger_q):
    try:
        from maix import touchscreen
        ts = touchscreen.TouchScreen()
    except Exception as e:
        log("触摸不可用:", e)
        return
    last = 0.0
    while True:
        try:
            x, y, pressed = ts.read()
        except Exception:
            time.sleep(0.5)
            continue
        if pressed and mono() - last > 2:
            last = mono()
            # 弹窗可见时触摸由 overlay 接管（避免同一次点击既关窗又唤醒）；
            # 刚处理过弹窗点击的 1.5s 内也忽略，防止关窗动作把窗口又拉起来。
            if (not S.session_active and not S.popup_visible
                    and mono() - S.last_popup_cmd > 1.5
                    and S.cfg.get("touch_wake", False)
                    and S.cfg.get("enabled", True)):
                log("触摸唤醒")
                loop.call_soon_threadsafe(trigger_q.put_nowait, "touch")
        time.sleep(0.05)


# ---------------- Opus TOC 解析（py-xiaozhi parse_opus_toc） -------------
_TOC_FRAME_MS = {0: 10, 1: 20, 2: 40, 3: 60}


def opus_frame_samples(data):
    """从 TOC 字节推导一包解出的样点数（服务器可能用 20/40/60ms）"""
    if not data:
        return int(OUTPUT_SR * 0.02)
    toc = data[0]
    code = toc & 0x03
    if code == 0:
        frame_ms, nframes = _TOC_FRAME_MS[(toc >> 3) & 0x03], 1
    elif code == 3:
        frame_ms, nframes = _TOC_FRAME_MS[(toc >> 3) & 0x03], 1  # 同 CELT/hybrid 单帧
    else:
        frame_ms, nframes = _TOC_FRAME_MS[code - 1], 2
    return int(OUTPUT_SR * frame_ms * nframes / 1000)


# ---------------- WS 会话（对齐 py-xiaozhi 协议） ----------------
def hello_msg():
    return {
        "type": "hello", "version": 1, "transport": "websocket",
        "features": {"mcp": True},
        "audio_params": {"format": "opus", "sample_rate": SAMPLE_RATE,
                         "channels": 1, "frame_duration": FRAME_MS},
    }


def _set_state(state, detail=None):
    S.session_state = state
    if detail is not None:
        S.detail = detail
    render_popup()
    write_state()


class Vad:
    """能量 VAD：自适应噪声底 + 起止滞回。

    本机实测服务器不做断句，必须由客户端给出说话结束点（listen stop）。
    """

    def __init__(self, silence_ms=650, start_ms=60, min_rms=180.0):
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.start_frames = max(1, start_ms // FRAME_MS)
        self.min_rms = min_rms
        self.noise = 300.0
        self.reset()

    def reset(self):
        self.speech = False
        self.hit = 0
        self.sil = 0
        self.frames = 0
        self.peak = 0.0

    def push(self, rms):
        """返回 (speech_started, speech_ended)"""
        self.frames += 1
        if not self.speech:
            # 噪声底跟踪（只在非语音段更新）
            self.noise = 0.95 * self.noise + 0.05 * rms
            thr = max(self.min_rms, self.noise * 2.6)
            if rms > thr:
                self.hit += 1
                if self.hit >= self.start_frames:
                    self.speech = True
                    self.sil = 0
                    self.peak = rms
                    return True, False
            else:
                self.hit = 0
            return False, False
        thr = max(self.min_rms * 0.8, self.noise * 1.8)
        self.peak = max(self.peak, rms)
        if rms < thr:
            self.sil += 1
            if self.sil >= self.silence_frames:
                self.speech = False
                return False, True
        else:
            self.sil = 0
        # 超长语音保护（20s 强制断句）
        if self.frames > 20 * 1000 // FRAME_MS:
            self.speech = False
            return False, True
        return False, False


async def session_run(reason="wake"):
    S.session_active = True
    S.emotion = ""
    S.bubbles = []
    S.tts_playing = False
    S.uplink_frames = 0
    with S.play_lock:
        S.play_q.clear()
    with S.live_lock:
        S.live.clear()
        S.live_seq = 0
    ws = None
    snd = rcv = None
    try:
        _set_state("connecting", "连接服务器…")
        set_status_icon("conn")

        url = ota_info["ws_url"] or "wss://api.tenclass.net/xiaozhi/v1/"
        headers = {
            "Authorization": f"Bearer {ota_info['token']}",
            "Protocol-Version": "1",
            "Device-Id": device_mac(),
            "Client-Id": S.cfg["client_id"],
        }
        log("WS 连接:", url)
        conn = dict(ping_interval=20, ping_timeout=20, close_timeout=10,
                    open_timeout=5, max_size=10 * 1024 * 1024, compression=None)
        try:
            ws = await asyncio.wait_for(
                websockets.connect(url, additional_headers=headers, **conn),
                timeout=15)
        except TypeError:
            ws = await asyncio.wait_for(
                websockets.connect(url, extra_headers=headers, **conn),
                timeout=15)
        S.ws = ws
        S.session_id = ""
        await ws.send(json.dumps(hello_msg()))

        enc = opuslib.Encoder(SAMPLE_RATE, 1, opuslib.APPLICATION_VOIP)
        S.enc = enc
        # 轮次状态机：
        #   LISTENING  turn_open=True  正在上行 + 本地 VAD 断句
        #   WAITING    turn_open=False 已发 listen stop，等 stt/llm/tts
        #   SPEAKING   tts_playing=True 播放中，丢弃麦克风（无 AEC，防自激）
        st = {
            "dec": opuslib.Decoder(OUTPUT_SR, 1),
            "hello": asyncio.Event(),
            "turn_open": False,
            "waiting_since": 0.0,
            "last_tts": mono(),
            "idle_since": mono(),
            "turn": 0,
            "rx_audio": 0,
            "rx_t0": 0.0,
            "sent_t0": 0.0,
            "sent_f0": 0,
            "sent_u0": 0,
            "vbuf": 0.0,
            "vbuf_min": 0.0,
            "seg_frames": 0,
            "seg_peak": 0.0,
        }

        vad = Vad(silence_ms=int(S.cfg.get("vad_silence_ms", 650)))

        async def send_listen_start():
            st["turn"] += 1
            st["turn_open"] = True
            st["waiting_since"] = 0.0
            st["idle_since"] = mono()
            vad.reset()
            with S.up_lock:
                S.up_q.clear()
            S.uplink_on = True
            await ws.send(json.dumps({"session_id": S.session_id,
                                      "type": "listen",
                                      "state": "start", "mode": "manual"}))
            log(f"→ listen start (第 {st['turn']} 轮)")

        async def send_listen_stop():
            if st["turn_open"]:
                st["turn_open"] = False
                S.uplink_on = False
                st["waiting_since"] = mono()
                await ws.send(json.dumps({"session_id": S.session_id,
                                          "type": "listen", "state": "stop",
                                          "mode": "manual"}))
                log("→ listen stop（本地 VAD 断句）")

        async def sender():
            spoke = False
            announced = False
            while True:
                tnow = mono()

                # ---- 播放中 / 刚播完 / 非聆听态：清空积压，保证下次 listen start
                #      之后上行的是实时音频而不是几秒前的旧数据 ----
                if S.tts_playing or tnow - st["last_tts"] < 0.20 \
                        or not st["turn_open"]:
                    with S.up_lock:
                        S.up_discard += len(S.up_q)   # 主动丢弃（播放中/非聆听态）
                        S.up_q.clear()
                    # 注意：这里不能直接 continue —— 下面的「无响应看门狗」必须在
                    # 「已断句、等服务器回话」这个状态下继续跑，否则服务器一沉默
                    # 会话就永久卡在「思考中」。
                    if st["waiting_since"] and tnow - st["waiting_since"] > 8:
                        log("服务器 8s 无响应 → 重开一轮")
                        st["waiting_since"] = 0.0
                        vad.reset()
                        await send_listen_start()
                    await asyncio.sleep(0.02)
                    continue

                with S.up_lock:
                    item = S.up_q.pop(0) if S.up_q else None

                if item is None:
                    await asyncio.sleep(0.002)
                else:
                    pcm, opus = item
                    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                    rms = float(np.sqrt(np.mean(a * a)) + 1e-6)
                    started, ended = vad.push(rms)
                    if started:
                        spoke = True
                        st["idle_since"] = tnow
                        st["seg_frames"] = 0
                        st["seg_peak"] = 0.0
                        st["speech_rms"] = max(st.get("speech_rms", 0.0), rms)
                        log("检测到说话 rms=%.0f 噪声底=%.0f 阈值=%.0f"
                            % (rms, vad.noise, max(vad.min_rms, vad.noise * 2.6)))
                        if not announced:
                            announced = True
                            _set_state("listening", "聆听中")
                    if ended and spoke:
                        spoke = False
                        announced = False
                        dur = st.get("seg_frames", 0) * FRAME_MS / 1000.0
                        log("本地断句：本段上行 %.2fs 峰值 rms=%.0f（累计丢 %d 块）"
                            % (dur, st.get("seg_peak", 0.0), S.up_dropped))
                        vad.reset()
                        _set_state("thinking", "思考中")
                        await send_listen_stop()
                    else:
                        if spoke:
                            st["seg_frames"] = st.get("seg_frames", 0) + 1
                            st["seg_peak"] = max(st.get("seg_peak", 0.0), rms)
                        try:
                            await ws.send(opus)
                            S.up_sent += 1
                        except Exception:
                            return

                # ---- 无响应看门狗：断句后 8s 无任何下行 → 重开一轮 ----
                if st["waiting_since"] and tnow - st["waiting_since"] > 8:
                    log("服务器 8s 无响应 → 重开一轮")
                    st["waiting_since"] = 0.0
                    announced = False
                    vad.reset()
                    await send_listen_start()

                # ---- 空闲收尾：连续对话长时间没人说话 → 结束会话 ----
                if st["turn_open"] and not spoke and \
                        tnow - st["idle_since"] > int(S.cfg.get("idle_close_s", 20)):
                    log("空闲超时 → 结束会话")
                    return

                await asyncio.sleep(0)

        async def receiver():
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=40)
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, bytes):
                    try:
                        n = opus_frame_samples(msg)
                        pcm = st["dec"].decode(msg, n)
                        with S.play_lock:
                            S.play_q.append((pcm, OUTPUT_SR))
                            qd = len(S.play_q)
                        # 帧间隔统计：区分「服务端均匀地慢」和「一顿一顿地来」
                        _now = mono()
                        _lf = st.get("last_frame_ts", 0.0)
                        if _lf:
                            g = _now - _lf
                            st["gap_max"] = max(st.get("gap_max", 0.0), g)
                            st["gap_sum"] = st.get("gap_sum", 0.0) + g
                            st["gap_n"] = st.get("gap_n", 0) + 1
                            if g > 0.06:
                                st["gap_slow"] = st.get("gap_slow", 0) + 1
                            # 虚拟缓冲：每收一帧 +20ms，每流逝 g 秒 -g。
                            # 它的最低点 = 这一句「不欠载」所需的最小抖动缓冲，
                            # 比只看单次最大间隔准确（能算上累计欠账）。
                            v = st.get("vbuf", 0.0) + 0.020 - g
                            st["vbuf"] = v
                            if v < st.get("vbuf_min", 0.0):
                                st["vbuf_min"] = v
                        st["last_frame_ts"] = _now
                        st["rx_audio"] += 1
                        if st["rx_audio"] == 1:
                            st["rx_t0"] = mono()
                            log("← 下行音频开始（采样率", OUTPUT_SR, "队列", qd, "）")
                        elif st["rx_audio"] % 250 == 0:
                            el = max(0.001, mono() - st["rx_t0"])
                            log("← 下行 %d 帧 / %.1fs = %.2f× 实时  队列 %d"
                                % (st["rx_audio"], el,
                                   st["rx_audio"] * 0.02 / el, qd))
                    except Exception as e:
                        log("opus 解码错误:", e)
                    continue
                try:
                    m = json.loads(msg)
                except Exception:
                    continue
                mtype = m.get("type")
                st["last_rx"] = time.time()
                if mtype == "tts":
                    log("← tts", m.get("state"), repr(m.get("text", ""))[:80])
                elif mtype != "llm":
                    log("← ", str(m)[:200])

                if mtype == "hello":
                    tr = m.get("transport")
                    if tr and tr != "websocket":
                        log("不支持的传输方式:", tr)
                        return
                    S.session_id = m.get("session_id", "")
                    ap = m.get("audio_params") or {}
                    sr = int(ap.get("sample_rate", OUTPUT_SR))
                    if sr != OUTPUT_SR:
                        try:
                            st["dec"] = opuslib.Decoder(sr, 1)
                        except Exception:
                            pass
                    log("服务器 hello session=", S.session_id, "下行采样率=", sr)
                    st["hello"].set()
                elif mtype == "tts":
                    s = m.get("state")
                    if s == "start":
                        S.tts_playing = True
                        st["waiting_since"] = 0.0
                        st["vbuf"] = 0.0
                        st["vbuf_min"] = 0.0
                        with S.play_lock:
                            S.play_q.clear()
                        _set_state("speaking", "说话中")
                    elif s == "sentence_start":
                        st["sent_t0"] = mono()
                        st["sent_f0"] = st["rx_audio"]
                        st["sent_u0"] = S.play_underrun
                        st["vbuf_min"] = 0.0
                        t = m.get("text", "")
                        # 过滤服务器的工具调用占位文本（如 "% get_weather..."）
                        if t.startswith("%") or t.rstrip().endswith("..."):
                            t = ""
                        if t:
                            S.bubbles.append(("ai", t))
                            del S.bubbles[:-6]
                            render_popup()
                    elif s == "sentence_end":
                        S.last_sent_end = mono()
                        n = st["rx_audio"] - st.get("sent_f0", 0)
                        el = max(0.001, mono() - st.get("sent_t0", mono()))
                        gn = max(1, st.get("gap_n", 1))
                        if n:
                            log("本句下行 %d 帧 / %.1fs = %.2f× 实时 | 帧间隔 平均%.0fms "
                                "最大%.0fms >60ms的停顿 %d 次"
                                % (n, el, n * 0.02 / el,
                                   st.get("gap_sum", 0.0) / gn * 1000,
                                   st.get("gap_max", 0.0) * 1000,
                                   st.get("gap_slow", 0)))
                        # 按实测抖动重定抖动缓冲目标：
                        #   本句有欠载            → 加大
                        #   本句最大帧间隔很大    → 至少覆盖住它
                        #   又顺又快              → 缓慢收回，压低延迟
                        new_under = S.play_underrun - st.get("sent_u0", 0)
                        gm = st.get("gap_max", 0.0) * 1000.0
                        need = int(-st.get("vbuf_min", 0.0) * 1000) + 250
                        need = max(300, min(1000, need))
                        # 只用虚拟缓冲算出的 need 收敛（欠载计数受句尾排空干扰，不参与）
                        S.play_prime_ms = int(0.6 * S.play_prime_ms + 0.4 * need)
                        S.play_prime_ms = max(350, min(1000, S.play_prime_ms))
                        log("本句需要 %dms 抖动缓冲 → 目标 %dms（欠载 %d，最大帧间隔 "
                            "%.0fms，>60ms 停顿 %d 次）| 事件循环卡顿 峰值%.0fms/%d 次"
                            % (need, S.play_prime_ms, new_under, gm,
                               st.get("gap_slow", 0),
                               S.loop_lag_max, S.loop_lag_n))
                        S.loop_lag_max = 0.0
                        S.loop_lag_n = 0
                        st["gap_sum"] = 0.0
                        st["gap_n"] = 0
                        st["gap_max"] = 0.0
                        st["gap_slow"] = 0
                        st["last_frame_ts"] = 0.0
                    elif s == "stop":
                        S.tts_playing = False
                        st["last_tts"] = mono()
                        st["waiting_since"] = 0.0
                        _set_state("listening", "聆听中")
                        await send_listen_start()      # 连续对话：立刻开下一轮
                elif mtype == "stt":
                    t = m.get("text", "")
                    st["waiting_since"] = 0.0
                    if t:
                        S.bubbles.append(("user", t))
                        del S.bubbles[:-6]
                    _set_state("thinking", "思考中")
                elif mtype == "llm":
                    st["waiting_since"] = 0.0
                    emo = m.get("emotion", "")
                    if emo:
                        S.emotion = emo
                    render_popup()
                elif mtype == "mcp":
                    payload = m.get("payload")
                    if payload is not None:
                        await mcp_server.parse_message(
                            payload if isinstance(payload, str)
                            else json.dumps(payload, ensure_ascii=False))
                elif mtype == "goodbye":
                    log("服务器 goodbye")
                    return
                elif mtype == "alert":
                    log("alert:", str(m)[:160])
                elif mtype == "system":
                    log("system:", str(m)[:160])

        snd = asyncio.ensure_future(sender())
        rcv = asyncio.ensure_future(receiver())
        try:
            await asyncio.wait_for(st["hello"].wait(), timeout=12)
        except asyncio.TimeoutError:
            raise RuntimeError("等待服务器 hello 超时")
        _set_state("listening", "聆听中")
        await send_listen_start()

        done, pending = await asyncio.wait({snd, rcv}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
            try:
                await p
            except Exception:
                pass
        for t in done:
            exc = t.exception()
            if exc:
                raise exc
    except asyncio.CancelledError:
        raise
    except Exception as e:
        S.last_error = str(e)[:200]
        log("会话错误:", e)
    finally:
        S.ws = None
        S.session_active = False
        S.uplink_on = False
        S.enc = None
        with S.up_lock:
            S.up_q.clear()
        S.tts_playing = False
        S.session_state = "idle"
        S.detail = ""
        with S.play_lock:
            S.play_q.clear()
        with S.live_lock:
            S.live.clear()
            S.live_seq = 0
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        set_status_icon("idle" if S.cfg.get("enabled", True) else "off")
        render_popup(visible=False)
        write_state()
        kws.fresh_stream()


# ---------------- 激活弹窗 ----------------
async def show_activation_popup():
    _set_state("activation", "待激活")
    try:
        await asyncio.sleep(15)
    except asyncio.CancelledError:
        raise
    if S.session_state == "activation":
        S.session_state = "idle"
        S.detail = ""
        render_popup(visible=False)
        write_state()


# ---------------- 主循环 ----------------
async def main_loop():
    S.wake_event_q = asyncio.Queue()
    loop = asyncio.get_running_loop()
    S.loop = loop

    threading.Thread(target=mic_thread, daemon=True).start()
    threading.Thread(target=play_thread, args=(getattr(S, "pa", None),),
                     daemon=True).start()
    threading.Thread(target=touch_thread, args=(loop, S.wake_event_q),
                     daemon=True).start()
    S.kws_q = queue.Queue(maxsize=25)
    threading.Thread(target=kws_thread, daemon=True).start()

    def _kws_init():
        if not kws.load_model():
            S.last_error = "唤醒模型加载失败（语音唤醒不可用）"
            write_state()
    threading.Thread(target=_kws_init, daemon=True).start()

    render_popup(visible=False)
    set_status_icon("idle" if S.cfg.get("enabled", True) else "off")
    write_state()

    async def ota_loop():
        fail = 0
        while True:
            try:
                await loop.run_in_executor(None, ota_poll)
                fail = 0
                S.last_error = ""
                if ota_info["need_activation"]:
                    ok = await loop.run_in_executor(None, try_activate)
                    if ok:
                        log("激活成功!")
                    write_state()
                    await asyncio.sleep(5)
                    continue
                write_state()
                await asyncio.sleep(3600)
            except Exception as e:
                fail += 1
                S.network = "离线" if fail >= 2 else "连接中…"
                S.last_error = f"OTA: {e}"[:160]
                write_state()
                await asyncio.sleep(min(30, 5 * fail))

    asyncio.ensure_future(ota_loop())

    async def lag_monitor():
        """事件循环卡顿监测：sleep(50ms) 实际睡过头多少 = 谁在阻塞收包。
        如果这里频繁报大值，说明下行“抖动”其实是我们自己处理的锅。"""
        while True:
            t0 = mono()
            await asyncio.sleep(0.05)
            d = (mono() - t0 - 0.05) * 1000.0
            if d > 60:
                S.loop_lag_max = max(S.loop_lag_max, d)
                S.loop_lag_n += 1
                if d > 800:
                    # 卡这么久一定有人在持 GIL 长阻塞，直接把所有线程的栈打出来
                    log("!!! 事件循环卡顿 %.0fms —— 打印全部线程栈" % d)
                    try:
                        faulthandler.dump_traceback(file=sys.stderr,
                                                    all_threads=True)
                    except Exception:
                        pass
                elif S.loop_lag_n % 10 == 1:
                    log("事件循环卡顿 %.0fms（累计 %d 次）" % (d, S.loop_lag_n))

    asyncio.ensure_future(lag_monitor())

    last_check = 0.0
    while True:
        now = mono()
        if now - last_check > 0.25:
            last_check = now
            if now - getattr(S, "_last_stats", 0) > 30:
                S._last_stats = now
                log("stats: state=%s mic=%d rms=%.0f/%.0f up=%d 丢=%d 主动弃=%d "
                    "欠载=%d kws=%s popup=%s"
                    % (S.session_state, S.mic_blocks, S.mic_rms, S.mic_peak,
                       S.up_sent, S.up_dropped, S.up_discard, S.play_underrun,
                       kws.ok, S.popup_visible))
                S.mic_peak = 0.0
            poll_popup_cmds()
            # 远程调试触发：touch /tmp/xz_call 开始一轮；/tmp/xz_stop 结束
            if os.path.exists("/tmp/xz_call"):
                os.unlink("/tmp/xz_call")
                log("调试触发：开始会话")
                S.wake_event_q.put_nowait("debug")
            if os.path.exists("/tmp/xz_stop"):
                os.unlink("/tmp/xz_stop")
                if S.session_task and not S.session_task.done():
                    log("调试触发：结束会话")
                    S.session_task.cancel()
            if os.path.exists("/tmp/xz_inwav"):
                try:
                    with open("/tmp/xz_inwav") as f:
                        path = f.read().strip()
                    os.unlink("/tmp/xz_inwav")
                    S.inject = load_wav_mono16(path)
                    log("调试注入音频:", path, len(S.inject), "字节")
                except Exception as e:
                    log("注入失败:", e)
            if os.path.exists("/tmp/xz_playwav"):
                try:
                    with open("/tmp/xz_playwav") as f:
                        path = f.read().strip()
                    os.unlink("/tmp/xz_playwav")
                    d = load_wav_mono16(path)
                    with S.play_lock:
                        S.play_q.append((d, SAMPLE_RATE))
                    log("调试播放音频:", path)
                except Exception as e:
                    log("播放失败:", e)
            m = cfg_mtime()
            if m > getattr(S, "_cfg_mtime", 0):
                S._cfg_mtime = m
                old = dict(S.cfg)
                S.cfg = load_cfg()
                if old.get("enabled") and not S.cfg.get("enabled"):
                    if S.session_task and not S.session_task.done():
                        S.session_task.cancel()
                    S.session_state = "idle"
                    render_popup(visible=False)
                    set_status_icon("off")
                elif not old.get("enabled") and S.cfg.get("enabled"):
                    set_status_icon("idle")
                if (old.get("wake_words") or [""])[0] != wake_word() and kws.ok:
                    log("唤醒词变更 →", wake_word())
                    threading.Thread(target=kws.reload_keyword, daemon=True).start()
                if old.get("volume") != S.cfg.get("volume"):
                    apply_volume()
                write_state()
        try:
            ev = await asyncio.wait_for(S.wake_event_q.get(), timeout=0.25)
        except asyncio.TimeoutError:
            continue
        if not S.cfg.get("enabled", True):
            continue
        if ev == "touch" and (S.popup_visible
                              or mono() - S.last_popup_cmd < 1.5):
            continue
        S.wake_src = ev
        log("唤醒来源:", ev)
        S.popup_dismissed = False
        if not S.activated:
            log("未激活 → 显示激活码")
            t = getattr(S, "activation_task", None)
            if t and not t.done():
                t.cancel()
            S.activation_task = asyncio.ensure_future(show_activation_popup())
            continue
        if S.session_active:
            # 打断当前会话：abort + 重开
            log("打断当前会话")
            with S.play_lock:
                S.play_q.clear()
            sess = S.session_task
            if sess and not sess.done():
                sess.cancel()
            await asyncio.sleep(0.35)
        S.session_task = asyncio.ensure_future(session_run(ev))
        await asyncio.sleep(0.3)


def main():
    os.makedirs(BASE, exist_ok=True)
    tune_gil()
    S.cfg = load_cfg()
    S._cfg_mtime = cfg_mtime()
    S.activated = bool(S.cfg.get("activated"))
    try:
        requests.packages.urllib3.disable_warnings()
    except Exception:
        pass
    try:
        import maix  # noqa: F401
        from maix import audio as maix_audio
        try:
            # block=False 关键：阻塞模式下 record() 的 pybind 绑定持 GIL 约 20ms，
            # 把整个进程量化成 20ms 一跳，事件循环收包成簇 → 下行 TTS 听起来一顿一顿。
            # 实测非阻塞模式主线程定时抖动从 10ms 降到 0.1ms，采集量不变。
            rec = maix_audio.Recorder(sample_rate=SAMPLE_RATE,
                                      format=maix_audio.Format.FMT_S16_LE,
                                      channel=1, block=False)
            rec.volume(max(0, min(100, int(S.cfg.get("mic_gain", 40)))))
            S.rec = rec
            log("maix.audio Recorder 就绪 sr=", rec.sample_rate())
        except Exception as e:
            S.rec = None
            log("Recorder 创建失败:", e)
    except Exception as e:
        S.rec = None
        log("maix 导入失败:", e)
    try:
        S.pa = pyaudio.PyAudio()
    except Exception as e:
        S.pa = None
        log("PyAudio 初始化失败:", e)
    try:
        apply_volume()
    except Exception:
        pass
    # 事件循环卡死 >3s 时自动把所有线程的栈打到 stderr（journal 里能看到），
    # 用来定位"下行音频一顿一顿"到底是网络还是我们自己在阻塞。
    log("小智服务 v2 启动 mac=", device_mac())
    write_state()
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
