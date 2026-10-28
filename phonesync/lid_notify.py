#!/usr/bin/env python3
"""
lid-notify — 笔记本盖子由「合上」变为「打开」时，向手机通知栏发布一条通知。

链路：
  1. 轮询 /proc/acpi/button/lid/LID*/state 检测盖子状态
     （无需 root；合盖睡眠已被 dsh-keepawake 的 logind inhibitor 阻止，所以脚本能持续运行）
  2. 检测到 closed → open 时，先读开关 ~/.local/opt/phonesync/config.json 里的 lid_notify
  3. 开关开 → 通过 adb 调用 Android 的 `cmd notification post`，把
     「HH:MM 电脑盖子打开」发布到手机通知栏（固定 tag，只保留最新一条）
     同时写一条事件到 events.jsonl，「手机连接」客户端消息流里也能看到
  4. 不再占用电脑剪贴板、也不覆盖手机剪贴板

开关（在「手机连接」客户端里点）：
  config.json 的 lid_notify = true / false，改完立即生效，不用重启服务

可用环境变量（调试/自定义）：
  LID_NOTIFY_STATE_PATH   覆盖盖子状态文件路径（测试用）
  LID_NOTIFY_INTERVAL_MS  轮询间隔毫秒，默认 1000
  LID_NOTIFY_TEXT_FMT     通知正文模板，默认 "{time} 电脑盖子打开"
  LID_NOTIFY_TITLE        通知标题，默认 "电脑盖子打开"
  LID_NOTIFY_DRY_RUN      =1 时只写日志、不真正发通知（测试开关逻辑用）
"""

import glob
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("lid-notify")
DEFAULT_INTERVAL_MS = 1000
DEFAULT_TITLE = "电脑盖子打开"
DEFAULT_TEXT_FMT = "{time} 电脑盖子打开"

HOME = Path.home()
CONFIG = HOME / ".local/opt/phonesync/config.json"
EVENTS = HOME / ".local/opt/phonesync/events.jsonl"
NOTIF_TAG = "phone-connect-lid"

# 调试开关：LID_NOTIFY_DRY_RUN=1 只打日志不碰手机
DRY_RUN = os.environ.get("LID_NOTIFY_DRY_RUN", "") not in ("", "0", "false")


def _resolve_bin(env_key, names, fallback):
    """环境变量 > ~/.local/bin > PATH。别人用发行版包安装也能跑。"""
    import shutil
    cands = [os.environ.get(env_key), str(HOME / ".local/bin" / names[0])]
    cands += [shutil.which(n) for n in names]
    for c in cands:
        if c and os.access(c, os.X_OK):
            return c
    return fallback


ADB = _resolve_bin("ADB", ["adb"], "adb")


# ────────────────────────────── 工具 ──────────────────────────────

def read_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def lid_enabled():
    """开关：缺省（还没在客户端里切换过）视为开启，保持原行为。"""
    return bool(read_config().get("lid_notify", True))


def cfg_text_fmt():
    return os.environ.get("LID_NOTIFY_TEXT_FMT", DEFAULT_TEXT_FMT)


def cfg_title():
    return os.environ.get("LID_NOTIFY_TITLE", DEFAULT_TITLE)


def run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def list_phone_devices():
    rc, out, _ = run([ADB, "devices"], timeout=10)
    devs = []
    if rc != 0:
        return devs
    for ln in out.splitlines()[1:]:
        parts = ln.split()
        if len(parts) >= 2 and parts[1] == "device":
            devs.append(parts[0])
    return devs


def pick_device():
    """优先无线通道（序列号形如 ip:port），其次 USB；和 phone_sync 的规则一致。"""
    devs = list_phone_devices()
    if not devs:
        return None
    for d in devs:
        if ":" in d:
            return d
    return devs[0]


