#!/usr/bin/env python3
"""Emby 媒体库去重守护进程 — 后台监看，新资源入库刮削完成后自动去重。

通过监控 Emby library.db 的变更，当检测到库有更新后等待刮削冷却期，
然后自动执行去重。仅对 /media/动漫/A/ 目录生效。

用法:
  python3 dedup_daemon.py              # 前台运行
  python3 dedup_daemon.py --daemon     # 后台守护（配合 systemd 或 nohup）
"""

import sqlite3
import os
import sys
import re
import json
import time
import urllib.request
import urllib.error
import logging
from datetime import datetime
from collections import defaultdict, Counter

# === 配置 ===
EMBY_URL = "http://localhost:8097"
EMBY_API_KEY = "83fc43765f704eb4b8334845f9ed1396"
LIBRARY_DB = "/docker/emby/config/data/library.db"
MEDIA_PATH = "/media/动漫/A/"
HOST_MEDIA_PATH = "/home/yang362323/users/vedio/动漫/A/"
PATH_MAPPING = {"/media/动漫/A/": HOST_MEDIA_PATH}

# 冷却时间：Emby 刮削完成后等待多久再检查（秒）
SCRAPE_COOLDOWN = 300   # 5 分钟
# 轮询间隔（秒）
POLL_INTERVAL = 60      # 1 分钟
# 日志文件
LOG_FILE = "/var/log/emby_dedup_daemon.log"

VERSION_PRIORITY = {"UC": 0, "C": 1, "other": 2, "U": 3, "dup": 4}

log = logging.getLogger("dedup_daemon")


def api_request(method, endpoint, body=None):
    url = f"{EMBY_URL}{endpoint}"
    headers = {"X-Emby-Token": EMBY_API_KEY, "Accept": "application/json"}
    data = json.dumps(body).encode() if body else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()) if resp.status != 204 else None
    except urllib.error.HTTPError as e:
        log.error(f"API 错误 {e.code}: {e.read().decode(errors='replace')[:200]}")
        return None


def container_to_host_path(container_path):
    for prefix, host_prefix in PATH_MAPPING.items():
        if container_path.startswith(prefix):
            return os.path.join(host_prefix, os.path.relpath(container_path, prefix))
    return container_path


def get_version(filename):
    stem = os.path.splitext(os.path.basename(filename))[0]
    if re.search(r'[-_]UC$', stem, re.IGNORECASE):
        return VERSION_PRIORITY["UC"], "UC"
    if re.search(r'[-_]C$', stem, re.IGNORECASE):
        return VERSION_PRIORITY["C"], "C"
    if re.search(r'[-_]U$', stem, re.IGNORECASE):
        return VERSION_PRIORITY["U"], "U"
    if re.search(r'\(\d+\)$', stem):
        return VERSION_PRIORITY["dup"], "副本"
    return VERSION_PRIORITY["other"], "其他"


def is_strm(path):
    return path.lower().endswith(".strm")


def detect_multi_part(items):
    if len(items) < 2:
        return False
    names = [os.path.splitext(os.path.basename(it["Path"]))[0] for it in items]
    base_nums = defaultdict(set)
    for n in names:
        m = re.match(r'^(.+)-(\d{1,2})$', n)
        if m:
            base_nums[m.group(1)].add(int(m.group(2)))
    return any(len(nums) >= 2 for nums in base_nums.values())


def find_duplicates():
    conn = sqlite3.connect(LIBRARY_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT Id, Name, Path, Size, DateCreated, Container "
        "FROM MediaItems "
        "WHERE Path LIKE ? AND type = 5 "
        "ORDER BY Name, DateCreated DESC",
        (MEDIA_PATH + "%",),
    )
    groups = defaultdict(list)
    for row in cur.fetchall():
        groups[row["Name"]].append(dict(row))
    conn.close()
    return {name: items for name, items in groups.items() if len(items) > 1}


