# 手机 ↔ Ubuntu 无线协同

把 Android 手机和 Ubuntu 电脑连起来：**剪贴板双向同步、手机截图/拍照直接进电脑剪贴板、短信和通知实时镜像。**

不需要数据线，**手机上也不用装任何 App** —— 只用 Android 自带的「无线调试」。

```bash
手机复制  →  电脑 Ctrl+V
电脑复制  →  手机粘贴
手机截图  →  0.4 秒后电脑可粘贴
手机拍照  →  0.7 秒后电脑可粘贴
```

---

## 功能

| 功能 | 说明 |
|---|---|
| **剪贴板双向同步** | 文字两个方向实时同步 |
| **截图 / 拍照 → 电脑剪贴板** | 手机拍完直接去电脑粘贴，实测 0.4~0.7 秒 |
| **短信同步** | 直读系统短信库，比抓通知更全 |
| **通知镜像** | 实时推送到桌面通知 + 客户端消息流 |
| **桌面客户端** | GTK4 界面，看连接状态 / 剪贴板 / 消息 |
| **托盘状态图标** | 灰色手机=待机，蓝色=有新内容，三个方向都提示 |
| **投屏 + 反控** | scrcpy 自带，鼠标键盘可操作手机 |
| **端口自愈** | 手机重启后端口变了，一条命令找回 |
| **开机自启** | 三个 systemd 用户服务 + 客户端 + 托盘 |

---

## 环境要求

- Ubuntu 22.04 / 24.04（其他发行版理论上也行，需要 GTK4）
- Python 3 + PyGObject
- `xdotool`、`xwininfo`（向 scrcpy 隐藏窗口发按键用）
- Android 11+（需要「无线调试」）
- **X11 会话**（Wayland 下「电脑→手机」方向不可用，见「已知限制」）

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
                 gir1.2-gdkpixbuf-2.0 xdotool x11-utils libnotify-bin curl tar
```

托盘图标另外需要（可选，20KB）：

```bash
sudo apt install gir1.2-ayatanaappindicator3-0.1
```

---

## 安装

```bash
git clone <本仓库地址> phone-connect
cd phone-connect
./install.sh
```

安装脚本会自动：

- 找不到 `scrcpy` 就从官方 Release 下载静态包（约 18MB，**不需要 root**），校验 SHA256
- 把 `scrcpy` / `adb` 软链到 `~/.local/bin/`
- 安装命令行工具、守护进程、systemd 服务、桌面项、开机自启、托盘图标
- 检查会话类型（Wayland 会提示哪个功能不可用）

> 已经用发行版包装过 scrcpy 也行（`sudo apt install scrcpy adb`），脚本认得出来。

---

## 手机准备（只需一次，不需要线）

```
1. 设置 → 关于本机 → 版本信息 → 连点「版本号」7 次
2. 设置 → 系统设置 → 开发者选项 → 打开「无线调试」
   （USB 调试可以一直关着，不需要插线）
3. 点进「无线调试」→「使用配对码配对设备」，记下弹窗上的两个值
```

然后电脑上：

```bash
phone pair 192.168.1.50:37105 123456   # 配对地址 + 6 位配对码
phone use  192.168.1.50:40123          # 「无线调试」主界面的连接地址
```

**完事。** 以后开机登录桌面自动连上。

> 💡 建议在手机 WLAN 设置里给这台电脑的网络配一个**静态 IP**——IP 固定后基本不用再管连接。

---

## 使用

### 命令

```bash
phone status      # 连接状态 / 当前通道 / 服务
phone log         # 实时日志
phone recover     # ★ 连不上时先跑这个（自动找端口并固定）
phone screen      # 投屏窗口（可鼠标键盘反控手机）
phone photo       # 手动把手机最新一张照片拉到剪贴板
phone start|stop  # 启停后台服务
phone-gui         # 桌面客户端
```

### 配置

```bash
phone clipmode all|camera|off    # 哪些照片自动进剪贴板（默认 all）
phone pushclip always|safe|off   # 电脑→手机推送模式（默认 always）
```

配置文件在 `~/.local/opt/phonesync/config.json`，改完重启对应服务生效。

### 托盘图标

登录后在屏幕**右下角**（时间左边）：

- **灰色手机** = 待机
- **蓝色手机 + 圆点** = 手机刚同步了新内容 / 电脑刚推送了内容，闪 1.5 秒
- 右键菜单：打开客户端 / 清除提示 / 退出

### 连不上时

按顺序试：

```bash
1. phone recover          # 90% 情况这一条就够了
2. 看手机屏幕：设置 → 开发者选项 → 无线调试 → 「IP 地址和端口」
   phone use <那个地址>
