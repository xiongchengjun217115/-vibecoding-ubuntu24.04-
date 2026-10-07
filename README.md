# 基于 vibecoding 的 ubuntu-android 剪贴板同步功能的实现

> 一根数据线都不用，手机上也不用装任何 App。
> 只靠 Android 自带的「无线调试」，把手机和 Ubuntu 焊在一起。

<p align="center">
  <code>手机复制</code> →→→ <code>电脑 Ctrl+V</code>
  &nbsp;&nbsp;·&nbsp;&nbsp;
  <code>电脑复制</code> →→→ <code>手机粘贴</code>
  <br>
  <code>咔嚓一张</code> →→→ <code>0.2 秒后已在你剪贴板里</code>
</p>

---

## 先说结论：快照

```
$ phone status
── 当前使用的通道 ──
  ✓ 无线  192.168.1.50:44747
  已记录无线 IP: 192.168.1.50

$ # 手机上按下截图键，然后在电脑上 Ctrl+V

18:35:28 [照片] 已保存 20261006-183527-IMG20261006183526.jpg (2693 KB，事件→落盘 921ms)
18:35:28 [照片] ✓ 已复制到剪贴板 4096x3072 [png+jpeg]，总耗时 922ms
```

**从手机落盘到电脑能粘贴：0.1 ~ 0.9 秒。** 不是"刷新一下"，是真正的秒级。

---

## 这玩意儿是怎么来的

起因是一个很朴素的问题：*"手机连蓝牙能干什么？能不能同步剪贴板？"*

然后就会发现一路上全是坑：

- 蓝牙协议栈里**根本没有剪贴板这个东西**
- 安卓生态里那些"跨设备复制粘贴"，底层都是 Wi-Fi + 厂商私有协议
- Linux 上正经的替代方案（KDE Connect）要装 App、要开防火墙端口、要 root
- 好用的商业方案（OPPO 互联之类）**压根没有 Linux 版**
- 想走 ADB 无线，结果校园网把 mDNS 组播**屏蔽了**
- 想省事用轮询，结果**延迟 4.9 秒**
- 改用事件监听，结果 12MP 照片**粘贴时卡死 2.3 秒**
- 好不容易作通了，发现电脑→手机方向**会把内容粘到手机当前界面上**

于是就有了这个项目。**每一个坑都在下面的「踩坑记」里，附实测数据和最终解法。**

---

## 能力清单

| 能力 | 状态 | 实测数据 |
|---|:---:|---|
| 手机复制 → 电脑粘贴 | ✅ | 双向文字实时同步 |
| 电脑复制 → 手机粘贴 | ✅ | **零副作用**（见下方原理） |
| 拍照 / 截图 → 电脑剪贴板 | ✅ | **0.1 ~ 0.9 秒** |
| 短信同步 | ✅ | 直读系统短信库，比抓通知更全 |
| 通知镜像 | ✅ | 秒级 + 桌面弹窗 |
| 桌面客户端 | ✅ | GTK4 + libadwaita |
| 投屏 + 反控手机 | ✅ | scrcpy 自带 |
| 开机自启 | ✅ | 3 个 systemd 用户服务 + 客户端 |
| **数据线** | ❌ | **完全不需要** |
| **手机装 App** | ❌ | **一个都不用装** |
| **root / sudo** | ❌ | **安装不需要** |

---

## 快速开始

### 第 0 步：装依赖（唯一需要 sudo 的一步）

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
                 gir1.2-gdkpixbuf-2.0 xdotool x11-utils libnotify-bin curl tar
```

### 第 1 步：一条命令安装

```bash
git clone https://github.com/xiongchengjun217115/-vibecoding-ubuntu24.04-.git
cd -- -vibecoding-ubuntu24.04-
./install.sh
```

安装脚本会自动：

- 找不到 scrcpy 就从官方 Release 下静态包（18MB，**不需要 root**），校验 SHA256
- 把 scrcpy / adb 软链到 `~/.local/bin/`
- 铺命令行工具、守护进程、systemd 服务、桌面项、自启项
- 检查会话类型（Wayland 会提示哪个功能不可用）

> 用发行版装 scrcpy 也行（`sudo apt install scrcpy adb`），脚本认得出来。

### 第 2 步：手机开无线调试（只需一次，也不需要线）

```
设置 → 关于本机 → 版本信息 → 连点「版本号」7 次
设置 → 系统设置 → 开发者选项 → 打开「无线调试」      ← USB 调试可以一直关着
       └→ 点「使用配对码配对设备」，记下弹窗上的两个值
