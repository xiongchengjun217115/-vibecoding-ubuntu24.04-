#!/usr/bin/env python3
"""
phone-gui — 手机连接监控客户端（GTK4 + libadwaita）

功能：
  · 连接状态：是否连接、设备型号、系统版本、连接方式（USB/无线）、电量
  · 总开关  ：一键启停后台同步服务
  · 剪贴板  ：实时显示当前剪贴板内容；手机复制的文字经 scrcpy 同步后会立即出现
  · 消息    ：实时滚动显示短信与通知（读 events.jsonl）

运行： phone-gui       （或点击应用菜单里的「手机连接」）
"""

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

APP_ID = "io.github.phoneconnect.PhoneConnect"

HOME = Path.home()
ADB = str(HOME / ".local/bin/adb")
DIR = HOME / ".local/opt/phonesync"
EVENTS = DIR / "events.jsonl"
WIRELESS_ENV = DIR / "wireless.env"

SERVICES = ("phone-clip.service", "phone-sync.service")
POLL_FAST = 2.0     # 连接状态轮询
POLL_BATT = 20.0    # 电量轮询
MAX_MSG = 120       # 消息列表上限
MAX_CLIP_HIST = 12  # 剪贴板历史上限

STATE_ICON = {
    "device": ("✓", "success"),
    "unauthorized": ("⚠", "warning"),
    "offline": ("✕", "error"),
    "no permissions": ("✕", "error"),
}


# ────────────────────────────── 工具 ──────────────────────────────

def run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except Exception:
        return 1, "", ""


def fmt_time(ts):
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "--:--:--"


def short_pkg(pkg):
    """把包名变成好读的应用名"""
    known = {
        "com.android.mms": "短信", "com.oplus.mms": "短信", "com.android.messaging": "短信",
        "com.tencent.mm": "微信", "com.tencent.mobileqq": "QQ", "tv.danmaku.bili": "哔哩哔哩",
        "com.zhihu.android": "知乎", "com.xingin.xhs": "小红书", "com.taobao.taobao": "淘宝",
        "com.shizhuang.duapp": "得物", "com.cainiao.wireless": "菜鸟", "com.chinamworld.main": "建行",
        "com.mcdonalds.gma.cn": "麦当劳", "com.eg.android.AlipayGphone": "支付宝",
        "com.sina.weibo": "微博", "com.netease.cloudmusic": "网易云音乐",
    }
    if pkg in known:
        return known[pkg]
    parts = pkg.split(".")
    return parts[-1] if parts else pkg


# ────────────────────────── 后台监控线程 ──────────────────────────

