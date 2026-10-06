#!/usr/bin/env python3
"""
find_port.py — 扫描手机 IP，自动找出 adb 无线调试端口

背景：无线调试的端口每次重启都会变，而校园网屏蔽 mDNS 组播，
      手机也不回应单播 mDNS 查询，所以只能主动扫描。

用法：
  find_port.py                      # 扫描默认范围
  find_port.py --range 43000-44000  # 指定范围（快）
  find_port.py --ip 192.168.1.50 --range 32768-60999
"""

import argparse
import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

ADB = str(Path.home() / ".local/bin/adb")


async def probe(ip, port, sem, timeout):
    async with sem:
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
            return port
        except Exception:
            return None


async def scan(ip, lo, hi, conc, timeout, progress, chunk=1500, pause=0.15):
    total = hi - lo + 1
    done = 0
    found = []
    # 分块扫描：一次性扔几万个 SYN 会把手机/AP 打到限速（实测目标端口会被漏掉），
    # 所以按 chunk 分块、块间短暂停顿，命中即停
    for start in range(lo, hi + 1, chunk):
        end = min(start + chunk - 1, hi)
        sem = asyncio.Semaphore(conc)
        tasks = [asyncio.ensure_future(probe(ip, p, sem, timeout)) for p in range(start, end + 1)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        found += [r for r in results if isinstance(r, int)]
        done += end - start + 1
        if progress:
            print(f"\r  扫描中… {done}/{total}  已发现 {len(found)}", end="", flush=True)
        if found:
            break
        await asyncio.sleep(pause)
    if progress:
        print("\r" + " " * 50 + "\r", end="")
    return sorted(found)


def adb_devices():
    p = subprocess.run([ADB, "devices"], capture_output=True, timeout=20)
    out = []
    for ln in p.stdout.decode().splitlines()[1:]:
        parts = ln.split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=None, help="手机 IP（默认从 wireless.env 读）")
    ap.add_argument("--range", default="32768-60999", help="端口范围 lo-hi")
    ap.add_argument("--conc", type=int, default=600, help="并发数")
    ap.add_argument("--timeout", type=float, default=0.6, help="单端口超时(秒)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--retries", type=int, default=3, help="整轮扫描的重试次数")
    args = ap.parse_args()

    ip = args.ip
    if not ip:
        env = Path.home() / ".local/opt/phonesync/wireless.env"
        try:
            for line in env.read_text().splitlines():
                if line.strip().startswith("WIRELESS_IP="):
                    ip = line.split("=", 1)[1].strip()
        except Exception:
            pass
    if not ip:
        print("✗ 不知道手机 IP，用 --ip 指定", file=sys.stderr)
        return 2

    lo, hi = (int(x) for x in args.range.split("-"))
    t0 = time.time()
    ports = []
    # 突发 SYN 会被手机/校园 AP 限速（表现为丢包而非拒绝），冷却后再来一轮即可
    for attempt in range(1, args.retries + 1):
        if not args.quiet:
            print(f"扫描 {ip} 端口 {lo}-{hi}（并发 {args.conc}，第 {attempt}/{args.retries} 轮）…")
        ports = asyncio.run(scan(ip, lo, hi, args.conc, args.timeout, not args.quiet))
        if ports:
            break
        if attempt < args.retries:
            if not args.quiet:
                print("  本轮无发现（疑似被打限速），4 秒后重试…")
            time.sleep(4)
    print(f"  耗时 {time.time()-t0:.1f}s，开放端口: {ports if ports else '无'}")

    # 逐个尝试 adb connect，找到真正的 adb 端口
    before = set(adb_devices())
    for p in ports:
        addr = f"{ip}:{p}"
        if addr in before:
            print(f"  已连接: {addr}")
            return 0
        subprocess.run([ADB, "connect", addr], capture_output=True, timeout=20)
        time.sleep(0.6)
        for d in adb_devices():
            if d == addr:
                print(f"  ✓ 找到 adb 无线调试端口: {addr}")
                return 0
        subprocess.run([ADB, "disconnect", addr], capture_output=True, timeout=20)

    print("  ✗ 范围内没找到可用的 adb 端口")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
