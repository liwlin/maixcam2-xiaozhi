#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小智AI 设置 App（MaixCAM2）
点击启动器里的"小智AI"图标进入本页面：
  - 启动小智AI      开关（启停后台服务）
  - 弹出聊天窗口    开关（唤醒时是否弹窗）
  - 网络状态 / 激活码 / 控制台 / 唤醒词
后台服务本体在 /root/xiaozhi/xiaozhi_service.py（systemd 自启）。
"""
import json
import os
import subprocess
import sys

from maix import app, display, image, time, touchscreen

CFG_PATH = "/root/xiaozhi/config.json"
STATE_PATH = "/root/xiaozhi/state.json"
WAKE_PRESETS = ["你好小智", "小智", "嗨小智", "你好小夏", "你好小哈"]

image.load_font("zh", "/maixapp/share/font/SourceHanSansCN-Regular.otf", size=20)
image.set_default_font("zh")

C = image.Color.from_rgb

COL_HEADER = C(46, 125, 178)      # 标题栏蓝
COL_BG = C(14, 24, 38)            # 深蓝底
COL_ROW = C(20, 34, 52)           # 行底
COL_TEXT = C(235, 238, 245)
COL_SUB = C(150, 160, 178)
COL_ON = C(41, 182, 246)          # 开关青蓝
COL_OFF = C(70, 78, 92)
COL_OK = C(80, 220, 120)
COL_WARN = C(255, 159, 10)


def load_cfg():
    try:
        return json.load(open(CFG_PATH))
    except Exception:
        return {"enabled": False, "popup_enabled": True, "wake_words": ["你好小智"]}


def save_cfg(cfg):
    try:
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_PATH)
    except Exception as e:
        print("save cfg err:", e)


def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {}


def svc(action):
    try:
        subprocess.run(["systemctl", action, "xiaozhi.service"],
                       timeout=15, capture_output=True)
    except Exception as e:
        print("systemctl err:", e)


class Toggle:
    def __init__(self, x, y, w=56, h=30):
        self.x, self.y, self.w, self.h = x, y, w, h

    def draw(self, img, on):
        body = COL_ON if on else COL_OFF
        img.draw_rect(self.x, self.y, self.w, self.h, body, -1)
        r = self.h
        if on:
            kx = self.x + self.w - r
        else:
            kx = self.x
        img.draw_circle(kx + r // 2, self.y + self.h // 2, r // 2 - 2, COL_TEXT, -1)

    def hit(self, x, y):
        return self.x - 6 <= x <= self.x + self.w + 6 and \
            self.y - 6 <= y <= self.y + self.h + 6


def main():
    disp = display.Display()
    W, H = disp.width(), disp.height()
    ts = touchscreen.TouchScreen()

    HEADER_H = 56
    scale = 1.0 if W <= 480 else 1.3
    row_h = int(64 * (H / 640.0)) if H > W else 64
    rows_top = HEADER_H + 18

    # 行定义（y 在 draw 时计算）
    toggles = {}

    def draw_ui(cfg, st):
        img = image.Image(W, H)
        img.draw_rect(0, 0, W, H, COL_BG, -1)
        # header
        img.draw_rect(0, 0, W, HEADER_H, COL_HEADER, -1)
        img.draw_string(16, HEADER_H // 2 - 16, "‹", COL_TEXT, 2.2)
        tw = image.string_size("小智AI", 1.4).width()
        img.draw_string((W - tw) // 2, HEADER_H // 2 - 15, "小智AI",
                        COL_TEXT, 1.4)

        y = rows_top
        pad = 14
        item_h = row_h

        def row_bg():
            img.draw_rect(pad, y - 8, W - pad * 2, item_h, COL_ROW, -1)

        # 1. 启动小智AI
        row_bg()
        img.draw_string(pad + 14, y + item_h // 2 - 13, "启动小智AI", COL_TEXT, 1.1)
        tg = Toggle(W - pad - 70, y + item_h // 2 - 15)
        tg.draw(img, bool(cfg.get("enabled")))
        toggles["enabled"] = tg
        y += item_h + 10

        # 2. 弹出聊天窗口
        row_bg()
        img.draw_string(pad + 14, y + item_h // 2 - 13, "弹出聊天窗口", COL_TEXT, 1.1)
        tg = Toggle(W - pad - 70, y + item_h // 2 - 15)
        tg.draw(img, bool(cfg.get("popup_enabled")))
        toggles["popup"] = tg
        y += item_h + 10

        # 3+ 状态行
        def info_row(label, value, vcol=COL_SUB):
            nonlocal y
            row_bg()
            img.draw_string(pad + 14, y + item_h // 2 - 13, label, COL_TEXT, 1.1)
            vw = image.string_size(value, 1.0).width()
            img.draw_string(W - pad - 14 - vw, y + item_h // 2 - 12, value,
                            vcol, 1.0)
            y += item_h + 10

        net = st.get("network", "未知")
        info_row("网络状态", net,
                 COL_OK if net == "可用" else (COL_WARN if net != "离线" else C(240, 90, 90)))
        act = "已激活" if st.get("activated") or cfg.get("activated") else \
            (st.get("activation_code") or "未激活")
        info_row("激活码", act,
                 COL_OK if act == "已激活" else COL_WARN)
        info_row("控制台", "xiaozhi.me", COL_SUB)
        ww = (cfg.get("wake_words") or ["你好小智"])[0]
        info_row("唤醒词", ww, COL_ON)

        # 底部提示
        img.draw_string(pad + 14, H - 34, "点 ‹ 返回 · 服务状态:" +
                        str(st.get("state", "unknown")), COL_SUB, 0.85)
        disp.show(img)

    cfg = load_cfg()
    st = load_state()
    draw_ui(cfg, st)

    last_refresh = time.ticks_ms()
    while not app.need_exit():
        x, y, pressed = ts.read()
        if pressed:
            # back
            if x < 60 and y < HEADER_H + 6:
                time.sleep_ms(150)
                return
            if toggles["enabled"].hit(x, y):
                cfg["enabled"] = not cfg.get("enabled")
                save_cfg(cfg)
                svc("start" if cfg["enabled"] else "stop")
                time.sleep_ms(400)
            elif toggles["popup"].hit(x, y):
                cfg["popup_enabled"] = not cfg.get("popup_enabled", True)
                save_cfg(cfg)
                time.sleep_ms(200)
            else:
                # 唤醒词行点击 → 切换预设
                words = cfg.get("wake_words") or ["你好小智"]
                try:
                    idx = WAKE_PRESETS.index(words[0])
                    words[0] = WAKE_PRESETS[(idx + 1) % len(WAKE_PRESETS)]
                except ValueError:
                    words[0] = WAKE_PRESETS[0]
                cfg["wake_words"] = words
                save_cfg(cfg)
                time.sleep_ms(250)
            st = load_state()
            draw_ui(cfg, st)
            while ts.read()[2]:
                time.sleep_ms(30)
        # 周期刷新状态（服务启动后状态会变）
        if time.ticks_ms() - last_refresh > 1500:
            last_refresh = time.ticks_ms()
            st = load_state()
            draw_ui(cfg, st)
        time.sleep_ms(30)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("xiaozhi app error:", e)
        raise
