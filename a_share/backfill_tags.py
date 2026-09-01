#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史成交 tag 回填脚本
=====================
背景：北京 DRY_RUN 实例行情来自离线缓存，没有 Gamma 原生 category，
      历史成交的 tag 是关键词回退值（crypto/economy/...）。本脚本把每笔
      成交的 tag 用 Gamma 真实 category 回填，使「按类别锁利汇总」覆盖
      全历史真实分布，而非「旧回退 + 新真实」混着。

回填键（优先级）：
  1. 成交记录带 token_id        -> 用 token_id 精确匹配
  2. 否则用 mkt（题目截断40字）  -> 前缀/包含匹配 Gamma 题目

数据源（任选其一）：
  --gamma       实时抓取 Gamma（需要出网；NB 机器或配好代理的北京机）
  --map-file F  离线映射 JSON：{ "token_id_or_题目子串": "category", ... }
                便于 NB 伙伴先导出映射、再离线回填；也便于无网环境测试

安全：
  - 先备份 trades.jsonl -> trades.jsonl.bak（同目录），再写回
  - 只改 tag 字段，其余字段原样保留
  - --dry-run 只报告会改变多少笔、不写文件
  - 幂等：新 tag 与旧 tag 相同则跳过（--force 可强制重写）

用法：
  python a_share/backfill_tags.py --gamma --dry-run
  python a_share/backfill_tags.py --gamma
  python a_share/backfill_tags.py --map-file map.json --dry-run
  python a_share/backfill_tags.py --map-file map.json
"""
import os
import sys
import json
import shutil
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
TRADE_PATH = os.path.join(DATA_DIR, "trades.jsonl")


def load_trades(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def build_category_maps_from_gamma():
    """实时抓取 Gamma（内部已深翻页到全站活跃池），构建 {token_id: cat} 与题目索引。"""
    sys.path.insert(0, HERE)
    import polymarket as P  # 仅 --gamma 时导入
    try:
        markets = P.fetch_poly_quotes(limit=1000, force=True)
    except Exception as e:
        print("[gamma] 抓取失败（可能无出网）: %s" % e, file=sys.stderr)
        return {}, []
    by_token = {}
    by_q = []
    for m in (markets or []):
        if not isinstance(m, dict):
            continue
        cat = (m.get("category") or "").strip().lower()
        if not cat:
            continue
        tid = str(m.get("token_id") or "")
        q = (m.get("question") or "").strip().lower()
        if tid:
            by_token[tid] = cat
        if q:
            by_q.append((q, cat))
    return by_token, by_q


def build_category_maps_from_file(map_path):
    """从离线映射文件构建同样的两种索引。键为 token_id 或题目子串。"""
    with open(map_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    by_token = {}
    by_q = []
    for k, v in raw.items():
        cat = str(v).strip().lower()
        if not cat:
            continue
        kk = str(k).strip()
        if kk.isdigit() and len(kk) >= 20:  # 形如 token_id
            by_token[kk] = cat
        else:
            by_q.append((kk.lower(), cat))
    return by_token, by_q


def match_tag(rec, by_token, by_q):
    tid = str(rec.get("token_id") or "").strip()
    if tid and tid in by_token:
        return by_token[tid]
    mkt = str(rec.get("mkt") or "").strip().lower()
    if not mkt:
        return None
    # 精确子串优先（map 键包含 mkt，或 mkt 包含 map 键）
    for q, cat in by_q:
        if q == mkt or q in mkt or mkt in q:
            return cat
    # 前缀匹配（Gamma 全题 -> 成交 mkt 是前 40 字）
    for q, cat in by_q:
        if q.startswith(mkt):
            return cat
    return None


def main():
    ap = argparse.ArgumentParser(description="历史成交 tag 回填（真实 category）")
    ap.add_argument("--gamma", action="store_true", help="实时抓取 Gamma 构建映射（需出网）")
    ap.add_argument("--map-file", default=None, help="离线映射 JSON 文件")
    ap.add_argument("--trades", default=TRADE_PATH, help="trades.jsonl 路径（默认 a_share/data/trades.jsonl）")
    ap.add_argument("--dry-run", action="store_true", help="只报告会改变多少笔，不写文件")
    ap.add_argument("--force", action="store_true", help="即使新 tag 与旧相同也重写")
    args = ap.parse_args()

    if not args.gamma and not args.map_file:
        ap.error("必须指定 --gamma 或 --map-file 之一来提供类目映射")

    if not os.path.exists(args.trades):
        print("[err] 找不到成交文件: %s" % args.trades, file=sys.stderr)
        return 2

    if args.gamma:
        print("[*] 从 Gamma 实时抓取类目映射 ...")
        by_token, by_q = build_category_maps_from_gamma()
    else:
        print("[*] 从映射文件加载类目: %s" % args.map_file)
        by_token, by_q = build_category_maps_from_file(args.map_file)
    print("    映射规模: token_id=%d, 题目=%d" % (len(by_token), len(by_q)))
    if not by_token and not by_q:
        print("[err] 映射为空，无法回填", file=sys.stderr)
        return 2

    rows = load_trades(args.trades)
    print("[*] 读取成交 %d 笔" % len(rows))

    updated = 0
    skipped = 0
    no_match = 0
    samples = []
    for rec in rows:
        new_tag = match_tag(rec, by_token, by_q)
        if not new_tag:
            no_match += 1
            continue
        old_tag = str(rec.get("tag") or "").strip().lower()
        if (not args.force) and old_tag == new_tag:
            skipped += 1
            continue
        if len(samples) < 12:
            samples.append((rec.get("mkt", "?"), old_tag or "(空)", new_tag))
        rec["tag"] = new_tag
        updated += 1

    print("[*] 可回填(匹配到): %d | 已正确跳过: %d | 无匹配: %d"
          % (updated + skipped, skipped, no_match))
    if samples:
        print("    样例(题目 | 旧tag -> 新tag):")
        for mkt, o, n in samples:
            print("      - %s | %s -> %s" % (mkt[:36], o, n))

    if args.dry_run:
        print("[dry-run] 不会改变文件。预计更新 %d 笔。" % updated)
        return 0

    if updated == 0:
        print("[*] 无需更新。")
        return 0

    # 备份 + 写回（只改 tag）
    bak = args.trades + ".bak"
    shutil.copy(args.trades, bak)
    print("[*] 已备份 -> %s" % bak)
    with open(args.trades, "w", encoding="utf-8") as f:
        for rec in rows:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("[ok] 已回填 %d 笔 -> %s" % (updated, args.trades))
    return 0


if __name__ == "__main__":
    sys.exit(main())
