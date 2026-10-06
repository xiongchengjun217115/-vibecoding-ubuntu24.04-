#!/usr/bin/env python3
"""
phone-pushclip — 把电脑剪贴板推到手机剪贴板

为什么需要单独做：scrcpy 的「电脑→手机」剪贴板同步只绑定在「在窗口里按 Ctrl+V」
这个动作上（我们跑的是无窗口模式，根本没这个入口，所以这个方向一直是断的）。

做法：
  1. 监听 X11 剪贴板变化（GTK3 owner-change）
  2. 变化时把 Ctrl+V 发给隐藏的 scrcpy 窗口（xdotool）
  3. scrcpy 收到后先 SET_CLIPBOARD，再注入一次 PASTE

⚠️ 关于那个 PASTE 副作用 —— 已经彻底解决了：
   phone-clip.service 里的 scrcpy 带了 --display-id=99（一个不存在的显示器）。
   scrcpy 服务端源码里这两个动作是分开的：

       Device.setClipboardText(text);                    // 我们要的
       if (paste && ...) pressReleaseKeycode(KEYCODE_PASTE, getActionDisplayId());

   而 getActionDisplayId() 返回的就是 --display-id 指定的值。指向不存在的 99 后，
   系统会直接丢弃这个按键事件（实测 InputDispatcher 日志）：
       Dropping KEY event because there is no focused window ... in display 99
   剪贴板照常设置成功，手机界面完全不受影响。

模式（一般不用改）：
     pushclip_mode = always  无条件推送（默认，因为已无副作用）
                   = safe    额外要求手机息屏/锁屏/无输入框才推
                   = off     关闭
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

HOME = Path.home()
ADB = str(HOME / ".local/bin/adb")
CONFIG = HOME / ".local/opt/phonesync/config.json"


def cfg(key, default=None):
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")).get(key, default)
    except Exception:
        return default


def run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except Exception as e:
        return 1, "", str(e)


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} [推送] {msg}", flush=True)


def phone_connected():
    rc, out, _ = run([ADB, "devices"])
    for ln in out.splitlines()[1:]:
        parts = ln.split()
        if len(parts) >= 2 and parts[1] == "device":
            return True
    return False


def safe_to_push():
    """判断此刻推送是否安全（PASTE 会不会粘进手机输入框）"""
    rc, out, _ = run([ADB, "shell", "dumpsys power | grep mWakefulness="])
    if "Awake" not in out:
        return True, "手机息屏"
    rc, out, _ = run([ADB, "shell", "dumpsys window | grep mDreamingLockscreen"])
    if "mDreamingLockscreen=true" in out:
        return True, "手机锁屏"
    rc, out, _ = run([ADB, "shell", "dumpsys input_method | grep mInputShown"])
    if "mInputShown=true" not in out:
        return True, "手机无聚焦输入框"
    return False, "手机正在输入（有聚焦输入框），跳过以免粘错地方"


def scrcpy_window():
    rc, out, _ = run(["xdotool", "search", "--class", "scrcpy"])
    ids = [x.strip() for x in out.splitlines() if x.strip()]
    return ids[-1] if ids else None


def hide_scrcpy_window():
    """
    scrcpy 的窗口纯粹是为了接收按键才存在的，把它藏起来免得碍眼。
    实测 unmap 之后 XSendEvent 送进去的按键仍然有效（scrcpy 照常 SET_CLIPBOARD）。
    """
    wid = scrcpy_window()
    if not wid:
        return
    rc, out, _ = run(["xwininfo", "-id", wid])
    if "IsViewable" in out:
        run(["xdotool", "windowunmap", wid])
        log("已隐藏 scrcpy 窗口（它只为接收 Ctrl+V 而存在）")


def push(text):
    wid = scrcpy_window()
    if not wid:
        return False, "找不到 scrcpy 窗口（服务是否在跑？）"
    run(["xdotool", "windowactivate", wid])
    time.sleep(0.25)
    run(["xdotool", "key", "--window", wid, "ctrl+v"])
    return True, "ok"


class Pusher:
    def __init__(self, mode):
        self.mode = mode
        self.last = None
        self.cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.cb.connect("owner-change", self._on_change)

    def _on_change(self, cb, event):
        cb.request_text(self._on_text)

    def _on_text(self, cb, text, _data=None):
        if not text:
            return
        if text == self.last:
            return
        self.last = text
        preview = " ".join(text.split())[:40]
        if not phone_connected():
            log(f"跳过（手机未连接）: {preview!r}")
            return
        if self.mode == "safe":
            ok, why = safe_to_push()
            if not ok:
                log(f"跳过（{why}）: {preview!r}")
                return
            reason = why
        else:
            reason = "always 模式"
        ok, msg = push(text)
        if ok:
            log(f"✓ 已推送到手机剪贴板（{reason}）: {preview!r}")
        else:
            log(f"✗ 推送失败: {msg}")


def main():
    mode = cfg("pushclip_mode", "always")
    if mode == "off":
        log("pushclip_mode=off，不启动")
        return 0
    Gtk.init([])
    Pusher(mode)
    hide_scrcpy_window()
    GLib.timeout_add_seconds(8, lambda: (hide_scrcpy_window(), True)[1])
    log(f"已启动，mode={mode}"
        + ("（无条件推送；粘贴注入已被 --display-id=99 丢弃，无副作用）" if mode == "always"
           else "（额外要求手机息屏/锁屏/无输入框）"))
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