3. 插一次 USB → phone wireless → 拔线
```

---

## 工作原理（简述）

**检测用事件而不是轮询。** 在手机上跑系统自带的 `inotifyd`，文件一写完就把事件推过来，延迟接近 0；轮询方案最坏要等满一个周期（实测 4.9 秒）。

**剪贴板图片预编码。** GTK3 是「粘贴时才现场编码」，12MP 照片要 2.3 秒（表现为卡死）。改成在后台线程预编码好字节，主线程只挂载，粘贴方读回从 2000+ms 降到 36ms。

**电脑→手机方向消除副作用。** scrcpy 收到剪贴板时除了设置剪贴板，还会往当前窗口注入一次 PASTE。给它加 `--display-id=99`（一个不存在的显示器），这次注入会被系统丢弃，而剪贴板照常设置。

**传输走 adb，不走网络文件协议。** 全部在 ADB 通道内完成，不在电脑上开任何端口。

---

## 常见问题

**Q：必须开着 USB 调试吗？**
不用。`adb_enabled=0` 也能跑 —— 无线调试有它自己的 `adbd` 开关，两者独立。

**Q：数据线插着会冲突吗？**
不会。两条通道并存时脚本会锁定序列号并**优先走无线**。

**Q：手机重启后要重新配对吗？**
不用。配对密钥存在手机里，只有**端口**会变，跑一次 `phone recover` 就能找回来。

**Q：手机屏幕上的「无线调试」开着，为什么就是连不上？**
`adb tcpip` 和「无线调试」是两种互斥的监听模式。如果之前跑过 `phone wireless`（等于 `adb tcpip 5555`），手机就只在 5555 明文端口监听，屏幕上的随机端口其实不存在。直接 `phone recover` 会自动试 5555。

**Q：走 Wi-Fi 传输安全吗？**
分两点看：

- **陌生人连不上你的手机** —— adb 有密钥认证（`ro.adb.secure=1`），未授权的连接会被拒绝，手机还会弹授权窗
- **但传统 tcpip 模式流量不加密** —— 同网段有人做 ARP 欺骗嗅探时，能看到明文内容。要求加密就用无线调试的 TLS 模式（`phone usb` → `phone recover --secure`），代价是端口随机

**Q：为什么不用 KDE Connect？**
它需要在手机装 App、在电脑开防火墙端口（要 sudo），而且拿不到完整短信库和相机原图。这个项目的前提就是**手机零安装**。

---

## 已知限制

- **Wayland 下「电脑→手机」不可用** —— 那一步靠 `xdotool` 往 scrcpy 窗口发按键，Wayland 不允许。其它功能正常。需要全功能请登录时选「Ubuntu on Xorg」。
- **手机重启后端口会变** —— `phone recover` 能找回；最可靠的办法是看手机屏幕上的地址。
- **代理 ARP 网络（校园网/企业网）下端口扫描不可靠** —— 网关会对任意端口回 SYN-ACK，产生假阳性。以 `adb connect` 结果为准。
- **剪贴板图片由守护进程持有** —— 停止服务后剪贴板里的图会失效（系统没有剪贴板管理器）。
- **短信只读** —— 能看到，不能从电脑回复。
- **手机端「无线调试」必须保持开启** —— 关掉后两条通道都会断。
- **Dock/任务栏图标不能实时变化** —— GNOME 缓存 `.desktop` 的图标，改文件不刷新，所以状态提示做在托盘上。

---

## 目录结构

```
bin/          7 个命令行工具
  phone           总入口
  phone-gui       客户端启动器
  phone-recover   深度恢复无线连接
  phone-tray      托盘状态图标
  phone-autoconnect / wait-for-adb / phone-daemon-run   服务辅助
phonesync/    4 个 Python 模块
  phone_sync.py      守护进程（短信 / 通知 / 照片）
  phone_gui.py       GTK4 客户端
  phone_pushclip.py  电脑→手机剪贴板
  find_port.py       端口扫描
systemd/      3 个用户服务
extras/       托盘图标 SVG
desktop/      桌面项模板
install.sh / uninstall.sh
```

运行时数据在 `~/.local/opt/phonesync/`：`config.json`、`wireless.env`、`events.jsonl`、`*.log`。
**这些是个人数据，已被 `.gitignore` 排除。**

---

## 卸载

```bash
./uninstall.sh           # 保留配置和日志
./uninstall.sh --purge   # 全部删掉
```

---

## 更新记录

见 [CHANGELOG.md](CHANGELOG.md)。

---

## 许可

MIT
