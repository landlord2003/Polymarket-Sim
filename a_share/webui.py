"""本地 Web 可视面板（零额外依赖，仅 Python 标准库）

启动：  python a_share/webui.py
打开：  http://127.0.0.1:8787   （端口可改环境变量 QT_WEB_PORT）

功能：
  - 顶部控制条：日常盯盘 / 板块选股 / 全部运行 / 离线验证 / 推送钉钉
  - 下方 iframe 实时显示信号看板（与 run_daily 同一引擎、同一 HTML 模板）
  - 扫描在后台线程跑（AkShare 联网取数可能要 30–90 秒），页面每 2 秒轮询状态
  - 无需敲命令行：扫描由面板按钮启动，结果自动刷新到页面

说明：
  - A股仍只给信号不自动下单；「推送钉钉」勾选后，联网实跑且配置了 .env 时推送手机。
  - 离线验证：用合成数据跑通链路、不触网，适合先把引擎/界面跑顺。
"""

from __future__ import annotations

import os
import sys
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_daily
from run_daily import load_watchlist, build_report
from signal_engine import analyze_stock, StockResult
from screener import run_screener
from dashboard import render_dashboard
from notify import send_markdown, send_wecom

PORT = int(os.getenv("QT_WEB_PORT", "8787"))

_state = {
    "running": False,
    "mode": None,
    "offline": False,
    "push": False,
    "started_at": None,
    "finished_at": None,
    "html": None,
    "error": None,
    "log": [],
}
_lock = threading.Lock()


def _placeholder_html() -> str:
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<style>body{background:#0f1419;color:#9fb0c0;font-family:-apple-system,"
        "'Microsoft YaHei',sans-serif;display:flex;align-items:center;"
        "justify-content:center;height:60vh;text-align:center;margin:0}"
        "h2{color:#e6e6e6} p{font-size:13px}</style></head><body>"
        "<div><h2>📡 尚未运行</h2>"
        "<p>点击上方「日常盯盘」或「板块选股」开始扫描</p>"
        "<p style='color:#6b7888;font-size:12px'>"
        "首次联网取数约需 30–90 秒，请耐心等待，页面会自动刷新</p></div>"
        "</body></html>"
    )


def _run_scan(mode: str, offline: bool, push: bool):
    try:
        with _lock:
            _state["running"] = True
            _state["mode"] = mode
            _state["offline"] = offline
            _state["push"] = push
            _state["started_at"] = time.time()
            _state["finished_at"] = None
            _state["error"] = None
            _state["log"] = ["开始扫描…"]

        watch = load_watchlist()
        weights = watch.get("weights")
        results = []
        any_offline = False
        if mode in ("daily", "both"):
            for item in watch["watchlist"]:
                if offline:
                    results.append(StockResult(
                        symbol=item["symbol"], name=item.get("name", ""),
                        offline=True,
                        notes=["离线合成数据，仅验证引擎，未产生真实信号"]))
                else:
                    r = analyze_stock(item["symbol"], item.get("name", ""),
                                      rules=item.get("rules"), weights=weights)
                    if r.offline:
                        any_offline = True
                    results.append(r)

        scr = None
        show_wl = mode in ("daily", "both")
        if mode in ("screener", "both"):
            scr = run_screener(offline=offline)

        mode_tag = "offline" if (offline or any_offline) else "online"
        html = render_dashboard(results if show_wl else [],
                                screener_result=scr, mode=mode_tag,
                                show_watchlist=show_wl)

        pushed = False
        if push and not offline and not any_offline and mode in ("daily", "both"):
            try:
                title = f"A股信号日报 {datetime.today().strftime('%Y-%m-%d')}"
                rep = build_report(results)
                send_markdown(title, rep)
                send_wecom(rep)
                pushed = True
            except Exception as e:  # noqa
                _state["log"].append(f"推送失败: {e}")

        with _lock:
            _state["html"] = html
            _state["finished_at"] = time.time()
            _state["log"].append("扫描完成" + ("，已推送钉钉" if pushed else ""))
    except Exception as e:  # noqa
        with _lock:
            _state["error"] = str(e)
            _state["log"].append(f"错误: {e}")
    finally:
        with _lock:
            _state["running"] = False


