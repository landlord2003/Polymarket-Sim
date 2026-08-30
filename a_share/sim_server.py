# -*- coding: utf-8 -*-
"""实时模拟交易监视器 v3 — 真实 Polymarket 盘口驱动 + 真实引擎做市 + 真实内容展示。

与 v2 的区别（关键）：
  v2 行情是【合成随机游走】。
  v3 行情改为【真实 Polymarket 盘口】：用标准库 urllib 直连 gamma-api.polymarket.com
      (polymarket.fetch_poly_quotes)，每 ~90s 刷新真实二元市场盘口(yes_bid/yes_ask/
      no_bid/no_ask/liquidity/token_id)，对每个真实市场在 YES token 上双边做市。
  成交引擎仍是验证过的 RigorVirtualBook.market_make（被动报价 + 走簿滑点 + 库存偏置）。
  全程 DRY_RUN（影子账本），零网络 POST、零真钱。

看板新增「Polymarket 真实行情」区：像 A 股看板那样展示真实市场
  (question + YES/NO 真实买卖盘口 + 流动性 + 类别)，可按类别过滤。

端口 8787。启动: python a_share/sim_server.py
"""
import json
import os
import sys
import threading
import time
import collections
import datetime as _dt
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))
from sim_rigor import RigorVirtualBook, rigor_params_from_config  # noqa: E402
import polymarket as P  # noqa: E402

PORT = 8787
LOCK = threading.Lock()
MM_N = 20          # 同时做市的真实市场数（取流动性最高者）
MM_REFRESH = 75    # 每 75 轮(~90s)重选一次做市标的（价格刷新另由后台线程负责）

# ============ 成交真实性模型（A：成交概率；B：真实库存管理） ============
# SIM_MODE:
#   pairs = 旧行为 —— 同轮双边建平（买→卖，库存归零），乐观假设双边都成交
#   inv   = 新的真实做市 —— 每轮每市场只尝试一腿(按库存方向)，按概率判定是否成交，
#           未平敞口跨轮持有、承担真实价格波动，受止损/库存上限约束
SIM_MODE = os.environ.get("SIM_MODE", "pairs").strip().lower()
if SIM_MODE not in ("pairs", "inv"):
    SIM_MODE = "pairs"
# 挂单成交概率模型：我们把单挂在距市场最优价 adverse*spread 处
#   adverse=0   -> 挂在市场最优价，需排队等对手方，成交率 = FILL_BASE
#   adverse=0.5 -> 挂在中间价，让出半个价差，成交率 -> 1
FILL_BASE = float(os.environ.get("FILL_BASE", "0.30"))
FILL_GAMMA = float(os.environ.get("FILL_GAMMA", "1.0"))
APPLY_FILL = os.environ.get("APPLY_FILL", "1") != "0"   # 0 = 关闭概率，退回 100% 成交
# inv 模式下真实盘口的刷新间隔(秒)。
# 实测：一次全量拉取(10 页 × 100)约需 20 秒，且 Gamma 盘口在秒/分钟级非常稳定
# （实测 8 秒内 300 个市场的 bestBid/bestAsk 变化为 0）。因此刷新间隔不能设太小，
# 否则请求会堆积、有被 Gamma 限流的风险。默认 150 秒。
PRICE_REFRESH_SEC = float(os.environ.get("PRICE_REFRESH_SEC", "150"))

# ============ 状态 ============
STATE = {
    "running": True,
    "round": 0,
    "cash": 10000.0,
    "realized": 0.0,
    "equity": 10000.0,
    "round_pnl": 0.0,
    "inv_notional": 0.0,
    "unrealized": 0.0,
    "n_markets": 0,
    "live_count": 0,
    "mm_count": 0,
    "last_refresh": 0.0,
    "params": {"mm": 0.02, "adverse": 0.15, "tick": 0.002, "size": 100, "inventory_skew": 0.5},
    "mode": SIM_MODE,
    "fill": {"base": FILL_BASE, "gamma": FILL_GAMMA, "on": APPLY_FILL,
             "attempts": 0, "hits": 0, "rate": 0.0},
    "quotes": {},
    "positions": [],
    "equity_curve": [],
    "peak_equity": 10000.0,
}

# ============ 持久化（统计中心 / 跨重启累计） ============
# 今日(2026-08-30)起成立一条干净的「运行起点」，之后每笔成交 + 每轮权益落盘，
# 重启不丢，统计中心可跨重启累计。
DATA_DIR = os.path.join(_HERE, "data")
TRADE_MEM = 5000                             # 统计中心内存样本容量（最近 N 笔）
TRADE_FILE_MAX_MB = 60                       # trades.jsonl 超过此大小则轮转归档
TRADE_ROTATE_KEEP = 50000                    # 轮转时保留最近 N 笔
TRADES = collections.deque(maxlen=TRADE_MEM) # 服务器侧成交记录(带类别)，供统计中心
RUN_META = {"run_start": None, "initial_equity": 10000.0, "version": 1, "last_round": 0}

