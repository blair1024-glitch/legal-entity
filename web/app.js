/* 儀表板前端邏輯。
 *
 * 設計原則：任何顯示推估值的地方都必須看得出它是推估值。免責說明由後端
 * 隨資料一起回傳（每個資金流端點都有 disclaimer 欄位），前端只是把它渲染
 * 出來——這樣就不會有某個畫面漏標的情況。
 */

const QUADRANT_COLORS = {
  '加速流入': '#f85149',
  '流入但放緩': '#d29922',
  '加速流出': '#3fb950',
  '流出但放緩': '#58a6ff',
};

const REFRESH_MS = 30000;

/* ---------- 格式化 ---------- */

// 台股習慣用「億」和「萬」，不是 M/B
function money(v) {
  if (v == null || !isFinite(v)) return '—';
  const abs = Math.abs(v);
  const sign = v > 0 ? '+' : v < 0 ? '−' : '';
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(2)} 億`;
  if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(0)} 萬`;
  return `${sign}${abs.toFixed(0)}`;
}

// 三大法人買賣超官方單位是「股」，換算成張比較符合看盤習慣
function lots(shares) {
  if (shares == null || !isFinite(shares)) return '—';
  const v = shares / 1000;
  const sign = v > 0 ? '+' : v < 0 ? '−' : '';
  return `${sign}${Math.abs(v).toLocaleString('zh-TW', { maximumFractionDigits: 0 })}`;
}

function pct(v, digits = 1) {
  return v == null || !isFinite(v) ? '—' : `${v.toFixed(digits)}%`;
}

function signClass(v) {
  return v > 0 ? 'pos' : v < 0 ? 'neg' : '';
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
  return res.json();
}

/* ---------- 四象限圖 ---------- */

const chart = echarts.init(document.getElementById('quadrant-chart'), null, {
  renderer: 'canvas',
});

function renderQuadrant(data) {
  const points = data.points || [];

  if (!points.length) {
    chart.clear();
    chart.setOption({
      title: {
        text: '尚無盤中資金流資料',
        subtext: '盤中執行 `twflow poll`，或用 `twflow demo` 產生合成資料試看看',
        left: 'center', top: 'center',
        textStyle: { color: '#6e7b8a', fontSize: 15, fontWeight: 'normal' },
        subtextStyle: { color: '#4d5865', fontSize: 12 },
      },
    });
    return;
  }

  // 泡泡大小依成交值開根號縮放——線性縮放會讓權值板塊大到蓋住整張圖
  const maxTurnover = Math.max(...points.map(p => p.turnover_value)) || 1;
  const bubble = t => 12 + 34 * Math.sqrt(t / maxTurnover);

  // 座標軸範圍取對稱，讓原點永遠在正中央，四個象限面積相等
  const maxX = Math.max(...points.map(p => Math.abs(p.strength)), 0.02) * 1.25;
  const maxY = Math.max(...points.map(p => Math.abs(p.momentum)), 0.01) * 1.25;

  const series = Object.keys(QUADRANT_COLORS).map(q => ({
    name: q,
    type: 'scatter',
    data: points.filter(p => p.quadrant === q).map(p => {
      // 板塊名稱塞得進泡泡就放裡面，塞不下就移到旁邊——否則長名稱會
      // 溢出小泡泡，糊成一團看不清楚。中文字寬約等於字級。
      const size = bubble(p.turnover_value);
      const fitsInside = size >= p.sector.length * 9.5 + 8;
      return {
        value: [p.strength, p.momentum],
        raw: p,
        label: fitsInside
          ? { position: 'inside', color: '#0d1117', fontSize: 9, fontWeight: 600 }
          : { position: 'right', distance: 5, color: '#c9d1d9', fontSize: 10, fontWeight: 400 },
      };
    }),
    // symbolSize 的第一個參數是 value 陣列本身，data item 要從 params 取
    symbolSize: (value, params) => bubble(params.data.raw.turnover_value),
    itemStyle: {
      color: QUADRANT_COLORS[q],
      opacity: 0.72,
      borderColor: QUADRANT_COLORS[q],
      borderWidth: 1,
    },
    label: { show: true, formatter: p => p.data.raw.sector },
    labelLayout: { hideOverlap: true },
    emphasis: { focus: 'series', itemStyle: { opacity: 1 } },
  }));

  chart.setOption({
    backgroundColor: 'transparent',
    legend: {
      data: Object.keys(QUADRANT_COLORS),
      textStyle: { color: '#9aa7b4', fontSize: 11 },
      top: 0, itemWidth: 10, itemHeight: 10,
    },
    grid: { left: 70, right: 90, top: 40, bottom: 55 },
    tooltip: {
      trigger: 'item',
      backgroundColor: '#1c2129',
      borderColor: '#2a313c',
      textStyle: { color: '#e6edf3', fontSize: 12 },
      formatter: p => {
        const d = p.data.raw;
        const src = d.custom ? '自訂細分板塊' : '官方產業別';
        return `<b>${d.sector}</b> <span style="color:#6e7b8a">(${src})</span><br>
          <span style="color:${QUADRANT_COLORS[d.quadrant]}">${d.quadrant}</span><br>
          強度　${d.strength >= 0 ? '+' : ''}${(d.strength * 100).toFixed(2)}%<br>
          動能　${d.momentum >= 0 ? '+' : ''}${(d.momentum * 100).toFixed(2)}%<br>
          淨流　${money(d.net_value)}<br>
          成交值 ${money(d.turnover_value)}<br>
          成分股 ${d.constituents} 檔
          <div style="margin-top:4px;color:#d29922;font-size:11px">推估值</div>`;
      },
    },
    xAxis: {
      name: '← 資金流出　　強度　　資金流入 →',
      nameLocation: 'middle', nameGap: 32,
      nameTextStyle: { color: '#9aa7b4', fontSize: 11 },
      min: -maxX, max: maxX,
      axisLine: { lineStyle: { color: '#2a313c' } },
      axisLabel: { color: '#6e7b8a', fontSize: 10, formatter: v => `${(v * 100).toFixed(0)}%` },
      splitLine: { lineStyle: { color: 'rgba(42,49,60,0.4)' } },
    },
    yAxis: {
      name: '← 放緩　　動能　　加速 →',
      nameLocation: 'middle', nameGap: 50, nameRotate: 90,
      nameTextStyle: { color: '#9aa7b4', fontSize: 11 },
      min: -maxY, max: maxY,
      axisLine: { lineStyle: { color: '#2a313c' } },
      axisLabel: { color: '#6e7b8a', fontSize: 10, formatter: v => `${(v * 100).toFixed(1)}%` },
      splitLine: { lineStyle: { color: 'rgba(42,49,60,0.4)' } },
    },
    series: [
      // 象限分隔的十字線
      {
        type: 'line', markLine: {
          silent: true, symbol: 'none',
          lineStyle: { color: '#3d4653', width: 1, type: 'solid' },
          label: { show: false },
          data: [{ xAxis: 0 }, { yAxis: 0 }],
        },
        data: [],
      },
      ...series,
    ],
  }, { notMerge: true });
}

