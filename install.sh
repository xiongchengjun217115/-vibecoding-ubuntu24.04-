#!/usr/bin/env bash
# install.sh — 安装 phone-connect
#
# 全部装到用户目录，**不需要 root**：
#   ~/.local/bin/               命令行工具 + scrcpy/adb 软链
#   ~/.local/opt/phonesync/     守护进程
#   ~/.local/opt/scrcpy/        找不到 scrcpy 时自动下载官方静态包
#   ~/.config/systemd/user/     systemd 用户服务
#   ~/.local/share/applications 应用菜单项
#   ~/.config/autostart/        开机自启
#
# 选项：
#   --no-download    不自动下载 scrcpy，只检查
#   --no-services    不碰 systemd（只铺文件）
set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
OPT="$HOME/.local/opt/phonesync"
SOPT="$HOME/.local/opt/scrcpy"
UNIT="$HOME/.config/systemd/user"
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart"
DO_DOWNLOAD=1
DO_SERVICES=1
for a in "$@"; do
  case "$a" in
    --no-download) DO_DOWNLOAD=0 ;;
    --no-services) DO_SERVICES=0 ;;
  esac
done

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ═════════════════════ 1. 基础依赖 ═════════════════════
say "检查基础依赖"

NEED_APT=0
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw" 2>/dev/null || NEED_APT=1
python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null || NEED_APT=1
python3 -c "import gi; gi.require_version('GdkPixbuf','2.0'); from gi.repository import GdkPixbuf" 2>/dev/null || NEED_APT=1
for c in xdotool xwininfo notify-send curl tar; do
  command -v "$c" >/dev/null || NEED_APT=1
done

if [ "$NEED_APT" = "1" ]; then
  warn "缺少系统依赖，请先执行（需要 sudo 密码，这一步得你自己来）："
  echo
  printf '    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \\\n'
  printf '                     gir1.2-gdkpixbuf-2.0 xdotool x11-utils libnotify-bin curl tar\n'
  echo
  warn "装完再跑一次 ./install.sh"
  exit 1
fi
ok "Python / GTK4 / GTK3 / xdotool / notify-send 都在"

# ═════════════════════ 2. scrcpy + adb ═════════════════════
say "准备 scrcpy 与 adb"
mkdir -p "$BIN" "$SOPT"

# 解析到**真实文件**（跟着软链走到底）。
# 注意顺序：先看真实位置，最后才看 ~/.local/bin —— 否则会捡到我们自己建的软链，
# 然后 ln -sf X X 变成自指，把 adb/scrcpy 全弄坏（这个坑踩过）。
resolve_bin() {
  local c real
  for c in "$@"; do
    [ -n "$c" ] || continue
    [ -x "$c" ] || continue
    real="$(readlink -f "$c" 2>/dev/null)"
    [ -n "$real" ] && [ -x "$real" ] || continue
    printf '%s' "$real"; return 0
  done
  return 1
}

SCRCPY_BIN="$(resolve_bin "$SOPT/scrcpy" "$(command -v scrcpy 2>/dev/null)" "$BIN/scrcpy" || true)"
ADB_BIN="$(resolve_bin "$SOPT/adb" "$(command -v adb 2>/dev/null)" "$BIN/adb" || true)"

if [ -z "$SCRCPY_BIN" ] && [ "$DO_DOWNLOAD" = "1" ]; then
  ARCH="$(uname -m)"
  if [ "$ARCH" != "x86_64" ]; then
    warn "官方只发布 x86_64 的 Linux 静态包，你的是 $ARCH"
    warn "改用发行版包：sudo apt install scrcpy adb，装完重跑 ./install.sh"
    exit 1
  fi
  say "没找到 scrcpy，从官方 Release 下载静态包（约 18MB，不需要 root）"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  URL="$(curl -fsSL https://api.github.com/repos/Genymobile/scrcpy/releases/latest 2>/dev/null \
        | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    for a in d.get('assets',[]):
        if a['name'].startswith('scrcpy-linux-x86_64-') and a['name'].endswith('.tar.gz'):
            print(a['browser_download_url']); break
except Exception: pass" 2>/dev/null)"
  if [ -z "$URL" ]; then
    warn "拿不到下载地址（网络或 API 限流）"
    warn "手动下载 https://github.com/Genymobile/scrcpy/releases"
    warn "解压到 $SOPT/ 后重跑 ./install.sh"
    exit 1
  fi
  ok "下载 $(basename "$URL")"
  curl -fL --retry 2 -o "$TMP/scrcpy.tar.gz" "$URL" || die "下载失败"
  TAG="$(basename "$(dirname "$URL")")"
  BASE="$(basename "$URL")"
  if curl -fsL -o "$TMP/SHA256SUMS.txt" \
       "https://github.com/Genymobile/scrcpy/releases/download/$TAG/SHA256SUMS.txt" 2>/dev/null; then
    EXP="$(grep "$BASE" "$TMP/SHA256SUMS.txt" | awk '{print $1}')"
    ACT="$(sha256sum "$TMP/scrcpy.tar.gz" | awk '{print $1}')"
    if [ -n "$EXP" ] && [ "$EXP" = "$ACT" ]; then ok "SHA256 校验通过"
    elif [ -n "$EXP" ]; then die "SHA256 不匹配，已中止"
    fi
  fi
  mkdir -p "$SOPT" && tar xzf "$TMP/scrcpy.tar.gz" -C "$SOPT" --strip-components=1
  SCRCPY_BIN="$SOPT/scrcpy"
  ADB_BIN="$SOPT/adb"
  ok "已解压到 $SOPT"
