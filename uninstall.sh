#!/usr/bin/env bash
# uninstall.sh — 卸载 phone-connect
#
# 默认保留运行时数据（配置、日志、照片列表），加 --purge 一并删除。
set -uo pipefail

BIN="$HOME/.local/bin"
OPT="$HOME/.local/opt/phonesync"
UNIT="$HOME/.config/systemd/user"
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

say "停止并禁用服务"
systemctl --user disable --now phone-clip.service phone-sync.service phone-push.service 2>/dev/null || true

say "删除 systemd 单元"
rm -f "$UNIT"/phone-clip.service "$UNIT"/phone-sync.service "$UNIT"/phone-push.service
systemctl --user daemon-reload 2>/dev/null || true

say "删除命令行工具"
for f in phone phone-gui phone-autoconnect wait-for-adb phone-daemon-run; do rm -f "$BIN/$f"; done

say "删除桌面项"
rm -f "$APPS/phone-connect.desktop" "$AUTOSTART/phone-connect.desktop"

if [ "$PURGE" = "1" ]; then
  say "删除运行时数据（含配置、日志、消息流）"
  rm -rf "$OPT"
  say "注意：~/Pictures/手机同步/ 里的照片没有删除，需要的话自己清理"
else
  say "保留运行时数据于 $OPT（用 ./uninstall.sh --purge 一并删除）"
  rm -f "$OPT"/*.py
fi

echo
say "卸载完成"
