#!/usr/bin/env python3
"""Polymarket 模拟盘守护启动器（launch_sim.py）。

用途：
  - 供 Windows 计划任务 / 启动文件夹调用，使 sim_server 像守护进程一样常驻。
  - 先探测 127.0.0.1:8787 是否已在监听：已运行则静默跳过（避免重复实例抢端口）；
    端口空闲才拉起 sim_server.main()。
  - 这样即使 WorkBuddy 后台任务回收了 sim_server 进程，计划任务（每 N 分钟触发）
    也会自动把它拉起来，看板不再莫名"连接中"。

注意：本机无外网时行情来自缓存快照（quotes_source=cache），属正常，不影响常驻。
"""
import os
import sys
import socket

PORT = 8787
HERE = os.path.dirname(os.path.abspath(__file__))


def is_already_running(host: str = "127.0.0.1", port: int = PORT) -> bool:
    """尝试连接本机端口：连得上说明 sim_server 已在跑。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


if __name__ == "__main__":
    if is_already_running():
        print(f"[launch_sim] :{PORT} 已在监听，sim_server 运行中，跳过。")
        sys.exit(0)
    sys.path.insert(0, HERE)
    import sim_server  # noqa: E402
    print(f"[launch_sim] 端口空闲，拉起 sim_server (listen :{PORT}) ...")
    sim_server.main()
