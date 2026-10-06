#!/usr/bin/env python3
"""
phone_sync.py — 手机 ↔ 电脑 同步守护进程（纯 ADB，无需 root，手机不装 App）

功能：
  1. 短信同步      : 直读 content://sms/inbox，新短信实时弹到桌面（含全文）
  2. 通知同步      : 轮询 dumpsys notification --noredact，镜像手机通知到桌面
  3. 拍照→剪贴板   : 监听相册新增图片，自动拉取、存本地、并塞进 X11 剪贴板

用法：
  phone_sync.py                # 前台运行
  phone_sync.py --once         # 只跑一轮（调试）
  phone_sync.py --no-photo     # 不同步相册
  phone_sync.py --no-notif     # 只同步短信/相册
  phone_sync.py --test         # 打印当前状态后退出
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import json
from pathlib import Path

import shutil


def _resolve_bin(env_key, names, fallback):
    """环境变量 > ~/.local/bin > PATH。别人用发行版包安装也能跑。"""
    import os
    cands = [os.environ.get(env_key), str(Path.home() / ".local/bin" / names[0])]
    cands += [shutil.which(n) for n in names]
    for c in cands:
        if c and os.access(c, os.X_OK):
            return c
    return fallback


try:
    from gi.repository import GLib          # 事件回调要回到主线程
except Exception:                            # --test / --once 场景没有 GTK 也能跑
    GLib = None

ADB = _resolve_bin("ADB", ["adb"], "adb")
SAVE_DIR = Path.home() / "Pictures" / "手机同步"
STATE_FILE = Path.home() / ".local/opt/phonesync/state.json"
CONFIG = Path.home() / ".local/opt/phonesync/config.json"


def cfg(key, default=None):
    """读可选配置 ~/.local/opt/phonesync/config.json"""
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")).get(key, default)
    except Exception:
        return default


SMS_POLL = 4.0       # 短信轮询间隔（秒）
NOTIF_POLL = 4.0     # 通知轮询间隔（秒）
PHOTO_POLL = 5.0     # 相册轮询间隔（秒）
MAX_ATTACH = 12 * 1024 * 1024   # 超过 12MB 不进剪贴板（只保存）

# 系统噪音通知，默认不镜像
NOISE_PKGS = {
    "android", "com.android.systemui", "com.android.providers.downloads",
    "com.android.settings", "com.android.bluetooth", "com.android.vending",
    "com.android.nfc", "com.android.server.telecom",
}


def adb(*args, timeout=30):
    try:
        p = subprocess.run([ADB, *args], capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, b"", "adb timeout"
    except FileNotFoundError:
        return 127, b"", f"adb not found at {ADB}"


def shell(cmd, timeout=30):
    rc, out, err = adb("shell", cmd, timeout=timeout)
    return out.decode("utf-8", "replace")


WIRELESS_ENV = Path.home() / ".local/opt/phonesync/wireless.env"


def list_devices():
    rc, out, _ = adb("devices")
    devs = []
    for ln in out.decode().strip().splitlines()[1:]:
        parts = ln.split()
        if len(parts) >= 2 and parts[1] == "device":
            devs.append(parts[0])
    return devs


def pick_device():
    """优先无线通道（序列号形如 ip:port），其次 USB；避免"more than one device"歧义"""
    devs = list_devices()
    if not devs:
        return None
    for d in devs:
        if ":" in d:
            return d
    return devs[0]


def device_ready():
    return pick_device()


def wireless_addr():
    """从 wireless.env 读无线地址，支持任意端口（无线调试的端口是随机的）"""
    ip = port = None
    try:
        for line in WIRELESS_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("WIRELESS_IP="):
                ip = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("WIRELESS_PORT="):
                port = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    if not ip:
        return None
    return f"{ip}:{port or 5555}"


def try_wireless_connect():
    """交给 phone-autoconnect：先试记录地址，必要时（限流）扫描找新端口"""
    helper = Path.home() / ".local/bin/phone-autoconnect"
    if not helper.exists():
        addr = wireless_addr()
        if not addr:
            return False
        adb("connect", addr, timeout=15)
        return True
    try:
        subprocess.run([str(helper)], capture_output=True, timeout=150)
    except Exception:
        pass
    return True


def notify_desktop(title, body, urgency="normal", icon="phone"):
    try:
        subprocess.run(["notify-send", "-a", "手机同步", "-u", urgency, "-i", icon, title, body],
                       capture_output=True, timeout=8)
    except Exception as e:
        print(f"[warn] notify-send 失败: {e}", flush=True)


def log(tag, msg):
    print(f"{time.strftime('%H:%M:%S')} [{tag}] {msg}", flush=True)


# 结构化事件流：桌面 GUI 实时读取它来显示消息
EVENTS = Path.home() / ".local/opt/phonesync/events.jsonl"
_emit_count = [0]


def emit(kind, **fields):
    rec = {"ts": time.time(), "type": kind, **fields}
    try:
        with EVENTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _emit_count[0] += 1
        if _emit_count[0] % 200 == 0 and EVENTS.stat().st_size > 2_000_000:
            keep = EVENTS.read_text(encoding="utf-8").splitlines()[-1000:]
            EVENTS.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass


# ─────────────────────────── 短信 ───────────────────────────

SMS_ROW = re.compile(r"^Row: \d+ address=(?P<addr>.*?), body=(?P<body>.*), date=(?P<date>\d+)$", re.M)


def fetch_sms_since(since_ms):
    """拉取 date > since_ms 的收件箱短信，按时间升序返回"""
    q = (f'content query --uri content://sms/inbox '
         f'--projection address:body:date --where "date > {int(since_ms)}" --sort "date DESC"')
    out = shell(q, timeout=40)
    msgs = []
    for m in SMS_ROW.finditer(out):
        msgs.append({
            "addr": m.group("addr").strip(),
            "body": m.group("body").strip(),
            "date": int(m.group("date")),
        })
    msgs.sort(key=lambda x: x["date"])
    return msgs


def sms_latest_ts():
    q = 'content query --uri content://sms/inbox --projection date --sort "date DESC"'
    out = shell(q, timeout=40)
    m = re.search(r"date=(\d+)", out)
    return int(m.group(1)) if m else int(time.time() * 1000)


# ─────────────────────────── 通知 ───────────────────────────

REC_SPLIT = re.compile(r"^\s*NotificationRecord\(", re.M)
TITLE_RE = re.compile(r"android\.title=(?:String|CharSequence)?\s*\((.*)\)\s*$", re.M)
TEXT_RE = re.compile(r"android\.text=(?:String|CharSequence)?\s*\((.*)\)\s*$", re.M)
BIG_RE = re.compile(r"android\.bigText=(?:String|CharSequence)?\s*\((.*)\)\s*$", re.M)
PKG_RE = re.compile(r"pkg=([\w\.]+)")
ID_RE = re.compile(r"\bid=(-?\d+)")
TAG_RE = re.compile(r"tag=(\S+)")


def parse_notifications(raw):
    idx = [m.start() for m in REC_SPLIT.finditer(raw)]
    recs = []
    for i, s in enumerate(idx):
        e = idx[i + 1] if i + 1 < len(idx) else len(raw)
        b = raw[s:e]
        head = b.split("\n", 1)[0]
        pkg, nid, tag = PKG_RE.search(head), ID_RE.search(head), TAG_RE.search(head)
        title, text, big = TITLE_RE.search(b), TEXT_RE.search(b), BIG_RE.search(b)
        body = big or text
        recs.append({
            "pkg": pkg.group(1) if pkg else "?",
            "id": nid.group(1) if nid else "?",
            "tag": tag.group(1) if tag else "null",
            "title": title.group(1).strip() if title else "",
            "text": body.group(1).strip() if body else "",
        })
    return recs


def notif_key(rec):
    return hashlib.sha1(f'{rec["pkg"]}|{rec["id"]}|{rec["tag"]}|{rec["title"]}|{rec["text"]}'.encode()).hexdigest()


# ─────────────────────────── 相册 ───────────────────────────

IMG_ROW = re.compile(r"^Row: \d+ _id=(\d+), _data=(.+)$", re.M)
IMG_URI = "content://media/external/images/media"
# Android 11+ 的 FUSE 会先把文件写成 .pending-<数字>-<真名>，再改名。
# 用它做去重 key 的规范化，避免同一张图被处理两次（.pending 一次、最终名一次）
PENDING_RE = re.compile(r"^\.pending-\d+-")


def max_photo():
    """相册里 _id 最大的图片 → (id: int, path)；本机 content 不支持 --limit，排序后取首行"""
    q = (f'content query --uri {IMG_URI} --projection _id:_data --sort "_id DESC"')
    out = shell(q, timeout=40)
    m = IMG_ROW.search(out)
    return (int(m.group(1)), m.group(2).strip()) if m else None


def photos_after(last_id):
    """所有 _id > last_id 的新图片，按 _id 升序 —— 连拍/批量新增都不会漏"""
    q = (f'content query --uri {IMG_URI} --projection _id:_data '
         f'--where "_id > {int(last_id)}" --sort "_id ASC"')
    out = shell(q, timeout=40)
    return [(int(m.group(1)), m.group(2).strip()) for m in IMG_ROW.finditer(out)]


def latest_photo():
    """兼容入口：最新图片 (id: str, path)"""
    r = max_photo()
    return (str(r[0]), r[1]) if r else None


def pull_file(remote, local):
    local.parent.mkdir(parents=True, exist_ok=True)
    rc, out, err = adb("exec-out", f"cat '{remote}'", timeout=120)
    if rc == 0 and out:
        local.write_bytes(out)
        return True
    rc, out, err = adb("pull", remote, str(local), timeout=120)
    return rc == 0 and local.exists() and local.stat().st_size > 0


class InotifyWatcher(threading.Thread):
    """
    用手机端的 inotifyd 做「事件级」新图片监听。

    对比轮询：轮询是每 N 秒问一次（实测延迟 4.9 秒），
    这个是文件一写完（IN_CLOSE_WRITE = 事件码 w）手机立刻推过来，延迟 <0.5 秒。
    连接断了会自动重连，连不上时由 tick_photo 轮询兜底。
    """

    CANDIDATE_DIRS = (
        "/sdcard/Pictures/Screenshots",
        "/sdcard/DCIM/Camera",
        "/sdcard/DCIM/Screenshots",
    )

    def __init__(self, on_photo):
        super().__init__(daemon=True)
        self.on_photo = on_photo          # 主线程回调（经 GLib.idle_add 派发）
        self.dirs = []
        self.ready = False

    def detect_dirs(self):
        found = []
        for d in self.CANDIDATE_DIRS:
            rc, out, _ = adb("shell", f"[ -d {d} ] && echo yes", timeout=15)
            if b"yes" in out:
                found.append(d)
        return found

    def run(self):
        while True:
            if not self.dirs:
                self.dirs = self.detect_dirs()
                if not self.dirs:
                    time.sleep(10)
                    continue
            try:
                cmd = [ADB, "exec-out", "inotifyd", "-"] + [f"{d}:nwy" for d in self.dirs]
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0)
                self.ready = True
                log("监听", f"inotify 事件监听就绪: {', '.join(self.dirs)}")
                # 每行格式: <事件码>\t<目录>\t<文件名>
                #   w = 写入完成（普通文件/ .pending 文件）
                #   y = 改名到位（FUSE 把 .pending-xxx 改成真名的瞬间）
                # 两条都收，靠 do_photo 里的文件名归一化去重，保证只处理一次
                for raw in iter(p.stdout.readline, b""):
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    parts = line.split("\t")
                    if len(parts) >= 3 and parts[0] in ("w", "y") and parts[2]:
                        # 直接在本线程拉取：不等主循环（否则会被通知轮询堵住）
                        self.on_photo(f"{parts[1].rstrip('/')}/{parts[2]}", time.time())
                self.ready = False
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()
            except Exception as e:
                self.ready = False
                log("监听", f"inotify 断开，5 秒后重连: {e}")
                time.sleep(5)


class ClipboardImage:
    """
    GTK4 剪贴板图片持有者（关键：把「编码」和「挂剪贴板」拆开）

      · prepare()  解码 + 预编码 —— 在后台线程做。
                   12MP 照片 PNG 编码要 2.3 秒，绝不能占主线程。
      · apply()    只把预编码好的字节挂上剪贴板 —— 实测 0.4 ms。

    为什么必须这样：GTK3 的 set_image 是「粘贴时才现场编码」，12MP 图要 2.3 秒，
    表现为「粘贴卡死」。改成预编码后，实测读回耗时从 2000+ ms 降到 ~240 ms。

    格式策略（clip_image_format: auto/png/jpeg/both）：
      auto = 小图（≤2MP，如截图）给 PNG+JPEG；大图（相机照片）只给 JPEG ——
             12MP 的 PNG 有 10.9MB，JPEG q90 只有 1.4MB。
    """

    def __init__(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import Gtk, Gdk, GdkPixbuf, GLib
        Gtk.init()
        self.Gdk, self.GdkPixbuf, self.GLib = Gdk, GdkPixbuf, GLib
        self.display = Gdk.Display.get_default()
        self.cb = self.display.get_clipboard()

    def prepare(self, path):
        """后台线程调用：解码 + 预编码成字节"""
        pb = self.GdkPixbuf.Pixbuf.new_from_file(str(path))
        w, h = pb.get_width(), pb.get_height()
        # 默认同时提供 PNG + JPEG：
        #   只给 JPEG 会让「只认 image/png」的应用粘贴不到东西（实测踩过）。
        #   PNG 用 compression=1（482ms）而不是默认 6（2280ms）—— 快 4.7 倍，
        #   代价是体积 10.9MB→12.9MB（反正只是内存里传，不走网络）。
        fmt = cfg("clip_image_format", "both")
        out = {"w": w, "h": h, "fmt": fmt}
        if fmt in ("png", "both"):
            out["png"] = bytes(pb.save_to_bufferv("png", ["compression"], ["1"])[1])
        if fmt in ("jpeg", "both"):
            out["jpg"] = bytes(pb.save_to_bufferv("jpeg", ["quality"], ["90"])[1])
        return out

    def apply(self, prepared):
        """主线程调用：挂上剪贴板（零编码开销）"""
        provs = []
        if "png" in prepared:
            provs.append(self.Gdk.ContentProvider.new_for_bytes(
                "image/png", self.GLib.Bytes.new(prepared["png"])))
        if "jpg" in prepared:
            provs.append(self.Gdk.ContentProvider.new_for_bytes(
                "image/jpeg", self.GLib.Bytes.new(prepared["jpg"])))
        if not provs:
            raise RuntimeError("没有可用的图像格式")
        content = provs[0] if len(provs) == 1 else self.Gdk.ContentProvider.new_union(provs)
        self.cb.set_content(content)
        return prepared["w"], prepared["h"]


# ─────────────────────────── 主流程 ───────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test", action="store_true", help="打印当前状态后退出")
    ap.add_argument("--no-photo", action="store_true")
    ap.add_argument("--no-notif", action="store_true", help="不同步普通通知")
    ap.add_argument("--all-notifications", action="store_true")
    args = ap.parse_args()

    dev = device_ready()
    if not dev:
        try_wireless_connect()
        dev = device_ready()
    if not dev:
        if args.once or args.test:
            print("✗ 没有已授权的设备。插好 USB，手机上点『允许 USB 调试』。", flush=True)
            adb("devices", "-l")
            return 1
        # 常驻模式：静默等待手机接入（USB 或无线），绝不退出、也不刷日志
        log("等待", "未检测到手机，静默等待接入（USB 或无线）")
        while not dev:
            time.sleep(5)
            try_wireless_connect()
            dev = device_ready()
    os.environ["ANDROID_SERIAL"] = dev
    log("连接", f"设备 {dev}")

    if args.test:
        info = shell("getprop ro.product.brand; getprop ro.product.model; getprop ro.build.version.release").replace("\r", "").split("\n")
        log("机型", " ".join(x for x in info if x))
        log("短信库", f"最新时间戳 {sms_latest_ts()}")
        raw = shell("dumpsys notification --noredact")
        recs = parse_notifications(raw)
        log("通知", f"当前 {len(recs)} 条活跃通知")
        for r in recs[:6]:
            log("  ·", f"{r['pkg']}: {r['title']} — {r['text'][:50]}")
        lt = latest_photo()
        log("相册", f"最新图片 {lt}")
        return 0

    seen = {}
    if STATE_FILE.exists():
        try:
            seen = json.loads(STATE_FILE.read_text())
        except Exception:
            seen = {}

    # 水位线：从"现在"开始，避免一启动就喷几百条历史
    sms_wm = sms_latest_ts()
    log("水位", f"短信起点 {sms_wm}（更早的历史不推送）")

    clip = None
    if not args.no_photo:
        try:
            clip = ClipboardImage()
            log("剪贴板", "图片通道就绪")
        except Exception as e:
            log("警告", f"剪贴板初始化失败，照片只存不进剪贴板: {e}")

    last_photo_id = None
    if not args.no_photo:
        mp = max_photo()
        if mp:
            last_photo_id = mp[0]
            log("相册", f"监听起点 _id={last_photo_id}")
        else:
            log("警告", "相册查询失败，本次不同步照片")
    log("就绪", "开始同步（Ctrl+C 退出）")

    _refresh = [0.0]

    def refresh_target(min_gap=10.0):
        """定期重挑目标设备：USB 拔了自动切无线，无线断了自动回 USB"""
        now = time.time()
        if now - _refresh[0] < min_gap:
            return
        _refresh[0] = now
        d = pick_device()
        if d is None:
            try_wireless_connect()
            d = pick_device()
        if d and d != os.environ.get("ANDROID_SERIAL"):
            os.environ["ANDROID_SERIAL"] = d
            log("连接", f"切换到设备 {d}")

    busy = {"sms": False, "notif": False}

    def in_worker(key, work, done):
        """把阻塞的 adb 调用丢到工作线程，结果回主线程处理。
        否则这些轮询会占着 GTK 主循环，把照片的「进剪贴板」堵住一两秒。"""
        if busy[key]:
            return
        busy[key] = True

        def run():
            try:
                res = work()
            except Exception as e:
                log("警告", f"{key} 轮询异常: {e}")
                res = None
            GLib.idle_add(lambda: (done(res), busy.__setitem__(key, False), False)[-1])

        threading.Thread(target=run, daemon=True).start()

    def tick_sms():
        nonlocal sms_wm
        refresh_target()

        def work():
            return fetch_sms_since(sms_wm)

        def done(msgs):
            nonlocal sms_wm
            for m in (msgs or []):
                if m["date"] <= sms_wm:
                    continue
                sms_wm = max(sms_wm, m["date"])
                who = m["addr"]
                log("短信", f"{who}: {m['body']}")
                emit("sms", sender=who, body=m["body"], date=m["date"])
                notify_desktop(f"📩 短信 · {who}", m["body"], icon="mail-unread")
            return False

        in_worker("sms", work, done)
        return True

    def tick_notif():
        refresh_target()

        def work():
            return parse_notifications(shell("dumpsys notification --noredact"))

        def done(recs):
            for rec in (recs or []):
                if rec["pkg"] in NOISE_PKGS and not args.all_notifications:
                    continue
                # 短信已由 SMS 通道处理，避免重复
                if "mms" in rec["pkg"].lower() or "messaging" in rec["pkg"].lower():
                    continue
                if not (rec["title"] or rec["text"]):
                    continue
                k = notif_key(rec)
                if k in seen:
                    continue
                seen[k] = time.time()
                log("通知", f"{rec['pkg']}: {rec['title']} — {rec['text']}")
                emit("notif", pkg=rec["pkg"], title=rec["title"], text=rec["text"])
                notify_desktop(rec["title"] or rec["pkg"], rec["text"], icon="dialog-information")
            if len(seen) > 4000:
                for k in sorted(seen, key=seen.get)[:2000]:
                    seen.pop(k, None)
            try:
                STATE_FILE.write_text(json.dumps(seen))
            except Exception:
                pass
            return False

        in_worker("notif", work, done)
        return True

    processed = {}          # basename -> 时间戳，防止 inotify 与轮询重复处理同一张图

    def finish_photo(local, remote_path, base, size, t_ev=None, prepared=None):
        """进剪贴板 + 桌面通知 + 事件（必须在主线程执行；只挂预编码字节，不做编码）"""
        copied, w, h = False, 0, 0
        if prepared:
            try:
                w, h = clip.apply(prepared)
                copied = True
                total = f"，总耗时 {(time.time()-t_ev)*1000:.0f}ms" if t_ev else ""
                log("照片", f"✓ 已复制到剪贴板 {w}x{h} [{prepared.get('fmt')}]{total}")
                notify_desktop("📷 新照片已复制到剪贴板", f"{base}  ({w}×{h})", icon="camera-photo")
            except Exception as e:
                log("照片", f"进剪贴板失败: {e}")
        if not copied:
            notify_desktop("📷 新照片已保存", base, icon="camera-photo")
        emit("photo", name=local.name, path=str(local), size=size, copied=copied, w=w, h=h)
        return False                     # 给 GLib.idle_add 用

    def do_photo(remote_path, in_thread=False, t_ev=None):
        """
        拉取图片并交付。
        in_thread=True 表示当前在监听线程：拉取 + 预编码都就地做（关键路径最短），
        只把「挂剪贴板」这一步丢回主线程（GTK 只能在主线程调用）。
        """
        base = PENDING_RE.sub("", Path(remote_path).name)   # 归一化，pending 与最终名视为同一张
        if not base or base in processed:
            return False
        processed[base] = time.time()
        if len(processed) > 800:
            for k in sorted(processed, key=processed.get)[:400]:
                processed.pop(k, None)

        local = SAVE_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{base}"
        if not pull_file(remote_path, local):
            log("照片", f"拉取失败: {remote_path}")
            return False
        size = local.stat().st_size
        # 预编码在这里做（后台线程）：12MP 图要 2 秒+，放主线程就会让粘贴卡死
        prepared = None
        mode = cfg("photo_clip_mode", "all")
        allow = (mode == "all") or (mode == "camera" and "/DCIM/Camera/" in remote_path)
        if clip and size <= MAX_ATTACH and allow:
            try:
                prepared = clip.prepare(local)
            except Exception as e:
                log("照片", f"预编码失败: {e}")
        cost = f"，事件→落盘 {(time.time()-t_ev)*1000:.0f}ms" if t_ev else ""
        log("照片", f"已保存 {local} ({size/1024:.0f} KB{cost})")
        if in_thread:
            GLib.idle_add(finish_photo, local, remote_path, base, size, t_ev, prepared)
        else:
            finish_photo(local, remote_path, base, size, t_ev, prepared)
        return True

    def tick_photo():
        """兜底轮询：inotify 连不上或漏事件时靠它（正常情况下几乎查不到新图）"""
        nonlocal last_photo_id
        refresh_target()
        if last_photo_id is None:
            return True
        for pid, path in photos_after(last_photo_id):
            last_photo_id = pid
            do_photo(path)
        return True

    if args.once:
        tick_sms()
        if not args.no_notif:
            tick_notif()
        if not args.no_photo:
            tick_photo()
        log("结束", "单轮模式")
        return 0

    import gi
    from gi.repository import GLib
    loop = GLib.MainLoop()
    GLib.timeout_add(int(SMS_POLL * 1000), tick_sms)
    if not args.no_notif:
        GLib.timeout_add(int(NOTIF_POLL * 1000), tick_notif)
    if not args.no_photo:
        GLib.timeout_add(int(PHOTO_POLL * 1000), tick_photo)
        # 事件级监听（主路径）：手机端 inotifyd 一有「写入完成」就立刻拉取
        InotifyWatcher(lambda p, t: do_photo(p, in_thread=True, t_ev=t)).start()
    try:
        loop.run()
    except KeyboardInterrupt:
        log("退出", "已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