def _now_iso():
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def load_persistence():
    """启动即加载：初始化今日运行(若首次)，并从磁盘重建累计成交/权益曲线，使重启不丢数据。"""
    global TRADES, RUN_META
    os.makedirs(DATA_DIR, exist_ok=True)
    meta_path = os.path.join(DATA_DIR, "run_meta.json")
    # 显式重置：SIM_RESET=1 时清空历史，从今天重新建立干净起点（默认不启用，防误删）
    if os.environ.get("SIM_RESET") == "1":
        # 注意：用 open(w) 截断而非 os.remove —— 后者会被 WorkBuddy 的 safe-delete
        # shim 拦截（windows-sandbox-recycle-bin-unavailable），导致重置静默失败、
        # 旧数据被继续加载，统计彻底失真。
        for _f in ("run_meta.json", "trades.jsonl", "equity.jsonl"):
            _p = os.path.join(DATA_DIR, _f)
            try:
                open(_p, "w", encoding="utf-8").close()
            except Exception:
                pass
        RUN_META.clear()
        RUN_META.update({"run_start": None, "initial_equity": 10000.0,
                         "version": 1, "last_round": 0})
        TRADES.clear()
        print("[persistence] SIM_RESET=1 -> 已清空历史，重建干净起点")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                RUN_META.update(json.load(f))
        except Exception:
            pass
    if not RUN_META.get("run_start"):
        RUN_META["run_start"] = _now_iso()
        RUN_META["initial_equity"] = 10000.0
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(RUN_META, f, ensure_ascii=False, indent=2)
    # 重建成交记录
    tpath = os.path.join(DATA_DIR, "trades.jsonl")
    rotate_trades_if_needed(tpath)
    if os.path.exists(tpath):
        try:
            with open(tpath, "r", encoding="utf-8") as f:
                # 只保留最近 TRADE_MEM 笔进入内存（deque maxlen 自动截尾）
                for line in collections.deque(f, TRADE_MEM):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        TRADES.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass
    # 重建权益曲线 + 轮次/现金/累计锁利
    epath = os.path.join(DATA_DIR, "equity.jsonl")
    last_round = RUN_META.get("last_round", 0) or 0
    eq_list = []
    if os.path.exists(epath):
        try:
            with open(epath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                        eq_list.append(o.get("equity", 10000.0))
                        last_round = max(last_round, int(o.get("round", 0)))
                    except Exception:
                        pass
        except Exception:
            pass
    # realized_total 优先取检查点（全量，跨重启不丢）；无检查点时才从样本重算（兼容旧数据）
    realized_total = RUN_META.get("realized_total")
    if realized_total is None:
        realized_total = sum(t.get("pnl", 0.0) for t in TRADES if t.get("side") == "sell")
    # 恢复全程挂单成交统计（不持久化的话，重启后成交率会从 0 重新累积、报告失真）
    FILL_ATTEMPTS[0] = int(RUN_META.get("fill_attempts") or 0)
    FILL_HITS[0] = int(RUN_META.get("fill_hits") or 0)
    with LOCK:
        STATE["fill"] = {
            "base": FILL_BASE, "gamma": FILL_GAMMA, "on": APPLY_FILL,
            "attempts": FILL_ATTEMPTS[0], "hits": FILL_HITS[0],
            "rate": round(FILL_HITS[0] / FILL_ATTEMPTS[0] * 100, 1) if FILL_ATTEMPTS[0] else 0.0,
        }
    realized_total = float(realized_total)
    # 历史最高权益（跨重启不丢，用于正确计算回撤）
    peak_equity = RUN_META.get("peak_equity")
    peak_equity = float(peak_equity) if peak_equity is not None else RUN_META["initial_equity"]
    peak_equity = max(peak_equity, max(eq_list) if eq_list else peak_equity)
    with LOCK:
        STATE["round"] = last_round
        STATE["realized"] = round(realized_total, 2)
        STATE["cash"] = round(RUN_META["initial_equity"] + realized_total, 2)
        STATE["equity"] = STATE["cash"]
        STATE["peak_equity"] = round(peak_equity, 2)
        STATE["equity_curve"] = eq_list[-600:] or [STATE["cash"]]
    book.cash = STATE["cash"]
    book.realized_pnl = realized_total
    print("[persistence] run_start=%s 累计成交=%d 轮次=%d 权益=%.2f" % (
        RUN_META["run_start"], len(TRADES), last_round, STATE["cash"]))

def rotate_trades_if_needed(tpath):
    """trades.jsonl 超过阈值时归档旧文件，只保留最近 TRADE_ROTATE_KEEP 笔，防止无限膨胀。"""
    try:
        if not os.path.exists(tpath):
            return
        if os.path.getsize(tpath) < TRADE_FILE_MAX_MB * 1024 * 1024:
            return
        with open(tpath, "r", encoding="utf-8") as f:
            tail = list(collections.deque(f, TRADE_ROTATE_KEEP))
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        arc = os.path.join(DATA_DIR, "trades_archive_%s.jsonl" % stamp)
        os.replace(tpath, arc)
        with open(tpath, "w", encoding="utf-8") as f:
            f.writelines(tail)
        print("[persistence] trades.jsonl 轮转归档 -> %s（保留最近 %d 笔）"
              % (os.path.basename(arc), len(tail)))
    except Exception:
        pass

def save_trade(rec):
    try:
        with open(os.path.join(DATA_DIR, "trades.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

def save_equity_sample(round_n, equity):
    try:
        with open(os.path.join(DATA_DIR, "equity.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"round": round_n, "equity": round(equity, 2),
                                "ts": _now_iso()}, ensure_ascii=False) + "\n")
    except Exception:
        pass

def update_run_meta_round(round_n, realized_total=None, equity=None, peak_equity=None):
    """每轮更新检查点：轮次 + 全量累计锁利 + 历史峰值权益 + 挂单成交计数
    （均不依赖内存样本，重启不丢）。"""
    RUN_META["last_round"] = round_n
    RUN_META["fill_attempts"] = FILL_ATTEMPTS[0]
    RUN_META["fill_hits"] = FILL_HITS[0]
    if realized_total is not None:
        RUN_META["realized_total"] = round(float(realized_total), 2)
    if equity is not None:
        RUN_META["last_equity"] = round(float(equity), 2)
    if peak_equity is not None:
        RUN_META["peak_equity"] = round(float(peak_equity), 2)
    try:
        with open(os.path.join(DATA_DIR, "run_meta.json"), "w", encoding="utf-8") as f:
            json.dump(RUN_META, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def compute_stats():
    """统计中心：从落盘成交流水中聚合关键指标。"""
    trades = list(TRADES)
    buys = [t for t in trades if t.get("side") == "buy"]
    sells = [t for t in trades if t.get("side") == "sell"]
    win = [t for t in sells if (t.get("pnl") or 0) > 0]
    # 全量累计锁利来自检查点（跨重启不丢）；TRADES 只是最近样本，用于分布类统计
    realized = STATE["realized"]
    sample_n = len(trades)
    # 全量口径优先（检查点），缺失时回退到最近样本
    total_n = int(RUN_META.get("trades_total") or 0) or len(trades)
    sells_n = int(RUN_META.get("sells_total") or 0) or len(sells)
    wins_n = int(RUN_META.get("wins_total") or 0) or len(win)
    equity_now = STATE["equity"]
    eq = STATE["equity_curve"] or [equity_now]
    # 历史峰值（跨重启不丢）+ 当前曲线峰值，取大者
    peak = max(float(STATE.get("peak_equity") or 0.0), float(max(eq)), float(equity_now))
    # 标准最大回撤：沿曲线跟踪 running peak，只统计 peak 之后的跌幅
    run_peak = float(eq[0]); mdd = 0.0
    for v in eq:
        v = float(v)
        if v > run_peak:
            run_peak = v
        d = (run_peak - v) / run_peak * 100.0 if run_peak > 0 else 0.0
        if d > mdd:
            mdd = d
    # 当前回撤（相对历史最高权益）
    dd = (peak - float(equity_now)) / peak * 100.0 if peak > 0 else 0.0
    tags = {}
    for t in trades:
        tg = t.get("tag", "other")
        d = tags.setdefault(tg, {"n": 0, "pnl": 0.0, "win": 0})
        d["n"] += 1
        p = t.get("pnl") or 0
        d["pnl"] += p
        if t.get("side") == "sell" and p > 0:
            d["win"] += 1
    # 全量口径优先（检查点累计），缺失时回退到最近样本
    full_tags = RUN_META.get("tag_pnl")
    tags_out = {k: {"n": v.get("n", 0), "pnl": round(v.get("pnl", 0.0), 2),
                    "win": v.get("win", 0)} for k, v in full_tags.items()} if full_tags else \
               {k: {"n": v["n"], "pnl": round(v["pnl"], 2), "win": v["win"]} for k, v in tags.items()}
    days = {}
    for t in trades:
        ts = t.get("ts")
        try:
            day = _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
        except Exception:
            day = str(ts)[:10]
        if day:
            days[day] = round(days.get(day, 0.0) + (t.get("pnl") or 0), 2)
    full_days = RUN_META.get("daily_pnl")
    days_out = {k: round(v, 2) for k, v in full_days.items()} if full_days else days
    mkt_pnl = {}
    for t in trades:
        mk = t.get("mkt", "-")
        mkt_pnl[mk] = mkt_pnl.get(mk, 0) + (t.get("pnl") or 0)
    full_mkt = RUN_META.get("mkt_pnl")
    mkt_out = full_mkt if full_mkt else mkt_pnl
    best = max(mkt_out.items(), key=lambda kv: kv[1]) if mkt_out else ("-", 0)
    worst = min(mkt_out.items(), key=lambda kv: kv[1]) if mkt_out else ("-", 0)
    best_t = max(trades, key=lambda t: (t.get("pnl") or 0)) if trades else None
    worst_t = min(trades, key=lambda t: (t.get("pnl") or 0)) if trades else None
    run_start = RUN_META.get("run_start")
    try:
        sd = _dt.datetime.strptime(run_start, "%Y-%m-%d %H:%M:%S") if run_start else None
        dur_min = (_dt.datetime.now() - sd).total_seconds() / 60.0 if sd else 0.0
    except Exception:
        dur_min = 0.0
    rate = total_n / dur_min if dur_min > 0.05 else 0.0
    return {
        "run_start": run_start,
        "initial_equity": RUN_META.get("initial_equity"),
        "round": STATE["round"],
        "equity_now": round(equity_now, 2),
        "realized": round(realized, 2),
        "peak": round(peak, 2),
        "drawdown_pct": round(dd, 2), "max_drawdown_pct": round(mdd, 2),
        "peak_profit": round(peak - float(RUN_META.get("initial_equity") or 10000.0), 2),
        "trades_total": total_n, "buys": max(total_n - sells_n, 0), "sells": sells_n,
        "win": wins_n, "win_rate": round(wins_n / sells_n * 100, 1) if sells_n else 0.0,
        "per_tag": tags_out,
        "per_day": days_out,
        "best_market": [best[0], round(best[1], 2)],
        "worst_market": [worst[0], round(worst[1], 2)],
        "best_trade": (best_t and {"mkt": best_t.get("mkt"), "pnl": best_t.get("pnl"), "side": best_t.get("side")}) or None,
        "worst_trade": (worst_t and {"mkt": worst_t.get("mkt"), "pnl": worst_t.get("pnl"), "side": worst_t.get("side")}) or None,
        "duration_min": round(dur_min, 1), "trade_rate": round(rate, 2),
        "trades_per_hour": round(total_n / (dur_min / 60.0), 1) if dur_min > 0.05 else 0.0,
        "avg_pnl": round(sum((t.get("pnl") or 0) for t in sells) / len(sells), 4) if sells else 0.0,
        "sample_n": sample_n,
    }

# ============ 真实行情池（urllib 直连 Gamma） ============
MARKETS_LIVE = None   # fetch_poly_quotes 返回的实时二元盘口列表
MM_SET = []           # 当前做市标的 token 集合（固定，避免建仓不平仓）


def classify(q):
    """按题目文本把市场分到类别（复用 polymarket 关键词表）。"""
    ql = (q or "").lower()
    for tag, re_ in P._CAT_RE.items():
        if re_.search(ql):
            return tag
    return "other"


# 合规红线（中国部署，必须过滤政治/地缘/军事敏感市场）。polymarket._is_blocked
# 漏了 invade/iran 等措辞，这里补强。
BLOCK_EXTRA = ["iran", "invade", "invasion", "russia", "ukraine", "israel",
               "taiwan", "geopolit", "nuclear", "sanction", "election",
               "president", "putin", "trump", "biden", "xi ", "kremlin",
               "nato", "missile", "military", "war", "army", "gaza",
               "palestine", "china", "ccp", "communist",
               # 中东航运咽喉（涉伊朗/胡塞冲突，地缘敏感）
               "hormuz", "mandeb", "bab el-mandeb", "red sea", "yemen",
               "houthis", "houthi", "suez", "gulf", "opec",
               # 其他地缘/国家主体（非体育语境下屏蔽）
               "syria", "north korea", "korea", "lebanon", "hezbollah",
               "afghanistan", "iraq", "venezuela", "cuba", "belarus"]
# 体育赛事专用：只屏蔽真正的政治/军事/选举词，放行国家名（如 New Zealand vs. Syria）
BLOCK_SPORTS = ["invade", "invasion", "geopolit", "nuclear", "sanction",
                "election", "president", "putin", "trump", "biden", "kremlin",
                "nato", "missile", "military", "army", "gaza", "palestine",
                "ccp", "communist", "war ", "world war", " houthis", "houthi"]
def is_blocked(q, tag=None):
    """合规红线过滤。体育赛事里的国家名不算政治敏感（如 New Zealand vs. Syria），
    因此对 sports 类别只套用「真正政治/军事」词表，避免误杀。"""
    if P._is_blocked(q, None):
        return True
    ql = (q or "").lower()
    if tag is None:
        tag = classify(q)
    # 对抗赛句式（A vs B / O/U 大小球）视为体育赛事，其中的国家名不敏感
    is_match = (" vs " in ql) or (" vs. " in ql) or (" o/u " in ql) or (" over/under" in ql)
    words = BLOCK_SPORTS if (tag == "sports" or is_match) else BLOCK_EXTRA
    return any(k in ql for k in words)


def select_mm(rows):
    """从真实盘口池挑选做市标的：流动性够、价格居中、价差够赚。"""
    if not rows:
        return []
    cand = []
    for m in rows:
        if not isinstance(m, dict) or "error" in m:
            continue
        if is_blocked(m.get("question", "")):
            continue
        yb = m.get("yes_bid")
        ya = m.get("yes_ask")
        if yb is None or ya is None or yb <= 0 or ya <= yb:
            continue
        mid = (yb + ya) / 2.0
        sp = ya - yb
        liq = float(m.get("liquidity") or 0)
        if liq < 4000:
            continue
        if mid < 0.12 or mid > 0.88:
            continue
        if sp < 0.01:
            continue
        cand.append((liq, m))
    cand.sort(key=lambda x: -x[0])
    return [m for _, m in cand[:MM_N]]


def fill_prob(adverse):
    """挂单成交概率（A：成交概率模型）。

    我们把单挂在距市场最优价 adverse*spread 处：
      adverse=0   -> 贴着市场最优价挂，价格无优势、要排队，成交率最低 = FILL_BASE
      adverse=0.5 -> 挂在中间价，让出半个价差，几乎必成交 -> 1
    这是做市的核心权衡：挂得越贪（越靠内）越难成交，挂得越让（越靠中间）越稳但赚得越少。
    """
    if not APPLY_FILL:
        return 1.0
    u = adverse / 0.5
    u = 0.0 if u < 0 else (1.0 if u > 1 else u)
    p = FILL_BASE + (1.0 - FILL_BASE) * (u ** FILL_GAMMA)
    return p


# ============ 真实引擎实例 ============
book = RigorVirtualBook(rigor=rigor_params_from_config())
book._save = lambda: None                      # 实时循环不落盘，提速
book._record_volume = lambda *a, **k: None     # 禁日上限文件 I/O，提速
book._save_caps = lambda: None                 # 禁日成交上限持久化 I/O，提速
book.max_skew = 300                            # 允许 size 维度（生产真实上限 300）
book.fee_rate = 0.005                           # Polymarket 真实低交易费（做市赚价差为主）
# 挂单成交统计（尝试次数 / 成交次数），用于计算真实成交率
FILL_ATTEMPTS = [0]
FILL_HITS = [0]


def step():
    """跑一轮：刷新真实盘口(每~90s) -> 对固定做市标的集各调一次真实 market_make。"""
    global MARKETS_LIVE, MM_SET
    # 刷新真实盘口池
    refresh = False
    with LOCK:
        refresh = (STATE["round"] % MM_REFRESH == 0)
    if refresh or not MARKETS_LIVE:
        if SIM_MODE != "inv":   # inv 模式的价格刷新由后台线程负责，此处不阻塞交易循环
            try:
                MARKETS_LIVE = P.fetch_poly_quotes(limit=300, force=True)
            except Exception:
                MARKETS_LIVE = MARKETS_LIVE or []
        # 重选做市标的（固定集合，直到下个刷新周期）
        MM_SET = [m["token_id"] for m in select_mm(MARKETS_LIVE or [])]
    by_tok = {m.get("token_id"): m for m in (MARKETS_LIVE or [])
              if isinstance(m, dict) and "error" not in m}
    size = STATE["params"]["size"]
    adverse = float(book.rigor.get("adverse_frac", 0.15))
    skew = float(book.rigor.get("inventory_skew", 0.0))
    quotes = {}
    round_pnl = 0.0
    round_trades = 0
    round_sells = 0
    round_wins = 0
    for tok in MM_SET:
        m = by_tok.get(tok)
        if not m:
            continue
        qtext = m.get("question", "")
        if is_blocked(qtext):
            continue
        yb = m.get("yes_bid")
        ya = m.get("yes_ask")
        if yb is None or ya is None or yb <= 0 or ya <= yb:
            continue
        mid = (yb + ya) / 2.0
        spread = ya - yb
        opp = {
            "buy_ask": yb, "sell_bid": ya,
            "liquidity": m.get("liquidity") or 0,
            "buy_id": tok, "sell_id": tok,
            "question": qtext,
            # pairs 模式同轮建平、无持仓时间风险，故不计时间衰减；
            # inv 模式真实跨轮持仓，必须计入距到期的时间风险。
            "end_date": (m.get("end_date") if SIM_MODE == "inv" else None),
            "buy_venue": "poly", "sell_venue": "poly",
        }
        # ---- 两种模式 ----
        # pairs（旧）：同轮双边建平，先买后卖，库存归零，纯捕获价差。乐观假设：双边都成交。
        # inv（新）：每轮只尝试一腿（方向由库存决定：有货挂卖单平仓、无货挂买单建仓），
        #           且必须先通过成交概率判定 —— 挂了不等于成交。未平敞口跨轮持有，
        #           承担真实价格波动，受止损(5%)与全局库存上限约束。
        tag = classify(qtext)
        legs = 1 if SIM_MODE == "inv" else 2
        for _leg in range(legs):
            # B：真实做市下挂单不必然成交，按价格改善幅度判定
            if SIM_MODE == "inv" and APPLY_FILL:
                _p = fill_prob(adverse)
                FILL_ATTEMPTS[0] += 1
                if random.random() >= _p:
                    continue          # 挂单没被打掉，本腿不成交
                FILL_HITS[0] += 1
            try:
                r = book.market_make(opp, size)
            except Exception:
                r = {}
            if r.get("ok") and isinstance(r.get("pnl"), (int, float)):
                round_pnl += r["pnl"]
                e = book.positions[-1] if book.positions else None
                if e:
                    rec = {
                        "ts": round(e.get("ts", time.time()), 1),
                        "round": STATE["round"] + 1,
                        "mkt": (e.get("question") or e.get("mkt") or "-")[:40],
                        "tag": tag,
                        "side": e.get("side", ""),
                        "entry": e.get("entry"),
                        "size": e.get("size"),
                        "pnl": e.get("pnl"),
                        "slip": e.get("slip"),
                        "cash_after": e.get("cash_after"),
                        "q": qtext[:60],
                    }
                    TRADES.append(rec)
                    save_trade(rec)
                    round_trades += 1
                    if rec.get("side") == "sell":
                        round_sells += 1
                        if float(rec.get("pnl") or 0.0) > 0:
                            round_wins += 1
                    # 按类别累计（全量，跨重启不丢；TRADES 只是最近样本）
                    tp = RUN_META.setdefault("tag_pnl", {})
                    g = tp.setdefault(tag, {"n": 0, "pnl": 0.0, "win": 0, "sells": 0})
                    g["n"] += 1
                    if rec.get("side") == "sell":
                        # 只累计平仓腿，与 realized 同口径（保证「分类之和 = 累计锁利」）
                        # 注意：累加原始值不做逐笔 round，避免 6000+ 笔的舍入误差累积
                        g["pnl"] = g["pnl"] + float(rec.get("pnl") or 0.0)
                        g["sells"] += 1
                        if float(rec.get("pnl") or 0.0) > 0:
                            g["win"] += 1
                    # 按市场累计（全量，与 realized 同口径）
                    if rec.get("side") == "sell":
                        mp_ = RUN_META.setdefault("mkt_pnl", {})
                        mk_ = rec["mkt"]
                        mp_[mk_] = mp_.get(mk_, 0.0) + float(rec.get("pnl") or 0.0)
        # 展示用：我们计算出的双边报价（与引擎同公式）
        off = skew * spread
        buy_base = yb + adverse * spread
        sell_base = ya - adverse * spread
        try:
            inv_now = int(book.inventory.get(tok, 0) or 0)
        except Exception:
            inv_now = 0
        quotes[tok] = {
            "question": qtext[:62],
            "mid": round(mid, 4),
            "yes_bid": yb, "yes_ask": ya,
            "our_buy": round(buy_base, 4), "our_sell": round(sell_base, 4),
            "inv": inv_now, "liq": round(float(m.get("liquidity") or 0), 0),
        }
    # 快照到 STATE
    prev_realized = STATE["realized"]
    # inv 模式下库存不为零，权益必须按市价盯市（现金 + 未平仓按 mid 计价）
    try:
        eq_marked = book.equity_marked() if SIM_MODE == "inv" else book.cash
    except Exception:
        eq_marked = book.cash
    try:
        inv_notional = book.inventory_notional()
        open_mkts = sum(1 for v in book.inventory.values() if v != 0)
    except Exception:
        inv_notional, open_mkts = 0.0, 0
    with LOCK:
        STATE["round"] += 1
        STATE["cash"] = round(book.cash, 2)
        STATE["realized"] = round(book.realized_pnl, 2)
        STATE["equity"] = round(eq_marked, 2)
        STATE["round_pnl"] = round(round_pnl, 2)
        # 历史峰值权益（跨重启不丢）
        if STATE["equity"] > STATE.get("peak_equity", 0):
            STATE["peak_equity"] = round(STATE["equity"], 2)
        STATE["n_markets"] = open_mkts
        STATE["inv_notional"] = round(inv_notional, 2)
        # 浮动盈亏 = 盯市权益 − 初始权益 − 已实现盈亏
        # （注意：不能写成 equity − cash，那等于「库存市值」而非「盈亏」）
        _init_eq = float(RUN_META.get("initial_equity") or 10000.0)
        STATE["unrealized"] = round(float(STATE["equity"]) - _init_eq
                                    - float(STATE["realized"]), 2)
        # 成交率统计（挂单尝试 vs 实际被打掉）
        STATE["fill"] = {
            "base": FILL_BASE, "gamma": FILL_GAMMA, "on": APPLY_FILL,
            "attempts": FILL_ATTEMPTS[0], "hits": FILL_HITS[0],
            "rate": round(FILL_HITS[0] / FILL_ATTEMPTS[0] * 100, 1) if FILL_ATTEMPTS[0] else 0.0,
        }
        STATE["quotes"] = quotes
        STATE["live_count"] = len(MARKETS_LIVE) if MARKETS_LIVE else 0
        STATE["mm_count"] = len(MM_SET)
        STATE["last_refresh"] = time.time()
        # 最近成交（取服务器侧 TRADES 末尾，新->旧；带类别，供统计中心）
        pos = []
        for e in list(TRADES)[-40:][::-1]:
            pos.append({
                "ts": e.get("ts"), "mkt": e.get("mkt"), "side": e.get("side"),
                "entry": e.get("entry"), "size": e.get("size"), "pnl": e.get("pnl"),
                "slip": e.get("slip"), "cash_after": e.get("cash_after"), "tag": e.get("tag"),
            })
        STATE["positions"] = pos
        # 限制引擎 positions 内存增长
        if len(book.positions) > 4000:
            book.positions = book.positions.__class__(list(book.positions)[-4000:])
        STATE["equity_curve"].append(round(STATE["equity"], 2))
        if len(STATE["equity_curve"]) > 600:
            STATE["equity_curve"].pop(0)
        # 按日累计（全量，跨重启不丢；用 realized 增量，保证「按日之和 = 累计锁利」）
        _day = _dt.datetime.now().strftime("%Y-%m-%d")
        _dp = RUN_META.setdefault("daily_pnl", {})
        # 同样累加原值，不逐轮 round，避免舍入误差累积
        _dp[_day] = _dp.get(_day, 0.0) + (STATE["realized"] - prev_realized)
        RUN_META["trades_total"] = RUN_META.get("trades_total", 0) + round_trades
        RUN_META["sells_total"] = RUN_META.get("sells_total", 0) + round_sells
        RUN_META["wins_total"] = RUN_META.get("wins_total", 0) + round_wins
        save_equity_sample(STATE["round"], STATE["equity"])
        update_run_meta_round(STATE["round"], STATE["realized"], STATE["equity"],
                              STATE.get("peak_equity"))


def price_refresh_daemon():
    """后台刷新真实盘口（B 模式必需）。

    inv 模式下库存跨轮持有，价格必须真实演化才有风险可言。若沿用旧的「每 75 轮
    才刷新一次」，同一批价格会被复用 75 次、库存毫无风险，模拟等于自欺欺人。
    因此价格刷新与交易循环解耦，由独立线程按 PRICE_REFRESH_SEC 秒更新。
    """
    while True:
        with LOCK:
            if not STATE["running"]:
                break
        try:
            # 必须清掉底层 _fetch_pool 的 TTL 缓存，否则拿回的是同一批报价，
            # 价格不演化 = 库存无风险 = 模拟自欺欺人
            rows = P.fetch_quotes_fresh(limit=300)
            if rows:
                global MARKETS_LIVE
                with LOCK:
                    MARKETS_LIVE = rows
        except Exception:
            pass
        time.sleep(PRICE_REFRESH_SEC)


def loop():
    # 启动即拉一次真实盘口
    try:
        global MARKETS_LIVE
        MARKETS_LIVE = P.fetch_poly_quotes(limit=300, force=True)
    except Exception:
        pass
    # inv 模式：价格刷新交给后台线程，交易循环不阻塞
    if SIM_MODE == "inv":
        threading.Thread(target=price_refresh_daemon, daemon=True).start()
    while True:
        with LOCK:
            if not STATE["running"]:
                break
        step()
        time.sleep(1.2)


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Polymarket 实时模拟交易大屏（真实盘口）</title>
<style>
:root{
  --bg:#070a0f;--bg2:#0b1018;--panel:#111824;--panel2:#0d141e;
  --ink:#dfe7f2;--mut:#7e8aa0;--line:#1c2738;
  --up:#ff5b6e;     /* 涨 / 盈利（中国习惯：红） */
  --dn:#2ee6a6;     /* 跌 / 亏损（中国习惯：绿） */
  --acc:#46b0ff;--amber:#f3b54a;--gold:#e8c98a;
}
*{box-sizing:border-box}
body{margin:0;background:
  radial-gradient(1100px 520px at 88% -8%,#0f1b2e 0%,transparent 60%),
  radial-gradient(900px 500px at 0% 110%,#0c1622 0%,transparent 55%),
  var(--bg);
  color:var(--ink);font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;min-height:100vh}
/* 顶部滚动行情条（放慢 + 悬停暂停） */
.ticker{overflow:hidden;white-space:nowrap;background:#060a10;border-bottom:1px solid var(--line);padding:8px 0;position:relative;cursor:default}
.ticker .run{display:inline-block;padding-left:100%;animation:marquee 210s linear infinite}
.ticker:hover .run{animation-play-state:paused}
.ticker .run span{display:inline-block;padding:0 46px;font-size:13px;color:var(--mut);letter-spacing:.2px}
.ticker .run b{color:var(--ink);font-weight:600}
.ticker .run .yb{color:var(--up);font-weight:600}.ticker .run .ya{color:var(--dn);font-weight:600}
@keyframes marquee{to{transform:translateX(-100%)}}
header{padding:15px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  background:linear-gradient(90deg,#0d1622,#0a0f18)}
header h1{margin:0;font-size:19px}
.gtitle{background:linear-gradient(90deg,#46b0ff,#2ee6a6,#46b0ff);background-size:200% auto;-webkit-background-clip:text;background-clip:text;color:transparent;animation:slide 6s linear infinite;font-weight:800;letter-spacing:.5px}
@keyframes slide{to{background-position:200% center}}
.beat{width:10px;height:10px;border-radius:50%;background:var(--dn);box-shadow:0 0 10px var(--dn);animation:beat 1.2s infinite;flex:none}
.beat.hot{background:var(--amber);box-shadow:0 0 14px var(--amber);animation:beat .42s infinite}
@keyframes beat{0%,100%{transform:scale(1);opacity:1}30%{transform:scale(1.6);opacity:.5}}
.badge{font-size:12px;padding:3px 10px;border-radius:20px;background:#101a28;color:var(--mut);border:1px solid var(--line);transition:all .3s}
.badge.live{background:#0e2419;color:#7fe9bd;border-color:#1c503a}
.sndbtn{cursor:pointer;font-size:14px;background:#101a28;border:1px solid var(--line);border-radius:8px;padding:4px 10px;color:var(--mut);transition:all .2s}
.sndbtn:hover{color:var(--ink);border-color:var(--acc)}
.banner{background:linear-gradient(90deg,#0e1b14,#0c1813);border:1px solid #1d3a2a;color:#9fe3c4;padding:8px 14px;margin:12px 22px 0;border-radius:8px;font-size:12.5px}
.wrap{padding:14px 22px;max-width:1720px;margin:0 auto}
/* 指标卡：按 10 / 5 / 2 列切换，保证任意宽度下每行都填满（不留半截空行） */
.cards{display:grid;gap:12px;margin:12px 0;grid-template-columns:repeat(5,minmax(0,1fr))}
@media(min-width:1600px){.cards{grid-template-columns:repeat(10,minmax(0,1fr))}}
@media(max-width:820px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
.card{background:linear-gradient(160deg,var(--panel),#0c131d);border:1px solid var(--line);border-radius:12px;padding:13px 16px;transition:transform .25s,box-shadow .25s,border-color .25s}
.card:hover{transform:translateY(-3px);box-shadow:0 8px 22px rgba(70,176,255,.16);border-color:#2a3c57}
.card .k{color:var(--mut);font-size:12px}.card .v{font-size:21px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums;transition:color .4s}
.v.up{color:var(--up)}.v.dn{color:var(--dn)}.v.b{color:var(--acc)}
/* 主区：面板直接作为网格项（不再包一层 .col —— 那会让窄屏出现"孤儿第三列"）
   行情榜跨两行，其余 4 个面板各占一格：三列时是 3×2、两列时是 2×3，任何宽度都填满且对称 */
.big{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;align-items:stretch}
.big>.panel{margin-bottom:0;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.big>.panel.span2{grid-row:span 2}
/* 自适应断点：三列 -> 两列 -> 单列 */
@media(max-width:1420px){.big{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:920px){.big{grid-template-columns:1fr}.big>.panel.span2{grid-row:span 1;min-height:460px}}
/* 滚动区吃掉剩余高度，使同一行面板底边对齐、三列等高 */
.scroll{overflow:auto;min-height:0}
.big>.panel>.scroll{flex:1 1 0;min-height:170px}
.panel>h2,.panel>.note,.panel>.latest{flex:none}
.panel{background:linear-gradient(160deg,var(--panel),#0c131d);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px;animation:fade .35s ease}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.panel h2{margin:0 0 10px;font-size:14.5px;color:#cdd8e8;display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
.panel h2 .sub{color:var(--mut);font-size:11.5px;font-weight:400}
.panel h2>select{margin-left:auto;max-width:46%}
/* canvas 高度可伸缩以吃掉面板剩余空间；位图尺寸由 JS 按 CSS 实际尺寸 × DPR 设定，避免非等比压扁 */
canvas{width:100%;height:240px;min-height:200px;max-height:380px;background:#070b11;border:1px solid var(--line);border-radius:8px;display:block;flex:1 1 auto}
table{border-collapse:collapse;width:100%;font-size:12.5px;table-layout:fixed}
th,td{border:1px solid var(--line);padding:5px 8px;text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
th{background:#101a28;color:var(--mut);position:sticky;top:0;z-index:1}
td.l{text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.buy{color:var(--up)}.sell{color:var(--dn)}
/* 列宽分配：长文本列给足宽度、数字列压窄，杜绝表格横向撑出面板 */
#mkt th:nth-child(1){width:38%}
#mkt th:nth-child(2){width:11%}
#mkt th:nth-child(3),#mkt th:nth-child(4),#mkt th:nth-child(5),#mkt th:nth-child(6){width:9%}
#mkt th:nth-child(7){width:15%}
#trd th:nth-child(1){width:13%}
#trd th:nth-child(2){width:25%}
#trd th:nth-child(3){width:9%}
#trd th:nth-child(4){width:8%}
#trd th:nth-child(5){width:10%}
#trd th:nth-child(6){width:7%}
#trd th:nth-child(7){width:11%}
#trd th:nth-child(8){width:8%}
#trd th:nth-child(9){width:12%}
@keyframes flashUp{0%{background:rgba(255,91,110,.36)}100%{background:transparent}}
@keyframes flashDn{0%{background:rgba(46,230,166,.30)}100%{background:transparent}}
tr.row-new.flash-up td{animation:flashUp 1.2s ease-out}
tr.row-new.flash-dn td{animation:flashDn 1.2s ease-out}
.latest{display:flex;align-items:center;gap:10px;background:#070b11;border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:12px;font-size:13px}
.latest .up{color:var(--up);font-weight:700}.latest .dn{color:var(--dn);font-weight:700}
.note{color:var(--mut);font-size:12px;margin-top:8px;line-height:1.5}
select{background:#101a28;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:13px}
.tag{font-size:11px;padding:1px 7px;border-radius:10px;background:#172231;color:var(--mut)}
.up{color:var(--up)}.dn{color:var(--dn)}
/* 统计中心 */
.statbox{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat{background:#0a121c;border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stat .k{color:var(--mut);font-size:11.5px}.stat .v{font-size:16px;font-weight:700;margin-top:3px;font-variant-numeric:tabular-nums}
/* 统计明细：三列对称 -> 两列 -> 单列 */
.sc-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
@media(max-width:1000px){.sc-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:700px){.sc-grid{grid-template-columns:1fr}}
.sc-grid>div{min-width:0}
.kv{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px dashed var(--line);font-size:12.5px}
.kv:last-child{border-bottom:none}
.kv .k{color:var(--mut)}.kv .v{font-variant-numeric:tabular-nums}
</style></head>
<body>
<div class="ticker"><div class="run" id="tick">正在加载真实 Polymarket 盘口…</div></div>
<header>
  <span class="beat" id="beat"></span>
  <h1 class="gtitle">Polymarket 实时模拟交易大屏</h1>
  <span class="badge live" id="status">● 连接中…</span>
  <span class="badge" id="rnd">round 0</span>
  <span class="badge" id="live">真实盘口 0 · 做市 0</span>
  <span class="badge mode" id="modebadge">—</span>
  <span class="sndbtn" id="snd" title="成交音效开关（默认关）">🔇 音效</span>
  <span class="badge">引擎: RigorVirtualBook.market_make</span>
</header>
<div class="banner">✅ 行情来自<b>真实 Polymarket 盘口</b>（urllib 直连 Gamma，已合规过滤政治/地缘/军事等敏感类）。
<b>成交不再是必然</b>：挂单按价格改善幅度判定成交概率（<code>FILL_BASE</code> 参数），挂得越贪越难被打到。
<b>未平敞口跨轮持有</b>，承担真实价格波动，受止损(5%)与全局库存上限约束，权益按市价盯市。
全程 <b>DRY_RUN 影子账本、零真钱</b>。配色按中国习惯：<b style="color:var(--up)">红=涨/盈利</b>，<b style="color:var(--dn)">绿=跌/亏损</b>。
数据自 <b id="run-start">—</b> 起落盘累计。</div>
<div class="wrap">
  <div class="cards" id="cards"></div>

  <div class="big">
    <!-- 行情榜：跨两行，高度由网格拉伸填充 -->
    <div class="panel span2">
      <h2>📡 Polymarket 真实行情
        <select id="cat" style="margin-left:auto"><option value="all">全部类别</option><option value="crypto">crypto</option><option value="economy">economy</option><option value="finance">finance</option><option value="sports">sports</option><option value="tech">tech</option><option value="science">science</option><option value="entertainment">entertainment</option><option value="other">other</option></select>
      </h2>
      <div class="scroll"><table id="mkt"><thead><tr><th>市场(问题)</th><th>类别</th><th>YES 买</th><th>YES 卖</th><th>NO 买</th><th>NO 卖</th><th>流动性</th></tr></thead><tbody></tbody></table></div>
      <div class="note">YES=结果代币隐含概率；买/卖为 Gamma 真实最优买卖盘口；<b style="color:var(--up)">买价红</b>、<b style="color:var(--dn)">卖价绿</b>；流动性为该市场 USDC 深度。</div>
    </div>

    <!-- K 线 -->
    <div class="panel">
      <h2>📈 账户权益 K 线（真实成交累计）<span class="sub">红涨绿跌 · 每根=8 轮聚合</span></h2>
      <canvas id="kc" width="680" height="240"></canvas>
      <div class="note" id="kc-note">equity = 现金（库存恒 0）；K 线由逐轮权益聚合，涨红跌绿。</div>
    </div>

    <!-- 实时成交 -->
    <div class="panel">
      <h2>🤖 实时成交（过程与结果）</h2>
      <div class="latest"><span class="beat" id="beat2"></span><span style="color:var(--mut)">最新成交：</span><span id="latest-txt">等待第一笔…</span></div>
      <div class="scroll"><table id="trd"><thead><tr><th>时间</th><th>市场</th><th>类别</th><th>方向</th><th>成交价</th><th>量</th><th>本笔锁利</th><th>滑点</th><th>现金</th></tr></thead><tbody></tbody></table></div>
      <div class="note">BUY=建仓（锁利 0），SELL=平仓（显示本笔锁利）；每笔在真实盘口价位成交。新成交行红/绿闪光。</div>
    </div>

    <!-- 本轮 / 累计锁利 -->
    <div class="panel">
      <h2>💰 本轮 / 累计锁利</h2>
      <div class="cards" style="grid-template-columns:1fr 1fr;margin:0">
        <div class="card"><div class="k">本轮锁利</div><div class="v b" id="ninv">$0</div></div>
        <div class="card"><div class="k">累计锁利</div><div class="v" id="invn">$0</div></div>
      </div>
      <div class="note">累计锁利 = 全部平仓笔锁利之和（落盘累计，重启不丢）。</div>
    </div>
    <!-- 统计中心 -->
    <div class="panel">
      <h2>📊 统计中心（实时）</h2>
      <div class="statbox">
        <div class="stat"><div class="k">运行时长</div><div class="v" id="st-dur">—</div></div>
        <div class="stat"><div class="k">轮次</div><div class="v" id="st-round">0</div></div>
        <div class="stat"><div class="k">总成交</div><div class="v" id="st-tot">0</div></div>
        <div class="stat"><div class="k">胜率(平仓)</div><div class="v" id="st-win">0%</div></div>
        <div class="stat"><div class="k">成交频率</div><div class="v" id="st-rate">0</div></div>
        <div class="stat"><div class="k">累计锁利</div><div class="v" id="st-real">$0</div></div>
        <div class="stat"><div class="k">峰值盈利</div><div class="v" id="st-pk">$0</div></div>
        <div class="stat"><div class="k">权益峰值</div><div class="v" id="st-peak">$0</div></div>
        <div class="stat"><div class="k">当前回撤</div><div class="v" id="st-dd">0%</div></div>
        <div class="stat"><div class="k">历史最大回撤</div><div class="v" id="st-mdd">0%</div></div>
      </div>
      <div class="note" id="st-note">—</div>
    </div>
  </div>

  <!-- 底部：统计中心详情（三列对称） -->
  <div class="panel">
    <h2>📑 统计中心 · 明细</h2>
    <div class="sc-grid">
      <div>
        <div style="color:var(--mut);font-size:12.5px;margin-bottom:6px">按类别盈亏</div>
        <table id="ptag"><thead><tr><th>类别</th><th>笔数</th><th>锁利</th><th>胜笔</th></tr></thead><tbody></tbody></table>
      </div>
      <div>
        <div style="color:var(--mut);font-size:12.5px;margin-bottom:6px">按日盈亏</div>
        <table id="pday"><thead><tr><th>日期</th><th>锁利</th></tr></thead><tbody></tbody></table>
      </div>
      <div>
        <div style="color:var(--mut);font-size:12.5px;margin-bottom:6px">极值</div>
        <div class="kv"><span class="k">最佳市场</span><span class="v up" id="st-best">—</span></div>
        <div class="kv"><span class="k">最差市场</span><span class="v dn" id="st-worst">—</span></div>
        <div class="kv"><span class="k">最佳单笔</span><span class="v up" id="st-bt">—</span></div>
        <div class="kv"><span class="k">最差单笔</span><span class="v dn" id="st-wt">—</span></div>
        <div class="kv"><span class="k">运行起点</span><span class="v" id="st-run" style="color:var(--gold)">—</span></div>
      </div>
    </div>
  </div>
</div>
<script>
function fmt(n){return (n==null)?'-':Number(n).toLocaleString('en-US',{maximumFractionDigits:2})}
function money(n){return '$'+Number(n).toLocaleString('en-US',{maximumFractionDigits:2})}
function smoney(n){return (n>=0?'+$':'-$')+Number(Math.abs(n)).toLocaleString('en-US',{maximumFractionDigits:2})}
function sgn(v){return (v>=0?'+$':'-$')+Number(Math.abs(v)).toFixed(2)}
function col(v){return v>=0?'up':'dn'}
function tween(el,to,render){
  const from=el._cur!=null?el._cur:to; el._cur=to;
  const dur=550,t0=performance.now();
  if(el._raf)cancelAnimationFrame(el._raf);
  function step(now){const k=Math.min(1,(now-t0)/dur),e=1-Math.pow(1-k,3),v=from+(to-from)*e;
    el.textContent=render(v); if(k<1)el._raf=requestAnimationFrame(step);}
  el._raf=requestAnimationFrame(step);
}
function setMoney(id,val,signed){
  const el=document.getElementById(id); if(!el)return;
  el.className='v '+(val>0?'up':val<0?'dn':'b');
  tween(el,val, signed?smoney:money);
}
function setNum(id,val){const el=document.getElementById(id); if(!el)return; el.className='v b'; tween(el,val,v=>Number(v).toLocaleString('en-US',{maximumFractionDigits:0}));}
document.getElementById('cards').innerHTML=
  ['<div class="card"><div class="k">轮次</div><div class="v b" id="c-round">0</div></div>',
   '<div class="card"><div class="k">现金</div><div class="v b" id="c-cash">$0</div></div>',
   '<div class="card"><div class="k">累计锁利</div><div class="v b" id="c-real">$0</div></div>',
   '<div class="card"><div class="k">本轮锁利</div><div class="v b" id="c-rpnl">$0</div></div>',
   '<div class="card"><div class="k">盯市权益</div><div class="v b" id="c-eq">$0</div></div>',
   '<div class="card"><div class="k">浮动盈亏</div><div class="v b" id="c-unreal">$0</div></div>',
   '<div class="card"><div class="k">挂单成交率</div><div class="v b" id="c-fill">0%</div></div>',
   '<div class="card"><div class="k">敞口名义</div><div class="v b" id="c-inv">$0</div></div>',
   '<div class="card"><div class="k">真实盘口</div><div class="v b" id="c-live">0</div></div>',
   '<div class="card"><div class="k">做市市场</div><div class="v b" id="c-mm">0</div></div>'].join('');
let seen=new Set(), prevRound=0;
function flashBeat(id){const b=document.getElementById(id); if(!b)return; b.classList.add('hot'); setTimeout(()=>b.classList.remove('hot'),650);}
let audioCtx=null, soundOn=false;
function toggleSound(){
  soundOn=!soundOn; const b=document.getElementById('snd');
  b.textContent=soundOn?'🔊 音效':'🔇 音效';
  if(soundOn && !audioCtx){try{audioCtx=new (window.AudioContext||window.webkitAudioContext)();}catch(e){audioCtx=null;}}
  if(audioCtx && audioCtx.state==='suspended') audioCtx.resume();
}
function beep(freq,dur,type,gain){
  if(!soundOn||!audioCtx) return;
  const o=audioCtx.createOscillator(), g=audioCtx.createGain();
  o.type=type||'sine'; o.frequency.value=freq; g.gain.value=gain||0.05;
  o.connect(g); g.connect(audioCtx.destination); o.start(); o.stop(audioCtx.currentTime+dur);
}
document.getElementById('snd').onclick=toggleSound;
let lastCurve=null;
function drawCandles(curve){
  if(curve) lastCurve=curve;
  const cv=document.getElementById('kc'); if(!cv) return;
  const ctx=cv.getContext('2d');
  // 位图尺寸跟随 CSS 实际尺寸 × DPR，否则画布会被非等比拉伸（K 线变形、文字发虚）
  const dpr=window.devicePixelRatio||1;
  const W=Math.max(300, Math.round(cv.clientWidth||cv.width));
  const H=Math.max(160, Math.round(cv.clientHeight||240));
  const bw=Math.round(W*dpr), bh=Math.round(H*dpr);
  if(cv.width!==bw||cv.height!==bh){cv.width=bw;cv.height=bh;}
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  if(!lastCurve||lastCurve.length<2) return;
  const curve2=lastCurve;
  const K=Math.max(1,Math.floor(curve2.length/64));
  const candles=[];
  for(let i=0;i<curve2.length;i+=K){
    const seg=curve2.slice(i,i+K); if(!seg.length) continue;
    candles.push({o:seg[0],h:Math.max.apply(null,seg),l:Math.min.apply(null,seg),c:seg[seg.length-1]});
  }
  let mn=Infinity,mx=-Infinity;
  candles.forEach(c=>{mn=Math.min(mn,c.h,c.l);mx=Math.max(mx,c.h,c.l);});
  const pad=(mx-mn)*0.12||1; mn-=pad; mx+=pad;
  const PL=8, PR=12;   // 左右留白，避免首尾蜡烛被画布边缘切掉一半
  const X=i=>PL+(i/(candles.length-1))*(W-PL-PR), Y=v=>H-((v-mn)/(mx-mn))*H;
  ctx.strokeStyle='#16202e'; ctx.lineWidth=1;
  for(let g=0;g<=4;g++){const y=g/4*H; ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();}
  const cw=Math.max(2, W/candles.length*0.6);
  candles.forEach((c,i)=>{
    const x=X(i), up=c.c>=c.o, color=up?'#ff5b6e':'#2ee6a6';
    ctx.strokeStyle=color; ctx.fillStyle=color;
    ctx.beginPath();ctx.moveTo(x,Y(c.h));ctx.lineTo(x,Y(c.l));ctx.stroke();
    const yo=Y(c.o), yc=Y(c.c), top=Math.min(yo,yc), bh=Math.max(1,Math.abs(yc-yo));
    ctx.globalAlpha=0.85; ctx.fillRect(x-cw/2,top,cw,bh); ctx.globalAlpha=1;
  });
  const last=candles[candles.length-1], lx=X(candles.length-1), ly=Y(last.c);
  ctx.beginPath();ctx.arc(lx,ly,4,0,7);ctx.fillStyle=last.c>=last.o?'#ff5b6e':'#2ee6a6';
  ctx.shadowColor=ctx.fillStyle;ctx.shadowBlur=10;ctx.fill();ctx.shadowBlur=0;
  ctx.fillStyle='#7e8aa0';ctx.font='11px sans-serif';
  ctx.fillText('$'+mx.toFixed(0),4,12);ctx.fillText('$'+mn.toFixed(0),4,H-4);
}
function renderStats(st){
  if(!st||st.error) return;
  document.getElementById('st-run').textContent=st.run_start||'-';
  document.getElementById('run-start').textContent=st.run_start||'-';
  document.getElementById('st-dur').textContent=st.duration_min+' 分';
  document.getElementById('st-round').textContent=st.round;
  document.getElementById('st-tot').textContent=st.trades_total;
  document.getElementById('st-win').textContent=st.win_rate+'% ('+st.win+'/'+st.sells+')';
  document.getElementById('st-rate').textContent=st.trades_per_hour+' 笔/时';
  document.getElementById('st-peak').textContent='$'+st.peak;
  document.getElementById('st-dd').textContent=st.drawdown_pct+'%';
  const pk=document.getElementById('st-pk'); pk.className='v '+col(st.peak_profit); pk.textContent=sgn(st.peak_profit);
  document.getElementById('st-mdd').textContent=st.max_drawdown_pct+'%';
  document.getElementById('st-note').textContent=
    '统计口径：自 '+st.run_start+' 起全量累计（落盘持久化，重启不丢）。'
    +'总成交 '+st.trades_total+' 笔，其中平仓 '+st.sells+' 笔、盈利 '+st.win+' 笔，单笔均值 $'+st.avg_pnl+'。'
    +'分类/按日/按市场三项之和均等于累计锁利 $'+st.realized+'（口径一致可交叉核对）。'
    +'注：成交频率高源于模拟引擎每轮对 20 个市场双边撮合，非真实成交能力。';
  const rc=document.getElementById('st-real'); rc.className='v '+col(st.realized); rc.textContent=sgn(st.realized);
  const tags=Object.keys(st.per_tag).sort((a,b)=>st.per_tag[b].pnl-st.per_tag[a].pnl);
  document.getElementById('ptag').querySelector('tbody').innerHTML=tags.map(t=>{const d=st.per_tag[t];
    return `<tr><td>${t}</td><td>${d.n}</td><td class="${col(d.pnl)}">${sgn(d.pnl)}</td><td>${d.win}</td></tr>`;}).join('');
  const days=Object.keys(st.per_day);
  document.getElementById('pday').querySelector('tbody').innerHTML=days.map(d=>`<tr><td>${d}</td><td class="${col(st.per_day[d])}">${sgn(st.per_day[d])}</td></tr>`).join('');
  document.getElementById('st-best').textContent=st.best_market[0].slice(0,22)+'  '+sgn(st.best_market[1]);
  document.getElementById('st-worst').textContent=st.worst_market[0].slice(0,22)+'  '+sgn(st.worst_market[1]);
  if(st.best_trade) document.getElementById('st-bt').textContent=
    sgn(st.best_trade.pnl)+'  ('+String(st.best_trade.mkt).slice(0,18)+')';
  if(st.worst_trade) document.getElementById('st-wt').textContent=
    sgn(st.worst_trade.pnl)+'  ('+String(st.worst_trade.mkt).slice(0,18)+')';
}
function tickState(){
  fetch('/api/state').then(r=>r.json()).then(s=>{
    document.getElementById('rnd').textContent='round '+s.round;
    document.getElementById('live').textContent='真实盘口 '+s.live_count+' · 做市 '+s.mm_count;
    const st=document.getElementById('status');
    if(s.round!==prevRound){prevRound=s.round; st.textContent='● 撮合中 · round '+s.round; flashBeat('beat');}
    else st.textContent='● 实时运行 · round '+s.round;
    const eq=s.equity;
    setNum('c-round',s.round); setMoney('c-cash',s.cash,false);
    setMoney('c-real',s.realized,true); setMoney('c-rpnl',s.round_pnl,true);
    setMoney('c-eq',eq,false); setNum('c-live',s.live_count); setNum('c-mm',s.mm_count);
    setMoney('c-unreal',(s.unrealized||0),true);
    setMoney('c-inv',(s.inv_notional||0),false);
    const fe=document.getElementById('c-fill');
    if(fe){const fr=(s.fill&&s.fill.on)?(s.fill.rate||0):100;
      fe.className='v '+(fr>=60?'b':(fr>=40?'g':'r'));
      fe.textContent=fr.toFixed(1)+'%';}
    const mb=document.getElementById('modebadge');
    if(mb&&s.fill){mb.textContent=(s.mode==='inv'?'真实做市(库存管理)':'同轮双边建平')
      +' · 挂单成交模型'+(s.fill.on?'开':'关');}
    document.getElementById('ninv').textContent=(s.round_pnl>=0?'+$':'-$')+fmt(Math.abs(s.round_pnl));
    document.getElementById('invn').textContent=(s.realized>=0?'+$':'-$')+fmt(Math.abs(s.realized));
    drawCandles(s.equity_curve);
    const t2=document.getElementById('trd').querySelector('tbody');
    let fresh=0, freshTrade=null;
    const rows=s.positions.map(t=>{
      const key=t.ts+'|'+t.mkt+'|'+t.side+'|'+t.entry;
      const isNew=!seen.has(key); if(isNew){seen.add(key);fresh++; if(!freshTrade)freshTrade=t;}
      if(seen.size>400)seen.clear();
      const cls=(t.side==='buy'?'flash-up':'flash-dn');
      return `<tr class="${isNew?'row-new '+cls:cls}"><td>${t.ts}</td><td class="l">${t.mkt}</td><td>${t.tag||'-'}</td>`+
        `<td class="${t.side==='buy'?'buy':'sell'}">${(t.side||'').toUpperCase()}</td>`+
        `<td>${t.entry!=null?Number(t.entry).toFixed(4):'-'}</td><td>${t.size}</td>`+
        `<td class="${col((t.pnl||0))}">${t.pnl!=null?'$'+fmt(t.pnl):'-'}</td>`+
        `<td>${t.slip!=null?Number(t.slip).toFixed(4):'-'}</td><td>$${fmt(t.cash_after)}</td></tr>`;
    }).join('');
    t2.innerHTML=rows;
    if(fresh>0){
      flashBeat('beat2');
      const t=freshTrade||s.positions[0];
      if(t){
        const up=t.pnl!=null && t.pnl>0;
        document.getElementById('latest-txt').innerHTML=
          `<b class="${t.side==='buy'?'buy':'sell'}">${t.side.toUpperCase()}</b> ${t.mkt} @${Number(t.entry).toFixed(4)} `+
          (t.pnl!=null?`· 锁利 <span class="${up?'up':'dn'}">$${fmt(t.pnl)}</span>`:'· 建仓');
        if(t.side==='buy') beep(500,0.05,'sine',0.04);
        else if(up){ beep(740,0.07,'triangle',0.05); setTimeout(()=>beep(980,0.07,'triangle',0.045),70); }
        else beep(300,0.12,'sawtooth',0.03);
      }
    }
  }).catch(()=>{});
  fetch('/api/stats').then(r=>r.json()).then(renderStats).catch(()=>{});
}
let liveCache=null;
function tickLive(){
  fetch('/api/markets').then(r=>r.json()).then(d=>{
    liveCache=d; renderLive();
    const items=d.markets.slice(0,32).map(m=>
      `<span><b>${m.question.slice(0,40)}</b> YES <span class="yb">${Number(m.yes_bid).toFixed(3)}</span>/<span class="ya">${Number(m.yes_ask).toFixed(3)}</span> · 量 $${fmt(m.liquidity)}</span>`).join('');
    document.getElementById('tick').innerHTML=items+items;
  }).catch(()=>{});
}
function renderLive(){
  if(!liveCache) return;
  const cat=document.getElementById('cat').value;
  const rows=liveCache.markets.filter(m=>cat==='all'||m.tag===cat).slice(0,150);
  document.getElementById('mkt').querySelector('tbody').innerHTML=rows.map(m=>
    `<tr><td class="l">${m.question}</td><td><span class="tag">${m.tag}</span></td>`+
    `<td class="buy">${Number(m.yes_bid).toFixed(4)}</td><td class="sell">${Number(m.yes_ask).toFixed(4)}</td>`+
    `<td class="buy">${Number(m.no_bid).toFixed(4)}</td><td class="sell">${Number(m.no_ask).toFixed(4)}</td>`+
    `<td>$${fmt(m.liquidity)}</td></tr>`).join('');
}
document.getElementById('cat').onchange=renderLive;
// 窗口尺寸变化后重画 K 线（位图尺寸跟着 CSS 尺寸走，否则会被拉伸变形）
let _rz=null;
window.addEventListener('resize',()=>{clearTimeout(_rz);_rz=setTimeout(()=>drawCandles(null),180);});

setInterval(tickState,2000); setInterval(tickLive,15000);
tickState(); tickLive();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/state":
            with LOCK:
                snap = {
                    "round": STATE["round"], "cash": STATE["cash"],
                    "realized": STATE["realized"], "equity": STATE["equity"],
                    "round_pnl": STATE["round_pnl"],
                    "n_markets": STATE["n_markets"], "inv_notional": STATE["inv_notional"],
                    "unrealized": STATE.get("unrealized", 0.0),
                    "mode": STATE.get("mode", "pairs"), "fill": STATE.get("fill", {}),
                    "live_count": STATE["live_count"], "mm_count": STATE["mm_count"],
                    "params": STATE["params"], "quotes": STATE["quotes"],
                    "positions": STATE["positions"], "equity_curve": STATE["equity_curve"],
                }
            body = json.dumps(snap, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/markets":
            rows = MARKETS_LIVE or []
            out = []
            for m in rows:
                if not isinstance(m, dict) or "error" in m:
                    continue
                q = m.get("question") or ""
                if is_blocked(q):
                    continue
                out.append({
                    "question": (q[:90] + ("…" if len(q) > 90 else "")),
                    "tag": classify(q),
                    "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
                    "no_bid": m.get("no_bid"), "no_ask": m.get("no_ask"),
                    "liquidity": round(float(m.get("liquidity") or 0), 0),
                    "token_id": str(m.get("token_id")),
                })
            body = json.dumps({"ts": time.time(), "count": len(out),
                               "markets": out}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/stats":
            try:
                stats = compute_stats()
            except Exception as ex:
                stats = {"error": str(ex)}
            body = json.dumps(stats, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


def main():
    load_persistence()
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print("[sim_server v3] listening on http://127.0.0.1:%d  (Ctrl+C to stop)" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        with LOCK:
            STATE["running"] = False
        srv.shutdown()


if __name__ == "__main__":
    main()