class Monitor(threading.Thread):
    """后台轮询 adb / systemd 状态，通过回调推给 UI 线程"""

    def __init__(self, push):
        super().__init__(daemon=True)
        self.push = push
        self.stop_flag = threading.Event()
        self.props_cache = {}
        self.battery = None
        self.batt_ts = 0.0
        self.snapshot = {}

    # ---- adb ----
    def _devices(self):
        rc, out, _ = run([ADB, "devices", "-l"], timeout=12)
        devs = []
        for ln in out.splitlines()[1:]:
            parts = ln.split()
            if len(parts) >= 2:
                devs.append({"serial": parts[0], "state": parts[1]})
        return devs

    @staticmethod
    def _pick(devs):
        ok = [d for d in devs if d["state"] == "device"]
        for d in ok:
            if ":" in d["serial"]:
                return d
        return ok[0] if ok else None

    def _props(self, serial):
        if serial in self.props_cache:
            return self.props_cache[serial]
        rc, out, _ = run([ADB, "-s", serial, "shell",
                          "getprop ro.product.brand; getprop ro.product.model; "
                          "getprop ro.build.version.release"], timeout=15)
        lines = [x.strip() for x in out.splitlines() if x.strip()]
        info = {
            "brand": lines[0] if len(lines) > 0 else "?",
            "model": lines[1] if len(lines) > 1 else "?",
            "android": lines[2] if len(lines) > 2 else "?",
        }
        self.props_cache[serial] = info
        return info

    def _battery(self, serial):
        now = time.time()
        if self.battery is not None and now - self.batt_ts < POLL_BATT:
            return self.battery
        rc, out, _ = run([ADB, "-s", serial, "shell", "dumpsys battery"], timeout=15)
        level, status = None, None
        for ln in out.splitlines():
            ln = ln.strip()
            if ln.startswith("level:"):
                level = ln.split(":", 1)[1].strip()
            elif ln.startswith("status:"):
                status = ln.split(":", 1)[1].strip()
        self.battery = (level, status)
        self.batt_ts = now
        return self.battery

    # ---- systemd ----
    @staticmethod
    def _services():
        rc, out, _ = run(["systemctl", "--user", "is-active", *SERVICES], timeout=10)
        states = [x.strip() for x in out.splitlines() if x.strip()]
        while len(states) < len(SERVICES):
            states.append("unknown")
        return dict(zip(SERVICES, states))

    def _wireless_addr(self):
        """从 wireless.env 读 (ip, port)；端口默认 5555（无线调试的端口是随机的）"""
        ip = port = None
        try:
            for line in WIRELESS_ENV.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("WIRELESS_IP="):
                    ip = line.split("=", 1)[1].strip()
                elif line.startswith("WIRELESS_PORT="):
                    port = line.split("=", 1)[1].strip()
        except Exception:
            pass
        return ip, (port or "5555")

    # ---- 网络层探测：区分「手机不在」和「USB 调试没开」----
    def _probe_phone(self, ip, port):
        """返回 (网络可达, adbd端口开放)，结果缓存 8 秒"""
        now = time.time()
        if getattr(self, "_probe_cache", None) and now - self._probe_ts < 8:
            return self._probe_cache
        online = run(["ping", "-c", "1", "-W", "2", ip], timeout=6)[0] == 0
        open_ = False
        if online:
            try:
                with socket.create_connection((ip, int(port)), timeout=2.5):
                    open_ = True
            except Exception:
                open_ = False
        self._probe_cache = (online, open_)
        self._probe_ts = now
        return self._probe_cache

    def run(self):
        while not self.stop_flag.is_set():
            try:
                snap = self._collect()
                self.snapshot = snap
                GLib.idle_add(self.push, snap)
            except Exception as e:
                GLib.idle_add(self.push, {"error": str(e)})
            self.stop_flag.wait(POLL_FAST)

    def _collect(self):
        devs = self._devices()
        cur = self._pick(devs)
        svc = self._services()
        wip, wport = self._wireless_addr()
        snap = {
            "devices": devs,
            "connected": cur is not None,
            "serial": cur["serial"] if cur else None,
            "wireless": bool(cur and ":" in cur["serial"]),
            "services": svc,
            "services_on": all(v == "active" for v in svc.values()),
            "services_partial": any(v == "active" for v in svc.values()),
            "wireless_ip": wip,
            "wireless_port": wport,
            "wireless_addr": f"{wip}:{wport}" if wip else None,
            "model": None, "android": None, "brand": None, "battery": None, "charging": None,
            "phone_online": None, "adbd_down": False,
        }
        # 未授权设备优先提示
        unauth = [d for d in devs if d["state"] == "unauthorized"]
        snap["unauthorized"] = bool(unauth)
        if not cur and wip:
            online, port_open = self._probe_phone(wip, wport)
            snap["phone_online"] = online
            snap["adbd_down"] = online and not port_open
        if cur:
            p = self._props(cur["serial"])
            snap.update(model=p["model"], android=p["android"], brand=p["brand"])
            level, status = self._battery(cur["serial"])
            snap["battery"] = level
            snap["charging"] = status in ("2", "5")
        return snap