CONTROL_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>量化信号面板</title>
<style>
* { box-sizing:border-box; }
body { margin:0; background:#0b0f14; color:#e6e6e6;
  font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
.bar { position:sticky; top:0; z-index:10; display:flex; align-items:center;
  gap:10px; flex-wrap:wrap; padding:12px 18px; background:#121821;
  border-bottom:1px solid #232c38; }
.bar h1 { font-size:16px; margin:0 14px 0 0; white-space:nowrap; }
button { background:#1f6feb; color:#fff; border:none; border-radius:8px;
  padding:8px 14px; font-size:13px; cursor:pointer; font-weight:600; }
button:hover { background:#2a7dff; }
button:disabled { background:#2a3340; color:#6b7888; cursor:not-allowed; }
label { font-size:13px; color:#9fb0c0; display:flex; align-items:center; gap:4px;
  cursor:pointer; user-select:none; }
.pill { padding:5px 12px; border-radius:16px; font-size:12px; font-weight:600; }
.pill.idle { background:#16341f; color:#5fd98a; }
.pill.run { background:#332c12; color:#e0c45a; }
#status { font-size:12px; color:#8b98a5; margin-left:auto; }
iframe { width:100%; height:calc(100vh - 58px); border:none; background:#0f1419; }
</style></head>
<body>
<div class="bar">
  <h1>🦀 量化信号面板</h1>
  <button id="b1" onclick="scan('daily')">📊 日常盯盘</button>
  <button id="b2" onclick="scan('screener')">🔎 板块选股</button>
  <button id="b3" onclick="scan('both')">🚀 全部运行</button>
  <label><input type="checkbox" id="offline"> 离线验证</label>
  <label><input type="checkbox" id="push"> 推送钉钉</label>
  <span id="pill" class="pill idle">🟢 空闲</span>
  <span id="status"></span>
</div>
<iframe id="board" src="/api/board"></iframe>
<script>
let lastFinished = null;
function scan(mode){
  const offline = document.getElementById('offline').checked;
  const push = document.getElementById('push').checked;
  fetch('/api/scan',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode,offline,push})})
    .then(r=>r.json()).then(j=>setStatus(j.msg||'已启动'))
    .catch(e=>setStatus('启动失败: '+e));
}
function setStatus(t){ document.getElementById('status').textContent = t; }
function poll(){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    const pill = document.getElementById('pill');
    const dis = s.running;
    ['b1','b2','b3'].forEach(id=>document.getElementById(id).disabled=dis);
    if(s.running){ pill.textContent='⏳ 扫描中…'; pill.className='pill run'; }
    else { pill.textContent='🟢 空闲'; pill.className='pill idle'; }
    if(s.error){ setStatus('❌ '+s.error); }
    else if(s.log && s.log.length){ setStatus(s.log[s.log.length-1]); }
    if(!s.running && s.finished_at && s.finished_at!==lastFinished){
      lastFinished = s.finished_at;
      document.getElementById('board').src = '/api/board?t='+Date.now();
    }
  }).catch(()=>{});
  setTimeout(poll, 2000);
}
window.onload = ()=>poll();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path in ("/", "/index.html"):
            self._send(200, CONTROL_HTML, "text/html; charset=utf-8")
        elif p.path == "/api/board":
            with _lock:
                html = _state["html"] or _placeholder_html()
            self._send(200, html, "text/html; charset=utf-8")
        elif p.path == "/api/status":
            with _lock:
                d = {k: _state[k] for k in
                     ("running", "mode", "offline", "push",
                      "started_at", "finished_at", "error", "log")}
            self._send(200, json.dumps(d, ensure_ascii=False),
                       "application/json; charset=utf-8")
        else:
            self._send(204, b"", "")

    def do_POST(self):
        p = urlparse(self.path)
        if p.path == "/api/scan":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                data = {}
            mode = data.get("mode", "daily")
            offline = bool(data.get("offline", False))
            push = bool(data.get("push", False))
            if mode not in ("daily", "screener", "both"):
                mode = "daily"
            with _lock:
                running = _state["running"]
            if running:
                self._send(200, json.dumps({"ok": False, "msg": "正在运行中，请稍候"}),
                           "application/json; charset=utf-8")
                return
            t = threading.Thread(target=_run_scan, args=(mode, offline, push),
                                 daemon=True)
            t.start()
            self._send(200, json.dumps({"ok": True, "msg": "已启动扫描"}),
                       "application/json; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"🦀 量化信号面板已启动： http://127.0.0.1:{PORT}")
    print("   在浏览器打开上面的地址，点按钮即可运行扫描（无需命令行）。Ctrl+C 退出。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