fi

if [ -z "$SCRCPY_BIN" ]; then
  warn "没找到 scrcpy。两种装法："
  warn "  · 重跑 ./install.sh（会自动下载官方静态包）"
  warn "  · 或 sudo apt install scrcpy adb"
  exit 1
fi
[ -n "$ADB_BIN" ] || { warn "没找到 adb（scrcpy 静态包自带；发行版包名是 adb / android-tools-adb）"; exit 1; }

SC_VER="$("$SCRCPY_BIN" --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1)"
if [ -n "$SC_VER" ]; then
  MAJ="${SC_VER%%.*}"
  if [ "$MAJ" -lt 3 ] 2>/dev/null; then
    warn "scrcpy 版本 $SC_VER 偏旧，建议 >= 3.0（当前：$SCRCPY_BIN）"
  else
    ok "scrcpy $SC_VER"
  fi
fi
ok "adb  $ADB_BIN"

# 软链到 ~/.local/bin：让脚本和 systemd 单元都用统一路径，且不挑来源
link_bin() {   # $1=真实文件  $2=目标软链
  local src="$1" dst="$2" cur
  [ -n "$src" ] && [ -x "$src" ] || return 1
  [ "$src" = "$dst" ] && return 0                       # 防自指
  cur="$(readlink -f "$dst" 2>/dev/null || true)"
  [ "$cur" = "$src" ] && return 0                       # 已经指对了
  ln -sfn "$src" "$dst"
}
link_bin "$SCRCPY_BIN" "$BIN/scrcpy"
link_bin "$ADB_BIN"    "$BIN/adb"
if [ -x "$BIN/scrcpy" ] && [ -x "$BIN/adb" ]; then
  ok "已链接到 $BIN/{scrcpy,adb}"
else
  die "软链创建失败（$BIN/scrcpy 或 $BIN/adb 不可用）"
fi

# ═════════════════════ 3. 铺文件 ═════════════════════
say "安装命令行工具 → $BIN"
for f in phone phone-gui phone-autoconnect wait-for-adb phone-daemon-run; do
  install -m 755 "$SRC/bin/$f" "$BIN/$f"
done
ok "5 个工具"

say "安装守护进程 → $OPT"
mkdir -p "$OPT"
for f in phone_sync.py phone_gui.py phone_pushclip.py find_port.py; do
  install -m 644 "$SRC/phonesync/$f" "$OPT/$f"
done
ok "4 个模块"

# ═════════════════════ 4. systemd ═════════════════════
if [ "$DO_SERVICES" = "1" ] && command -v systemctl >/dev/null 2>&1; then
  say "安装 systemd 用户服务"
  mkdir -p "$UNIT"
  cp "$SRC"/systemd/*.service "$UNIT/"
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable phone-clip.service phone-sync.service phone-push.service 2>/dev/null || true
  ok "已启用（登录桌面后自动运行）"
else
  warn "跳过 systemd 安装（可稍后手动 cp systemd/*.service ~/.config/systemd/user/）"
fi

# ═════════════════════ 5. 桌面项 ═════════════════════
say "安装应用菜单项与自启项"
mkdir -p "$APPS" "$AUTOSTART"
sed "s|@HOME@|$HOME|g" "$SRC/desktop/phone-connect.desktop.in" > "$APPS/phone-connect.desktop"
cp "$APPS/phone-connect.desktop" "$AUTOSTART/phone-connect.desktop"
cat >> "$AUTOSTART/phone-connect.desktop" <<'EOF'
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=8
EOF
ok "应用菜单 + 开机自启"

# ═════════════════════ 6. 会话类型提示 ═════════════════════
echo
SESSION="${XDG_SESSION_TYPE:-unknown}"
if [ "$SESSION" = "wayland" ]; then
  warn "检测到 Wayland 会话，功能有取舍："
  warn "  · 手机→电脑剪贴板、照片、短信、通知  都正常"
  warn "  · 电脑→手机剪贴板 依赖 xdotool 给 scrcpy 窗口发按键，Wayland 上不可用"
  warn "  · 需要它就重新登录并选「Ubuntu on Xorg」（登录界面右下角齿轮）"
elif [ "$SESSION" = "x11" ]; then
  ok "X11 会话，全部功能可用"
else
  warn "会话类型未知（$SESSION），电脑→手机剪贴板可能不可用"
fi

echo
say "安装完成 ✅"
cat <<EOF

下一步（只需一次，全程不需要数据线）：

  1. 手机：设置 → 关于本机 → 版本信息 → 连点「版本号」7 次
  2. 手机：设置 → 系统设置 → 开发者选项 → 打开「无线调试」
           （USB 调试可以一直关着，不需要插线）
  3. 手机：点进「无线调试」→「使用配对码配对设备」，记下弹窗上的两个值
  4. 电脑：

       phone pair 192.168.1.50:37105 123456    # 配对地址 + 6 位配对码
       phone use  192.168.1.50:40123           # 「无线调试」主界面的连接地址

  5. 确认：

       phone status

常用命令：

    phone status     查看连接状态 / 当前通道 / 服务
    phone log        实时日志
    phone screen     投屏 + 鼠标键盘反控手机
    phone clipmode   照片要不要自动进剪贴板
    phone pushclip   电脑→手机剪贴板推送模式
    phone-gui        打开桌面客户端

开机自启已配好，以后登录桌面自动连上。
卸载：./uninstall.sh    （加 --purge 连数据一起删）
EOF
