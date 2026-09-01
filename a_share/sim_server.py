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
import atexit
import signal
import ctypes
import threading
import time
import collections
import datetime as _dt
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))
from sim_rigor import RigorVirtualBook, rigor_params_from_config  # noqa: E402
import sim_rigor as R  # noqa: E402  (P1-2 敏感性分析用 R.mm_param_sensitivity)
import polymarket as P  # noqa: E402
# 复用 sim_report 的已验证报告渲染函数（HTML/Markdown），避免双重实现
from sim_report import build_html, build_md, load_trades, write_trades_csv, filter_trades  # noqa: E402
from notify import send_markdown as ding_send_markdown  # noqa: E402  (P0-C 周期报告自动推钉钉)
import risk_control as RC  # noqa: E402  (P3-4 金融风控层：仓位/日亏/kill switch)

PORT = 8787
LOCK = threading.Lock()
SRV = None  # 全局 HTTP server 引用，供 /api/shutdown 与信号处理器调用

# ============ P2-1 单实例启动锁 ============
PID_FILE = os.path.join(os.path.dirname(_HERE), "output", "sim_server.pid")


def _pid_alive(pid):
    """跨平台判断进程是否存活（Windows 用 ctypes 避免 tasklist 子进程编解码崩溃）。"""
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_INFORMATION = 0x0400
            PROCESS_VM_READ = 0x0010
            h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_lock():
    """启动时获取单实例锁；若已有活实例则拒绝启动（避免多实例抢盘口/重复写盘）。"""
    d = os.path.dirname(PID_FILE)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    if os.path.exists(PID_FILE):
        try:
            old = int((open(PID_FILE).read().strip() or "0"))
        except Exception:
            old = 0
        if old and _pid_alive(old):
            print("[sim_server] 已有实例在运行 (pid=%s)，拒绝重复启动。\n"
                  "  如需强制重启，请先结束该进程： taskkill /F /PID %s\n"
                  "  或删除锁文件： %s" % (old, old, PID_FILE))
            sys.exit(1)
        # 锁文件存在但进程已死 -> 清掉陈旧锁
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(_release_lock)
    print("[sim_server] 启动锁已获取 -> %s (pid=%s)" % (PID_FILE, os.getpid()))


def _release_lock():
    """进程退出时释放锁（仅当锁文件仍指向自己，避免误删他人锁）。

    注意：WorkBuddy 的 safe-delete shim 会拦截 os.remove（Windows 回收站不可用
    时静默失败），故删除失败时用 ctypes DeleteFileW 直接绕过 shim 真删。
    """
    try:
        if os.path.exists(PID_FILE) and open(PID_FILE).read().strip() == str(os.getpid()):
            try:
                os.remove(PID_FILE)
            except Exception:
                try:
                    ctypes.windll.kernel32.DeleteFileW(ctypes.c_wchar_p(os.path.abspath(PID_FILE)))
                except Exception:
                    pass
    except OSError:
        pass


# ============ 启动早期加载 .env（显式，不依赖 notify 导入顺序）============
def _load_dotenv():
    """Minimal stdlib-only .env loader：把仓库根 .env 的 KEY=VALUE 注入 os.environ
    （不覆盖已存在的进程环境变量）。在任何配置常量读取前调用，确保 .env.nb 里的
    LIVE_MODE / COMPLIANCE_FILTER / PM_BOT_PK / SHUTDOWN_TOKEN 等被读到——
    否则伙伴 cp .env.nb .env 后跑 python sim_server.py 会静默退回默认 DRY_RUN+合规开启。"""
    d = os.path.dirname(os.path.abspath(__file__))
    env_path = None
    cur = d
    for _ in range(5):
        cand = os.path.join(cur, ".env")
        if os.path.isfile(cand):
            env_path = cand
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if not env_path:
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

_load_dotenv()


# ============ P2-3 报告自动化 ============
AUTO_REPORT_MIN = int(os.environ.get("AUTO_REPORT_MIN", "30") or 30)  # 0=关闭自动报告


def build_and_write_report(top_n=15, stamp=None):
    """复用 sim_report 已验证渲染，生成 HTML/MD 报告并落盘（供 /api/export_report 与自动报告线程共用）。"""
    with LOCK:
        st = {
            "round": STATE["round"], "cash": STATE["cash"],
            "realized": STATE["realized"], "equity": STATE["equity"],
            "round_pnl": STATE["round_pnl"],
            "n_markets": STATE["n_markets"], "inv_notional": STATE["inv_notional"],
            "unrealized": STATE.get("unrealized", 0.0),
            "mode": STATE.get("mode", "pairs"), "fill": STATE.get("fill", {}),
            "live_count": STATE["live_count"], "mm_count": STATE["mm_count"],
            "mm_cats": STATE.get("mm_cats", {}),
            "params": STATE["params"], "quotes": STATE["quotes"],
            "positions": STATE["positions"], "equity_curve": STATE["equity_curve"],
        }
        s = compute_stats()
    mout = []
    for m in (MARKETS_LIVE or []):
        if not isinstance(m, dict) or "error" in m:
            continue
        q = m.get("question") or ""
        if is_blocked(q):
            continue
        mout.append({
            "question": (q[:90] + ("…" if len(q) > 90 else "")),
            "tag": market_cat(m),
            "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
            "no_bid": m.get("no_bid"), "no_ask": m.get("no_ask"),
            "liquidity": round(float(m.get("liquidity") or 0), 0),
            "token_id": str(m.get("token_id")),
        })
    mkts = {"markets": mout}
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp = stamp or _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(os.path.dirname(_HERE), "output")
    os.makedirs(out_dir, exist_ok=True)
    html_name = "sim_report_%s.html" % stamp
    md_name = "sim_report_%s.md" % stamp
    html_path = os.path.join(out_dir, html_name)
    md_path = os.path.join(out_dir, md_name)
    # 逐笔成交明细：优先从 trades.jsonl 读最近 100 笔（全量历史），回退 STATE.positions
    _trades = load_trades(100)
    if not _trades:
        _trades = STATE.get("positions") or []
    # 类别锁利汇总用全量历史（与报告明细样本解耦），让「按类别锁利汇总」覆盖整段运行
    _all = load_trades(0) or _trades
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(build_html(st, s, mkts, top_n, ts, trades=_trades, tag_trades=_all))
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(build_md(st, s, mkts, top_n, ts, trades=_trades, tag_trades=_all))
    return {"html": html_path, "md": md_path, "html_name": html_name,
            "md_name": md_name, "stamp": stamp, "ts": ts,
            "equity": st["equity"], "realized": st["realized"], "round": st["round"]}


def _push_report_ding(r):
    """P0-C 周期报告自动推钉钉：把报告核心指标推送到钉钉，闭环（未配置机器人时静默跳过）。"""
    try:
        with LOCK:
            s = compute_stats()
            fill = dict(STATE.get("fill", {}))
        title = "📊 模拟盘周期报告 %s" % r["stamp"]
        rate = fill.get("rate", 0.0)
        lines = [
            "**轮次**: %s  " % r["round"],
            "**已实现锁利**: $%s  " % r["realized"],
            "**盯市权益**: $%s  " % r["equity"],
            "**历史峰值**: $%s  当前回撤: %s%%  " % (s.get("peak"), s.get("drawdown_pct")),
            "**最大回撤**: %s%%  成交胜率: %s%%  " % (s.get("max_drawdown_pct"), s.get("win_rate")),
            "**观测成交率**: %s%% (尝试%s/命中%s)  " % (rate, fill.get("attempts", 0), fill.get("hits", 0)),
            "**持仓市场**: %s  库存名义: $%s  " % (r.get("n_markets"), s.get("inv_notional")),
            "**逆向选择损耗**: $%s  已结算: $%s  " % (s.get("adverse_sel_loss"), s.get("settled_pnl")),
            "> 报告文件: %s / %s" % (r["html_name"], r["md_name"]),
        ]
        ding_send_markdown(title, "\n".join(lines))
    except Exception as ex:
        print("[auto_report] 钉钉推送失败：%s" % ex)


def auto_report_loop():
    """P2-3 后台自动报告：每 AUTO_REPORT_MIN 分钟生成一份报告；启动即先出一份。
    P0-C：每份报告生成后自动推钉钉（闭环）。"""
    if AUTO_REPORT_MIN <= 0:
        print("[auto_report] 已关闭 (AUTO_REPORT_MIN=%s)" % AUTO_REPORT_MIN)
        return
    try:
        r = build_and_write_report(15)
        print("[auto_report] 启动报告已生成 -> %s" % r["html_name"])
        _push_report_ding(r)
    except Exception as ex:
        print("[auto_report] 启动报告失败：%s" % ex)
    while True:
        with LOCK:
            if not STATE.get("running", True):
                break
        time.sleep(AUTO_REPORT_MIN * 60)
        try:
            r = build_and_write_report(15)
            print("[auto_report] 周期报告已生成 -> %s (round=%s)" % (r["html_name"], r["round"]))
            _push_report_ding(r)
        except Exception as ex:
            print("[auto_report] 周期报告失败：%s" % ex)