# ────────────────────────────── 界面 ──────────────────────────────

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=0)
        self.clip_history = []
        self.msgs = []
        self.events_pos = 0
        self.switch_guard = False
        self._built = False
        self._scanning = False

    # ---------- 构建界面 ----------
    def _build_ui(self):
        self.win = Adw.ApplicationWindow(application=self)
        self.win.set_title("手机连接")
        self.win.set_default_size(470, 900)

        self.title = Adw.WindowTitle(title="手机连接", subtitle="正在检测…")
        header = Adw.HeaderBar()
        header.set_title_widget(self.title)

        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="同步服务开关")
        self.switch.connect("state-set", self.on_switch)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.append(Gtk.Label(label="同步"))
        box.append(self.switch)
        header.pack_end(box)

        page = Adw.PreferencesPage()

        # ── 连接状态 ──
        g_conn = Adw.PreferencesGroup(title="连接状态")
        self.row_dev = Adw.ActionRow(title="设备", subtitle="—")
        self.row_os = Adw.ActionRow(title="系统", subtitle="—")
        self.row_link = Adw.ActionRow(title="连接方式", subtitle="—")
        self.row_batt = Adw.ActionRow(title="电量", subtitle="—")
        self.row_svc = Adw.ActionRow(title="后台服务", subtitle="—")

        # 无线端口 + 手动「重新扫描」（手机重启后端口会变，用它找回）
        self.row_port = Adw.ActionRow(title="无线端口", subtitle="—")
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.btn_scan = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                                   tooltip_text="扫描手机 IP，自动找回无线调试端口")
        self.btn_scan.add_css_class("flat")
        self.btn_scan.connect("clicked", self.on_scan)
        self.row_port.add_suffix(self.spinner)
        self.row_port.add_suffix(self.btn_scan)

        for r in (self.row_dev, self.row_os, self.row_link, self.row_batt,
                  self.row_svc, self.row_port):
            g_conn.add(r)
        page.add(g_conn)

        # ── 剪贴板 ──
        g_clip = Adw.PreferencesGroup(
            title="剪贴板",
            description="手机复制的文字会经 scrcpy 自动同步到这里，实时刷新",
        )
        self.clip_label = Gtk.Label(
            label="（空）", wrap=True, selectable=True, xalign=0,
            wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=48,
            css_classes=["monospace"],
        )
        clip_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=4,
                            margin_bottom=8, margin_start=4, margin_end=4)
        clip_wrap.append(self.clip_label)
        g_clip.add(clip_wrap)

        self.clip_hist_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                         css_classes=["boxed-list"])
        g_clip.add(self.clip_hist_box)
        page.add(g_clip)

        # ── 消息 ──
        g_msg = Adw.PreferencesGroup(title="消息", description="短信与通知实时滚动（新→旧）")
        self.msg_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                   css_classes=["boxed-list"])
        # 先放进 ScrolledWindow，再整体挂到分组上（否则 GTK 会断言失败）
        self.msg_scroll = Gtk.ScrolledWindow(min_content_height=220, max_content_height=380,
                                             propagate_natural_height=True,
                                             hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.msg_scroll.set_child(self.msg_box)
        g_msg.add(self.msg_scroll)
        page.add(g_msg)

        self.toast_overlay = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.set_content(page)
        self.toast_overlay.set_child(tv)
        self.win.set_content(self.toast_overlay)

    # ---------- 剪贴板 ----------
    def _hook_clipboard(self):
        display = Gdk.Display.get_default()
        if display is None:
            return
        self.clipboard = display.get_clipboard()
        self.clipboard.connect("changed", self.on_clipboard_changed)
        self.on_clipboard_changed(self.clipboard)

    def on_clipboard_changed(self, cb):
        cb.read_text_async(None, self._clip_text_done)

    def _clip_text_done(self, cb, result):
        try:
            text = cb.read_text_finish(result)
        except GLib.Error:
            text = None
        if text:
            self._set_clip(text)
            return
        # 不是文本，试试图片
        cb.read_texture_async(None, self._clip_image_done)

    def _clip_image_done(self, cb, result):
        try:
            tex = cb.read_texture_finish(result)
        except GLib.Error:
            tex = None
        if tex:
            self.clip_label.set_label(f"🖼  图片  {tex.get_width()} × {tex.get_height()}")
            self._push_clip(f"🖼 图片 {tex.get_width()}×{tex.get_height()}")
        else:
            self.clip_label.set_label("（空 或 不支持的类型）")

    def _set_clip(self, text):
        shown = text if len(text) <= 600 else text[:600] + " …"
        self.clip_label.set_label(shown)
        self._push_clip(text)

    def _push_clip(self, text):
        one = " ".join(text.split())
        if self.clip_history and self.clip_history[0][1] == one:
            return
        self.clip_history.insert(0, (time.time(), one))
        del self.clip_history[MAX_CLIP_HIST:]
        self._render_clip_hist()

    def _render_clip_hist(self):
        while (child := self.clip_hist_box.get_first_child()) is not None:
            self.clip_hist_box.remove(child)
        for ts, text in self.clip_history[1:]:
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(text[:70] + ("…" if len(text) > 70 else "")),
                subtitle=fmt_time(ts),
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("edit-copy-symbolic"))
            self.clip_hist_box.append(row)

    # ---------- 事件流 ----------
    def _read_events(self):
        try:
            if not EVENTS.exists():
                return
            size = EVENTS.stat().st_size
            if size < self.events_pos:      # 文件被轮转
                self.events_pos = 0
            with EVENTS.open("r", encoding="utf-8") as f:
                f.seek(self.events_pos)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._add_msg(json.loads(line))
                    except Exception:
                        pass
                self.events_pos = f.tell()
        except Exception:
            pass

    def _add_msg(self, ev):
        t = ev.get("type")
        if t == "sms":
            icon, title, body = "mail-unread-symbolic", f"短信 · {ev.get('sender', '?')}", ev.get("body", "")
        elif t == "notif":
            icon = "dialog-information-symbolic"
            title = f"{short_pkg(ev.get('pkg', '?'))} · {ev.get('title', '')}".strip(" ·")
            body = ev.get("text", "")
        elif t == "photo":
            icon = "camera-photo-symbolic"
            title = "新照片" + ("（已进剪贴板）" if ev.get("copied") else "（已保存）")
            body = ev.get("name", "")
        else:
            return

        self.msgs.insert(0, {"ts": ev.get("ts", time.time()), "icon": icon,
                             "title": title, "body": body})
        del self.msgs[MAX_MSG:]

        row = Adw.ActionRow(title=GLib.markup_escape_text(title or "(无标题)"),
                            subtitle=GLib.markup_escape_text((body or "")[:160]))
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        lbl = Gtk.Label(label=fmt_time(ev.get("ts", time.time())), css_classes=["dim-label"])
        row.add_suffix(lbl)
        self.msg_box.prepend(row)
        while self.msg_box.get_first_child() and self._count_rows() > MAX_MSG:
            last = self.msg_box.get_last_child()
            if last is None:
                break
            self.msg_box.remove(last)

    def _count_rows(self):
        n, c = 0, self.msg_box.get_first_child()
        while c:
            n += 1
            c = c.get_next_sibling()
        return n

    # ---------- 开关 ----------
    def on_switch(self, sw, state):
        if self.switch_guard:
            return False
        action = "start" if state else "stop"
        # 立刻给反馈，实际状态由轮询刷新
        self.row_svc.set_subtitle("正在启动…" if state else "正在停止…")
        threading.Thread(target=self._svc_cmd, args=(action,), daemon=True).start()
        return False

    def _svc_cmd(self, action):
        run(["systemctl", "--user", action, *SERVICES], timeout=40)
        GLib.idle_add(self._after_svc, action)

    def _after_svc(self, action):
        self.toast_overlay.add_toast(
            Adw.Toast(title="同步服务已启动" if action == "start" else "同步服务已停止"))
        return False

    # ---------- 手动重新扫描端口 ----------
    def on_scan(self, *_):
        if self._scanning:
            return
        self._scanning = True
        self.btn_scan.set_sensitive(False)
        self.spinner.set_visible(True)
        self.spinner.start()
        self.row_port.set_subtitle("正在扫描，最多约 1 分钟…")
        self.toast_overlay.add_toast(Adw.Toast(title="开始扫描手机端口…"))
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        helper = HOME / ".local/bin/phone-autoconnect"
        rc, out, err = run([str(helper), "--force"], timeout=320)
        GLib.idle_add(self._scan_done, rc, out, err)

    def _scan_done(self, rc, out, err):
        self._scanning = False
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.btn_scan.set_sensitive(True)
        text = f"{out}\n{err}"
        if "端口已自动更新" in text:
            detail = [l for l in text.splitlines() if "端口已自动更新" in l]
            self.toast_overlay.add_toast(Adw.Toast(title=detail[-1] if detail else "端口已更新"))
            # 清掉探测器缓存，让下一拍立刻重新判定连接状态
            if getattr(self, "mon", None):
                self.mon._probe_cache = None
                self.mon._probe_ts = 0
        else:
            _, devs, _ = run([ADB, "devices"], timeout=15)
            ok = any(len(l.split()) >= 2 and l.split()[1] == "device"
                     for l in devs.splitlines()[1:])
            self.toast_overlay.add_toast(Adw.Toast(
                title="✓ 已连接" if ok else "✗ 没找到端口 —— 确认手机「无线调试」已打开"))
        return False

    # ---------- 状态刷新 ----------
    def _apply(self, snap):
        if "error" in snap:
            self.title.set_subtitle("监控异常")
            return
        # 开关
        want = snap["services_on"]
        if self.switch.get_active() != want:
            self.switch_guard = True
            self.switch.set_active(want)
            self.switch_guard = False

        if snap["connected"]:
            link = "无线" if snap["wireless"] else "USB"
            addr = snap["serial"]
            self.title.set_subtitle(f"● 已连接 · {link}")
            self.row_dev.set_subtitle(f"{snap['brand']} {snap['model']}")
            self.row_os.set_subtitle(f"Android {snap['android']}")
            self.row_link.set_subtitle(f"{link}  ·  {addr}")
            if snap["battery"]:
                b = f"{snap['battery']}%"
                if snap["charging"]:
                    b += "  ⚡充电中"
                self.row_batt.set_subtitle(b)
            else:
                self.row_batt.set_subtitle("—")
        elif snap.get("unauthorized"):
            self.title.set_subtitle("⚠ 未授权")
            self.row_dev.set_subtitle("手机已连接，但未授权调试")
            self.row_os.set_subtitle("请在手机上点「允许 USB 调试」")
            self.row_link.set_subtitle("—")
            self.row_batt.set_subtitle("—")
        elif snap.get("adbd_down"):
            addr = snap.get("wireless_addr")
            self.title.set_subtitle("⚠ 手机在线，调试未开")
            self.row_dev.set_subtitle("手机在网络上，但调试服务没在跑")
            self.row_os.set_subtitle("请打开「开发者选项 → USB 调试」或「无线调试」")
            self.row_link.set_subtitle(f"{addr} 可达，但该端口拒绝连接")
            self.row_batt.set_subtitle("—")
        else:
            self.title.set_subtitle("○ 未连接")
            addr = snap.get("wireless_addr")
            if snap.get("phone_online") is False:
                self.row_dev.set_subtitle(f"手机 {snap.get('wireless_ip')} 不在网络上")
            else:
                self.row_dev.set_subtitle("未检测到手机")
            self.row_os.set_subtitle("—")
            self.row_link.set_subtitle(f"无线已配置 {addr}，等待连接…" if addr else "尚未配置无线")
            self.row_batt.set_subtitle("—")

        svc = snap["services"]
        on = [k.split(".")[0] for k, v in svc.items() if v == "active"]
        tail = f"（{'、'.join(on)}）" if on else ""
        if snap["connected"]:
            self.row_svc.set_subtitle(f"{len(on)}/{len(SERVICES)} 运行中{tail}")
        else:
            # 没连上时服务是 active 但空转，明确说清楚，避免误判
            self.row_svc.set_subtitle(f"{len(on)}/{len(SERVICES)} 已就绪 · 等待手机接入{tail}")

        # 无线端口行（扫描中不覆盖提示文字）
        if not self._scanning:
            cur = snap.get("serial")
            addr = snap.get("wireless_addr")
            if cur and ":" in cur:
                self.row_port.set_subtitle(f"{cur} · 使用中")
            elif addr:
                self.row_port.set_subtitle(f"{addr} · 未使用，点 ↻ 重扫")
            else:
                self.row_port.set_subtitle("尚未配置 · 点右侧 ↻ 扫描")

    def do_activate(self):
        if not self._built:
            self._build_ui()
            self._built = True
        self.win.present()
        self._hook_clipboard()

        mon = Monitor(lambda s: (self._apply(s), False)[1])
        self.mon = mon
        mon.start()

        def tick_events():
            self._read_events()
            return True

        GLib.timeout_add(800, tick_events)
        # 启动时先灌入已有事件
        self._read_events()


def main():
    app = App()
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
