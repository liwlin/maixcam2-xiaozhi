#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小智协议诊断 v2：回答 MCP initialize / manual 模式 / 文本注入，找出对话不通的根因"""
import asyncio, json, os, sys, time, wave
import requests, websockets, opuslib

requests.packages.urllib3.disable_warnings()
CFG = json.load(open("/root/xiaozhi/config.json"))
SR, FRAME_MS, CHUNK = 16000, 20, 320
MAC = open("/sys/class/net/wlan0/address").read().strip().lower()
WAV = "/maixapp/share/audio/demo.wav"


def ota():
    h = {"Device-Id": MAC, "Client-Id": CFG["client_id"],
         "Content-Type": "application/json",
         "User-Agent": "maixcam2/xiaozhi-1.0.0",
         "Accept-Language": "zh-CN", "Activation-Version": "2"}
    p = {"application": {"version": "1.0.0", "elf_sha256": CFG.get("hmac_key", "x")},
         "board": {"type": "maixcam2", "name": "xiaozhi-maixcam2", "ip": "0.0.0.0", "mac": MAC}}
    return requests.post(CFG["ota_url"], headers=h, json=p, timeout=15, verify=False).json()


def wav_pcm():
    w = wave.open(WAV)
    n = w.getnframes()
    return w.readframes(n - (n % CHUNK))


async def trial(label, mode, strip_lead_ms=0, answer_mcp=True, text=None):
    print(f"\n########## 试验: {label} (mode={mode}) ##########")
    d = ota()
    url = d["websocket"]["url"]
    tok = d["websocket"].get("token", "") or ""
    hdr = {"Authorization": f"Bearer {tok}", "Protocol-Version": "1",
           "Device-Id": MAC, "Client-Id": CFG["client_id"]}
    ws = await websockets.connect(url, additional_headers=hdr, ping_interval=20,
                                  ping_timeout=20, max_size=10 << 20, compression=None)
    enc = opuslib.Encoder(SR, 1, opuslib.APPLICATION_AUDIO)
    st = {"dec": None, "sr": 24000, "sid": "", "got_tts": 0, "txt": "", "stop": False,
          "hello_seen": False}

    async def rx():
        async for m in ws:
            t = time.strftime("%H:%M:%S")
            if isinstance(m, bytes):
                st["got_tts"] += 1
                if st["got_tts"] <= 3:
                    n = 0
                    try:
                        n = len(st["dec"].decode(m, 5760)) // 2 if st["dec"] else 0
                    except Exception as e:
                        print("  decode err", e)
                    print(f"[{t}] << BIN {len(m)}B -> {n}smp")
                continue
            print(f"[{t}] << {m[:300]}")
            j = json.loads(m)
            ty = j.get("type")
            if ty == "hello":
                st["sr"] = int((j.get("audio_params") or {}).get("sample_rate", 24000))
                st["dec"] = opuslib.Decoder(st["sr"], 1)
                st["sid"] = j.get("session_id", "")
                st["hello_seen"] = True
            elif ty == "mcp" and answer_mcp:
                pl = j.get("payload") or {}
                if pl.get("method") == "initialize":
                    rep = {"session_id": st["sid"], "type": "mcp", "payload": {
                        "jsonrpc": "2.0", "id": pl.get("id"),
                        "result": {"protocolVersion": "2024-11-05",
                                   "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "maixcam2", "version": "1.0.0"}}}}
                    await ws.send(json.dumps(rep))
                    print(f"[{t}] >> MCP initialize 应答")
            elif ty == "tts":
                st["txt"] += j.get("text", "")
                if j.get("state") == "stop":
                    st["stop"] = True
                    return
            elif ty == "stt":
                st["txt"] += "[STT]" + j.get("text", "")
            elif ty == "goodbye":
                return

    rxt = asyncio.ensure_future(rx())
    await ws.send(json.dumps({
        "type": "hello", "version": 1, "transport": "websocket",
        "features": {"mcp": True},
        "audio_params": {"format": "opus", "sample_rate": SR,
                         "channels": 1, "frame_duration": FRAME_MS}}))
    print(">> hello")
    for _ in range(200):
        if st["hello_seen"]:
            break
        await asyncio.sleep(0.05)
    start = {"session_id": st["sid"], "type": "listen", "state": "start", "mode": mode}
    if text:
        start["text"] = text
    await ws.send(json.dumps(start))
    print(">> listen start", json.dumps(start, ensure_ascii=False))

    if text is None:
        pcm = wav_pcm()
        lead = b"\x00" * (CHUNK * 2 * (strip_lead_ms // FRAME_MS))
        for i in range(0, len(lead), CHUNK * 2):
            await ws.send(enc.encode(lead[i:i + CHUNK * 2], CHUNK))
        t0 = time.time()
        k = 0
        for i in range(0, len(pcm) - CHUNK, CHUNK):
            if st["stop"] or rxt.done():
                break
            await ws.send(enc.encode(pcm[i:i + CHUNK], CHUNK))
            k += 1
            if k % 25 == 0:
                await asyncio.sleep(max(0, k * FRAME_MS / 1000 - (time.time() - t0)))
        print(f">> 上行 {k} 帧")
        if mode == "manual":
            await asyncio.sleep(0.4)
            await ws.send(json.dumps({"session_id": st["sid"], "type": "listen",
                                      "state": "stop", "mode": "manual"}))
            print(">> listen stop")
    try:
        await asyncio.wait_for(rxt, timeout=20)
    except asyncio.TimeoutError:
        rxt.cancel()
        print("!! 20s 内服务器无 tts stop")
    print(f"结果: tts_bin={st['got_tts']} text={st['txt'][:200]!r}")
    try:
        await ws.close()
    except Exception:
        pass


async def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "text"):
        await trial("文本注入(跳过ASR)", "manual", text="你好，请介绍一下你自己")
    if which in ("all", "auto"):
        await trial("auto 模式连续上行", "auto")
    if which in ("all", "manual"):
        await trial("manual 模式 + listen stop", "manual")


asyncio.run(main())