def emit(kind, **fields):
    """写结构化事件流，桌面客户端「消息」区会实时显示。"""
    rec = {"ts": time.time(), "type": kind, **fields}
    try:
        with EVENTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def post_phone_notification(serial, title, text):
    """在手机通知栏发布（固定 tag：重复发布时更新同一条，只留最新时间）。"""
    rc, _out, err = run(
        [ADB, "-s", serial, "shell", "cmd", "notification", "post",
         "-S", "bigtext", "-t", title, NOTIF_TAG, text],
        timeout=15,
    )
    if rc == 0:
        return True, ""
    # 有的旧 Android 不支持 -S bigtext，退回最简形式再试一次
    rc2, _out2, err2 = run(
        [ADB, "-s", serial, "shell", "cmd", "notification", "post",
         "-t", title, NOTIF_TAG, text],
        timeout=15,
    )
    if rc2 == 0:
        return True, ""
    return False, (err or err2 or "未知错误").strip()


# ────────────────────────────── 盖子检测 ──────────────────────────────

def find_lid_state_path():
    """返回盖子状态文件路径；显式指定时优先，找不到返回 None。"""
    env_path = os.environ.get("LID_NOTIFY_STATE_PATH")
    if env_path:
        return env_path
    candidates = sorted(glob.glob("/proc/acpi/button/lid/*/state"))
    return candidates[0] if candidates else None


def read_lid_state(path):
    """读盖子状态，返回 'open' / 'closed' / None。"""
    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            content = f.read().strip()
    except OSError:
        return None
    if "closed" in content:
        return "closed"
    if "open" in content:
        return "open"
    return None


class LidNotifier:
    def __init__(self, state_path):
        self.path = state_path
        self.last = None
        interval_ms = int(os.environ.get("LID_NOTIFY_INTERVAL_MS", DEFAULT_INTERVAL_MS))
        self.interval_ms = max(100, interval_ms)

    def poll(self):
        state = read_lid_state(self.path)
        if state is None:
            # 文件暂时读不到（如测试文件尚未创建）时静默等待
            return
        if self.last is None:
            LOG.info("启动：当前盖子状态=%s（只记录基线，不触发通知）", state)
        elif state != self.last:
            LOG.info("盖子状态变化：%s → %s", self.last, state)
            if self.last == "closed" and state == "open":
                self.notify_lid_opened()
        self.last = state

    def notify_lid_opened(self):
        if not lid_enabled():
            LOG.info("开盖通知开关已关闭（手机连接客户端里可重新打开），跳过")
            return

        now = datetime.now()
        try:
            text = cfg_text_fmt().format(time=now.strftime("%H:%M"),
                                         date=now.strftime("%Y-%m-%d"))
        except (KeyError, IndexError) as exc:
            LOG.warning("文本模板非法 %r（%s），回退默认格式", cfg_text_fmt(), exc)
            text = DEFAULT_TEXT_FMT.format(time=now.strftime("%H:%M"))
        title = cfg_title()

        serial = pick_device()
        if not serial:
            LOG.warning("跳过：手机未连接（adb 里没有设备），本次开盖通知没发出去")
            return

        if DRY_RUN:
            LOG.info("[dry-run] 应当发布到 %s：%s / %s", serial, title, text)
            return

        ok, err = post_phone_notification(serial, title, text)
        if not ok:
            LOG.error("发布通知失败（%s）：%s", serial, err)
            return

        LOG.info("已发布到手机通知栏（%s）：%s", serial, title)
        emit("lid", time=now.strftime("%H:%M"), title=title,
             text=text, device=serial, delivered=True)

    def run(self):
        LOG.info("开始监听 %s（每 %d ms 轮询）", self.path, self.interval_ms)
        while True:
            try:
                self.poll()
            except Exception as exc:  # noqa: BLE001  单次异常不能让服务退出
                LOG.warning("轮询异常：%s", exc)
            time.sleep(self.interval_ms / 1000.0)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    state_path = find_lid_state_path()
    if not state_path:
        LOG.error("找不到盖子状态文件 /proc/acpi/button/lid/*/state，退出")
        return 1
    if not os.path.exists(state_path) and not os.environ.get("LID_NOTIFY_STATE_PATH"):
        LOG.error("盖子状态文件 %s 不存在，退出", state_path)
        return 1
    LOG.info("使用盖子状态文件：%s", state_path)
    LOG.info("开盖通知开关：%s%s", "开" if lid_enabled() else "关",
             "（dry-run）" if DRY_RUN else "")
    LidNotifier(state_path).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
