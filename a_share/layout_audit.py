# -*- coding: utf-8 -*-
"""
看板布局几何审计：不依赖人眼看图，直接用无头浏览器测量每个元素的真实边界框。

原理：
  1) 生成离线快照 HTML（把 /api/state|stats|markets 内联进页面，劫持 fetch）
  2) 注入一段审计脚本，页面渲染完成后把自己量出来的结果写进 <pre id="__geo">
  3) 用无头 Edge 以多个视口宽度 --dump-dom，把 <pre> 里的内容抽出来

检出的问题类型：
  PANEL_OVERLAP  面板两两重叠
  CELL_OVERLAP   指标卡/统计格两两重叠
  CHILD_OVERFLOW 子元素横向/纵向溢出父容器
  TEXT_CLIP      文字被容器裁掉（内容宽 > 可视宽）
  TABLE/CANVAS   表格固有宽度、canvas 实际尺寸
  COL            三列各自的宽高

用法：
  python a_share/layout_audit.py                     # 默认 1920/1600/1440/1280/1100
  python a_share/layout_audit.py --widths 1920 1366
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BASE = "http://127.0.0.1:8787"
OUT_HTML = os.path.join(ROOT, "output", "_audit_page.html")
OUT_TXT = os.path.join(ROOT, "output", "layout_audit.txt")

EDGES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

AUDIT_JS = r"""
(function(){
  function run(){
    var out = [];
    var doc = document.documentElement;
    out.push('viewport=' + window.innerWidth + ' x ' + window.innerHeight);
    out.push('doc scrollW=' + doc.scrollWidth + ' clientW=' + doc.clientWidth +
             ' 横向溢出=' + (doc.scrollWidth - doc.clientWidth) + 'px');
    function nm(e){
      var h = e.querySelector ? e.querySelector('h2') : null;
      var t = ((h ? h.textContent : e.textContent) || '').trim().replace(/\s+/g,' ').slice(0,24);
      return t || (e.className || e.tagName);
    }
    function rect(e){ return e.getBoundingClientRect(); }
    function pair(list, label, cap){
      var ov = [];
      for (var i=0;i<list.length;i++) for (var j=i+1;j<list.length;j++){
        var a = rect(list[i]), b = rect(list[j]);
        var ix = Math.min(a.right,b.right) - Math.max(a.left,b.left);
        var iy = Math.min(a.bottom,b.bottom) - Math.max(a.top,b.top);
        if (ix > 1 && iy > 1) ov.push('  ' + label + ' [' + nm(list[i]) + '] x [' + nm(list[j]) +
                                       '] 重叠 ' + Math.round(ix) + 'x' + Math.round(iy) + 'px');
      }
      out.push(label + '=' + ov.length);
      for (var k=0;k<Math.min(ov.length,cap);k++) out.push(ov[k]);
    }
    pair([].slice.call(document.querySelectorAll('.panel')), 'PANEL_OVERLAP', 12);
    pair([].slice.call(document.querySelectorAll('.card, .stat')), 'CELL_OVERLAP', 10);

    var bad = [];
    [].slice.call(document.querySelectorAll('.panel, .card, .stat, .col, .wrap')).forEach(function(p){
      var pr = rect(p);
      [].slice.call(p.children).forEach(function(c){
        var cr = rect(c);
        if (!cr.width && !cr.height) return;
        var dx = Math.max(0, pr.left - cr.left) + Math.max(0, cr.right - pr.right);
        var dy = Math.max(0, cr.bottom - pr.bottom);
        if (dx > 2 || dy > 2) bad.push('  溢出 父[' + nm(p) + '] 子[' + (c.className || c.tagName) +
                                       '] 横向+' + Math.round(dx) + ' 纵向+' + Math.round(dy));
      });
    });
    out.push('CHILD_OVERFLOW=' + bad.length);
    for (var i=0;i<Math.min(bad.length,20);i++) out.push(bad[i]);

    var tc = [];
    [].slice.call(document.querySelectorAll('.card .v, .stat .v, .kv .v')).forEach(function(e){
      if (e.scrollWidth - e.clientWidth > 1)
        tc.push('  裁切 [' + (e.textContent||'').trim() + '] 需要' + e.scrollWidth + ' 只有' + e.clientWidth);
    });
    out.push('TEXT_CLIP=' + tc.length);
    for (var i=0;i<Math.min(tc.length,12);i++) out.push(tc[i]);

    [].slice.call(document.querySelectorAll('table')).forEach(function(t){
      var p = rect(t.parentElement);
      out.push('TABLE #' + t.id + ' 固有宽=' + t.scrollWidth + ' 容器宽=' + Math.round(p.width) +
               ' 行数=' + (t.tBodies[0] ? t.tBodies[0].rows.length : 0) +
               ' 撑出=' + (t.scrollWidth - Math.round(p.width)));
    });
    [].slice.call(document.querySelectorAll('canvas')).forEach(function(c){
      var r = rect(c);
      out.push('CANVAS #' + c.id + ' 显示=' + Math.round(r.width) + 'x' + Math.round(r.height) +
               ' 位图=' + c.width + 'x' + c.height);
    });
    [].slice.call(document.querySelectorAll('.big > .panel')).forEach(function(p,i){
      var r = rect(p);
      var t = (p.querySelector('h2') ? p.querySelector('h2').textContent : '').trim().replace(/\s+/g,' ').slice(0,16);
      out.push('PANEL' + i + ' [' + t + '] w=' + Math.round(r.width) + ' h=' + Math.round(r.height) +
               ' top=' + Math.round(r.top) + ' bottom=' + Math.round(r.bottom) + ' left=' + Math.round(r.left));
    });
    var rows = {};
    [].slice.call(document.querySelectorAll('.big > .panel')).forEach(function(p){
      var r = rect(p); var k = Math.round(r.top);
      rows[k] = rows[k] || []; rows[k].push(Math.round(r.bottom));
    });
    Object.keys(rows).sort(function(a,b){return a-b;}).forEach(function(k){
      var bs = rows[k];
      out.push('  行@' + k + ' 底边=[' + bs.join(',') + '] 最大差=' +
               (Math.max.apply(null,bs) - Math.min.apply(null,bs)) + 'px');
    });
    var cr0 = document.getElementById('cards');
    if (cr0){
      out.push('顶部指标卡 n=' + cr0.children.length + ' 高=' + Math.round(rect(cr0).height));
      [].slice.call(cr0.children).slice(0,12).forEach(function(c,i){
        var r = rect(c);
        out.push('  card' + i + ' w=' + Math.round(r.width) + ' h=' + Math.round(r.height) +
                 ' | ' + (c.textContent||'').trim().replace(/\s+/g,' ').slice(0,26));
      });
    }
    var pre = document.createElement('pre');
    pre.id = '__geo';
    pre.textContent = out.join('\n');
    document.body.appendChild(pre);
  }
  setTimeout(run, 3000);
})();
"""


def _get(path, timeout=25):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(BASE + path, timeout=timeout) as r:
        return r.read().decode("utf-8")


def build_page():
    html = _get("/")
    data = {}
    for name, path in (("state", "/api/state"), ("stats", "/api/stats"), ("markets", "/api/markets")):
        try:
            data[name] = json.loads(_get(path))
        except Exception as e:
            print("  /api/%-8s 失败: %s" % (name, e))
            data[name] = None
    if not data.get("state"):
        print("错误：拿不到 /api/state，服务是否在运行？")
        return None

    payload = json.dumps(data, ensure_ascii=False)
    inject = (
        "<script>\nwindow.__SNAP__ = " + payload + ";\n"
        "(function(){var S=window.__SNAP__||{};var M={'/api/state':'state','/api/stats':'stats',"
        "'/api/markets':'markets'};window.fetch=function(u,o){if(typeof u==='string'){for(var k in M){"
        "if(u.indexOf(k)===0&&S[M[k]]){var d=S[M[k]];return Promise.resolve({ok:true,"
        "json:function(){return Promise.resolve(d);}});}}}return Promise.reject(new Error('offline'));};})();\n"
        "</script>\n"
    )
    idx = html.find("<body>")
    html = html[: idx + 6] + "\n" + inject + html[idx + 6:]
    html = html.replace("</body>", "<script>" + AUDIT_JS + "</script>\n</body>")
    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    return data["state"]


def find_edge():
    for p in EDGES:
        if os.path.exists(p):
            return p
    return None


def dump_dom(edge, width, height=1200, budget=14000):
    cmd = [
        edge, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--hide-scrollbars",
        "--window-size=%d,%d" % (width, height),
        "--virtual-time-budget=%d" % budget,
        "--run-all-compositor-stages-before-draw",
        "--dump-dom", "file:///" + OUT_HTML.replace("\\", "/"),
    ]
    p = subprocess.run(cmd, capture_output=True, timeout=180)
    return p.stdout.decode("utf-8", "ignore")


def extract(dom):
    m = re.search(r'<pre id="__geo">(.*?)</pre>', dom, re.S)
    if not m:
        return None
    return (m.group(1)
            .replace("&amp;", "&").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--widths", nargs="*", type=int,
                    default=[1920, 1600, 1440, 1280, 1100, 900])
    args = ap.parse_args()

    edge = find_edge()
    if not edge:
        print("错误：找不到 Edge")
        return 1
    print("浏览器: %s" % edge)

    print("拉取实况并生成审计页 ...")
    st = build_page()
    if st is None:
        return 1
    print("  模式 %s | 轮次 %s | 权益 $%.2f" % (st.get("mode"), st.get("round"), st.get("equity", 0)))

    lines = ["布局几何审计  " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "页面: " + OUT_HTML, ""]
    for w in args.widths:
        print("  测量宽度 %d ..." % w)
        dom = dump_dom(edge, w)
        got = extract(dom)
        lines.append("=" * 66)
        lines.append("### 视口宽度 %d px" % w)
        lines.append("=" * 66)
        lines.append(got if got else "!! 审计脚本没跑起来（DOM %d 字节，可能渲染失败）" % len(dom))
        lines.append("")

    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print()
    print("结果: %s" % OUT_TXT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
