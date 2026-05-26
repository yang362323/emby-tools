#!/usr/bin/env python3
"""Emby 媒体库去重工具 — 仅对 /media/动漫/A/ 目录下的资源生效。

规则:
  1. 版本优先级: UC > C > U > 其他
  2. .strm 文件直接删除
  3. 多分卷资源（同 base 编号序列）全部删除
  4. 同版本多处出现时保留最新上传的
"""

import sqlite3
import os
import sys
import re
import json
import urllib.request
import urllib.error
from collections import defaultdict, Counter

# === 配置 ===
EMBY_URL = "http://localhost:8097"
EMBY_API_KEY = "83fc43765f704eb4b8334845f9ed1396"
LIBRARY_DB = "/docker/emby/config/data/library.db"
MEDIA_PATH = "/media/动漫/A/"
HOST_MEDIA_PATH = "/home/yang362323/users/vedio/动漫/A/"
PATH_MAPPING = {
    "/media/动漫/A/": HOST_MEDIA_PATH,
}

DRY_RUN = "--dry-run" in sys.argv
VERSION_LABELS = {0: "UC", 1: "C", 2: "其他", 3: "U", 4: "副本(1)"}
VERSION_PRIORITY = {"UC": 0, "C": 1, "other": 2, "U": 3, "dup": 4}


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
        body_text = e.read().decode(errors="replace")
        print(f"  API 错误 {e.code}: {body_text[:200]}")
        return None


def container_to_host_path(container_path):
    for prefix, host_prefix in PATH_MAPPING.items():
        if container_path.startswith(prefix):
            return os.path.join(host_prefix, os.path.relpath(container_path, prefix))
    return container_path


def get_version(filename):
    """从文件名检测版本类型。返回 (priority, label)"""
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
    """检测是否为多分卷资源。规则：group 中存在 2+ 文件，其文件名
    共享同一 base 前缀且以 -N 结尾（N 为 1~2 位数字），且数字各不相同。"""
    if len(items) < 2:
        return False

    names = [os.path.splitext(os.path.basename(it["Path"]))[0] for it in items]
    base_nums = defaultdict(set)
    for n in names:
        m = re.match(r'^(.+)-(\d{1,2})$', n)
        if m:
            base_nums[m.group(1)].add(int(m.group(2)))

    # 同一 base 下存在 >= 2 个不同数字 → 多分卷
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


def delete_file(host_path):
    if not os.path.exists(host_path):
        print(f"    文件不存在，跳过: {host_path}")
        return True
    if DRY_RUN:
        print(f"    [DRY-RUN] 将删除: {host_path}")
        return True
    try:
        os.remove(host_path)
        print(f"    已删除: {host_path}")
        return True
    except OSError as e:
        print(f"    删除失败: {host_path} — {e}")
        return False


def notify_emby_updated(deleted_paths):
    updates = [{"Path": p, "UpdateType": "Deleted"} for p in deleted_paths]
    api_request("POST", "/emby/Library/Media/Updated", {"Updates": updates})
    print("  通知 Emby 更新已完成")


def main():
    print("=== Emby 媒体库去重工具 ===")
    print(f"目标: {MEDIA_PATH}")
    print(f"规则: UC > C > U > 其他 | .strm 直接删除 | 多分卷全部删除")
    if DRY_RUN:
        print("模式: DRY-RUN（仅预览，不实际删除）")
    print()

    print("[1/3] 正在查询 Emby 库数据库...")
    duplicates = find_duplicates()
    total_items = sum(len(v) for v in duplicates.values())
    print(f"  发现 {len(duplicates)} 组重复资源（共 {total_items} 个文件）\n")

    if not duplicates:
        print("未发现重复资源，退出。")
        return

    print("[2/3] 分析重复资源...\n")
    delete_tasks = []
    keep_items = []
    multipart_groups = 0

    for name, items in sorted(duplicates.items()):
        # 标记每条目的版本
        for it in items:
            priority, label = get_version(it["Path"])
            it["_version_priority"] = priority
            it["_version_label"] = label

        print(f"  [{name}] ({len(items)} 个文件)")

        # 规则2: .strm 直接删除
        strm_items = [it for it in items if is_strm(it["Path"])]
        non_strm = [it for it in items if not is_strm(it["Path"])]

        for it in strm_items:
            print(f"    [.strm] 删除: {it['Path']}")
            host_path = container_to_host_path(it["Path"])
            delete_tasks.append({
                "container_path": it["Path"],
                "host_path": host_path,
                "name": name,
                "reason": ".strm"
            })

        if not non_strm:
            continue

        # 规则3: 多分卷检测（全部删除）
        if detect_multi_part(non_strm):
            multipart_groups += 1
            print(f"    [多分卷] 全部删除:")
            for it in non_strm:
                print(f"      {it['Path']} (大小: {it['Size']})")
                host_path = container_to_host_path(it["Path"])
                delete_tasks.append({
                    "container_path": it["Path"],
                    "host_path": host_path,
                    "name": name,
                    "reason": "多分卷"
                })
            continue

        # 规则1: 按版本优先级选择保留
        non_strm.sort(key=lambda x: (x["_version_priority"], -x["DateCreated"]))
        keep = non_strm[0]
        to_delete = non_strm[1:]

        keep_items.append(keep)
        print(f"    保留 [{keep['_version_label']}]: {keep['Path']} (上传: {keep['DateCreated']})")

        for it in to_delete:
            reason = f"版本 {it['_version_label']} < {keep['_version_label']}" \
                     if it["_version_priority"] > keep["_version_priority"] else "更旧"
            print(f"    删除 [{it['_version_label']}]: {it['Path']} ({reason})")
            host_path = container_to_host_path(it["Path"])
            delete_tasks.append({
                "container_path": it["Path"],
                "host_path": host_path,
                "name": name,
                "reason": reason
            })

    # 统计
    total_del_size = sum(t["_size"] if "_size" in t else 0 for t in delete_tasks)
    print(f"\n  将保留 {len(keep_items)} 个文件")
    print(f"  将删除 {len(delete_tasks)} 个文件")
    if multipart_groups:
        print(f"  其中多分卷组: {multipart_groups} 组")

    if DRY_RUN:
        print("\n[3/3] [DRY-RUN] 跳过实际删除和通知。")
        return

    print("\n[3/3] 正在执行...")
    confirm = input(f"  确认删除 {len(delete_tasks)} 个文件？[y/N] ")
    if confirm.lower() != "y":
        print("  已取消。")
        return

    deleted_paths = []
    failed = 0
    for i, task in enumerate(delete_tasks, 1):
        print(f"  [{i}/{len(delete_tasks)}] [{task['reason']}] {task['name']}")
        if delete_file(task["host_path"]):
            deleted_paths.append(task["container_path"])
        else:
            failed += 1

    print(f"\n  完成: 成功 {len(deleted_paths)} 个, 失败 {failed} 个")

    if deleted_paths:
        print("\n正在通知 Emby 服务器更新媒体库...")
        notify_emby_updated(deleted_paths)

    print("\n=== 去重完成 ===")


if __name__ == "__main__":
    main()