def run_dedup():
    """执行一次去重，返回删除数量。"""
    duplicates = find_duplicates()
    if not duplicates:
        return 0

    delete_tasks = []

    for name, items in duplicates.items():
        for it in items:
            priority, label = get_version(it["Path"])
            it["_version_priority"] = priority
            it["_version_label"] = label

        strm_items = [it for it in items if is_strm(it["Path"])]
        non_strm = [it for it in items if not is_strm(it["Path"])]

        for it in strm_items:
            delete_tasks.append({
                "container_path": it["Path"],
                "host_path": container_to_host_path(it["Path"]),
                "name": name,
            })

        if not non_strm:
            continue

        if detect_multi_part(non_strm):
            for it in non_strm:
                delete_tasks.append({
                    "container_path": it["Path"],
                    "host_path": container_to_host_path(it["Path"]),
                    "name": name,
                })
            continue

        non_strm.sort(key=lambda x: (x["_version_priority"], -x["DateCreated"]))
        for it in non_strm[1:]:
            delete_tasks.append({
                "container_path": it["Path"],
                "host_path": container_to_host_path(it["Path"]),
                "name": name,
            })

    if not delete_tasks:
        return 0

    log.info(f"发现 {len(duplicates)} 组重复，将删除 {len(delete_tasks)} 个文件")

    deleted = 0
    failed = 0
    deleted_paths = []

    for task in delete_tasks:
        host_path = task["host_path"]
        if not os.path.exists(host_path):
            continue
        try:
            os.remove(host_path)
            deleted_paths.append(task["container_path"])
            deleted += 1
        except OSError as e:
            log.error(f"删除失败: {host_path} — {e}")
            failed += 1

    log.info(f"删除完成: 成功 {deleted}, 失败 {failed}")

    if deleted_paths:
        updates = [{"Path": p, "UpdateType": "Deleted"} for p in deleted_paths]
        api_request("POST", "/emby/Library/Media/Updated", {"Updates": updates})
        log.info("已通知 Emby 更新")

    return deleted


def get_total_items():
    """获取当前库中 A 目录下的视频总数。"""
    try:
        conn = sqlite3.connect(LIBRARY_DB)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM MediaItems WHERE Path LIKE ? AND type = 5",
            (MEDIA_PATH + "%",),
        )
        count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def main():
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ]
    )

    log.info("=== Emby 去重守护进程启动 ===")
    log.info(f"目标: {MEDIA_PATH}")
    log.info(f"冷却时间: {SCRAPE_COOLDOWN}s, 轮询间隔: {POLL_INTERVAL}s")

    last_db_mtime = 0
    last_scan_time = 0
    last_count = get_total_items()
    stable_since = 0  # 库不再变化的时间戳

    while True:
        try:
            # 检查 library.db 修改时间
            current_mtime = os.path.getmtime(LIBRARY_DB)
            current_count = get_total_items()

            if current_mtime != last_db_mtime or current_count != last_count:
                # 库有变化，记录变化时间
                stable_since = time.time()
                last_db_mtime = current_mtime
                last_count = current_count
                log.debug(f"库已更新 (items: {current_count})，等待冷却...")

            # 检查是否可以扫描
            if stable_since > 0:
                elapsed = time.time() - stable_since
                if elapsed >= SCRAPE_COOLDOWN and stable_since > last_scan_time:
                    log.info("冷却完成，开始检查重复...")
                    deleted = run_dedup()
                    if deleted > 0:
                        log.info(f"本轮去重完成，删除 {deleted} 个文件")
                    else:
                        log.info("未发现重复")
                    last_scan_time = time.time()
                    stable_since = 0  # 重置

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log.info("收到退出信号，守护进程停止")
            break
        except Exception as e:
            log.error(f"异常: {e}", exc_info=True)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        # 后台化
        pid = os.fork()
        if pid > 0:
            print(f"守护进程已启动，PID: {pid}")
            sys.exit(0)
        os.setsid()
        sys.stdout = open(LOG_FILE, "a")
        sys.stderr = sys.stdout
    main()