/* ---------- 排行清單 ---------- */

function renderRankList(el, rows, { label, value, badge } = {}) {
  if (!rows.length) {
    el.innerHTML = '<div class="empty">尚無資料</div>';
    return;
  }
  const max = Math.max(...rows.map(r => Math.abs(value(r)))) || 1;
  el.innerHTML = rows.map((r, i) => {
    const v = value(r);
    const width = (Math.abs(v) / max) * 100;
    const b = badge ? badge(r) : '';
    return `<div class="rank-row ${signClass(v)}">
      <span class="bar" style="width:${width}%"></span>
      <span class="idx">${i + 1}</span>
      <span class="label">${label(r)}${b}</span>
      <span class="val">${money(v)}</span>
    </div>`;
  }).join('');
}

function quadrantBadge(p) {
  const c = QUADRANT_COLORS[p.quadrant] || '#6e7b8a';
  return `<span class="qbadge" style="color:${c};background:${c}22">${p.quadrant}</span>`;
}

/* ---------- 各區塊 ---------- */

async function loadQuadrant() {
  const win = document.getElementById('window-select').value;
  const data = await getJSON(`/api/quadrant?window=${win}`);

  renderQuadrant(data);
  document.getElementById('disclaimer-text').textContent = data.disclaimer || '';

  // 板塊排行沿用同一份資料，不必再打一次 API
  renderRankList(document.getElementById('sector-rank'), data.points, {
    label: p => `${p.sector} <small>${p.constituents}檔</small>`,
    value: p => p.net_value,
    badge: quadrantBadge,
  });

  const acc = data.accuracy || {};
  const badge = document.getElementById('accuracy-badge');
  if (acc.available) {
    const l = acc.latest;
    badge.innerHTML = `推估準確度　等級相關 <b>${l.spearman >= 0 ? '+' : ''}${l.spearman.toFixed(2)}</b>
      · 方向一致 <b>${(l.sign_match * 100).toFixed(0)}%</b>
      <span style="color:#6e7b8a">（${l.trade_date}，${l.n_stocks} 檔）</span>`;
    badge.title = `近 ${acc.days} 日平均等級相關 ${acc.mean_spearman.toFixed(2)}。`
      + '這是把盤中推估值與收盤後官方三大法人買賣超比對得出的：'
      + '1.0 代表排序完全一致，0 代表毫無關聯。';
  } else {
    badge.innerHTML = '<span style="color:#6e7b8a">推估準確度：尚無資料'
      + '（需要盤中推估與盤後官方數據各一天才能比對）</span>';
  }
}

async function loadStocks() {
  const data = await getJSON('/api/stocks?limit=15');
  renderRankList(document.getElementById('stock-rank'), data.stocks, {
    label: s => `${s.code} ${s.name || ''} <small>${s.sector}</small>`,
    value: s => s.net_value,
  });
}

