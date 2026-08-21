import urllib.request, json, ssl
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
secid="1.600519"  # 茅台
url=("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
     "?lmt=6&klt=101&secid=%s"
     "&fields1=f1,f2,f3,f7"
     "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
     "&ut=b2884a393a59ad64002292a3e90d46a5" % secid)
req=urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
try:
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        s=r.read().decode("utf-8")
    d=json.loads(s)
    print("rc:", d.get("rc"), "data?", bool(d.get("data")))
    if d.get("data"):
        print("name:", d["data"].get("name"), d["data"].get("code"))
        for k in d["data"]["klines"][:3]:
            print("  ", k)
except Exception as e:
    print("ERR", repr(e)[:200])
