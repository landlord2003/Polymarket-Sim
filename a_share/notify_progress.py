# -*- coding: utf-8 -*-
"""P0/P1/P2 迭代进度推送：每完成一项，钉钉推送一次，自动进入下一项。

用法:
  python notify_progress.py <任务号> "<标题>" "行1 @@ 行2 @@ 行3"
行内用 " @@ " 分隔多行；中文直传，避免 shell 转义问题。
"""
import sys
import notify as N

SEP = " @@ "


def push(task_no, title, lines):
    body = "## ✅ %s %s\n\n" % (task_no, title)
    body += "\n".join("- " + ln for ln in lines) + "\n\n---\n> Quant-Trading 模拟盘迭代 · 自动推送"
    r = N.send_markdown("%s %s" % (task_no, title), body)
    if r is None:
        print("[push] 未配置钉钉 / 未发送（离线模式）")
    else:
        print("[push] errcode=%s" % r.get("errcode"))
    return r


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python notify_progress.py <任务号> <标题> <行1 @@ 行2>")
        sys.exit(1)
    task_no = sys.argv[1]
    title = sys.argv[2]
    lines = sys.argv[3].split(SEP) if len(sys.argv) > 3 else []
    push(task_no, title, lines)