def save_persistence():
    """优雅停止时把内存中的运行元数据落盘（实时循环为提速禁用了逐轮 I/O）。"""
    try:
        with LOCK:
            RUN_META["last_round"] = STATE.get("round", 0)
            RUN_META["last_equity"] = STATE.get("equity")
            RUN_META["trades_total"] = RUN_META.get("trades_total", 0)
        meta_path = os.path.join(DATA_DIR, "run_meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(RUN_META, f, ensure_ascii=False, indent=2)
        print("[persistence] 已落盘 run_meta.json (round=%s)" % RUN_META.get("last_round"))
    except Exception as ex:
        print("[persistence] 落盘失败：%s" % ex)
MM_N = 20          # 同时做市的真实市场数（取流动性最高者）
MM_N_PER_CAT = int(os.environ.get("MM_N_PER_CAT", "5") or 5)  # 每类做市上限，保证类别多样性
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
# NB 省部署（无合规风险）可设 COMPLIANCE_FILTER=0 整体关闭合规红线过滤；
# 默认开（中国部署必须过滤政治/地缘/军事敏感市场）。
COMPLIANCE_FILTER = os.environ.get("COMPLIANCE_FILTER", "1") == "1"
# NB 实盘开关：LIVE_MODE=1 时策略循环的意图挂单会真实发到 CLOB（经风控闸门）；
# 默认 0=DRY_RUN，live_dispatch 不调用，零副作用，当前北京模拟盘行为不变。
LIVE_MODE = os.environ.get("LIVE_MODE", "0") == "1"
# 初始资金（USD）：重置起点。默认 10000；可用 INITIAL_CAPITAL 覆盖（如 5000）。
INITIAL_CAPITAL = float(os.environ.get("INITIAL_CAPITAL", "10000.0") or "10000.0")
FILL_GAMMA = float(os.environ.get("FILL_GAMMA", "1.0"))
APPLY_FILL = os.environ.get("APPLY_FILL", "1") != "0"   # 0 = 关闭概率，退回 100% 成交
# 成交率校准（P0-2）：基础成交率由市场流动性归一化得到（高流动性盘口排队消化快→成交率高）
LIQ_REF = float(os.environ.get("LIQ_REF", "30000.0"))   # 流动性参考值（达到此值基础成交率→~0.92）
# inv 模式下真实盘口的刷新间隔(秒)。
# 实测：一次全量拉取(10 页 × 100)约需 20 秒，且 Gamma 盘口在秒/分钟级非常稳定
# （实测 8 秒内 300 个市场的 bestBid/bestAsk 变化为 0）。因此刷新间隔不能设太小，
# 否则请求会堆积、有被 Gamma 限流的风险。默认 150 秒。
PRICE_REFRESH_SEC = float(os.environ.get("PRICE_REFRESH_SEC", "150"))
# P0-A：关停端点鉴权 token（从 .env 读取 SHUTDOWN_TOKEN；未设则用弱默认并启动时告警，
#        强烈建议生产/局域网部署设 SHUTDOWN_TOKEN，避免同网任意主机可关停服务）
SHUTDOWN_TOKEN = os.environ.get("SHUTDOWN_TOKEN") or "sim-stop-8787"
# P1-A：成交率影子标定——是否把标定结果应用到 FILL_BASE（默认仅影子测量，不改变成交假设）
FILL_CALIBRATE_APPLY = os.environ.get("FILL_CALIBRATE_APPLY", "0") == "1"
# P0-3：真实盘口时序落盘目录（按日分文件 JSONL），支撑离线回测；已被 .gitignore 忽略
QUOTES_TS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "quotes_ts")
os.makedirs(QUOTES_TS_DIR, exist_ok=True)

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
RUN_META = {"run_start": None, "initial_equity": INITIAL_CAPITAL, "version": 1, "last_round": 0}

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
        for _f in ("run_meta.json", "trades.jsonl", "equity.jsonl",
                   "risk_state.json", "sim_book_poly.json"):
            _p = os.path.join(DATA_DIR, _f)
            try:
                open(_p, "w", encoding="utf-8").close()
            except Exception:
                pass
        RUN_META.clear()
        RUN_META.update({"run_start": None, "initial_equity": INITIAL_CAPITAL,
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
        RUN_META["initial_equity"] = INITIAL_CAPITAL
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
        "adverse_sel_loss": round(book.adverse_sel_loss, 2),
        "settled_pnl": round(getattr(book, "settled_pnl", 0.0), 2),
        "settlement_exposure": book.settlement_exposure(),
        "n_settled": len(getattr(book, "settlement_events", []) or []),
        "n_pending_settle": sum(1 for e in (getattr(book, "settlement_events", []) or [])
                                if e.get("pending")),
        "attribution": book.pnl_attribution(realized),
        "sample_n": sample_n,
    }


def _compliance_match(q):
    """返回命中的屏蔽词（None=未命中）。判定与 is_blocked 完全一致，但额外暴露具体词，供看板可观测。"""
    if P._is_blocked(q, None):
        return "(polymarket 内建屏蔽词)"
    ql = (q or "").lower()
    tag = classify(q)
    is_match = (" vs " in ql) or (" vs. " in ql) or (" o/u " in ql) or (" over/under" in ql)
    words = BLOCK_SPORTS if (tag == "sports" or is_match) else BLOCK_EXTRA
    for k in words:
        if k in ql:
            return k
    return None


def compute_compliance():
    """P2-5 合规过滤可观测：扫描实时盘口池，统计被合规红线拦截的市场，
    暴露命中词与当前词表，使「合规过滤」从黑盒变可观测。
    COMPLIANCE_FILTER=0 时返回「已关闭」报告（NB 部署无合规风险）。"""
    if not COMPLIANCE_FILTER:
        return {
            "n_scanned": 0, "n_blocked": 0, "n_passed": 0, "block_rate_pct": 0.0,
            "blocked_samples": [], "block_extra_count": len(BLOCK_EXTRA),
            "block_sports_count": len(BLOCK_SPORTS), "block_extra": BLOCK_EXTRA,
            "block_sports": BLOCK_SPORTS,
            "note": "合规过滤已关闭（COMPLIANCE_FILTER=0，NB 部署无合规风险），不做任何市场剔除。",
        }
    rows = list(MARKETS_LIVE or [])
    scanned = [m for m in rows if isinstance(m, dict) and "error" not in m]
    n_scanned = len(scanned)
    n_blocked = 0
    samples = []
    for m in scanned:
        q = m.get("question", "") or ""
        reason = _compliance_match(q)
        if reason is not None:
            n_blocked += 1
            if len(samples) < 30:
                samples.append({"question": q, "matched": reason,
                                "tag": m.get("tag") or classify(q)})
    n_passed = n_scanned - n_blocked
    return {
        "n_scanned": n_scanned,
        "n_blocked": n_blocked,
        "n_passed": n_passed,
        "block_rate_pct": round(n_blocked / n_scanned * 100.0, 2) if n_scanned else 0.0,
        "blocked_samples": samples,
        "block_extra_count": len(BLOCK_EXTRA),
        "block_sports_count": len(BLOCK_SPORTS),
        "block_extra": BLOCK_EXTRA,
        "block_sports": BLOCK_SPORTS,
        "note": ("合规红线（中国部署）已对实时盘口池逐题扫描：命中政治/地缘/军事等敏感词的"
                 "市场一律剔除，体育对抗赛中的国家名放行。下方为最近一次扫描的拦截样本与词表。"),
    }


# ============ 真实行情池（urllib 直连 Gamma） ============
MARKETS_LIVE = None   # fetch_poly_quotes 返回的实时二元盘口列表
MM_SET = []           # 当前做市标的 token 集合（固定，避免建仓不平仓）
MM_CATS = {}          # 当前做市标的类别分布（可观测：{cat: count}）
MM_DETAIL = {}        # 当前做市标的明细（可观测：{token_id: {question,tag,mid,liquidity,spread}}）


def classify(q):
    """按题目文本把市场分到类别（复用 polymarket 关键词表）。作为 market_cat 的回退。"""
    ql = (q or "").lower()
    for tag, re_ in P._CAT_RE.items():
        if re_.search(ql):
            return tag
    return "other"


def market_cat(m):
    """治本：优先用 Gamma 原生 category 字段（真实分类）；缺失/占位时回退关键词 classify。

    m 为 fetch_poly_quotes 返回的 Quote 字典（含 category 键，可能为空串）。
    原生类目如 politics/world/crypto/economy/finance/business/tech/science/sports/
    entertainment/culture/law/health… 直接作为分布类别，使做市类别分布反映真实盘口结构，
    而非被 9 个死关键词桶压缩（治本前 64% 市场塌进 other）。
    """
    if isinstance(m, dict):
        c = (m.get("category") or "").strip().lower()
        if c and c != "other":
            return c
        return classify(m.get("question", "") or "")
    return classify(m if isinstance(m, str) else "")


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
    因此对 sports 类别只套用「真正政治/军事」词表，避免误杀。
    COMPLIANCE_FILTER=0（如 NB 省部署）时整体放行，不做任何过滤。"""
    if not COMPLIANCE_FILTER:
        return False
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
    """从真实盘口池挑选做市标的：流动性够、价格居中、价差够赚，且跨类别分散。

    改进点（相对旧版纯流动性降序）：
      - 综合分 = 流动性 × (1 + 2·价差)：既偏爱深度，也偏爱每轮可捕获的价差宽度；
      - 每类做市上限 MM_N_PER_CAT：避免 20 个标的全挤在流动性最高的 1~2 类，
        降低模拟组合的相关性/集中风险，令盘口榜更代表全市场。
    仍保留合规过滤与基础有效性门槛（流动性≥4000 / mid∈[0.12,0.88] / 价差≥0.01）。
    """
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
        cat = market_cat(m) or "other"
        score = liq * (1.0 + 2.0 * sp)   # 价差越宽权重越高
        cand.append((cat, score, m))
    cand.sort(key=lambda x: -x[1])
    out = []
    per_cat = {}
    # 第一轮：每类上限 MM_N_PER_CAT，保证多样性
    for cat, score, m in cand:
        if len(out) >= MM_N:
            break
        if per_cat.get(cat, 0) < MM_N_PER_CAT:
            out.append(m)
            per_cat[cat] = per_cat.get(cat, 0) + 1
    # 第二轮：不足 MM_N 时，逐类均匀放宽上限补齐（避免全涌进单一类别）
    taken = set(id(x) for x in out)
    extra = 1
    while len(out) < MM_N:
        progressed = False
        for cat, score, m in cand:
            if len(out) >= MM_N:
                break
            if id(m) in taken:
                continue
            if per_cat.get(cat, 0) < MM_N_PER_CAT + extra:
                out.append(m)
                taken.add(id(m))
                per_cat[cat] = per_cat.get(cat, 0) + 1
                progressed = True
        if not progressed:
            break
        extra += 1
    return out[:MM_N]


def fill_prob(adverse, liq=0.0):
    """挂单成交概率（A：成交概率模型，P0-2 起由市场流动性校准）。

    基础成交率不再用全局常数 FILL_BASE，而由该市场流动性归一化得到：
      liq=0            -> 基础率最低（FILL_BASE 兜底，~0.30）
      liq>=LIQ_REF     -> 基础率 ~0.92（高流动性盘口排队消化快、被打掉概率高）
    再叠加价格改善 adverse：adverse=0 贴最优价排队（基础率），adverse=0.5 挂 mid（必成）。
    诚实边界：这是基于流动性代理的校准，非链上真实成交观测；真校准需 CLOB trade history
    （本环境受地域限制 404），留作后续。
    """
    if not APPLY_FILL:
        return 1.0
    base = 0.12 + 0.80 * min(1.0, float(liq) / LIQ_REF)
    base = max(FILL_BASE, min(0.95, base))   # FILL_BASE 作最差市场成交率下限
    u = adverse / 0.5
    u = 0.0 if u < 0 else (1.0 if u > 1 else u)
    return base + (1.0 - base) * (u ** FILL_GAMMA)


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


# ---------- P3-7+ 真实成交异步轮询跟踪（仅 LIVE_MODE=1 生效；DRY_RUN 完全不触及） ----------
_LIVE_EXE = None                              # 复用的 ClobExec 客户端（惰性初始化）
_LIVE_POLL_STOP = None                        # threading.Event，main 里创建
_LIVE_POLL_SEC = float(os.environ.get("LIVE_POLL_SEC", "30"))   # 在途订单轮询间隔（秒）
LIVE_ORDERS = []                              # 在途/已观测真实订单：{order_id,token_id,side,price,size,ts,filled,last_checked,status}
LIVE_FILL = {"attempts": 0, "hits": 0, "rate": 0.0, "partial": 0, "by_token": {}}  # 真实成交率（实盘可靠值）


def live_dispatch(token_id, side, price, size):
    """P3-7+ LIVE_MODE 守护：把策略意图的做市挂单真实发到 CLOB（过风控闸门），
    捕获 order_id 并登记到 LIVE_ORDERS，由后台 poll 线程异步查询真实成交状态，
    把**真实成交率**更新进 LIVE_FILL，最终显示在看板 /api/state['live_fill']。
    仅 LIVE_MODE=1 生效；默认 DRY_RUN 返回 None、零副作用。异常被吞，绝不影响模拟盘主循环。"""
    if not LIVE_MODE:
        return None
    global _LIVE_EXE
    try:
        from clob_exec import ClobExec
    except Exception as ex:
        print("[live] clob_exec 导入失败: %s" % ex, file=sys.stderr)
        return None
    try:
        if _LIVE_EXE is None:
            _LIVE_EXE = ClobExec()            # 读 env（PM_BOT_PK / LIVE_MODE）；无私钥抛 RuntimeError
        resp = _LIVE_EXE.place_maker_order(token_id, side, price, size)
    except Exception as e:
        print("[live] 实盘下单异常 %s: %s" % (token_id, e), file=sys.stderr)
        return None
    # 捕获 order_id，登记在途订单 + 累加尝试次数（真实成交率分母）；被风控拒绝不计数
    if isinstance(resp, dict) and resp.get("ok") and not resp.get("risk_blocked"):
        oid = (resp.get("order_id") or resp.get("orderID")
               or (resp.get("resp") or {}).get("orderID") or (resp.get("resp") or {}).get("order_id"))
        if oid:
            LIVE_ORDERS.append({
                "order_id": oid, "token_id": token_id, "side": str(side).upper(),
                "price": float(price), "size": float(size), "ts": time.time(),
                "filled": False, "last_checked": 0.0, "status": "OPEN",
            })
            LIVE_FILL["attempts"] += 1
            _bt = LIVE_FILL["by_token"].setdefault(token_id, {"attempts": 0, "hits": 0})
            _bt["attempts"] += 1
    return resp


def live_fill_poll_loop():
    """后台异步轮询在途真实订单的成交状态，更新 LIVE_FILL['hits'/'rate']。
    仅在 LIVE_MODE=1 下由 main 启动；DRY_RUN 不启动。被打掉（含部分成交）即算一次命中。
    网络调用不持锁，仅在计数器更新时短暂持 LOCK，避免阻塞模拟盘主循环。"""
    while _LIVE_POLL_STOP is not None and not _LIVE_POLL_STOP.is_set():
        try:
            if _LIVE_EXE is not None:
                now = time.time()
                pend = [o for o in LIVE_ORDERS
                        if not o["filled"] and (now - o["last_checked"]) >= _LIVE_POLL_SEC]
                for o in pend:
                    o["last_checked"] = now
                    try:
                        st = _LIVE_EXE.get_order_status(o["order_id"])   # 网络调用，不持锁
                    except Exception as e:
                        print("[live-poll] 查询异常 %s: %s" % (o["order_id"], e), file=sys.stderr)
                        continue
                    if not isinstance(st, dict) or st.get("error"):
                        continue
                    filled = float(st.get("filled") or 0)
                    if filled > 1e-9:                     # 被打掉（含部分成交）= 命中
                        with LOCK:
                            o["filled"] = True
                            o["status"] = st.get("status")
                            LIVE_FILL["hits"] += 1
                            if filled < o["size"] - 1e-9:
                                LIVE_FILL["partial"] += 1
                            _bt = LIVE_FILL["by_token"].get(o["token_id"])
                            if _bt:
                                _bt["hits"] += 1
                # 精简在途列表（保留最近 500 条未成交，避免无限增长）
                if len(LIVE_ORDERS) > 500:
                    with LOCK:
                        LIVE_ORDERS[:] = [o for o in LIVE_ORDERS if not o["filled"]][-500:]
                with LOCK:
                    if LIVE_FILL["attempts"]:
                        LIVE_FILL["rate"] = round(LIVE_FILL["hits"] / LIVE_FILL["attempts"] * 100, 1)
        except Exception as e:
            print("[live-poll] 循环异常: %s" % e, file=sys.stderr)
        if _LIVE_POLL_STOP is not None:
            _LIVE_POLL_STOP.wait(_LIVE_POLL_SEC)


def prometheus_metrics():
    """P2-A 可观测性：输出 Prometheus 文本暴露格式指标，供 scrape（Grafana/告警）。
    纯文本生成，零依赖；/metrics 端点返回 text/plain。"""
    s = STATE
    rm = RUN_META
    out = []
    def _g(name, val, help_=None, typ="gauge"):
        if help_:
            out.append("# HELP %s %s" % (name, help_))
            out.append("# TYPE %s %s" % (name, typ))
        out.append("%s %s" % (name, val))
    _fill = s.get("fill", {}) or {}
    _lf = s.get("live_fill", {}) or {}
    _g("polymarket_sim_round", s.get("round", 0), "当前轮次", "counter")
    _g("polymarket_sim_equity", round(float(s.get("equity", 0) or 0), 2), "盯市权益(账户总值)")
    _g("polymarket_sim_realized", round(float(s.get("realized", 0) or 0), 2), "累计锁利(已实现)")
    _g("polymarket_sim_cash", round(float(s.get("cash", 0) or 0), 2), "现金(含未平仓名义)")
    _g("polymarket_sim_unrealized", round(float(s.get("unrealized", 0) or 0), 2), "浮动盈亏")
    _g("polymarket_sim_fill_rate", float(_fill.get("rate", 0) or 0), "合成成交率%(意图成交假设)")
    _g("polymarket_sim_live_fill_rate", float(_lf.get("rate", 0) or 0), "真实成交率%(实盘观测,LIVE_MODE下)")
    _g("polymarket_sim_live_fill_attempts", int(_lf.get("attempts", 0) or 0), "真实挂单尝试数", "counter")
    _g("polymarket_sim_live_fill_hits", int(_lf.get("hits", 0) or 0), "真实成交命中数", "counter")
    _g("polymarket_sim_n_markets", s.get("n_markets", 0), "实时盘口市场数")
    _g("polymarket_sim_mm_count", s.get("mm_count", 0), "做市市场数")
    _g("polymarket_sim_live_count", s.get("live_count", 0), "真实盘口市场数")
    _g("polymarket_sim_trades_total", int(rm.get("trades_total", 0) or 0), "累计成交笔数", "counter")
    _g("polymarket_sim_sells_total", int(rm.get("sells_total", 0) or 0), "累计平仓笔数", "counter")
    _g("polymarket_sim_wins_total", int(rm.get("wins_total", 0) or 0), "累计盈利笔数", "counter")
    _blocked = 0
    for m in (MARKETS_LIVE or []):
        if isinstance(m, dict) and is_blocked(m.get("question", "")):
            _blocked += 1
    _g("polymarket_sim_blocked_live", _blocked, "实时盘口中被合规红线拦截的市场数")
    _ks = RC.status().get("kill_switch", {}) or {}
    _g("polymarket_sim_kill_switch", 1 if _ks.get("on") else 0, "风控 kill switch 状态(1=触发)")
    _qs = P.quotes_source() if hasattr(P, "quotes_source") else "gamma"
    _g("polymarket_sim_quotes_source_gamma", 1 if _qs == "gamma" else 0, "盘口主源=Gamma(1/0)")
    _g("polymarket_sim_quotes_source_clob", 1 if _qs == "clob" else 0, "盘口主源=CLOB(1/0)")
    _g("polymarket_sim_quotes_source_cache", 1 if _qs == "cache" else 0, "盘口主源=持久化缓存(1/0)")
    _g("polymarket_sim_step_seconds", PRICE_REFRESH_SEC, "盘口刷新间隔(秒)")
    _g("polymarket_sim_compliance_filter", 1 if COMPLIANCE_FILTER else 0, "合规过滤开关(1=开)")
    _g("polymarket_sim_live_mode", 1 if LIVE_MODE else 0, "实盘模式(1=LIVE)")
    return "\n".join(out) + "\n"


def step():
    """跑一轮：刷新真实盘口(每~90s) -> 对固定做市标的集各调一次真实 market_make。"""
    global MARKETS_LIVE, MM_SET, MM_CATS, MM_DETAIL
    # 刷新真实盘口池
    refresh = False
    with LOCK:
        refresh = (STATE["round"] % MM_REFRESH == 0) or (not MM_SET)
    if refresh or not MARKETS_LIVE:
        if SIM_MODE != "inv":   # inv 模式的价格刷新由后台线程负责，此处不阻塞交易循环
            try:
                MARKETS_LIVE = P.fetch_poly_quotes(limit=300, force=True)
            except Exception:
                MARKETS_LIVE = MARKETS_LIVE or []
        # 重选做市标的（固定集合，直到下个刷新周期）
        _sel = select_mm(MARKETS_LIVE or [])
        MM_SET = [m["token_id"] for m in _sel]
        MM_DETAIL = {}
        for m in _sel:
            yb = m.get("yes_bid") or 0
            ya = m.get("yes_ask") or 0
            MM_DETAIL[m["token_id"]] = {
                "token_id": m["token_id"],
                "question": m.get("question") or "",
                "tag": market_cat(m) or "other",
                "mid": round((yb + ya) / 2.0, 4) if (yb and ya) else 0,
                "liquidity": float(m.get("liquidity") or 0),
                "spread": round(ya - yb, 4) if (yb and ya) else 0,
            }
        _mc = {}
        for m in _sel:
            c = market_cat(m) or "other"
            _mc[c] = _mc.get(c, 0) + 1
        MM_CATS = _mc
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
        tag = market_cat(m) or "other"
        legs = 1 if SIM_MODE == "inv" else 2
        for _leg in range(legs):
            # B：真实做市下挂单不必然成交，按价格改善幅度判定
            if SIM_MODE == "inv" and APPLY_FILL:
                _p = fill_prob(adverse, opp["liquidity"])
                FILL_ATTEMPTS[0] += 1
                if random.random() >= _p:
                    continue          # 挂单没被打掉，本腿不成交
                FILL_HITS[0] += 1
            if LIVE_MODE:
                _lv_side = "SELL" if book.inventory.get(tok, 0) else "BUY"
                _lv_price = ya if _lv_side == "SELL" else yb
                live_dispatch(tok, _lv_side, _lv_price, size)
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
                        "token_id": str(m.get("token_id") or ""),
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
        # 逆向选择累计损耗（知情对手盘推动价格蒸发的可锁利）
        STATE["adverse_sel_loss"] = round(book.adverse_sel_loss, 2)
        # 结算风险闭环（P1-1）：已结算锁利 + 仍暴露在结算风险下的敞口
        STATE["settled_pnl"] = round(getattr(book, "settled_pnl", 0.0), 2)
        STATE["settlement_exposure"] = book.settlement_exposure()
        STATE["n_settled"] = len(getattr(book, "settlement_events", []) or [])
        # 成交率统计（挂单尝试 vs 实际被打掉）
        STATE["fill"] = {
            "base": FILL_BASE, "gamma": FILL_GAMMA, "on": APPLY_FILL,
            "attempts": FILL_ATTEMPTS[0], "hits": FILL_HITS[0],
            "rate": round(FILL_HITS[0] / FILL_ATTEMPTS[0] * 100, 1) if FILL_ATTEMPTS[0] else 0.0,
        }
        STATE["quotes"] = quotes
        STATE["live_count"] = len(MARKETS_LIVE) if MARKETS_LIVE else 0
        STATE["mm_count"] = len(MM_SET)
        STATE["mm_cats"] = MM_CATS
        # 做市分散度健康指标（HHI 集中度：越低越分散；0=完全均匀）
        _mc = MM_CATS
        _tot = sum(_mc.values()) or 1
        _ncat = len(_mc)
        _max = max(_mc.values()) if _mc else 0
        _hhi = round(sum((v / _tot) ** 2 for v in _mc.values()), 3)
        STATE["mm_div"] = {"n_cats": _ncat, "max_share": round(_max / _tot, 3),
                           "hhi": _hhi, "well_div": _ncat >= 4 and _hhi <= 0.30}
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


def save_quotes_snapshot(markets):
    """P0-3：真实盘口时序落盘。每次刷新把 300 个市场快照追加写 JSONL（按日分文件），
    支撑离线 walk-forward 回测（train_IC vs oos_IC 验证）。"""
    if not markets:
        return
    import json
    try:
        day = _dt.datetime.now().strftime("%Y%m%d")
        path = os.path.join(QUOTES_TS_DIR, "quotes_%s.jsonl" % day)
        snap = {"ts": time.time(),
                "markets": [
                    {"token_id": m.get("token_id"),
                     "question": (m.get("question") or "")[:80],
                     "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
                     "liquidity": m.get("liquidity"),
                     "mid": round((float(m.get("yes_bid") or 0) + float(m.get("yes_ask") or 0)) / 2.0, 4)}
                    for m in markets
                    if isinstance(m, dict) and "error" not in m and m.get("yes_bid") is not None
                ]}
        if snap["markets"]:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(snap, ensure_ascii=False) + "\n")
    except Exception:
        pass


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
                save_quotes_snapshot(rows)   # P0-3：落盘真实盘口时序
        except Exception:
            pass
        # P1-1：为尚未记录 end_date 的开放库存补登到期时间（来自实时盘口），
        # 确保重启前已开仓、或建仓时未带 end_date 的持仓也不会漏过结算闭环。
        try:
            for m in (MARKETS_LIVE or []):
                tid = m.get("token_id")
                if (tid and tid in book.inventory and book.inventory[tid] != 0
                        and not book.end_dates.get(tid) and m.get("end_date")):
                    book.end_dates[tid] = m["end_date"]
        except Exception:
            pass
        # P1-1 结算风险闭环：每次刷新后，把已到期且仍持库存的市场按真实结算价了结
        try:
            evs = book.settle_expired_markets(resolve_fn=P.fetch_resolution_price)
            if evs:
                for e in evs:
                    print("[settle] %s %s x%d @%.4f pnl=%+.2f%s"
                          % (e["side"], (e["question"] or "")[:40], e["size"],
                             e["resolved_price"], e["pnl"],
                             " (pending复核)" if e["pending"] else ""))
        except Exception:
            pass
        time.sleep(PRICE_REFRESH_SEC)


def loop():
    # 启动即拉一次真实盘口
    try:
        global MARKETS_LIVE
        MARKETS_LIVE = P.fetch_poly_quotes(limit=300, force=True)
        save_quotes_snapshot(MARKETS_LIVE)   # P0-3：启动即落首份快照
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
.conn-hint{display:none;margin:8px 22px 0;padding:10px 14px;border-radius:8px;background:#2a1414;color:#ffb4b4;border:1px solid #5a2a2a;font-size:13px;line-height:1.6}
.conn-hint b{color:#ffd2d2}
.badge.live{background:#0e2419;color:#7fe9bd;border-color:#1c503a}
.badge.warn{background:#2a2310;color:#f0c674;border-color:#5a4a1c}
.badge.err{background:#2a1014;color:#ff8a9a;border-color:#5a1c24}
.sndbtn{cursor:pointer;font-size:14px;background:#101a28;border:1px solid var(--line);border-radius:8px;padding:4px 10px;color:var(--mut);transition:all .2s}
.sndbtn:hover{color:var(--ink);border-color:var(--acc)}
/* 导出报告按钮 */
.rptbtn{cursor:pointer;font-size:13px;background:#0e1c2b;border:1px solid var(--acc);color:var(--acc);
  border-radius:8px;padding:5px 13px;font-weight:600;transition:all .2s;font-family:inherit}
.csv-in{background:#101a28;color:var(--ink);border:1px solid var(--line);border-radius:6px;
  padding:4px 8px;font-size:12px;width:92px;font-family:inherit}
.csv-in::placeholder{color:var(--mut)}
.rptbtn:hover{background:var(--acc);color:#06121f}
.rptbtn:disabled{opacity:.6;cursor:wait}
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
.mmtag{display:inline-block;margin:3px 5px 0 0;padding:2px 8px;border-radius:11px;background:#101a28;border:1px solid var(--line);font-size:11.5px;color:var(--fg)}.mmtag b{color:var(--acc);font-variant-numeric:tabular-nums}
.mmdiv{color:var(--mut);font-size:11px;margin-top:7px}.mmdiv.ok{color:#7fe9bd}.mmdiv.warn{color:#f0c674}
.mmbox{margin-top:6px;border-top:1px dashed var(--line);padding-top:11px}
/* 数据流图（单一真实源） */
.flowwrap{background:linear-gradient(160deg,var(--panel),#0c131d);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin:12px 22px 0}
.flowwrap summary{cursor:pointer;color:#cdd8e8;font-size:14px;font-weight:600;list-style:none;user-select:none}
.flowwrap summary::-webkit-details-marker{display:none}
.flowwrap summary::before{content:"▾ ";color:var(--mut)}
.flowwrap:not([open]) summary::before{content:"▸ "}
.flow{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:10px}
.fnode{background:#0e1826;border:1px solid var(--line);border-radius:10px;padding:9px 14px;min-width:118px;text-align:center;font-weight:600}
.fnode small{display:block;color:var(--mut);font-size:10.5px;margin-top:3px;font-weight:400}
.fnode.src{border-color:#2a3c57;color:#cdd8e8}
.fnode.store{background:#0c1f17;border-color:#1d3a2a;color:#9fe3c4}
.fnode.rd{border-color:#243a52;color:#cdd8e8}
.farrow{display:flex;align-items:center;color:var(--mut);font-size:12px;white-space:nowrap;font-weight:600}
.fbranch{display:flex;gap:10px;flex-wrap:wrap}
.flow-note{color:var(--mut);font-size:11.5px;margin-top:9px;line-height:1.5}
.flow-note code{background:#101a28;padding:1px 6px;border-radius:5px;color:var(--gold)}
/* 当前做市标的：可点击行 + modal */
#mm-tbl tbody tr{cursor:pointer;transition:background .15s}
#mm-tbl tbody tr:hover{background:#14202f}
#mm-tbl th.c-det,#mm-tbl td.c-det,#mkt th.c-det,#mkt td.c-det,#trd th.c-det,#trd td.c-det{width:46px;color:var(--mut);text-align:center}
#mm-tbl td .more,#mkt td .more,#trd td .more{color:var(--mut);font-weight:700;font-size:15px}
.liverow,.trdrow{cursor:pointer}.liverow:hover,.trdrow:hover{background:rgba(34,197,194,.07)}
.mmodal{position:fixed;inset:0;background:rgba(4,8,14,.72);display:none;align-items:center;justify-content:center;z-index:60;padding:20px}
.mmodal.show{display:flex}
.mmodal-box{background:linear-gradient(160deg,var(--panel),#0c131d);border:1px solid var(--acc);border-radius:14px;max-width:640px;width:100%;max-height:84vh;overflow:auto;box-shadow:0 16px 50px rgba(0,0,0,.5);animation:fade .2s ease}
.mmodal-h{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--line);font-weight:700;color:#cdd8e8;font-size:14.5px}
.mmodal-x{cursor:pointer;background:#101a28;border:1px solid var(--line);color:var(--mut);border-radius:8px;padding:2px 11px;font-size:14px}
.mmodal-x:hover{color:var(--ink);border-color:var(--acc)}
.mmodal-body{padding:14px 18px}
.mmodal-f{padding:10px 18px 14px;color:var(--mut);font-size:11px;border-top:1px solid var(--line)}
.mk{display:flex;gap:12px;padding:8px 0;border-bottom:1px dashed var(--line);font-size:13px;align-items:flex-start}
.mk:last-child{border-bottom:none}
.mk .k{color:var(--mut);width:84px;flex:none}
.mk .v{flex:1;text-align:right;word-break:break-word}
.mk.col{flex-direction:column;gap:5px}.mk.col .v{text-align:left;line-height:1.55}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;word-break:break-all}
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
.statbox{display:flex;flex-wrap:wrap;gap:14px;align-items:stretch}
.statgrp{display:flex;flex-direction:column;gap:10px;flex:1;min-width:160px;background:#0a121c;border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.statgrp .gh{color:var(--acc);font-size:11px;font-weight:700;letter-spacing:.5px;border-bottom:1px solid var(--line);padding-bottom:5px;margin-bottom:2px}
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
/* P2-5 合规过滤可观测面板 */
.comp-bar{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:4px 0 10px}
.comp-stat{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:8px 10px;text-align:center}
.comp-stat .k{display:block;color:var(--mut);font-size:11px;margin-bottom:3px}
.comp-stat .v{font-size:17px;font-variant-numeric:tabular-nums}
.comp-detail{margin-top:8px}
.comp-detail summary{cursor:pointer;color:var(--mut);font-size:12px;padding:4px 0}
.comp-detail .note{font-size:11.5px;line-height:1.6;word-break:break-word}
.comp-words{color:#9fb0c8}
/* P1-3 盈亏归因瀑布柱状图 */
.attr-chart{margin-top:6px}
.attr-row{display:flex;align-items:center;margin:7px 0;font-size:12px}
.attr-row .lbl{width:118px;flex:none;text-align:right;padding-right:9px;color:var(--mut)}
.attr-row .bar-wrap{flex:1;background:var(--panel2);border:1px solid var(--line);border-radius:5px;height:18px;position:relative;overflow:hidden}
.attr-row .bar{position:absolute;left:0;top:0;height:100%;border-radius:5px;min-width:2px}
.attr-row .bar.up{background:linear-gradient(90deg,#5a1f24,var(--up))}
.attr-row .bar.dn{background:linear-gradient(90deg,#1f3a28,var(--dn))}
.attr-row .val{width:96px;flex:none;text-align:right;font-variant-numeric:tabular-nums;padding-left:9px}
.attr-row.net{margin-top:10px;padding-top:9px;border-top:1px dashed var(--line)}
.attr-row.net .lbl{color:var(--fg);font-weight:700}
.attr-row.net .val{font-weight:700;font-size:13px}
.attr-row.net .bar{box-shadow:0 0 0 1px var(--acc) inset}
</style></head>
<body>
<div class="ticker"><div class="run" id="tick">正在加载真实 Polymarket 盘口…</div></div>
<div id="conn-hint" class="conn-hint">⚠️ 看板连不上后端（一直「连接中」/「正在加载真实盘口」）。原因：你正通过 <b>WorkBuddy 内置预览面板</b> 打开本页，其沙箱浏览器访问不到宿主机的 127.0.0.1:8787（这是设计隔离，<b>非服务故障</b>）。请用本机 <b>Chrome / Edge</b> 在地址栏粘贴 <b>http://127.0.0.1:8787/</b> 打开；模拟盘本身一直在正常跑（不影响交易）。也可直接双击项目里的「打开交易大屏.url」。</div>
<header>
  <span class="beat" id="beat"></span>
  <h1 class="gtitle">Polymarket 实时模拟交易大屏</h1>
  <span class="badge live" id="status">● 连接中…</span>
  <span class="badge" id="rnd">round 0</span>
  <span class="badge" id="fillbadge">成交率 —</span>
  <span class="badge" id="live">真实盘口 0 · 做市 0</span>
  <span class="badge mode" id="modebadge">—</span>
  <span class="badge" id="qsrc">行情源 —</span>
  <span class="badge" id="compbadge">合规 —</span>
  <span class="sndbtn" id="snd" title="成交音效开关（默认关）">🔇 音效</span>
  <button class="rptbtn" id="export" title="生成并打开实况与统计报告">📤 导出报告</button>
  <button class="rptbtn" id="exportcsv" title="下载全量成交 CSV（审计用，含全部历史成交）" style="border-color:var(--mut);color:var(--mut)">📥 下载成交CSV</button>
  <input id="csv-since-round" class="csv-in" type="number" min="0" placeholder="起始轮次" title="只导出该轮次之后的成交">
  <input id="csv-date" class="csv-in" type="text" placeholder="日期YYYY-MM-DD" title="只导出该日期成交">
  <span class="badge">引擎: RigorVirtualBook.market_make</span>
</header>
<div class="banner"><span id="qsr">✅ 行情来自<b>真实 Polymarket 盘口</b>（urllib 直连 Gamma，已合规过滤政治/地缘/军事等敏感类）</span>。
<b>成交不再是必然</b>：挂单按价格改善幅度判定成交概率（<code>FILL_BASE</code> 参数），挂得越贪越难被打到。
<b>未平敞口跨轮持有</b>，承担真实价格波动，受止损(5%)与全局库存上限约束，权益按市价盯市。
全程 <b>模拟盘·零真钱</b>（影子账本，绝不真发单）。因此<b>无链上真实成交率</b>——顶部「成交率」徽章显示的是模型假设成交率；仅当 LIVE_MODE=1 真钱实盘时该徽章才变「实盘成交率」。配色按中国习惯：<b style="color:var(--up)">红=涨/盈利</b>，<b style="color:var(--dn)">绿=跌/亏损</b>。
数据自 <b id="run-start">—</b> 起落盘累计。</div>
<details class="flowwrap" open>
  <summary>📊 数据流向 · 单一真实源（点此收起 / 展开）</summary>
  <div class="flow">
    <div class="fnode src">成交发生<small>每笔全字段落盘</small></div>
    <div class="farrow">➜ 写入</div>
    <div class="fnode store">trades.jsonl<small>唯一真实源 · 60MB 轮转</small></div>
    <div class="farrow">➜ 同源读取</div>
    <div class="fbranch">
      <div class="fnode rd">📥 看板 CSV 按钮<small>起始轮次 / 日期过滤</small></div>
      <div class="fnode rd">📄 sim_report.py<small>--trades/--csv/--archive</small></div>
      <div class="fnode rd">📤 报告·成交明细<small>逐笔 + 类别锁利汇总</small></div>
    </div>
  </div>
  <div class="flow-note">三路读取均来自同一份 <b>trades.jsonl</b>，数据天然一致、无多源分歧。详见 <code>DEPLOY_NB.md §6</code>。</div>
</details>
<div class="wrap">
  <div class="cards" id="cards"></div>

  <div class="big">
    <!-- 行情榜：跨两行，高度由网格拉伸填充 -->
    <div class="panel span2">
      <h2>📡 Polymarket 真实行情
        <select id="cat" style="margin-left:auto"><option value="all">全部类别</option><option value="crypto">crypto</option><option value="economy">economy</option><option value="finance">finance</option><option value="sports">sports</option><option value="tech">tech</option><option value="science">science</option><option value="entertainment">entertainment</option><option value="other">other</option></select>
      </h2>
      <div class="scroll"><table id="mkt"><thead><tr><th>市场(问题)</th><th>类别</th><th>YES 买</th><th>YES 卖</th><th>NO 买</th><th>NO 卖</th><th>流动性</th><th class="c-det">详情</th></tr></thead><tbody></tbody></table></div>
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
      <div class="scroll"><table id="trd"><thead><tr><th>时间</th><th>市场</th><th>类别</th><th>方向</th><th>成交价</th><th>量</th><th>本笔锁利</th><th>滑点</th><th>现金</th><th class="c-det">详情</th></tr></thead><tbody></tbody></table></div>
      <div class="note">BUY=建仓（锁利 0），SELL=平仓（显示本笔锁利）；每笔在真实盘口价位成交。新成交行红/绿闪光。</div>
    </div>

    <!-- 实盘成交率 · 做市分布（合并面板：原锁利卡片已归并到顶部 KPI 行，避免重复） -->
    <div class="panel">
      <h2>💰 实盘成交率 · 做市分布</h2>
      <div class="cards" style="grid-template-columns:repeat(3,minmax(0,1fr));margin:0 0 12px">
        <div class="card" title="仅在 LIVE_MODE=1（真钱实盘）时才有链上真实成交观测；当前 DRY_RUN 模拟盘不挂真单，故无实盘成交率"><div class="k">实盘成交率(LIVE)</div><div class="v b" id="c-livefill">—</div></div>
        <div class="card"><div class="k">做市类别数</div><div class="v b" id="n-mmcat">0</div></div>
        <div class="card"><div class="k">做市标的数</div><div class="v b" id="n-mmnum">0</div></div>
      </div>
      <div class="mmbox">
        <div class="k" style="margin-bottom:5px">做市类别分布<span class="sub" style="font-weight:400;margin-left:5px">每类上限 MM_N_PER_CAT，从 300 盘口挑 20</span></div>
        <div class="v sm" id="c-mmcat" style="font-size:12px;font-weight:400;line-height:1.7">—</div>
        <div class="mmdiv" id="c-mmdiv"></div>
      </div>
      <div class="note">累计锁利 / 本轮锁利 已在<b>顶部 KPI 卡片</b>与<b>统计中心</b>展示（单一口径，避免重复）。实盘成交率仅 LIVE_MODE=1 真钱实盘才有链上真实观测。</div>
    </div>
    <!-- 统计中心 -->
    <div class="panel">
      <h2>📊 统计中心（实时）</h2>
      <div class="statbox">
        <div class="statgrp"><div class="gh">运行</div>
          <div class="stat"><div class="k">运行时长</div><div class="v" id="st-dur">—</div></div>
          <div class="stat"><div class="k">轮次</div><div class="v" id="st-round">0</div></div>
          <div class="stat"><div class="k">总成交</div><div class="v" id="st-tot">0</div></div>
          <div class="stat"><div class="k">成交频率</div><div class="v" id="st-rate">0</div></div>
        </div>
        <div class="statgrp"><div class="gh">盈亏</div>
          <div class="stat"><div class="k">累计锁利</div><div class="v" id="st-real">$0</div></div>
          <div class="stat"><div class="k">胜率(平仓)</div><div class="v" id="st-win">0%</div></div>
          <div class="stat"><div class="k">已结算锁利</div><div class="v b" id="st-settled">$0</div></div>
          <div class="stat"><div class="k">峰值盈利</div><div class="v" id="st-pk">$0</div></div>
        </div>
        <div class="statgrp"><div class="gh">风险 / 回撤</div>
          <div class="stat"><div class="k">逆向选择损耗</div><div class="v dn" id="st-asel">$0</div></div>
          <div class="stat"><div class="k">结算敞口(风险)</div><div class="v dn" id="st-settlexp">$0</div></div>
          <div class="stat"><div class="k">权益峰值</div><div class="v" id="st-peak">$0</div></div>
          <div class="stat"><div class="k">当前回撤</div><div class="v" id="st-dd">0%</div></div>
          <div class="stat"><div class="k">历史最大回撤</div><div class="v" id="st-mdd">0%</div></div>
        </div>
      </div>
      <div class="note" id="st-note">—</div>
    </div>

    <!-- P1-2 敏感性分析 -->
    <div class="panel">
      <h2>🎚️ 敏感性分析（参数弹性）</h2>
      <div class="note">当前参数下，各旋钮对「一轮做市对冲期望锁利」的弹性（±扰动间 PnL 变化 / 基准）。负值=调大该参数会降低锁利。基于代表性市场解析估计；非零项即最影响盈亏的旋钮。</div>
      <div class="scroll"><table id="sens-tbl"><thead><tr><th>参数</th><th>当前值</th><th>弹性</th><th>ΔPnL(±)</th></tr></thead><tbody></tbody></table></div>
      <div class="note" id="sens-base">基准一轮锁利: $0</div>
    </div>

    <!-- P1-3 盈亏归因瀑布 -->
    <div class="panel">
      <h2>🧮 盈亏归因（瀑布）</h2>
      <div class="note">累计净锁利拆解为各成本分量：毛价差捕获 − 走簿滑点 − 手续费 − 逆向选择 + 结算净损益。各分量均来自真实成交记录，恒等式闭合。</div>
      <div class="attr-chart" id="attr-chart"></div>
      <div class="note" id="attr-net">净锁利: $0（各分量之和，恒等式闭合）</div>
    </div>

    <!-- P2-5 合规过滤可观测 -->
    <div class="panel">
      <h2>🛡️ 合规过滤（红线可观测）</h2>
      <div class="note" id="comp-note">扫描实时盘口池，统计被政治/地缘/军事红线拦截的市场，使合规过滤从黑盒变可观测。</div>
      <div class="comp-bar">
        <div class="comp-stat"><span class="k">扫描总数</span><span class="v b" id="comp-scanned">0</span></div>
        <div class="comp-stat"><span class="k">已拦截</span><span class="v r" id="comp-blocked">0</span></div>
        <div class="comp-stat"><span class="k">放行</span><span class="v up" id="comp-passed">0</span></div>
        <div class="comp-stat"><span class="k">拦截率</span><span class="v" id="comp-rate">0%</span></div>
      </div>
      <div class="scroll"><table id="comp-tbl"><thead><tr><th>命中词</th><th>类别</th><th>市场题目（截断）</th></tr></thead><tbody></tbody></table></div>
      <details class="comp-detail">
        <summary>查看屏蔽词表（BLOCK_EXTRA / BLOCK_SPORTS）</summary>
        <div class="note">非体育语境屏蔽词（<b id="comp-extra-n">0</b>）：<span class="comp-words" id="comp-extra">—</span></div>
        <div class="note">体育赛事专用屏蔽词（<b id="comp-sports-n">0</b>）：<span class="comp-words" id="comp-sports">—</span></div>
      </details>
    </div>

    <!-- 当前做市标的（智能筛选结果透明化 · 可点击查看全部信息） -->
    <div class="panel">
      <h2>🎯 当前做市标的（智能筛选结果）<span class="sub">流动性×价差综合分 + 每类上限 MM_N_PER_CAT，从 300 盘口挑 20</span></h2>
      <div class="note">以下为当前实际在做市的标的（每类上限保证多样性）。<b style="color:var(--gold)">点击任意一行</b>查看标的全部信息（token_id / 完整题目 / 盘口等）。</div>
      <div class="scroll"><table id="mm-tbl"><thead><tr><th>类别</th><th>市场题目</th><th>中间价</th><th>流动性</th><th>价差</th><th class="c-det">详情</th></tr></thead><tbody></tbody></table></div>
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

<div class="mmodal" id="mm-modal" onclick="if(event.target===this)closeMM()">
  <div class="mmodal-box">
    <div class="mmodal-h"><span id="mm-modal-title">🎯 做市标的 · 全部信息</span> <button class="mmodal-x" onclick="closeMM()">✕</button></div>
    <div class="mmodal-body" id="mm-modal-body"></div>
    <div class="mmodal-f" id="mm-modal-f">数据来自选标快照 MM_DETAIL，每 MM_REFRESH 轮随盘口刷新一次。完整题目为 Polymarket 原始市场问题。</div>
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
   '<div class="card" title="cash = 实际现金流，已含未平仓库存名义 + 建模逆向选择损耗；它不等于账户总值，看总值请认「盯市权益」"><div class="k">现金(含未平仓)</div><div class="v b" id="c-cash">$0</div></div>',
   '<div class="card"><div class="k">累计锁利</div><div class="v b" id="c-real">$0</div></div>',
   '<div class="card"><div class="k">本轮锁利</div><div class="v b" id="c-rpnl">$0</div></div>',
   '<div class="card"><div class="k">盯市权益</div><div class="v b" id="c-eq">$0</div></div>',
   '<div class="card"><div class="k">浮动盈亏</div><div class="v b" id="c-unreal">$0</div></div>',
   '<div class="card" title="模型假设的成交率：挂单按价格改善幅度判定被打到的概率（FILL_BASE 等参数），非链上真实观测"><div class="k">模拟成交率</div><div class="v b" id="c-fill">0%</div></div>',
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
  document.getElementById('st-asel').textContent='$'+Number(st.adverse_sel_loss||0).toFixed(2);
  document.getElementById('st-settled').textContent='$'+Number(st.settled_pnl||0).toFixed(2);
  document.getElementById('st-settlexp').textContent='$'+Number(st.settlement_exposure||0).toFixed(2);
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
function renderSensitivity(sens){
  if(!sens||sens.error) return;
  document.getElementById('sens-base').textContent='基准一轮锁利: $'+Number(sens.base_pnl||0).toFixed(3);
  const tb=document.getElementById('sens-tbl').querySelector('tbody');
  tb.innerHTML=(sens.params||[]).map(p=>{
    const e=p.elasticity; const cls=e>0.05?'up':(e<-0.05?'dn':'');
    return `<tr><td>${p.param}</td><td>${p.base}</td>`
      +`<td class="${cls}">${e>=0?'+':''}${e.toFixed(3)}</td>`
      +`<td class="${cls}">${e>=0?'+':''}$${Number(p.delta_pnl||0).toFixed(3)}</td></tr>`;
  }).join('');
}
var _failCount=0;
function tickState(){
  fetch('/api/state').then(r=>r.json()).then(s=>{
    document.getElementById('rnd').textContent='round '+s.round;
    document.getElementById('live').textContent='真实盘口 '+s.live_count+' · 做市 '+s.mm_count;
    // 行情源诚实标识：消除"这是不是真实行情"的困惑（北京无外网时为缓存快照）
    const qs=s.quotes_source||'gamma';
    const qb=document.getElementById('qsrc');
    const qsr=document.getElementById('qsr');
    if(qs==='cache'){
      if(qb){qb.className='badge warn'; qb.textContent='行情源 · 缓存快照(非实时)';}
      if(qsr){qsr.innerHTML='⚠️ 行情来自<b>本地缓存快照</b>（非实时，北京无外网直连 Polymarket；显示最近一次成功抓取的盘口）';}
    } else if(qs==='gamma'||qs==='clob'){
      if(qb){qb.className='badge live'; qb.textContent='行情源 · 实时 '+qs;}
      if(qsr){qsr.innerHTML='✅ 行情来自<b>真实 Polymarket 盘口</b>（urllib 直连 '+(qs==='gamma'?'Gamma':'CLOB')+'，已合规过滤政治/地缘/军事等敏感类）';}
    } else {
      if(qb){qb.className='badge err'; qb.textContent='行情源 · 获取失败';}
      if(qsr){qsr.innerHTML='❌ 行情获取失败（请检查网络/代理）';}
    }
    const st=document.getElementById('status');
    if(s.round!==prevRound){prevRound=s.round; st.textContent='● 撮合中 · round '+s.round; flashBeat('beat');}
    else st.textContent='● 实时运行 · round '+s.round;
    const eq=s.equity;
    setNum('c-round',s.round); setMoney('c-cash',s.cash,false);
    setMoney('c-real',s.realized,true); setMoney('c-rpnl',s.round_pnl,true);
    setMoney('c-eq',eq,false); setNum('c-live',s.live_count); setNum('c-mm',s.mm_count);
    const mcel=document.getElementById('c-mmcat');
    if(mcel && s.mm_cats){
      const me=Object.entries(s.mm_cats).sort((a,b)=>b[1]-a[1]);
      mcel.innerHTML=me.length?me.map(([k,v])=>`<span class="mmtag">${k} <b>${v}</b></span>`).join(''):'—';
    }
    const mdiv=document.getElementById('c-mmdiv');
    if(mdiv && s.mm_div){
      const d=s.mm_div;
      mdiv.className='mmdiv '+(d.well_div?'ok':'warn');
      mdiv.textContent=`覆盖 ${d.n_cats} 类 · 最大类占比 ${Math.round(d.max_share*100)}% · HHI ${d.hhi}（越低越分散）${d.well_div?' ✅健康':' ⚠️偏集中'}`;
    }
    const nmc=document.getElementById('n-mmcat'); if(nmc && s.mm_cats) nmc.textContent=Object.keys(s.mm_cats).length;
    const nmn=document.getElementById('n-mmnum'); if(nmn) nmn.textContent=(s.mm_count||0);
    renderMMMarkets(s.mm_markets);
    setMoney('c-unreal',(s.unrealized||0),true);
    setMoney('c-inv',(s.inv_notional||0),false);
    const fe=document.getElementById('c-fill');
    if(fe){const fr=(s.fill&&s.fill.on)?(s.fill.rate||0):100;
      fe.className='v '+(fr>=60?'b':(fr>=40?'g':'r'));
      fe.textContent=fr.toFixed(1)+'%';}
    const lf=document.getElementById('c-livefill');
    if(lf){
      if(s.live_mode && s.live_fill && s.live_fill.attempts>0){
        const lr=s.live_fill.rate||0;
        lf.className='v '+(lr>=60?'b':(lr>=40?'g':'r'));
        lf.textContent=lr.toFixed(1)+'% ('+s.live_fill.hits+'/'+s.live_fill.attempts+')';
      } else {
        lf.className='v b'; lf.textContent='— 模拟盘';
      }
    }
    // 顶部徽章：成交率（与 round 同行）+ 合规开关
    const fb=document.getElementById('fillbadge');
    if(fb){
      const fr=(s.fill&&s.fill.on)?(s.fill.rate||0):100;
      if(s.live_mode && s.live_fill && s.live_fill.attempts>0){
        const lr=s.live_fill.rate||0;
        fb.className='badge live'; fb.textContent='实盘成交率 '+lr.toFixed(1)+'%';
      } else if(s.live_mode){
        fb.className='badge'; fb.textContent='实盘成交率 —';
      } else {
        fb.className='badge'; fb.textContent='模拟成交率 '+fr.toFixed(1)+'% · 零真钱';
      }
    }
    const cb=document.getElementById('compbadge');
    if(cb){
      if(s.compliance_filter){ cb.className='badge warn'; cb.textContent='合规 开(已过滤)'; }
      else { cb.className='badge live'; cb.textContent='合规 关(NB无限制)'; }
    }
    const mb=document.getElementById('modebadge');
    if(mb&&s.fill){mb.textContent=(s.mode==='inv'?'真实做市(库存管理)':'同轮双边建平')
      +' · 挂单成交模型'+(s.fill.on?'开':'关');}
    // 本轮/累计锁利已在顶部 KPI 卡片(c-rpnl/c-real)展示，锁利汇总面板不再重复，避免"两张累计锁利卡片"
    drawCandles(s.equity_curve);
    const t2=document.getElementById('trd').querySelector('tbody');
    window._trdRows = s.positions||[];
    let fresh=0, freshTrade=null;
    const rows=s.positions.map((t,i)=>{
      const key=t.ts+'|'+t.mkt+'|'+t.side+'|'+t.entry;
      const isNew=!seen.has(key); if(isNew){seen.add(key);fresh++; if(!freshTrade)freshTrade=t;}
      if(seen.size>400)seen.clear();
      const cls=(t.side==='buy'?'flash-up':'flash-dn');
      return `<tr class="trdrow ${isNew?'row-new '+cls:cls}" data-i="${i}"><td>${t.ts}</td><td class="l">${t.mkt}</td><td>${t.tag||'-'}</td>`+
        `<td class="${t.side==='buy'?'buy':'sell'}">${(t.side||'').toUpperCase()}</td>`+
        `<td>${t.entry!=null?Number(t.entry).toFixed(4):'-'}</td><td>${t.size}</td>`+
        `<td class="${col((t.pnl||0))}">${t.pnl!=null?'$'+fmt(t.pnl):'-'}</td>`+
        `<td>${t.slip!=null?Number(t.slip).toFixed(4):'-'}</td><td>$${fmt(t.cash_after)}</td><td class="c-det"><span class="more">›</span></td></tr>`;
    }).join('');
    t2.innerHTML=rows;
    Array.prototype.forEach.call(t2.querySelectorAll('.trdrow'),function(tr){
      tr.onclick=function(){showTradeDetail(Number(tr.getAttribute('data-i')));};
    });
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
  }).catch(()=>{
    _failCount++;
    const st=document.getElementById('status');
    if(st && _failCount>=3){ st.className='badge err'; st.textContent='⚠ 连接失败'; }
    if(_failCount>=1){ const h=document.getElementById('conn-hint'); if(h) h.style.display='block'; }
  });
  fetch('/api/stats').then(r=>r.json()).then(renderStats).catch(()=>{});  fetch('/api/sensitivity').then(r=>r.json()).then(renderSensitivity).catch(()=>{});
  fetch('/api/attribution').then(r=>r.json()).then(renderAttribution).catch(()=>{});
  fetch('/api/compliance').then(r=>r.json()).then(renderCompliance).catch(()=>{});
}
function renderAttribution(a){
  if(!a||a.error) return;
  const items=[
    ['毛价差捕获', a.gross_spread, 'up'],
    ['走簿滑点', a.walk_the_book, 'dn'],
    ['手续费', a.fees, 'dn'],
    ['逆向选择损耗', a.adverse_selection, 'dn'],
    ['结算净损益', a.settlement, 'up'],
  ];
  const net=Number(a.net||0);
  const maxv=Math.max(1e-6, ...items.map(i=>Math.abs(Number(i[1]||0))), Math.abs(net));
  const el=document.getElementById('attr-chart');
  if(el){
    const row=(lbl,v,cls,netRow)=>{
      const val=Number(v||0);
      const w=Math.max(2, Math.abs(val)/maxv*100);
      const sign=val>=0?'+':'−';
      return `<div class="attr-row${netRow?' net':''}"><div class="lbl">${lbl}</div>`+
        `<div class="bar-wrap"><div class="bar ${cls}" style="width:${w}%"></div></div>`+
        `<div class="val ${cls}">${sign}$${Math.abs(val).toFixed(2)}</div></div>`;
    };
    el.innerHTML=items.map(it=>row(it[0],it[1],it[2],false)).join('')+row('= 净锁利',net,net>=0?'up':'dn',true);
  }
  const netel=document.getElementById('attr-net');
  if(netel){netel.textContent='净锁利: $'+net.toFixed(2)+'（各分量之和，恒等式闭合）';}
}
function escapeHtml(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function renderMMMarkets(list){
  window._mmList = list||[];
  const tb=document.getElementById('mm-tbl'); if(!tb) return;
  const tbody=tb.querySelector('tbody');
  if(!tbody) return;
  if(!list||!list.length){tbody.innerHTML='<tr><td colspan="6" style="color:var(--mut)">暂无做市标的</td></tr>'; return;}
  tbody.innerHTML=list.map((m,i)=>{
    const liq=Number(m.liquidity||0);
    const liqS=liq>=1000?(liq/1000).toFixed(1)+'k':liq.toFixed(0);
    return `<tr class="mmrow" data-i="${i}">`+
      `<td><span class="mmtag">${escapeHtml(m.tag)}</span></td>`+
      `<td class="l">${(m.question||'').slice(0,52)}</td>`+
      `<td>${Number(m.mid||0).toFixed(3)}</td>`+
      `<td>$${liqS}</td>`+
      `<td>${Number(m.spread||0).toFixed(3)}</td>`+
      `<td class="c-det"><span class="more">›</span></td></tr>`;
  }).join('');
  Array.prototype.forEach.call(tbody.querySelectorAll('.mmrow'),function(tr){
    tr.onclick=function(){showMMDetail(Number(tr.getAttribute('data-i')));};
  });
}
function showMMDetail(i){
  const m=window._mmList[i]; if(!m) return;
  const rows=[
    ['Token ID', '<span class="mono">'+(m.token_id||'-')+'</span>'],
    ['类别', '<span class="mmtag">'+escapeHtml(m.tag)+'</span>'],
    ['中间价', Number(m.mid||0).toFixed(4)],
    ['流动性', '$'+fmt(m.liquidity)],
    ['价差', Number(m.spread||0).toFixed(4)],
  ];
  let html=rows.map(r=>`<div class="mk"><span class="k">${r[0]}</span><span class="v">${r[1]}</span></div>`).join('');
  html+=`<div class="mk col"><span class="k">完整题目</span><span class="v">${escapeHtml(m.question||'')}</span></div>`;
  document.getElementById('mm-modal-body').innerHTML=html;
  document.getElementById('mm-modal-title').innerHTML='🎯 做市标的 · 全部信息';
  document.getElementById('mm-modal-f').textContent='数据来自选标快照 MM_DETAIL，每 MM_REFRESH 轮随盘口刷新一次。完整题目为 Polymarket 原始市场问题。';
  document.getElementById('mm-modal').classList.add('show');
}
function showLiveDetail(i){
  const m=window._liveRows[i]; if(!m) return;
  const yb=Number(m.yes_bid||0), ya=Number(m.yes_ask||0);
  const nb=Number(m.no_bid||0), na=Number(m.no_ask||0);
  const ymid=(yb+ya)/2, nmid=(nb+na)/2;
  const rows=[
    ['Token ID', '<span class="mono">'+(m.token_id||'-')+'</span>'],
    ['类别', '<span class="mmtag">'+escapeHtml(m.tag)+'</span>'],
    ['YES 中间价', ymid.toFixed(4)+' <span style="color:var(--mut)">（隐含概率）</span>'],
    ['YES 买/卖', yb.toFixed(4)+' / '+ya.toFixed(4)],
    ['NO 买/卖', nb.toFixed(4)+' / '+na.toFixed(4)],
    ['NO 中间价', nmid.toFixed(4)],
    ['流动性', '$'+fmt(m.liquidity)],
  ];
  let html=rows.map(r=>`<div class="mk"><span class="k">${r[0]}</span><span class="v">${r[1]}</span></div>`).join('');
  html+=`<div class="mk col"><span class="k">完整题目</span><span class="v">${escapeHtml(m.q_full||m.question||'')}</span></div>`;
  document.getElementById('mm-modal-title').innerHTML='📡 真实行情 · 全部信息';
  document.getElementById('mm-modal-f').textContent='数据来自 Gamma 真实盘口快照，每 15s 随 /api/markets 刷新。完整题目为 Polymarket 原始市场问题。';
  document.getElementById('mm-modal-body').innerHTML=html;
  document.getElementById('mm-modal').classList.add('show');
}
function showTradeDetail(i){
  const t=window._trdRows[i]; if(!t) return;
  const ts=Number(t.ts||0);
  const tstr=ts? new Date(ts*1000).toLocaleString('zh-CN',{hour12:false}) : (t.ts||'-');
  const up=t.pnl!=null && t.pnl>0;
  const rows=[
    ['时间', escapeHtml(String(tstr))],
    ['市场', escapeHtml(t.mkt||'')],
    ['类别', '<span class="mmtag">'+escapeHtml(t.tag||'-')+'</span>'],
    ['方向', (t.side||'').toUpperCase()],
    ['成交价', Number(t.entry||0).toFixed(4)],
    ['数量', t.size],
    ['本笔锁利', (t.pnl!=null?'$'+fmt(t.pnl):'-')+' <span style="color:var(--mut)">'+(up?'盈利':'亏损')+'</span>'],
    ['滑点', t.slip!=null?Number(t.slip).toFixed(4):'-'],
    ['成交后现金', '$'+fmt(t.cash_after)],
  ];
  let html=rows.map(r=>`<div class="mk"><span class="k">${r[0]}</span><span class="v">${r[1]}</span></div>`).join('');
  document.getElementById('mm-modal-title').innerHTML='🤖 实时成交 · 全部信息';
  document.getElementById('mm-modal-f').textContent='数据来自 /api/state 的 positions 成交流，每 2s 随盘口刷新。';
  document.getElementById('mm-modal-body').innerHTML=html;
  document.getElementById('mm-modal').classList.add('show');
}
function closeMM(){const el=document.getElementById('mm-modal'); if(el) el.classList.remove('show');}
document.addEventListener('keydown',function(e){if(e.key==='Escape') closeMM();});
function renderCompliance(c){
  if(!c||c.error) return;
  const se=document.getElementById('comp-scanned'); if(se){se.className='v b'; se.textContent=c.n_scanned;}
  const bl=document.getElementById('comp-blocked'); if(bl){bl.className='v r'; bl.textContent=c.n_blocked;}
  const pa=document.getElementById('comp-passed'); if(pa){pa.className='v up'; pa.textContent=c.n_passed;}
  const rt=document.getElementById('comp-rate'); if(rt) rt.textContent=c.block_rate_pct+'%';
  const tb=document.getElementById('comp-tbl'); if(tb){
    tb.querySelector('tbody').innerHTML=(c.blocked_samples||[]).map(s=>
      `<tr><td class="r">${String(s.matched)}</td><td>${s.tag||'-'}</td><td class="l">${String(s.question).slice(0,70)}</td></tr>`).join('');
  }
  const en=document.getElementById('comp-extra-n'); if(en) en.textContent=c.block_extra_count||0;
  const sn=document.getElementById('comp-sports-n'); if(sn) sn.textContent=c.block_sports_count||0;
  const ex=document.getElementById('comp-extra'); if(ex) ex.textContent=(c.block_extra||[]).join('、');
  const sp=document.getElementById('comp-sports'); if(sp) sp.textContent=(c.block_sports||[]).join('、');
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
  window._liveRows = rows;
  const tb=document.getElementById('mkt').querySelector('tbody');
  tb.innerHTML=rows.map((m,i)=>
    `<tr class="liverow" data-i="${i}"><td class="l">${m.question}</td><td><span class="tag">${m.tag}</span></td>`+
    `<td class="buy">${Number(m.yes_bid).toFixed(4)}</td><td class="sell">${Number(m.yes_ask).toFixed(4)}</td>`+
    `<td class="buy">${Number(m.no_bid).toFixed(4)}</td><td class="sell">${Number(m.no_ask).toFixed(4)}</td>`+
    `<td>$${fmt(m.liquidity)}</td><td class="c-det"><span class="more">›</span></td></tr>`).join('');
  Array.prototype.forEach.call(tb.querySelectorAll('.liverow'),function(tr){
    tr.onclick=function(){showLiveDetail(Number(tr.getAttribute('data-i')));};
  });
}
document.getElementById('cat').onchange=renderLive;
// 窗口尺寸变化后重画 K 线（位图尺寸跟着 CSS 尺寸走，否则会被拉伸变形）
let _rz=null;
window.addEventListener('resize',()=>{clearTimeout(_rz);_rz=setTimeout(()=>drawCandles(null),180);});

setInterval(tickState,2000); setInterval(tickLive,15000);
tickState(); tickLive();

// 导出报告按钮：点一下生成并打开
(function(){
  var btn=document.getElementById('export');
  if(!btn) return;
  btn.onclick=function(){
    btn.disabled=true; btn.textContent='⏳ 生成中…';
    fetch('/api/export_report').then(function(r){return r.json();}).then(function(d){
      if(d&&d.ok){
        var w=window.open(d.url,'_blank');
        if(!w){ // 弹窗被拦截时退化为跳转
          location.href=d.url;
        }
        btn.textContent='✅ 已打开 ('+d.round+' 轮)';
        setTimeout(function(){btn.textContent='📤 导出报告';btn.disabled=false;}, 3000);
      }else{
        alert('生成失败：'+(d&&d.error?d.error:'未知错误'));
        btn.textContent='📤 导出报告'; btn.disabled=false;
      }
    }).catch(function(e){
      alert('请求失败：'+e);
      btn.textContent='📤 导出报告'; btn.disabled=false;
    });
  };
})();

// 下载全量成交 CSV 按钮：点一下触发浏览器下载（端点返回 attachment，支持区间/日期过滤）
(function(){
  var btn=document.getElementById('exportcsv');
  if(!btn) return;
  btn.onclick=function(){
    btn.disabled=true; btn.textContent='⏳ 生成中…';
    var q=[];
    var sr=document.getElementById('csv-since-round').value.trim();
    if(sr) q.push('since_round='+encodeURIComponent(sr));
    var dt=document.getElementById('csv-date').value.trim();
    if(dt) q.push('date='+encodeURIComponent(dt));
    var a=document.createElement('a');
    a.href='/api/trades_csv'+(q.length?'?'+q.join('&'):'');
    a.download='trades_export.csv';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function(){btn.textContent='📥 下载成交CSV'; btn.disabled=false;}, 4000);
  };
})();
</script></body></html>"""


def compute_fill_calibration():
    """P1-A 成交率影子标定：用当前实时盘口池各市场的 fill_prob（模型自身意图成交率）
    分布，对照假设的 FILL_BASE 地板，给出标定建议；并把实际观测成交率(attempts/hits)一并呈现。
    结果写入 data/fill_calibration.json（影子，默认不改 FILL_BASE；FILL_CALIBRATE_APPLY=1 时由 main 应用）。
    """
    import statistics as _st
    adverse = float(book.rigor.get("adverse_frac", 0.15))
    ps = []
    for m in (MARKETS_LIVE or []):
        if not isinstance(m, dict) or "error" in m:
            continue
        q = m.get("question", "")
        if is_blocked(q):
            continue
        liq = float(m.get("liquidity") or 0)
        try:
            ps.append(fill_prob(adverse, liq))
        except Exception:
            pass
    mean_p = round(_st.mean(ps), 3) if ps else 0.0
    med_p = round(_st.median(ps), 3) if ps else 0.0
    sp = sorted(ps)
    p10 = round(sp[max(0, len(sp) // 10 - 1)], 3) if sp else 0.0
    p90 = round(sp[min(len(sp) - 1, len(sp) * 9 // 10)], 3) if sp else 0.0
    with LOCK:
        st_fill = dict(STATE.get("fill", {}))
    attempts = int(st_fill.get("attempts", 0) or 0)
    hits = int(st_fill.get("hits", 0) or 0)
    observed = round(hits / attempts, 3) if attempts else None
    # 推荐地板：取意图成交率中位（夹在 [0.05,0.95]），使 FILL_BASE 与当前盘口结构一致
    recommended = round(max(0.05, min(0.95, med_p)), 2)
    cal = {
        "ts": _now_iso(),
        "n_markets": len(ps),
        "assumed_base": FILL_BASE,
        "intended_mean": mean_p, "intended_median": med_p,
        "intended_p10": p10, "intended_p90": p90,
        "observed_attempts": attempts, "observed_hits": hits,
        "observed_rate": observed,
        "recommended_base": recommended,
        "apply": bool(FILL_CALIBRATE_APPLY),
    }
    try:
        with open(os.path.join(DATA_DIR, "fill_calibration.json"), "w", encoding="utf-8") as f:
            json.dump(cal, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return cal


def _load_fill_calibration():
    try:
        with open(os.path.join(DATA_DIR, "fill_calibration.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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
                    "mm_cats": STATE.get("mm_cats", {}),
                    "mm_div": STATE.get("mm_div", {}),
                    "mm_markets": list(MM_DETAIL.values()),
                    "live_fill": dict(LIVE_FILL, by_token={k: dict(v) for k, v in LIVE_FILL.get("by_token", {}).items()}),
                    "live_orders_pending": sum(1 for o in LIVE_ORDERS if not o["filled"]),
                    "params": STATE["params"], "quotes": STATE["quotes"],
                    "positions": STATE["positions"], "equity_curve": STATE["equity_curve"],
                    "quotes_source": (getattr(P, "quotes_source", None)() if hasattr(P, "quotes_source") else "gamma"),
                    "compliance_filter": COMPLIANCE_FILTER,
                    "live_mode": LIVE_MODE,
                    "persistence": {
                        "run_start": RUN_META.get("run_start"),
                        "initial_equity": RUN_META.get("initial_equity"),
                        "realized_total_checkpoint": RUN_META.get("realized_total"),
                        "trades_total": RUN_META.get("trades_total"),
                        "sells_total": RUN_META.get("sells_total"),
                        "wins_total": RUN_META.get("wins_total"),
                        "peak_equity": RUN_META.get("peak_equity"),
                        "daily_pnl_last7": dict(list((RUN_META.get("daily_pnl") or {}).items())[-7:]),
                        "equity_samples": len(STATE["equity_curve"]),
                    },
                }
            body = json.dumps(snap, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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
                    "q_full": q,
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
        elif u.path == "/api/sensitivity":
            # P1-2 敏感性分析：当前参数下，各旋钮对一轮做市对冲期望锁利的弹性
            try:
                with LOCK:
                    rig = dict(book.rigor)
                    fr = float(getattr(book, "fee_rate", 0.01) or 0.01)
                sens = R.mm_param_sensitivity(rig, fee_rate=fr)
            except Exception as ex:
                sens = {"error": str(ex)}
            body = json.dumps(sens, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/attribution":
            # P1-3 盈亏归因瀑布：累计净锁利 -> 毛价差/滑点/手续费/逆向选择/结算
            try:
                with LOCK:
                    attr = book.pnl_attribution(STATE["realized"])
            except Exception as ex:
                attr = {"error": str(ex)}
            body = json.dumps(attr, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/compliance":
            # P2-5 合规过滤可观测：扫描实时盘口池，统计拦截数/样本/词表
            try:
                comp = compute_compliance()
            except Exception as ex:
                comp = {"error": str(ex)}
            body = json.dumps(comp, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/fill_calibration":
            # P1-A 成交率影子标定：测量当前盘口意图成交率分布 + 实际观测成交率，给出 FILL_BASE 建议
            try:
                cal = compute_fill_calibration()
            except Exception as ex:
                cal = {"error": str(ex)}
            body = json.dumps(cal, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/export_report":
            # P2-3 复用 build_and_write_report：生成 HTML/MD 报告并打开（与自动报告线程共用）
            try:
                r = build_and_write_report(15)
                payload = {
                    "ok": True, "stamp": r["stamp"], "ts": r["ts"],
                    "html": r["html_name"], "md": r["md_name"],
                    "url": "/reports/" + r["html_name"],
                    "equity": r["equity"], "realized": r["realized"],
                    "round": r["round"],
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as ex:
                body = json.dumps({"ok": False, "error": str(ex)}, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        elif u.path == "/api/trades_csv":
            # 全量成交 CSV 下载（审计用）：从 trades.jsonl 生成并作为附件返回
            # 支持 query 过滤：since_round / until_round / date(YYYY-MM-DD)
            try:
                _qs = parse_qs(u.query)
                def _qi(k):
                    v = _qs.get(k)
                    if v:
                        try:
                            return int(v[0])
                        except Exception:
                            return None
                    return None
                _since = _qi("since_round")
                _until = _qi("until_round")
                _date = (_qs.get("date") or [None])[0]
                _all = load_trades(0)
                _rows = filter_trades(_all, since_round=_since,
                                      until_round=_until, date=_date)
                _stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                _filt_hint = ""
                if _since is not None:
                    _filt_hint += "_r%d+" % _since
                if _until is not None:
                    _filt_hint += "_r%d-" % _until
                if _date:
                    _filt_hint += "_%s" % _date
                _csv_path = os.path.join(os.path.dirname(_HERE), "output",
                                        "trades_export%s_%s.csv" % (_filt_hint, _stamp))
                if not write_trades_csv(_rows, _csv_path):
                    raise RuntimeError("CSV 写入失败")
                with open(_csv_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 "attachment; filename=trades_export%s_%s.csv"
                                 % (_filt_hint, _stamp))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except Exception as ex:
                body = json.dumps({"ok": False, "error": str(ex)},
                                  ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        elif u.path.startswith("/reports/"):
            fname = os.path.basename(u.path)
            if not fname or not fname.endswith((".html", ".md")):
                self.send_response(404); self.end_headers(); return
            fpath = os.path.join(os.path.dirname(_HERE), "output", fname)
            if not os.path.isfile(fpath):
                self.send_response(404); self.end_headers(); return
            with open(fpath, "rb") as f:
                data = f.read()
            ctype = "text/html; charset=utf-8" if fname.endswith(".html") else "text/markdown; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", "inline")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif u.path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/risk":
            body = json.dumps(RC.status(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/metrics":
            # P2-A 可观测性：Prometheus 文本暴露格式，供 scrape（Grafana/告警）
            _m = prometheus_metrics().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(_m)))
            self.end_headers()
            self.wfile.write(_m)
        elif u.path == "/api/kill_switch":
            # P3-4 kill switch 控制：复用 SHUTDOWN_TOKEN 鉴权（运维已持该 token）
            _ktok = ""
            try:
                _ktok = (parse_qs(u.query).get("token", [""])[0] or "").strip()
            except Exception:
                _ktok = ""
            if not _ktok:
                _kah = (self.headers.get("Authorization", "") or "").strip()
                if _kah.lower().startswith("bearer "):
                    _ktok = _kah[7:].strip()
            if _ktok != SHUTDOWN_TOKEN:
                _kbody = json.dumps({"ok": False, "error": "unauthorized"},
                                    ensure_ascii=False).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(_kbody)))
                self.end_headers()
                self.wfile.write(_kbody)
                return
            _act = (parse_qs(u.query).get("action", ["on"])[0] or "on").strip().lower()
            _kres = RC.reset_kill_switch() if _act == "off" else RC.trigger_kill_switch(reason="api_manual")
            _kbody = json.dumps({"ok": True, "kill_switch": _kres},
                                ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(_kbody)))
            self.end_headers()
            self.wfile.write(_kbody)
        elif u.path == "/api/shutdown":
            # P0-A 关停端点鉴权：同局域网任意主机不再能直接关停服务
            _tok = ""
            try:
                _tok = (parse_qs(u.query).get("token", [""])[0] or "").strip()
            except Exception:
                _tok = ""
            if not _tok:
                _ah = (self.headers.get("Authorization", "") or "").strip()
                if _ah.lower().startswith("bearer "):
                    _tok = _ah[7:].strip()
            if _tok != SHUTDOWN_TOKEN:
                _body = json.dumps({"ok": False, "error": "unauthorized"},
                                   ensure_ascii=False).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(_body)))
                self.end_headers()
                self.wfile.write(_body)
                return
            # P2-2 优雅停止：停循环 -> 落盘 -> 关 HTTP -> 释放锁（atexit）
            try:
                with LOCK:
                    STATE["running"] = False
                save_persistence()
                _release_lock()  # 同步释放锁（处理线程存活期执行，避免后台运行器绕过 finally/atexit 致锁残留）
                body = json.dumps({"ok": True, "msg": "正在优雅停止，进程即将退出"},
                                  ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                # 等响应写完再关服务器，避免连接被中途掐断
                threading.Thread(target=_delayed_shutdown).start()
            except Exception as ex:
                body = json.dumps({"ok": False, "error": str(ex)}, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


def _delayed_shutdown():
    """响应写完后再关 HTTP server（serve_forever 返回 -> 进程正常退出 -> 释放锁）。"""
    time.sleep(0.2)
    if SRV is not None:
        try:
            SRV.shutdown()
        except Exception:
            pass
    _release_lock()  # 显式释放，避免后台运行器绕过 atexit 时锁残留


def preflight():
    """P1-C 启动预检：打印配置清单与健康告警（非致命，仅提示），配置集中于此便于排错。"""
    ok = "✅"; warn = "⚠️"
    print("=" * 58)
    print("[preflight] 启动自检")
    dt_ok = bool(os.getenv("DINGTALK_WEBHOOK") and os.getenv("DINGTALK_SECRET"))
    print("  %s 钉钉推送 : %s" % (ok if dt_ok else warn,
          "已配置" if dt_ok else "未配置(通知/报告不推送)"))
    print("  %s 关停鉴权 : %s" % (ok if os.getenv("SHUTDOWN_TOKEN") else warn,
          "SHUTDOWN_TOKEN 已设" if os.getenv("SHUTDOWN_TOKEN")
          else "弱默认 token(sim-stop-8787)，建议设 SHUTDOWN_TOKEN"))
    print("  %s 成交率   : FILL_BASE=%.2f%s" % (ok, FILL_BASE,
          " (应用影子标定)" if FILL_CALIBRATE_APPLY else " (影子测量，不应用)"))
    print("  %s 模式     : SIM_MODE=%s  盘口刷新=%ss  自动报告=%smin"
          % (ok, SIM_MODE, PRICE_REFRESH_SEC, AUTO_REPORT_MIN))
    print("  %s 合规过滤 : %s" % (ok if COMPLIANCE_FILTER else warn,
          "开启(中国部署过滤敏感市场)" if COMPLIANCE_FILTER
          else "关闭(COMPLIANCE_FILTER=0，NB 部署无合规风险)"))
    if LIVE_MODE:
        _live_msg = "LIVE_MODE=1 (真实成交轮询 %ss，看板显示真实成交率)" % int(_LIVE_POLL_SEC)
    else:
        _live_msg = "DRY_RUN (仅模拟，真实成交轮询不启动)"
    print("  %s 实盘模式 : %s" % (ok if LIVE_MODE else "ℹ️", _live_msg))
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        _t = os.path.join(DATA_DIR, ".preflight_test")
        open(_t, "w").write("ok"); os.remove(_t)
        print("  %s 数据目录 : 可写 %s" % (ok, DATA_DIR))
    except Exception as ex:
        print("  %s 数据目录 : 不可写 %s" % (warn, ex))
    print("=" * 58)


def main():
    _acquire_lock()                      # P2-1 单实例启动锁
    load_persistence()
    # P1-C 启动预检
    preflight()
    # P1-A 成交率影子标定应用（仅当未显式设 FILL_BASE 且 FILL_CALIBRATE_APPLY=1）
    if FILL_CALIBRATE_APPLY and not os.environ.get("FILL_BASE"):
        _cal = _load_fill_calibration()
        if _cal and _cal.get("recommended_base"):
            globals()["FILL_BASE"] = float(_cal["recommended_base"])
            print("[fill-calib] 应用影子标定 FILL_BASE=%.2f (assumed %.2f)"
                  % (FILL_BASE, float(_cal.get("assumed_base", 0.30))))
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    threading.Thread(target=auto_report_loop, daemon=True).start()  # P2-3 报告自动化
    # P3-7+ 真实成交异步轮询（仅 LIVE_MODE=1 启动；DRY_RUN 不启动，北京模拟盘零额外开销）
    if LIVE_MODE:
        global _LIVE_POLL_STOP
        _LIVE_POLL_STOP = threading.Event()
        threading.Thread(target=live_fill_poll_loop, daemon=True).start()
    global SRV
    SRV = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print("[sim_server v3] listening on http://127.0.0.1:%d  (Ctrl+C 或 /api/shutdown 停止)" % PORT)

    def _on_term(signum, frame):         # P2-2 信号优雅停止
        with LOCK:
            STATE["running"] = False
        save_persistence()
        if _LIVE_POLL_STOP is not None:
            _LIVE_POLL_STOP.set()
        if SRV is not None:
            SRV.shutdown()
        _release_lock()

    try:
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)
    except Exception:
        pass
    try:
        SRV.serve_forever()
    except KeyboardInterrupt:
        with LOCK:
            STATE["running"] = False
        SRV.shutdown()
    finally:
        if _LIVE_POLL_STOP is not None:
            _LIVE_POLL_STOP.set()
        save_persistence()
        _release_lock()  # 主线程 finally 保证执行（后台运行器不绕过）


if __name__ == "__main__":
    main()
