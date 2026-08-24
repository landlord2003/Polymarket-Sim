/* 共享个股操作 JS：详情/模拟买卖/自选/删除 + 通用弹窗。
   由 webui 控制页与 dashboard 看板页均通过 /static/stock_actions.js 引用，
   避免两处重复维护同一套模态逻辑（抽自原 dashboard._modal_js）。*/
function closeModal(){ var m=document.getElementById('modalMask')||document.getElementById('mainModalMask'); if(m) m.classList.remove('show'); }
function openModal(html){ var b=document.getElementById('modalBox')||document.getElementById('mainModalBox'); if(b) b.innerHTML=html;
  var m=document.getElementById('modalMask')||document.getElementById('mainModalMask'); if(m) m.classList.add('show'); }

function showDetail(symbol, name){
  const _ctrl = new AbortController();
  const _to = setTimeout(()=>_ctrl.abort(), 28000);
  fetch('/api/stock_detail?symbol='+symbol+'&format=json', {signal:_ctrl.signal})
    .then(r=>r.json()).then(d=>{ clearTimeout(_to);
      if(d.error && !d.snapshot || !d.snapshot.price){
        openModal('<button class="close" onclick="closeModal()">关闭</button><h2>'+symbol+' '+name+'</h2><div class="sub" style="color:#ef7a66">'+d.error+'</div>'); return;
      }
      const warn = d.error || (d.warnings && d.warnings.length ? d.warnings.join('；') : '');
      const warnBanner = warn ? '<div class="sub" style="color:#e0a45a;background:#332712;padding:8px 10px;border-radius:6px;margin-bottom:10px">⚠️ 部分数据源失败，已用可用数据回填：'+warn+'</div>' : '';
      const s = d.snapshot||{};
      const f = d.fund_flow||{};
      const fin = d.financials||{};
      const cls = (s.pct||0)>0?'up':((s.pct||0)<0?'down':'flat');
      const sign = (s.pct||0)>0?'+':'';
      const srcTag = d.synthetic ? '<span class="tag-syn">⚠️ 合成数据</span>'
                    : '<span class="tag-real">✅ '+(d.source||'真实行情')+'</span>';
      let kpi = '<div class="kpi">';
      kpi += '<div class="cell"><div class="k">现价</div><div class="v '+cls+'">'+(s.price!=null? s.price.toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">涨跌幅</div><div class="v '+cls+'">'+(s.pct!=null? sign+s.pct.toFixed(2)+'%':'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">今开/昨收</div><div class="v">'+(s.open!=null?s.open.toFixed(2):'—')+' / '+(s.prev_close!=null?s.prev_close.toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">最高/最低</div><div class="v">'+(s.high!=null?s.high.toFixed(2):'—')+' / '+(s.low!=null?s.low.toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">振幅</div><div class="v">'+(s.amplitude!=null?s.amplitude.toFixed(2)+'%':'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">换手率</div><div class="v">'+(s.turnover!=null?s.turnover.toFixed(2)+'%':'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">量比</div><div class="v">'+(s.vol_ratio!=null?s.vol_ratio.toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">市盈率</div><div class="v">'+(s.pe!=null?s.pe.toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">市净率</div><div class="v">'+(s.pb!=null?s.pb.toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">总市值(亿)</div><div class="v">'+(s.mktcap!=null?(s.mktcap/1e8).toFixed(2):'—')+'</div></div>';
      kpi += '<div class="cell"><div class="k">流通市值(亿)</div><div class="v">'+(s.float_mktcap!=null?(s.float_mktcap/1e8).toFixed(2):'—')+'</div></div>';
      kpi += '</div>';
      let flow = '<div class="flowbar">';
      const fb = (lbl,val)=> { if(val==null) return ''; const c=val>0?'up':(val<0?'down':'flat');
        const sg=val>0?'+':''; return '<div class="fb" style="background:#161c24"><div class="lbl">'+lbl+'</div><div class="val '+c+'">'+sg+(val/1e8).toFixed(2)+'亿</div></div>'; };
      flow += fb('主力净流入', f.main) + fb('超大单', f.huge) + fb('大单', f.big) + fb('中单', f.mid) + fb('小单', f.retail);
      flow += '</div>';
      let finHtml = '<table class="fin"><tr><th>报告期</th><th>营收(亿)</th><th>归母净利(亿)</th><th>ROE</th><th>毛利率</th><th>净利同比</th></tr>';
      if(fin.report_date){ finHtml += '<tr><td>'+fin.report_date+'</td>'+
        '<td>'+(fin.revenue!=null?(fin.revenue/1e8).toFixed(2):'—')+'</td>'+
        '<td>'+(fin.net_profit!=null?(fin.net_profit/1e8).toFixed(2):'—')+'</td>'+
        '<td>'+(fin.roe!=null?fin.roe.toFixed(2)+'%':'—')+'</td>'+
        '<td>'+(fin.gross_margin!=null?fin.gross_margin.toFixed(2)+'%':'—')+'</td>'+
        '<td>'+(fin.profit_yoy!=null?fin.profit_yoy.toFixed(2)+'%':'—')+'</td></tr>'; }
      else { finHtml += '<tr><td colspan="6" class="tag-na">财务数据暂不可用（需联网取 F10）</td></tr>'; }
      finHtml += '</table>';
      let news = '<ul style="color:#9fb0c0;font-size:12px;line-height:1.7">';
      if(d.news && d.news.length){ d.news.forEach(t=>news+='<li>'+t+'</li>'); }
      else { news += '<li class="tag-na">暂无新闻（需联网）</li>'; }
      news += '</ul>';
      const html = '<button class="close" onclick="closeModal()">关闭</button>'
        + '<button class="add" data-sym="'+symbol+'" data-name="'+name.replace(/"/g, "")+'" onclick="addToWatchlist(this.dataset.sym, this.dataset.name)">＋加入自选股</button>'
        + '<h2>'+symbol+' '+name+'</h2>'
        + '<div class="sub">'+srcTag+' ｜ 数据日 '+(d.data_date||'—')+'</div>'
        + warnBanner
        + kpi
        + '<div id="kchart" class="chart"></div>'
        + '<h3 style="font-size:14px;margin:14px 0 4px">资金流向</h3>'+flow
        + '<h3 style="font-size:14px;margin:14px 0 4px">财务摘要</h3>'+finHtml
        + '<h3 style="font-size:14px;margin:14px 0 4px">相关新闻</h3>'+news;
      openModal(html);
      const chart = echarts.init(document.getElementById('kchart'));
      const dl = (d.kline||[]).map(x=>x.date);
      const ohlc = (d.kline||[]).map(x=>[x.open,x.close,x.low,x.high]);
      chart.setOption({
        backgroundColor:'#0d1219',
        grid:{left:55,right:18,top:16,bottom:28},
        tooltip:{trigger:'axis'},
        xAxis:{type:'category',data:dl,axisLine:{lineStyle:{color:'#445'}},axisLabel:{color:'#8b98a5',fontSize:10}},
        yAxis:{scale:true,axisLine:{lineStyle:{color:'#445'}},axisLabel:{color:'#8b98a5'},splitLine:{lineStyle:{color:'#1c2530'}}},
        dataZoom:[{type:'inside'},{type:'slider',height:14,bottom:6}],
        series:[{type:'candlestick',data:ohlc,
          itemStyle:{color:'#ff5b5b',color0:'#2ecc71',borderColor:'#ff5b5b',borderColor0:'#2ecc71'}}]
      });
      window.addEventListener('resize',()=>chart.resize());
    }).catch(e=>openModal('<button class="close" onclick="closeModal()">关闭</button><h2>加载失败</h2><div class="sub" style="color:#ef7a66">'+e+'</div>'));
}

function showTrade(symbol, name){
  const _ctrl = new AbortController();
  const _to = setTimeout(()=>_ctrl.abort(), 28000);
  fetch('/api/stock_detail?symbol='+symbol+'&format=json', {signal:_ctrl.signal})
    .then(r=>r.json()).then(d=>{ clearTimeout(_to);
      const px = (d.snapshot&&d.snapshot.price)|| (d.kline&&d.kline.length? d.kline[d.kline.length-1].close : 0);
      const html = '<button class="close" onclick="closeModal()">关闭</button>'
        + '<h2>📝 模拟买卖 · '+symbol+' '+name+'</h2>'
        + '<div class="sub">🧪 模拟盘 · 零资金 · 不构成投资建议 ｜ 当前价 '+(px? px.toFixed(2):'—')+'</div>'
        + '<div class="trade-form">'
        + '<label>方向 <select id="tSide"><option value="buy">买入</option><option value="sell">卖出</option></select></label>'
        + '<label>价格 <input id="tPrice" type="number" step="0.01" value="'+(px?px.toFixed(2):'')+'"></label>'
        + '<label>数量(股,100倍数) <input id="tQty" type="number" step="100" value="100"></label>'
        + '<button data-sym="'+symbol+'" data-name="'+name.replace(/"/g, "")+'" onclick="submitTrade(this.dataset.sym, this.dataset.name)">确认</button>'
        + '</div><div class="res" id="tRes"></div>'
        + '<div id="tBook"></div>';
      openModal(html);
      refreshBook();
    }).catch(e=>{ clearTimeout(_to); openModal('<button class="close" onclick="closeModal()">关闭</button><h2>加载失败</h2><div class="sub" style="color:#ef7a66">'+ (e && e.name==='AbortError' ? '请求超时（28秒），请重试或检查网络' : e) +'</div>'); });
}

function submitTrade(symbol, name){
  const side = document.getElementById('tSide').value;
  const price = parseFloat(document.getElementById('tPrice').value);
  const qty = parseInt(document.getElementById('tQty').value,10);
  fetch('/api/trade',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol,name,side,price,qty})})
    .then(r=>r.json()).then(j=>{
      const el = document.getElementById('tRes');
      el.innerHTML = j.ok? '<span style="color:#5fd98a">✅ '+j.msg+'</span>'
                         : '<span style="color:#ef7a66">⛔ '+j.msg+'</span>';
      refreshBook();
    }).catch(e=>{ document.getElementById('tRes').innerHTML='<span style="color:#ef7a66">'+e+'</span>'; });
}

function refreshBook(){
  fetch('/api/portfolio?format=json').then(r=>r.json()).then(j=>{
    if(!j.book){ return; }
    const b = j.book;
    let h = '<div class="kpi" style="grid-template-columns:repeat(auto-fill,minmax(130px,1fr))">';
    h += '<div class="cell"><div class="k">总资产</div><div class="v">¥'+(b.total_asset/1).toLocaleString()+'</div></div>';
    h += '<div class="cell"><div class="k">可用资金</div><div class="v">¥'+(b.cash/1).toLocaleString()+'</div></div>';
    h += '<div class="cell"><div class="k">总盈亏</div><div class="v '+(b.total_pnl>=0?'up':'down')+'">'+(b.total_pnl>=0?'+':'')+b.total_pnl.toLocaleString()+' ('+(b.total_pct>=0?'+':'')+b.total_pct+'%)</div></div>';
    h += '<div class="cell"><div class="k">已实现</div><div class="v">¥'+(b.realized_pnl/1).toLocaleString()+'</div></div>';
    h += '</div>';
    if(b.positions && b.positions.length){
      h += '<table class="fin"><tr><th>代码</th><th>名称</th><th>数量</th><th>成本</th><th>现价</th><th>浮盈</th><th>浮盈%</th></tr>';
      b.positions.forEach(p=>{ const c=p.float_pnl>=0?'up':'down';
        h += '<tr><td>'+p.symbol+'</td><td>'+p.name+'</td><td>'+p.qty+'</td><td>'+p.cost_price.toFixed(2)+'</td><td>'+(p.current||'-')+'</td><td class="'+c+'">'+(p.float_pnl>=0?'+':'')+p.float_pnl.toFixed(2)+'</td><td class="'+c+'">'+(p.float_pct>=0?'+':'')+p.float_pct+'%</td></tr>'; });
      h += '</table>';
    } else { h += '<div class="tag-na" style="margin-top:8px">当前无持仓</div>'; }
    const box = document.getElementById('tBook'); if(box) box.innerHTML = h;
  });
}

function addToWatchlist(symbol, name){
  fetch('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'add',symbol:symbol,name:name})})
    .then(r=>r.json()).then(j=>{
      if(j.ok){ alert('已加入自选股：'+symbol+' '+name);
        if(parent && parent.document && parent.document.getElementById('board'))
          parent.document.getElementById('board').src='/api/board?t='+Date.now(); }
      else { alert('加入失败：'+(j.msg||'未知错误')); }
    }).catch(e=>alert('加入失败：'+e));
}

function removeFromWatchlist(symbol){
  if(!confirm('确认从自选股删除 '+symbol+' ？')) return;
  fetch('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'remove',symbol:symbol})})
    .then(r=>r.json()).then(j=>{
      if(j.ok){ if(parent && parent.document && parent.document.getElementById('board'))
          parent.document.getElementById('board').src='/api/board?t='+Date.now(); }
      else { alert('删除失败：'+(j.msg||'未知错误')); }
    }).catch(e=>alert('删除失败：'+e));
}