```

### 第 3 步：配对

```bash
phone pair 192.168.1.50:37105 123456   # 配对地址 + 6 位配对码
phone use  192.168.1.50:40123          # 「无线调试」主界面的连接地址
```

**完事。** 以后开机登录桌面自动连上，什么都不用管。

---

## 它是怎么工作的

### ① 检测：不用轮询，用手机自己的 inotifyd

轮询相册最坏要等满一个周期（实测 **4.9 秒**）。改成在手机上跑 Android 自带的 `inotifyd`：

```bash
adb exec-out inotifyd - /sdcard/DCIM/Camera:nwy /sdcard/Pictures/Screenshots:nwy
```

`w` = `IN_CLOSE_WRITE`（写完落盘）、`y` = `IN_MOVED_TO`（FUSE 改名到位）。
事件经 adb 流实时推过来：

```
落盘 → 事件到达电脑 : 0.000 秒      ← 检测延迟基本为零
拉取 411 KB        : 0.23 秒       ← 剩下的全是 Wi-Fi 传输
```

### ② 剪贴板图片：把「编码」和「挂剪贴板」拆开

这是最坑的一环。GTK3 的 `gtk_clipboard_set_image` 是**粘贴时才现场编码**：

| 操作 | 耗时 | 产物 |
|---|---|---|
| 解码 JPEG | 44 ms | — |
| **编码 PNG（GTK3 粘贴时做）** | **2270 ms** | **10.9 MB** |
| 编码 JPEG q90 | 30 ms | 1.26 MB |

12MP 照片粘贴一次卡 2.3 秒 —— 这就是"卡死"的真相。

改用 GTK4 的 `Gdk.ContentProvider.new_for_bytes` 后：

```
后台线程：解码 + 预编码  →  2352 ms（不在关键路径上）
主线程  ：只挂预编码字节  →     0.4 ms
```

**粘贴方读回：2000+ms → 36ms（快 53 倍）。**

格式上同时提供 `image/png` + `image/jpeg`，PNG 用 `compression=1`（482ms 而非默认的 2280ms）。
只给 JPEG 会让"只认 PNG"的应用粘贴不到东西 —— 这个坑也踩过。

### ③ 电脑 → 手机：如何做到零副作用

scrcpy 的「电脑→手机」剪贴板同步**只绑定在"在窗口里按 Ctrl+V"这个动作上**。
而看服务端源码会发现它做了两件事：

```java
boolean ok = Device.setClipboardText(text);     // ① 设置剪贴板 ← 要的
if (ok) Ln.i("Device clipboard set");

if (paste && ...) {
    pressReleaseKeycode(KEYCODE_PASTE, ...);    // ② 注入粘贴 ← 不想要的
}
```

关键在于 `pressReleaseKeycode` 走的是 `getActionDisplayId()`，而它**返回的就是 `--display-id` 的值**。

于是给 scrcpy 加上 `--display-id=99`（一个不存在的显示器）—— 粘贴键会被系统直接丢弃：

```
I InputDispatcher: Dropping KEY event because there is no focused window
                   or focused application in display 99.
