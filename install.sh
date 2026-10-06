#!/usr/bin/env bash
# install.sh — 安装 phone-connect
#
# 全部装到用户目录，不需要 root：
#   ~/.local/bin/               命令行工具
#   ~/.local/opt/phonesync/     守护进程
#   ~/.config/systemd/user/     systemd 用户服务
#   ~/.local/share/applications 应用菜单项
#   ~/.config/autostart/        开机自启
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
OPT="$HOME/.local/opt/phonesync"
UNIT="$HOME/.config/systemd/user"
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ───────────────────────── 依赖检查 ─────────────────────────
say "检查依赖"
missing=()
python3 - <<'PY' 2>/dev/null || missing+=("python3-gi / gir1.2-gtk-4.0 / gir1.2-adw-1")
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
PY
python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null \
  || missing+=("python3-gi (GTK3，phone_pushclip 需要)")
for c in xdotool xwininfo notify-send; do
  command -v "$c" >/dev/null || missing+=("$c")
done

if [ ${#missing[@]} -gt 0 ]; then
  warn "缺少依赖：${missing[*]}"
  warn "Ubuntu/Debian 可执行："
  warn "  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \\"
  warn "                   gir1.2-gdkpixbuf-2.0 xdotool x11-utils libnotify-bin"
  echo
fi

# ───────────────────────── scrcpy ─────────────────────────
if ! command -v scrcpy >/dev/null; then
  warn "没找到 scrcpy。去官方 Release 下静态包（不需要 root）："
  warn "  https://github.com/Genymobile/scrcpy/releases"
  warn "  解压后把 scrcpy 和 adb 放进 ~/.local/bin/ 即可"
  echo
fi

# ───────────────────────── 拷文件 ─────────────────────────
say "安装命令行工具 → $BIN"
mkdir -p "$BIN"
for f in phone phone-gui phone-autoconnect wait-for-adb phone-daemon-run; do
  install -m 755 "$SRC/bin/$f" "$BIN/$f"
done

say "安装守护进程 → $OPT"
mkdir -p "$OPT"
for f in phone_sync.py phone_gui.py phone_pushclip.py find_port.py; do
  install -m 644 "$SRC/phonesync/$f" "$OPT/$f"
done

# ───────────────────────── systemd ─────────────────────────
say "安装 systemd 用户服务"
mkdir -p "$UNIT"
cp "$SRC"/systemd/*.service "$UNIT/"
systemctl --user daemon-reload

# ───────────────────────── 桌面项 ─────────────────────────
say "安装应用菜单项与自启项"
mkdir -p "$APPS" "$AUTOSTART"
sed "s|@HOME@|$HOME|g" "$SRC/desktop/phone-connect.desktop.in" > "$APPS/phone-connect.desktop"
cp "$APPS/phone-connect.desktop" "$AUTOSTART/phone-connect.desktop"
cat >> "$AUTOSTART/phone-connect.desktop" <<'EOF'
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=8
EOF

# ───────────────────────── 启用 ─────────────────────────
say "启用服务（登录后自动运行）"
systemctl --user enable phone-clip.service phone-sync.service phone-push.service 2>/dev/null || true

echo
say "安装完成 ✅"
cat <<EOF

下一步：

  1. 手机打开「开发者选项 → 无线调试」，点「使用配对码配对设备」
  2. 电脑执行（IP 和端口换成你手机屏幕上的）：

       phone pair 192.168.1.50:37105 123456    # 配对地址 + 配对码
       phone use  192.168.1.50:40123           # 「连接地址」

  3. 确认状态：

       phone status

  常用命令：

       phone status     查看状态
       phone log        实时日志
       phone clipmode   照片是否自动进剪贴板
       phone pushclip   电脑→手机推送模式
       phone-gui        打开桌面客户端

开机自启已配好，以后登录桌面就自动连上。

卸载：./uninstall.sh
EOF
