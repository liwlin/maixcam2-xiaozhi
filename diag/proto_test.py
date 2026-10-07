#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小智协议端到端诊断：OTA -> WS -> hello -> listen(auto) -> opus 上行 -> 打印全部下行"""
import asyncio, json, os, sys, time, wave, hashlib, hmac as hmac_mod
import requests, websockets, opuslib
import numpy as np

requests.packages.urllib3.disable_warnings()

CFG = json.load(open("/root/xiaozhi/config.json"))
OTA_URL = CFG["ota_url"]
CLIENT_ID = CFG["client_id"]
SR = 16000
FRAME_MS = 20
CHUNK = SR * FRAME_MS // 1000          # 320 samples

WAV = sys.argv[1] if len(sys.argv) > 1 else "/maixapp/share/audio/demo.wav"
MODE = sys.argv[2] if len(sys.argv) > 2 else "auto"


def mac():
    return open("/sys/class/net/wlan0/address").read().strip().lower()


def ota():
    h = {"Device-Id": mac(), "Client-Id": CLIENT_ID,
         "Content-Type": "application/json",
         "User-Agent": "maixcam2/xiaozhi-1.0.0",
         "Accept-Language": "zh-CN", "Activation-Version": "2"}
    p = {"application": {"version": "1.0.0", "elf_sha256": CFG.get("hmac_key", "x")},
         "board": {"type": "maixcam2", "name": "xiaozhi-maixcam2",
                   "ip": "0.0.0.0", "mac": mac()}}
    r = requests.post(OTA_URL, headers=h, json=p, timeout=15, verify=False)
    print("OTA HTTP", r.status_code)
    d = r.json()
    print("OTA resp:", json.dumps(d, ensure_ascii=False)[:900])
    return d


def load_wav():
    w = wave.open(WAV)
    assert w.getframerate() == SR, w.getframerate()
    n = w.getnframes()
    data = w.readframes(n - (n % CHUNK))
    a = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    print(f"WAV {WAV}: {n} frames, {n/SR:.2f}s, rms={np.sqrt(np.mean(a*a)):.1f}, peak={np.abs(a).max():.0f}")
    return data


async def run():
    d = ota()
    ws_url = (d.get("websocket") or {}).get("url")
    token = (d.get("websocket") or {}).get("token", "") or ""
    act = d.get("activation")
    print("ws_url:", ws_url, "| token:", (token[:24] + "...") if token else "(none)",
          "| activation:", act)
    if not ws_url:
        print("!! 无 websocket url"); return
    headers = {"Authorization": f"Bearer {token}", "Protocol-Version": "1",
               "Device-Id": mac(), "Client-Id": CLIENT_ID}
    ws = await websockets.connect(ws_url, additional_headers=headers,
                                  ping_interval=20, ping_timeout=20,
                                  max_size=10 * 1024 * 1024, compression=None)
    print("WS connected")
    hello = {"type": "hello", "version": 1, "transport": "websocket",
             "features": {"mcp": True},
             "audio_params": {"format": "opus", "sample_rate": SR,
                              "channels": 1, "frame_duration": FRAME_MS}}
    print(">> hello", json.dumps(hello, ensure_ascii=False))
    await ws.send(json.dumps(hello))

    pcm = load_wav()
    enc = opuslib.Encoder(SR, 1, opuslib.APPLICATION_AUDIO)
    dec = None
    dec_sr = 24000
    stop = False
    sent = 0

    async def rx():
        nonlocal dec, dec_sr, stop
        async for m in ws:
            t = time.strftime("%H:%M:%S")
            if isinstance(m, bytes):
                n = 0
                if dec:
                    try:
                        p = dec.decode(m, 5760)
                        n = len(p) // 2
                    except Exception as e:
                        print(f"[{t}] << BIN {len(m)}B decode-err {e}")
                        continue
                print(f"[{t}] << BIN {len(m)}B -> {n} samples @{dec_sr}")
                continue
            print(f"[{t}] << {m[:400]}")
            try:
                j = json.loads(m)
            except Exception:
                continue
            ty = j.get("type")
            if ty == "hello":
                dec_sr = int((j.get("audio_params") or {}).get("sample_rate", 24000))
                dec = opuslib.Decoder(dec_sr, 1)
                sid = j.get("session_id", "")
                print(f"   server hello sr={dec_sr} session={sid}")
                await ws.send(json.dumps({"session_id": sid, "type": "listen",
                                          "state": "start", "mode": MODE}))
                print(f">> listen start mode={MODE}")
            elif ty == "goodbye":
                stop = True
                return
            elif ty == "tts" and j.get("state") == "stop":
                stop = True

    rxt = asyncio.ensure_future(rx())
    # 先等 listen start 发出
    for _ in range(200):
        if dec is not None:
            break
        await asyncio.sleep(0.05)
    print("--- 开始上行音频 ---")
    t0 = time.time()
    for i in range(0, len(pcm) - CHUNK, CHUNK):
        if stop or rxt.done():
            break
        try:
            await ws.send(enc.encode(pcm[i:i + CHUNK], CHUNK))
            sent += 1
        except Exception as e:
            print("send err", e)
            break
        if sent % 25 == 0:
            el = time.time() - t0
            await asyncio.sleep(max(0, sent * FRAME_MS / 1000 - el))
    print(f"--- 上行结束 {sent} 帧 ({sent*FRAME_MS/1000:.2f}s) ---")
    # 静音尾随 1.5s
    sil = b"\x00" * (CHUNK * 2)
    for _ in range(75):
        if stop or rxt.done():
            break
        try:
            await ws.send(enc.encode(sil, CHUNK))
        except Exception:
            break
        await asyncio.sleep(0.02)
    print("--- 尾随静音结束，等待下行 12s ---")
    try:
        await asyncio.wait_for(rxt, timeout=12)
    except asyncio.TimeoutError:
        rxt.cancel()
    print("=== 结束 ===")


asyncio.run(run())
