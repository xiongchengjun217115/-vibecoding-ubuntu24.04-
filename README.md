# phone-connect

把 Android 手机和 Linux 桌面连起来：**剪贴板双向同步 · 短信/通知实时镜像 · 拍照自动进剪贴板**。

全程 **不需要数据线**，也 **不需要在手机上装任何 App** —— 只用 Android 自带的「无线调试」。

```
手机复制  →  电脑 Ctrl+V          ✅
电脑复制  →  手机粘贴             ✅（无副作用）
手机拍照/截图  →  电脑剪贴板      ✅ 0.1~0.3 秒
手机短信/通知  →  桌面通知 + 客户端 ✅
```

---

## 特性

| 能力 | 说明 |
|---|---|
| **剪贴板双向同步** | 文字两个方向都实时同步 |
| **照片/截图 → 剪贴板** | 手机拍完照，电脑直接 Ctrl+V（事件级监听，实测 0.1~0.3 秒） |
| **短信同步** | 直读系统短信库，不比通知抓取更全 |
| **通知同步** | 实时镜像到桌面通知 + 客户端消息流 |
| **桌面客户端** | GTK4 + libadwaita，显示连接状态/剪贴板/消息，带总开关 |
| **完全无线** | 无线调试配对一次，之后永不需要数据线 |
| **端口自愈** | 手机重启后端口会变，自动扫描找回 |
| **开机自启** | 三个 systemd 用户服务 + 桌面客户端 |

---

## 环境要求

- Linux 桌面（GNOME，X11 或 Wayland 均可；剪贴板部分依赖 GTK）
- Python 3 + PyGObject（GTK4、libadwaita、GdkPixbuf）
- `xdotool`、`xwininfo`（用于向 scrcpy 隐藏窗口发按键）
- Android 11+（需要「无线调试」）
- scrcpy（本仓库不含二进制，安装脚本会引导下载官方静态包）

Ubuntu / Debian 依赖：

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
                 gir1.2-gdkpixbuf-2.0 xdotool x11-utils
```

---

## 安装

```bash
git clone <本仓库> phone-connect
cd phone-connect
./install.sh
```

安装脚本会：

1. 把 `bin/` 拷到 `~/.local/bin/`
2. 把 `phonesync/` 拷到 `~/.local/opt/phonesync/`
3. 生成 systemd 用户服务并 `enable`
4. 生成应用菜单项和开机自启项

依赖检查：如果没装 scrcpy，脚本会给出去官方静态包的指引（**不需要 root**）。

---

## 首次配置（只需一次，也不需要线）

1. 手机：`设置 → 关于本机 → 版本信息 → 连点「版本号」7 次`
2. 手机：`设置 → 系统设置 → 开发者选项 → 打开「无线调试」` ⚠️ **USB 调试可以一直关着**
3. 手机：点进「无线调试」→「**使用配对码配对设备**」，记下弹窗上的两个值
4. 电脑：

```bash
phone pair 192.168.1.50:37105 123456     # 配对地址 + 6 位配对码
phone use  192.168.1.50:40123            # 无线调试主界面上的「连接地址」
```

之后一切自动。

---

## 使用

```bash
phone status      # 连接状态 / 当前通道 / 服务状态
phone start       # 启动后台服务
phone stop        # 停止
phone log         # 实时看同步日志

