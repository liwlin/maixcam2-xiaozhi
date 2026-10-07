#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦克风增益标定：扬声器播放固定语音，用 maix.audio.Recorder 采集，量化 RMS"""
import os, subprocess, sys, threading, time, wave
import numpy as np
import pyaudio
from maix import audio as maix_audio

WAV = "/tmp/xz_say16.wav"
SR = 16000


def amixer(ctl, val):
    subprocess.run(["amixer", "sset", ctl, f"{val}%"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def load(path):
    w = wave.open(path)
    d = w.readframes(w.getnframes())
    return np.frombuffer(d, dtype=np.int16)


def play(pcm, out_sr=48000):
    pa = pyaudio.PyAudio()
    up = np.repeat(pcm, 2).astype(np.int16)      # 16k -> 48k 线性
    s = pa.open(format=pyaudio.paInt16, channels=1, rate=out_sr,
                output=True, frames_per_buffer=out_sr // 10)
    s.write(up.tobytes())
    s.stop_stream(); s.close(); pa.terminate()


def measure(recvol, tx_l, tx_r, alc):
    amixer("TX LEFT ANA GAIN", tx_l)
    amixer("TX RIGHT ANA GAIN", tx_r)
    amixer("ALC ENABLE", alc)
    rec = maix_audio.Recorder(sample_rate=SR,
                              format=maix_audio.Format.FMT_S16_LE, channel=1)
    rec.volume(recvol)
    pcm = load(WAV)
    th = threading.Thread(target=play, args=(pcm,), daemon=True)
    th.start()
    chunks = []
    t0 = time.time()
    while time.time() - t0 < 8:
        d = rec.record(20)
        if d:
            chunks.append(bytes(d))
    th.join()
    rec.reset(False)
    raw = b"".join(chunks)
    n = len(raw) // 2
    a = np.frombuffer(raw[:n * 2], dtype=np.int16).astype(np.float32)
    if a.size < 320:
        return None
    f = 320
    frames = a[: (a.size // f) * f].reshape(-1, f)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    # 取最响的 3 秒作为「说话段」
    k = min(len(rms), 150)
    loud = np.sort(rms)[-k:]
    return dict(peak=float(rms.max()), p95=float(np.percentile(rms, 95)),
                speech=float(loud.mean()),
                noise=float(np.percentile(rms, 20)), n=len(rms))


def main():
    print("recvol txL txR alc |  peak     p95   speech   noise")
    for recvol in (40, 70, 100):
        for (tl, tr, alc) in ((100, 16, "off"), (100, 100, "off"),
                              (100, 100, "on")):
            r = measure(recvol, tl, tr, alc)
            if r:
                print(f"  {recvol:3d}  {tl:3d} {tr:3d} {alc:3s} | "
                      f"{r['peak']:7.0f} {r['p95']:7.0f} {r['speech']:8.0f} "
                      f"{r['noise']:7.0f}")
            else:
                print(f"  {recvol:3d}  {tl:3d} {tr:3d} {alc:3s} | 采集失败")
            time.sleep(1)


if __name__ == "__main__":
    main()