W InputDispatcher: Asynchronous input event injection failed.
[server] INFO: Device clipboard set          ← 剪贴板照常设置成功
```

**手机界面完全不受影响。** 窗口本身用 `xdotool windowunmap` 藏起来（实测 unmap 后仍能收到合成按键）。

### ④ 端口自愈：主动扫描，但要温柔

无线调试的端口每次重开都变，而校园网/企业网常屏蔽 mDNS 组播，手机也不回应单播查询（QU 位试过，超时）。

所以只能主动扫段。但这里又有个坑：

```
窄范围扫描 43000-44000  : 10/10 成功
全范围扫描 32768-60999  :  0/3  成功，且耗时 34 秒
```

失败时耗时翻倍 = 包被**丢弃**而非拒绝 = 触发了手机/AP 的限速冷却。
解法是**分块扫描**（1500 端口/块 + 块间停顿）**+ 整轮重试**，之后 4/4 成功，约 16 秒。

---

## 踩坑记

一共七个真 Bug，每一个都值得记下来：

| # | 症状 | 根因 | 解法 |
|---|---|---|---|
| 1 | 守护进程显示 active 但什么都不干 | USB + 无线两条通道并存时 `adb shell` 报 `more than one device` | 所有调用锁定序列号，优先无线 |
| 2 | 粘贴卡死 2 秒+ | GTK3 粘贴时现场编码 12MP PNG | GTK4 预编码字节 |
| 3 | 照片进了剪贴板但粘不出来 | 上一条优化成"大图只给 JPEG"，而目标应用只要 PNG | 两种格式都给 |
| 4 | 同一张图被处理两次 | Android FUSE 先写 `.pending-<数字>-真名` 再改名，两个名字各触发一次事件 | 文件名归一化去重 |
| 5 | 延迟忽大忽小（151ms → 700ms） | 加参数时漏改 lambda 签名，inotify 一直在崩溃重连，实际靠轮询兜底 | 修签名（被"有兜底"掩盖了） |
| 6 | 电脑→手机推送会粘到手机界面上 | scrcpy 把 SET_CLIPBOARD 和 PASTE 注入绑成一个动作 | `--display-id=99` 让注入落空 |
| 7 | 一晚刷了 **68570 行日志 / 8.8MB** | 重连用的 `time.sleep(5)` 只写在 `except` 分支里；设备断开时 `inotifyd` 是**正常退出**（不抛异常）→ 无停顿 → 死循环 | `sleep` 移到循环末尾；日志只在状态切换时打；再加 5MB 体积兜底 |

**第 5 条最阴险** —— 因为有 5 秒轮询兜底，功能"看起来是好的"，只是慢。要不是去量真实延迟，根本发现不了。

**第 7 条最惨烈** —— 死循环反复 spawn `adb` 进程 6.8 万次，**把 adb server 也拖垮了**，
表现出来却是"手机连不上"。排查时差点一直往网络方向找。教训：**日志暴涨本身就是故障信号，不是副作用。**

---

## 已知限制

- **Wayland 下「电脑→手机」不可用** —— 那一步依赖 `xdotool` 给 scrcpy 窗口发按键，Wayland 不允许。
  手机→电脑、照片、短信通知都正常。需要全功能就登录时选「Ubuntu on Xorg」。
- **手机重启／重开无线调试后端口会变** —— 服务会尝试自动扫描找回，但**校园网/企业网下扫描并不可靠**（见下条）。
  **最可靠的办法是直接看手机屏幕**：设置 → 开发者选项 → 无线调试 → 「IP 地址和端口」，
  然后 `phone use <那个地址>`。配对密钥是持久的，**换端口不需要重新配对**。
- **代理 ARP 网络会让端口扫描出现假阳性** —— 实测同一台主机连续三次扫描得到三个不同结果：

  ```
  第 1 次: 开放端口 [33215]
  第 2 次: 开放端口 [18218]
  第 3 次: 开放端口 [64660]
  ```

  这是网关对任意端口的 SYN 都回 SYN-ACK 造成的。所以 `phone find` 的输出仅供参考，
  最终以 `adb connect` 的结果为准。另外**手机「无线调试」没开时扫描毫无意义** —— 先确认它是开着的。
- **剪贴板图片由守护进程持有** —— 停止服务后剪贴板里的图会失效（系统里没有剪贴板管理器）。
- **短信是只读同步** —— 能看到，不能从电脑回复。
- **手机端「无线调试」必须保持开启** —— 关掉后两条通道都会断（USB 和无线都依赖 `adbd`）。
- **`--display-id=99` 依赖 scrcpy 的行为** —— 升级 scrcpy 后建议重新验证。

---

## 常用命令

```bash
phone status      # 连接状态 / 当前通道 / 服务
phone log         # 实时日志
phone screen      # 投屏 + 鼠标键盘反控手机
phone photo       # 手动把最新一张照片拉到剪贴板
phone find        # 扫描手机 IP 找回无线调试端口
phone clipmode    # 照片要不要自动进剪贴板（all / camera / off）
phone pushclip    # 电脑→手机推送模式（always / safe / off）
phone start|stop  # 启停后台服务
phone-gui         # 桌面客户端
```

---

## FAQ

**Q：必须开着 USB 调试吗？**
不用。`adb_enabled=0` 也能跑 —— 无线调试有它自己的 `adbd` 开关，两者独立。

**Q：数据线插着也没事吧？**
没事。两条通道并存时脚本会自动锁定序列号并**优先走无线**。

**Q：会被别人看到我的短信吗？**
不会。所有数据都在本机 `~/.local/opt/phonesync/`，走的是 ADB 本地通道。
仓库里 `.gitignore` 挡住了所有日志和状态文件 —— 那些才含短信原文。

**Q：手机重启后要重新配对吗？**
不用。配对密钥存在手机里，无线调试开关也是持久化设置。只有**端口**会变，服务会自己扫回来。

**Q：为什么不用 KDE Connect？**
它是好方案，但需要在手机上装 App、在我们这边开 ufw 端口（要 sudo），而且功能没有 ADB 全
（比如拿不到完整的短信库和相机原图）。这个项目的前提就是**手机零安装**。

---

## 目录结构

```
bin/         5 个命令行工具
phonesync/   4 个 Python 模块（约 2500 行）
systemd/     3 个用户服务
desktop/     桌面项模板
install.sh   安装（自动搞定 scrcpy）
uninstall.sh 卸载
```

运行时数据在 `~/.local/opt/phonesync/`：
`config.json` · `wireless.env` · `events.jsonl` · `*.log`
**这些都属于个人数据，已被 `.gitignore` 排除。**

---

## 卸载

```bash
./uninstall.sh           # 保留配置和日志
./uninstall.sh --purge   # 全部删掉
```

---

## 许可

MIT
