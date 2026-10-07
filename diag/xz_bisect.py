#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定位「stt/llm 有、tts 无」的触发条件：逐个变量对照"""
import asyncio, json, sys, time, wave
import requests, websockets, opuslib

requests.packages.urllib3.disable_warnings()
CFG = json.load(open("/root/xiaozhi/config.json"))
SR, FRAME_MS, CHUNK = 16000, 20, 320
MAC = open("/sys/class/net/wlan0/address").read().strip().lower()
OTA = CFG["ota_url"]


def ota():
    h = {"Device-Id": MAC, "Client-Id": CFG["client_id"],
         "Content-Type": "application/json",
         "User-Agent": "maixcam2/xiaozhi-2.0.0",
         "Accept-Language": "zh-CN", "Activation-Version": "2"}
    p = {"application": {"version": "2.0.0", "elf_sha256": CFG.get("hmac_key", "x")},
         "board": {"type": "maixcam2", "name": "xiaozhi-maixcam2",
                   "ip": "0.0.0.0", "mac": MAC}}
    return requests.post(OTA, headers=h, json=p, timeout=10, verify=False).json()


def pcm(path):
    w = wave.open(path)
    n = w.getnframes()
    return w.readframes(n - (n % CHUNK))


TOOLS = [{"name": "self.audio_speaker.set_volume", "description": "设置音量",
          "inputSchema": {"type": "object",
                          "properties": {"volume": {"type": "integer",
                                                    "minimum": 0, "maximum": 100}},
                          "required": ["volume"]}},
         {"name": "self.get_device_status", "description": "设备状态",
          "inputSchema": {"type": "object", "properties": {}}}]


async def trial(name, *, answer_tools=True, answer_init=True, mode="manual",
                stop=True, wav="/tmp/xz_say16.wav", pace=True, trail_ms=0):
    print(f"\n===== {name} :: tools={answer_tools} init={answer_init} "
          f"mode={mode} stop={stop} pace={pace} trail={trail_ms} =====")
    d = ota()
    url = d["websocket"]["url"]
    tok = d["websocket"].get("token", "") or ""
    hdr = {"Authorization": f"Bearer {tok}", "Protocol-Version": "1",
           "Device-Id": MAC, "Client-Id": CFG["client_id"]}
    ws = await websockets.connect(url, additional_headers=hdr, ping_interval=20,
                                  ping_timeout=20, max_size=10 << 20, compression=None)
    enc = opuslib.Encoder(SR, 1, opuslib.APPLICATION_VOIP)
    st = {"sid": "", "dec": None, "sr": 24000, "tts": 0, "txt": "", "stop": False,
          "hello": False, "bin": 0}

    async def rx():
        async for m in ws:
            t = time.strftime("%H:%M:%S")
            if isinstance(m, bytes):
                st["bin"] += 1
                continue
            j = json.loads(m)
            ty = j.get("type")
            print(f"[{t}] << {str(m)[:150]}")
            if ty == "hello":
                st["sid"] = j.get("session_id", "")
                st["sr"] = int((j.get("audio_params") or {}).get("sample_rate", 24000))
                st["dec"] = opuslib.Decoder(st["sr"], 1)
                st["hello"] = True
            elif ty == "mcp":
                pl = j.get("payload") or {}
                meth = pl.get("method")
                if meth == "initialize" and answer_init:
                    await ws.send(json.dumps({"session_id": st["sid"], "type": "mcp",
                        "payload": {"jsonrpc": "2.0", "id": pl.get("id"),
                                    "result": {"protocolVersion": "2024-11-05",
                                               "capabilities": {"tools": {}},
                                               "serverInfo": {"name": "maixcam2",
                                                              "version": "2.0.0"}}}}))
                    print(f"[{t}] >> init reply")
                elif meth == "tools/list" and answer_tools:
                    await ws.send(json.dumps({"session_id": st["sid"], "type": "mcp",
                        "payload": {"jsonrpc": "2.0", "id": pl.get("id"),
                                    "result": {"tools": TOOLS}}}))
                    print(f"[{t}] >> tools/list reply")
            elif ty == "tts":
                st["tts"] += 1
                st["txt"] += j.get("text", "")
                if j.get("state") == "stop":
                    st["stop"] = True
                    return
            elif ty == "stt":
                st["txt"] += "[STT]" + j.get("text", "")
            elif ty == "goodbye":
                return

    rxt = asyncio.ensure_future(rx())
    await ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
                              "features": {"mcp": True},
                              "audio_params": {"format": "opus", "sample_rate": SR,
                                               "channels": 1, "frame_duration": FRAME_MS}}))
    for _ in range(200):
        if st["hello"]:
            break
        await asyncio.sleep(0.05)
    if mode == "auto":
        await ws.send(json.dumps({"session_id": st["sid"], "type": "listen",
                                  "state": "start", "mode": "auto"}))
    else:
        await ws.send(json.dumps({"session_id": st["sid"], "type": "listen",
                                  "state": "start", "mode": "manual"}))
    data = pcm(wav)
    sil = b"\x00" * (CHUNK * 2)
    t0 = time.time()
    k = 0

    async def send(p):
        nonlocal k
        await ws.send(enc.encode(p, CHUNK))
        k += 1
        if pace and k % 25 == 0:
            await asyncio.sleep(max(0, k * FRAME_MS / 1000 - (time.time() - t0)))

    for i in range(0, len(data) - CHUNK, CHUNK):
        await send(data[i:i + CHUNK])
    for _ in range(trail_ms // FRAME_MS):
        await send(sil)
    print(f">> 上行 {k} 帧 ({k*FRAME_MS/1000:.2f}s)")
    if stop and mode == "manual":
        await asyncio.sleep(0.3)
        await ws.send(json.dumps({"session_id": st["sid"], "type": "listen",
                                  "state": "stop", "mode": "manual"}))
        print(">> listen stop")
    try:
        await asyncio.wait_for(rxt, timeout=25)
    except asyncio.TimeoutError:
        rxt.cancel()
    print(f"结果: tts消息={st['tts']} 音频帧={st['bin']} text={st['txt'][:160]!r}")
    try:
        await ws.close()
    except Exception:
        pass
    return st["tts"] > 0, st["bin"]


async def main():
    const = sys.argv[1] if len(sys.argv) > 1 else "all"
    cases = {
        "A": ("不答 tools/list", dict(answer_tools=False)),
        "B": ("答 tools/list", dict(answer_tools=True)),
        "C": ("答 tools/list + 无尾静音 + 不 stop", dict(answer_tools=True, stop=False)),
        "D": ("答 tools/list + auto 模式", dict(answer_tools=True, mode="auto", stop=False)),
        "E": ("答 tools/list + 1.5s 尾静音", dict(answer_tools=True, trail_ms=1500)),
    }
    todo = list(cases) if const == "all" else [const]
    res = {}
    for k in todo:
        n, kw = cases[k]
        try:
            res[k] = await trial(n, **kw)
        except Exception as e:
            print("ERR", e)
            res[k] = ("ERR", 0)
    print("\n===== 汇总 =====")
    for k in todo:
        print(f"  {k} {cases[k][0]:32s} -> tts={res.get(k)}")


asyncio.run(main())