phone screen      # 打开投屏窗口（可选，鼠标键盘可反控手机）
phone photo       # 手动把手机最新一张照片拉到剪贴板
phone find        # 扫描手机 IP 自动找回无线调试端口
phone clipmode    # 照片要不要自动进剪贴板（all / camera / off）
phone pushclip    # 电脑→手机推送模式（always / safe / off）
```

桌面客户端：应用菜单搜「**手机连接**」，或终端 `phone-gui`。

---

## 工作原理（几个值得说的点）

### 1. 检测：事件级监听，不是轮询

轮询 MediaStore 最坏要等满一个周期（实测 4.9 秒）。改成在手机上跑自带的 `inotifyd`：

```
adb exec-out inotifyd - /sdcard/DCIM/Camera:nwy /sdcard/Pictures/Screenshots:nwy
```

`w` = IN_CLOSE_WRITE（文件写完）、`y` = IN_MOVED_TO（FUSE 改名到位）。
事件经 adb 流实时推过来，**检测延迟实测 0.000 秒**，剩下的只有传输时间。

### 2. 剪贴板图片：预编码，避免"粘贴卡死"

GTK3 的 `gtk_clipboard_set_image` 是**粘贴时才现场编码**：12MP 照片编码成 PNG 要 **2270ms / 10.9MB**，表现就是粘贴卡死。

改成 GTK4 的 `Gdk.ContentProvider.new_for_bytes`，把「编码」和「挂剪贴板」拆开：

```
后台线程：解码 + 预编码（PNG compression=1 → 482ms，或 JPEG q90 → 30ms）
主线程  ：只挂预编码字节                        → 0.4ms
```

粘贴方读回从 **2000+ms 降到 36ms**。

同时提供 `image/png` + `image/jpeg` 两种格式 —— 只给 JPEG 会让"只认 PNG"的应用粘贴不到东西。

### 3. 电脑 → 手机剪贴板：如何去掉副作用

scrcpy 的「电脑→手机」剪贴板同步只绑定在「在窗口里按 Ctrl+V」这个动作上。
而服务端源码里它做了两件事：

```java
boolean ok = Device.setClipboardText(text);     // ① 设置剪贴板 ← 要的
if (ok) Ln.i("Device clipboard set");

if (paste && ...) {
    pressReleaseKeycode(KEYCODE_PASTE, ...);    // ② 注入粘贴 ← 不想要的
}
```

关键在于 `pressReleaseKeycode` 走的是 `getActionDisplayId()`，而它返回的就是 `--display-id` 的值。

**所以给 scrcpy 加上 `--display-id=99`（一个不存在的显示器）**，粘贴键事件会被系统直接丢弃：

```
I InputDispatcher: Dropping KEY event because there is no focused window
                   or focused application in display 99.
[server] INFO: Device clipboard set          ← 剪贴板照常设置
```

手机界面完全不受影响。窗口本身用 `xdotool windowunmap` 藏起来（实测 unmap 后仍能收到合成的按键事件）。

### 4. 端口自愈

无线调试的端口每次重开都变，而校园网/企业网常屏蔽 mDNS 组播，手机也不回应单播查询。
所以：**主动扫描手机 IP 的临时端口段**（32768–60999，约 16 秒），找到就更新配置。

扫描要分块 + 重试 —— 一次性扔几万个 SYN 会把手机/AP 打到限速（表现为丢包），
目标端口反而会被漏掉。

---

## 已知限制

- **手机重启后**无线调试会重开、配对仍有效，但端口会变，需要重新发现（通常服务会自愈，实在不行 `phone find`）
- 剪贴板图片由守护进程持有，**停止服务后剪贴板里的图会失效**（系统里没有剪贴板管理器）
- 短信是**只读**同步，不能从电脑回复
- `--display-id=99` 依赖 scrcpy 的行为，升级 scrcpy 后建议重新验证
- 手机端需要保持「无线调试」开启；关闭后两条通道（USB 和无线）都会断，因为都依赖 `adbd`

---

## 目录结构

```
bin/         命令行工具（phone / phone-gui / phone-autoconnect / wait-for-adb / phone-daemon-run）
phonesync/   守护进程（phone_sync.py / phone_gui.py / phone_pushclip.py / find_port.py）
systemd/     三个用户服务单元
desktop/     应用菜单项与自启项模板
install.sh   安装
uninstall.sh 卸载
```

运行时数据在 `~/.local/opt/phonesync/`：
`config.json`（配置）、`wireless.env`（无线地址）、`events.jsonl`（消息流）、`*.log`。
**这些都属于个人数据，不要提交到仓库。**

---

## 许可

MIT
