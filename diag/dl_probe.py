#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小下行客户端：只连 WS、要一轮 TTS、记录每个 opus 帧的到达间隔。
目的：把「服务端发送节奏」和「我们客户端自己阻塞」彻底分开。
跑之前先 systemctl stop xiaozhi，避免抢麦克风/CPU。"""
import asyncio, json, sys, time, wave
import requests, websockets, opuslib
import numpy as np

requests.packages.urllib3.disable_warnings()
CFG = json.load(open("/root/xiaozhi/config.json"))
SR, FRAME_MS, CHUNK = 16000, 20, 320
MAC = open("/sys/class/net/wlan0/address").read().strip().lower()
WAV = "/tmp/xz_say16.wav"


def ota():
    h = {"Device-Id": MAC, "Client-Id": CFG["client_id"],
         "Content-Type": "application/json",
         "User-Agent": "maixcam2/xiaozhi-2.0.0",
         "Accept-Language": "zh-CN", "Activation-Version": "2"}
    p = {"application": {"version": "2.0.0", "elf_sha256": CFG.get("hmac_key", "x")},
         "board": {"type": "maixcam2", "name": "xiaozhi-maixcam2",
                   "ip": "0.0.0.0", "mac": MAC}}
    return requests.post(CFG["ota_url"], headers=h, json=p, timeout=10, verify=False).json()


def pcm():
    w = wave.open(WAV)
    n = w.getnframes()
    return w.readframes(n - (n % CHUNK))


async def main():
    d = ota()
    url = d["websocket"]["url"]
    tok = d["websocket"].get("token", "") or ""
    hdr = {"Authorization": f"Bearer {tok}", "Protocol-Version": "1",
           "Device-Id": MAC, "Client-Id": CFG["client_id"]}
    ws = await websockets.connect(url, additional_headers=hdr, ping_interval=20,
                                  ping_timeout=20, max_size=10 << 20, compression=None)
    enc = opuslib.Encoder(SR, 1, opuslib.APPLICATION_VOIP)
    st = {"sid": "", "dec": None, "sr": 24000, "hello": False,
          "sent_t0": 0.0, "gaps": [], "last": 0.0, "n": 0, "first": 0.0,
          "stop": False, "done": asyncio.Event()}
    out = bytearray()

    async def rx():
        async for m in ws:
            if isinstance(m, bytes):
                now = time.monotonic()
                if st["last"]:
                    st["gaps"].append(now - st["last"])
                else:
                    st["first"] = now
                st["last"] = now
                st["n"] += 1
                try:
                    out.extend(st["dec"].decode(m, 5760))
                except Exception:
                    pass
                continue
            j = json.loads(m)
            ty = j.get("type")
            if ty == "hello":
                st["sid"] = j.get("session_id", "")
                st["sr"] = int((j.get("audio_params") or {}).get("sample_rate", 24000))
                st["dec"] = opuslib.Decoder(st["sr"], 1)
                st["hello"] = True
            elif ty == "mcp":
                pl = j.get("payload") or {}
                if pl.get("method") == "initialize":
                    rep = {"session_id": st["sid"], "type": "mcp",
                           "payload": {"jsonrpc": "2.0", "id": pl.get("id"),
                                       "result": {"protocolVersion": "2024-11-05",
                                                  "capabilities": {"tools": {}},
                                                  "serverInfo": {"name": "maixcam2",
                                                                 "version": "2.0.0"}}}}
                    await ws.send(json.dumps(rep))
                elif pl.get("method") == "tools/list":
                    rep = {"session_id": st["sid"], "type": "mcp",
                           "payload": {"jsonrpc": "2.0", "id": pl.get("id"),
                                       "result": {"tools": []}}}
                    await ws.send(json.dumps(rep))
            elif ty == "tts":
                s = j.get("state")
                print(f"  [{time.monotonic()-st['first']:6.2f}s] tts {s} "
                      f"{j.get('text','')!r}"[:90], flush=True)
                if s == "sentence_start":
                    st["sent_t0"] = time.monotonic()
                    st["sent_n"] = st["n"]
                    st["sent_gaps"] = []
                elif s == "sentence_end":
                    g = st["sent_gaps"]
                    el = time.monotonic() - st["sent_t0"]
                    nn = st["n"] - st["sent_n"]
                    if g:
                        a = np.array(g) * 1000
                        print(f"    └ 本句 {nn} 帧 / {el:.1f}s = {nn*0.02/el:.2f}× 实时"
                              f" | 帧间隔 平均{a.mean():.0f}ms 中位{np.median(a):.0f}ms "
                              f"p95={np.percentile(a,95):.0f}ms 最大{a.max():.0f}ms "
                              f">60ms {int((a>60).sum())} 次", flush=True)
                elif s == "stop":
                    st["stop"] = True
                    st["done"].set()
                    return
            elif ty == "stt":
                print(f"  [{time.monotonic()-st['first']:6.2f}s] stt {j.get('text','')!r}",
                      flush=True)

    t = asyncio.ensure_future(rx())
    await ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
                              "features": {"mcp": True},
                              "audio_params": {"format": "opus", "sample_rate": SR,
                                               "channels": 1, "frame_duration": FRAME_MS}}))
    for _ in range(200):
        if st["hello"]:
            break
        await asyncio.sleep(0.05)
    await ws.send(json.dumps({"session_id": st["sid"], "type": "listen",
                              "state": "start", "mode": "manual"}))
    data = pcm()
    t0 = time.monotonic()
    k = 0
    for i in range(0, len(data) - CHUNK, CHUNK):
        await ws.send(enc.encode(data[i:i + CHUNK], CHUNK))
        k += 1
        if k % 25 == 0:
            await asyncio.sleep(max(0, k * FRAME_MS / 1000 - (time.monotonic() - t0)))
    await asyncio.sleep(0.3)
    await ws.send(json.dumps({"session_id": st["sid"], "type": "listen",
                              "state": "stop", "mode": "manual"}))
    print(f"上行 {k} 帧完成，等下行…", flush=True)
    try:
        await asyncio.wait_for(st["done"].wait(), timeout=40)
    except asyncio.TimeoutError:
        t.cancel()
        print("!! 超时")
    if st["gaps"]:
        a = np.array(st["gaps"]) * 1000
        print(f"\n总计 {st['n']} 帧 = {st['n']*0.02:.1f}s 音频")
        print(f"帧间隔: 平均{a.mean():.1f}ms 中位{np.median(a):.1f}ms "
              f"p90={np.percentile(a,90):.1f}ms p99={np.percentile(a,99):.1f}ms "
              f"最大{a.max():.1f}ms")
        for th in (25, 40, 60, 100, 200):
            print(f"  >{th}ms: {int((a>th).sum())} 次 ({100*(a>th).mean():.1f}%)")
        print(f"相当于 {st['n']*0.02/(time.monotonic()-st['first']):.2f}× 实时")
    if out:
        w = wave.open("/tmp/dl_check.wav", "wb")
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(bytes(out)); w.close()
        print(f"已存 /tmp/dl_check.wav（{len(out)//2/24000:.1f}s @24k）")
    try:
        await ws.close()
    except Exception:
        pass


asyncio.run(main())
