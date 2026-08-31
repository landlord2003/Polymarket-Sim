# -*- coding: utf-8 -*-
"""Polymarket 模拟盘 / 实盘 —— NB 伙伴一键启动器（跨平台）。

为什么需要它：
  - 自动建 venv + 装依赖（requirements_nb.txt，含 websockets/py-clob-client/web3）
  - 检查 .env 是否存在（不存在则 cp .env.nb .env 并提示填值）
  - 启动前校验关键环境变量（SHUTDOWN_TOKEN；LIVE_MODE=1 时必须 PM_BOT_PK）
  - 崩溃自动拉起（有限重试 + 退避），/api/shutdown 优雅退出则不重试
  - 转发 Ctrl+C 给子进程做优雅停止

用法：
  python start_nb.py              # 直接起（假设依赖已装、.env 已就绪）
  python start_nb.py --setup      # 先建 venv + 装依赖，再起
  python start_nb.py --venv .venv # 指定虚拟环境目录
  python start_nb.py --check      # 只做环境校验，不起服务
编码：纯 ASCII 启动入口逻辑 + UTF-8 中文提示；Windows .bat 保持 ASCII 调本文件。
"""
import os
import sys
import time
import subprocess
import argparse

ROOT = os.path.dirname(os.path.abspath(__file__))
A_SHARE = os.path.join(ROOT, "a_share")
SIM = os.path.join(A_SHARE, "sim_server.py")
ENV = os.path.join(ROOT, ".env")
ENV_NB = os.path.join(ROOT, ".env.nb")
VENV = os.path.join(ROOT, ".venv")
REQ = os.path.join(ROOT, "requirements_nb.txt")
MAX_RETRIES = 10
BACKOFF = 3  # 秒


def _read_env_vals():
    """极简解析 .env，返回 dict（不依赖 python-dotenv）。"""
    d = {}
    try:
        with open(ENV, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return d


def _resolve_python(venv):
    if venv and os.path.isdir(venv):
        if os.name == "nt":
            cand = os.path.join(venv, "Scripts", "python.exe")
        else:
            cand = os.path.join(venv, "bin", "python")
        if os.path.isfile(cand):
            return cand
    return sys.executable


def _setup(venv):
    """建 venv + 装依赖。"""
    py = sys.executable
    if not os.path.isdir(venv):
        print("[start_nb] 创建虚拟环境 %s ..." % venv)
        subprocess.run([py, "-m", "venv", venv], check=True)
    vpy = _resolve_python(venv)
    print("[start_nb] 安装依赖 %s ..." % REQ)
    subprocess.run([vpy, "-m", "pip", "install", "-U", "pip"], check=False)
    subprocess.run([vpy, "-m", "pip", "install", "-r", REQ], check=True)
    return vpy


def _check():
    """环境校验；返回 True/False。"""
    ok = True
    if not os.path.isfile(SIM):
        print("[start_nb][✗] 找不到 %s" % SIM)
        return False
    if not os.path.isfile(ENV):
        if os.path.isfile(ENV_NB):
            print("[start_nb][!] .env 不存在，已复制 .env.nb -> .env，请编辑填真实值（PM_BOT_PK / SHUTDOWN_TOKEN 等）")
            import shutil
            shutil.copyfile(ENV_NB, ENV)
        else:
            print("[start_nb][✗] .env 与 .env.nb 都不存在，无法启动")
            return False
    env = _read_env_vals()
    live = (os.environ.get("LIVE_MODE") or env.get("LIVE_MODE", "0")) == "1"
    comp = (os.environ.get("COMPLIANCE_FILTER") or env.get("COMPLIANCE_FILTER", "1")) == "1"
    tok = os.environ.get("SHUTDOWN_TOKEN") or env.get("SHUTDOWN_TOKEN")
    pk = os.environ.get("PM_BOT_PK") or env.get("PM_BOT_PK")
    if not tok:
        print("[start_nb][⚠] SHUTDOWN_TOKEN 未设置，将用弱默认 sim-stop-8787（建议设强随机值）")
    else:
        print("[start_nb][✓] SHUTDOWN_TOKEN 已设")
    if live:
        if pk:
            print("[start_nb][✓] LIVE_MODE=1 且 PM_BOT_PK 已设（实盘模式）")
        else:
            print("[start_nb][✗] LIVE_MODE=1 但 PM_BOT_PK 缺失 —— 实盘无法签名下单，请先填钱包私钥")
            ok = False
    else:
        print("[start_nb][✓] LIVE_MODE=0（DRY_RUN 模拟，无真实资金风险）")
    print("[start_nb][%s] 合规过滤 COMPLIANCE_FILTER=%s" % ("✓" if not comp else "ℹ️", "0(关闭)" if not comp else "1(开启)"))
    if not os.path.isfile(REQ):
        print("[start_nb][⚠] requirements_nb.txt 不存在，跳过依赖检查")
    return ok


def _serve(py):
    """启动 sim_server.py，崩溃重试，优雅退出(0)不重试。"""
    print("[start_nb] 启动 %s" % SIM)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            rc = subprocess.run([py, SIM]).returncode
        except KeyboardInterrupt:
            print("\n[start_nb] 收到 Ctrl+C，转交子进程优雅停止")
            return 0
        if rc == 0:
            print("[start_nb] 子进程干净退出（returncode=0，疑似 /api/shutdown），停止看守")
            return 0
        print("[start_nb][!] 子进程异常退出 rc=%s（第 %d/%d 次），%ss 后重试" % (rc, attempt, MAX_RETRIES, BACKOFF))
        time.sleep(BACKOFF)
    print("[start_nb][✗] 已达最大重试次数 %d，放弃。请查看 output/sim_server.log" % MAX_RETRIES)
    return 1


def main():
    ap = argparse.ArgumentParser(description="Polymarket Sim NB 启动器")
    ap.add_argument("--setup", action="store_true", help="先建 venv + 装依赖再启动")
    ap.add_argument("--venv", default=VENV, help="虚拟环境目录（默认 .venv）")
    ap.add_argument("--check", action="store_true", help="只做环境校验，不起服务")
    args = ap.parse_args()

    if args.check:
        sys.exit(0 if _check() else 1)

    py = sys.executable
    if args.setup:
        py = _setup(args.venv)
    if not _check():
        sys.exit(1)
    sys.exit(_serve(py))


if __name__ == "__main__":
    main()
