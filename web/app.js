/* 儀表板前端邏輯。
 *
 * 設計原則：任何顯示推估值的地方都必須看得出它是推估值。免責說明由後端
 * 隨資料一起回傳（每個資金流端點都有 disclaimer 欄位），前端只是把它渲染
 * 出來——這樣就不會有某個畫面漏標的情況。
 *
 * 純渲染函式（money/lots/pct/signClass/renderQuadrant/renderRankList/
 * quadrantBadge/limitLockBadge/QUADRANT_COLORS）在 shared/render.js，
 * 本機版與 GitHub Pages 靜態版共用，這裡只負責「資料從哪裡來」。
 */

const REFRESH_MS = 30000;

// 目前鑽取的板塊（null = 看全市場個股）。點板塊泡泡或板塊排行列即可切換。
let selectedSector = null;

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
  return res.json();
}

/* ---------- 主題 ---------- */

let currentTheme = initTheme();
// 快取上一次成功拿到的四象限資料，主題切換時直接用它重畫，
// 不必為了換個顏色重新打一次 API。
let lastQuadrantData = null;

function updateThemeButton() {
  const btn = document.getElementById('theme-toggle');
  // 按鈕顯示的是「點下去會變成」的圖示，不是目前的狀態。
  btn.textContent = currentTheme === 'light' ? '🌙' : '☀️';
}

document.getElementById('theme-toggle').addEventListener('click', () => {
  currentTheme = toggleTheme();
  updateThemeButton();
  // lastQuadrantData 是即時 API 回的資料，trail 欄位早就依當下的勾選
  // 狀態抓好了（勾/不勾本身就會觸發重新 loadQuadrant），這裡直接重畫即可。
  if (lastQuadrantData) renderQuadrant(chart, lastQuadrantData, { theme: currentTheme });
});
updateThemeButton();

/* ---------- 四象限圖 ---------- */

const chart = echarts.init(document.getElementById('quadrant-chart'), null, {
  renderer: 'canvas',
});

/* ---------- 各區塊 ---------- */

async function loadQuadrant() {
  const win = document.getElementById('window-select').value;
  const showTrail = document.getElementById('trail-toggle').checked;
  const trail = showTrail ? '&trail=6&trail_step=20&trail_top=6' : '';
  const data = await getJSON(`/api/quadrant?window=${win}${trail}`);
  lastQuadrantData = data;

  document.getElementById('trail-foot').hidden = !showTrail;

  renderQuadrant(chart, data, { theme: currentTheme });
  document.getElementById('disclaimer-text').textContent = data.disclaimer || '';

  // 開盤初期視窗會自動縮短，要讓使用者知道現在看的是幾分鐘的動能
  const noteEl = document.getElementById('window-note');
  if (data.window_shortened) {
    noteEl.textContent = `開盤未滿 ${data.window_minutes * 2} 分鐘，`
      + `動能改以 ${data.effective_window_minutes} 分鐘計算`;
    noteEl.hidden = false;
  } else if (data.momentum_unknown_sectors > 0) {
    noteEl.textContent = `${data.momentum_unknown_sectors} 個板塊歷史不足，動能待觀察`;
    noteEl.hidden = false;
  } else {
    noteEl.hidden = true;
  }

  // 板塊排行沿用同一份資料，不必再打一次 API
  const sectorEl = document.getElementById('sector-rank');
  renderRankList(sectorEl, data.points, {
    label: p => `${p.sector} <small>${p.constituents}檔</small>`,
    value: p => p.net_value,
    badge: quadrantBadge,
    key: p => p.sector,
  });
  sectorEl.querySelectorAll('[data-key]').forEach(el => {
    el.classList.add('clickable');
    el.addEventListener('click', () => selectSector(el.dataset.key));
  });

  const acc = data.accuracy || {};
  const badge = document.getElementById('accuracy-badge');
  if (acc.available) {
    const l = acc.latest;
    badge.innerHTML = `推估準確度　等級相關 <b>${l.spearman >= 0 ? '+' : ''}${l.spearman.toFixed(2)}</b>
      · 方向一致 <b>${(l.sign_match * 100).toFixed(0)}%</b>
      <span class="faint">（${l.trade_date}，${l.n_stocks} 檔）</span>`;
    badge.title = `近 ${acc.days} 日平均等級相關 ${acc.mean_spearman.toFixed(2)}。`
      + '這是把盤中推估值與收盤後官方三大法人買賣超比對得出的：'
      + '1.0 代表排序完全一致，0 代表毫無關聯。';
  } else {
    badge.innerHTML = '<span class="faint">推估準確度：尚無資料'
      + '（需要盤中推估與盤後官方數據各一天才能比對）</span>';
  }
}

async function loadStocks() {
  const q = selectedSector
    ? `?sector=${encodeURIComponent(selectedSector)}`
    : '?limit=15';
  const data = await getJSON(`/api/stocks${q}`);

  document.getElementById('stock-rank-title').textContent =
    selectedSector ? `${selectedSector} · 成分股` : '個股資金流排行';
  document.getElementById('clear-sector').hidden = !selectedSector;

  const el = document.getElementById('stock-rank');
  if (selectedSector && !data.stocks.length) {
    el.innerHTML = '<div class="empty">這個板塊今天還沒有成交資料。</div>';
    return;
  }
  renderRankList(el, data.stocks, {
    label: s => `${s.code} ${s.name || ''} <small>${s.sector}</small>`,
    value: s => s.net_value,
    badge: s => limitLockBadge(s.limit_lock),
  });
}

function selectSector(sector) {
  // 再點一次同一個板塊就取消篩選
  selectedSector = (selectedSector === sector) ? null : sector;
  loadStocks().catch(err => console.warn('載入成分股失敗:', err));
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
      <td>${it.name || '—'}${limitLockBadge(it.limit_lock)}</td>
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
document.getElementById('trail-toggle').addEventListener('change', loadQuadrant);
chart.on('click', params => {
  const sector = params?.data?.raw?.sector;
  if (sector) selectSector(sector);
});

document.getElementById('clear-sector').addEventListener('click', () => {
  selectedSector = null;
  loadStocks().catch(err => console.warn('載入個股失敗:', err));
});

window.addEventListener('resize', () => chart.resize());

refreshAll();
setInterval(refreshAll, REFRESH_MS);