async function loadWatchlist() {
  const data = await getJSON('/api/watchlist');
  const tbody = document.querySelector('#watchlist-table tbody');
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="muted">自選股清單是空的，'
      + '請編輯 config.yaml 的 watchlist。</td></tr>';
    return;
  }
  tbody.innerHTML = data.items.map(it => {
    const o = it.official || {};
    return `<tr>
      <td>${it.code}</td>
      <td>${it.name || '—'}</td>
      <td class="muted">${it.sector}</td>
      <td class="num">${it.last_price ? it.last_price.toFixed(2) : '—'}</td>
      <td class="num ${signClass(it.est_net_value)}">${money(it.est_net_value)}</td>
      <td class="num">${pct(it.foreign_ratio, 2)}</td>
      <td class="num ${signClass(o.foreign_net)}">${lots(o.foreign_net)}</td>
      <td class="num ${signClass(o.trust_net)}">${lots(o.trust_net)}</td>
      <td class="num ${signClass(o.total_net)}">${lots(o.total_net)}</td>
    </tr>`;
  }).join('');
}

async function loadInstitutional() {
  const data = await getJSON('/api/institutional?limit=10');
  const el = document.getElementById('insti-rank');
  const rows = [...(data.buy || []), ...(data.sell || [])];
  if (!rows.length) {
    el.innerHTML = '<div class="empty">尚無官方三大法人資料——收盤後執行 '
      + '<code>twflow eod</code> 取得。</div>';
    return;
  }
  // 官方買賣超單位是股，這裡直接顯示張數
  const max = Math.max(...rows.map(r => Math.abs(r.total_net))) || 1;
  el.innerHTML = rows.map((r, i) => {
    const width = (Math.abs(r.total_net) / max) * 100;
    return `<div class="rank-row ${signClass(r.total_net)}">
      <span class="bar" style="width:${width}%"></span>
      <span class="idx">${i + 1}</span>
      <span class="label">${r.code} ${r.name || ''} <small>${r.sector}</small></span>
      <span class="val">${lots(r.total_net)} 張</span>
    </div>`;
  }).join('');
}

async function loadFutures() {
  const data = await getJSON('/api/futures');
  const el = document.getElementById('futures-panel');
  if (!data.rows || !data.rows.length) {
    el.innerHTML = '<div class="empty">尚無期貨法人資料。</div>';
    return;
  }
  el.innerHTML = data.rows.map(r => `
    <div class="panel-row">
      <span class="k">${r.contract} · ${r.party}</span>
      <span class="v ${signClass(r.net_oi)}">${r.net_oi > 0 ? '+' : ''}${r.net_oi.toLocaleString('zh-TW')} 口</span>
    </div>`).join('')
    + `<div class="note" style="padding:0.3rem 0.5rem">資料日 ${data.trade_date}　·　淨未平倉＝多方－空方</div>`;
}

async function loadBrokers() {
  const data = await getJSON('/api/brokers?limit=10');
  const el = document.getElementById('brokers-panel');
  if (!data.available || !data.rows.length) {
    el.innerHTML = `<div class="empty">${data.note || '尚無分點資料。'}</div>`;
    return;
  }
  el.innerHTML = data.rows.map(r => `
    <div class="panel-row">
      <span class="k">${r.code} ${r.name || ''}</span>
      <span class="v ${signClass(r.state_net_shares)}">${lots(r.state_net_shares)} 張</span>
    </div>`).join('')
    + `<div class="note" style="padding:0.3rem 0.5rem">資料日 ${data.trade_date}　·　公股行庫券商分點合計</div>`;
}

async function loadMeta() {
  const m = await getJSON('/api/meta');
  const pill = document.getElementById('session-pill');
  pill.textContent = m.session_open ? '● 盤中' : '○ 已收盤';
  pill.className = `pill ${m.session_open ? 'open' : 'closed'}`;

  document.getElementById('meta-line').textContent =
    `${m.securities} 檔 · ${m.sectors} 板塊（${m.custom_classified} 檔細分）`
    + (m.latest_flow_date ? ` · 盤中資料 ${m.latest_flow_date}` : '')
    + (m.latest_insti_date ? ` · 官方數據 ${m.latest_insti_date}` : '');
}

/* ---------- 啟動 ---------- */

async function refreshAll() {
  const tasks = [
    ['meta', loadMeta], ['quadrant', loadQuadrant], ['stocks', loadStocks],
    ['watchlist', loadWatchlist], ['institutional', loadInstitutional],
    ['futures', loadFutures], ['brokers', loadBrokers],
  ];
  // 個別區塊失敗不該讓整頁空白——例如分點資料通常是沒有的
  const results = await Promise.allSettled(tasks.map(([, fn]) => fn()));
  results.forEach((r, i) => {
    if (r.status === 'rejected') console.warn(`載入 ${tasks[i][0]} 失敗:`, r.reason);
  });
}

document.getElementById('explain-toggle').addEventListener('click', e => {
  const box = document.getElementById('explainer');
  box.hidden = !box.hidden;
  e.target.textContent = box.hidden ? '怎麼看這張表 ▾' : '收起說明 ▴';
});

document.getElementById('window-select').addEventListener('change', loadQuadrant);
window.addEventListener('resize', () => chart.resize());

refreshAll();
setInterval(refreshAll, REFRESH_MS);
