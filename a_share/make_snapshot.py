# -*- coding: utf-8 -*-
"""
生成看板的离线快照 HTML：把当前真实行情 / 成交 / 统计数据直接烧进页面，
双击即可查看，不依赖 sim_server 是否在运行。

用法：
    .venv/Scripts/python.exe a_share/make_snapshot.py
    .venv/Scripts/python.exe a_share/make_snapshot.py --out output/sim_snapshot.html
"""
import argparse
import json
import os
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
DEFAULT_OUT = os.path.join(ROOT, "output", "sim_snapshot.html")
BASE = "http://127.0.0.1:8787"


def _get(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 HTML 路径")
    ap.add_argument("--base", default=BASE, help="服务地址")
    args = ap.parse_args()

    BASE = args.base.rstrip("/")

    print("从 %s 拉取当前实况 ..." % BASE)
    html = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(BASE + "/", timeout=20) as r:
        html = r.read().decode("utf-8")

    data = {}
    for name, path in (("state", "/api/state"), ("stats", "/api/stats"),
                       ("markets", "/api/markets")):
        try:
            data[name] = _get(path)
            print("  /api/%-8s OK" % name)
        except Exception as e:
            print("  /api/%-8s 失败: %s" % (name, e))
            data[name] = None

    if not data.get("state"):
        print("错误：拿不到 /api/state，服务是否在运行？")
        return 1

    payload = json.dumps(data, ensure_ascii=False)

    # 在主脚本执行前劫持 fetch，让页面直接用快照数据渲染
    inject = (
        "<script>\n"
        "window.__SNAP__ = " + payload + ";\n"
        "window.__SNAP_ONLY__ = true;\n"
        "(function(){\n"
        "  var S = window.__SNAP__ || {};\n"
        "  var MAP = {'/api/state':'state','/api/stats':'stats','/api/markets':'markets'};\n"
        "  var _f = window.fetch;\n"
        "  window.fetch = function(u, o){\n"
        "    if (typeof u === 'string'){\n"
        "      for (var k in MAP){\n"
        "        if (u.indexOf(k) === 0 && S[MAP[k]]){\n"
        "          var d = S[MAP[k]];\n"
        "          return Promise.resolve({ok:true, json:function(){return Promise.resolve(d);}});\n"
        "        }\n"
        "      }\n"
        "    }\n"
        "    return (_f ? _f(u, o) : Promise.reject(new Error('offline')));\n"
        "  };\n"
        "})();\n"
        "</script>\n"
    )

    # 插到 <body> 之后，确保早于页面主脚本执行
    idx = html.find("<body>")
    if idx == -1:
        print("错误：页面里找不到 <body>")
        return 1
    pos = idx + len("<body>")
    html = html[:pos] + "\n" + inject + html[pos:]

    # 顶部加一行快照说明
    note = (
        '<div style="background:#1b1207;border-bottom:1px solid #3a2a10;'
        'color:#e8c98a;padding:7px 22px;font-size:12.5px">'
        '📷 离线快照 · 生成于 %s · 数据为抓取瞬间状态，不会自动更新。'
        '查看实时滚动请访问 %s</div>'
    ) % (_ts(), BASE)
    hidx = html.find("</header>")
    if hidx != -1:
        html = html[:hidx + len("</header>")] + "\n" + note + html[hidx + len("</header>"):]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    st = data["state"]
    print()
    print("已生成：%s  (%.0f KB)" % (args.out, os.path.getsize(args.out) / 1024))
    print("  模式 %s | 轮次 %s | 权益 $%.2f | 成交率 %.1f%%"
          % (st.get("mode"), st.get("round"), st.get("equity", 0),
             (st.get("fill") or {}).get("rate", 0)))
    return 0


def _ts():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    sys.exit(main())
