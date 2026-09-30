/* GitHub Pages 靜態版的資料載入邏輯。
 *
 * 純渲染函式（money/renderQuadrant/renderRankList/…）在 shared/render.js，
 * 與本機版（web/app.js）共用——這裡只負責「資料從哪裡來」：固定檔名的
 * 靜態 JSON（由 GitHub Actions 定時算好發布），不是即時 API。
 *
 * 因此這裡刻意不做任何跨次呼叫的快取：資料本來就是每 5 分鐘才更新
 * 一次，每次載入都直接 fetch 最新的靜態檔（帶時間戳避免瀏覽器快取到
 * 上一輪），邏輯上比本機版更單純，不必煩惱「快取何時該失效」。
 */

const REFRESH_MS = 5 * 60 * 1000; // 跟伺服器端更新頻率對齊，30 秒轮询沒有意義

// 目前鑽取的板塊（null = 看全市場個股）。點板塊泡泡或板塊排行列即可切換。
let selectedSector = null;

/* ---------- 主題 ---------- */

let currentTheme = initTheme();
let lastQuadrantData = null; // 主題切換時用來重畫，不必為了換顏色重新 fetch

function updateThemeButton() {
  const btn = document.getElementById('theme-toggle');
  btn.textContent = currentTheme === 'light' ? '🌙' : '☀️';
}

document.getElementById('theme-toggle').addEventListener('click', () => {
  currentTheme = toggleTheme();
  updateThemeButton();
  if (lastQuadrantData) renderQuadrant(chart, lastQuadrantData, quadrantRenderOpts());
});
updateThemeButton();

/* ---------- 自訂自選股（存在瀏覽器 localStorage，只有這個版本有） ---------- */

const CUSTOM_WATCHLIST_KEY = 'twflow_custom_watchlist';

function loadCustomWatchlist() {
  try {
    const raw = JSON.parse(localStorage.getItem(CUSTOM_WATCHLIST_KEY));
    return Array.isArray(raw) ? raw.filter(c => typeof c === 'string') : [];
  } catch {
    return [];
  }
}

function saveCustomWatchlist(codes) {
  try {
    localStorage.setItem(CUSTOM_WATCHLIST_KEY, JSON.stringify(codes));
  } catch {
    // 存不進去（無痕視窗、被封鎖）就只在這次瀏覽期間生效
  }
}

let customWatchlist = loadCustomWatchlist();
let defaultWatchlistItems = []; // loadWatchlist() 每次重新整理時填入

async function getJSON(path) {
  const res = await fetch(`${path}?_=${Date.now()}`);
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
  return res.json();
}

function formatGeneratedAt(iso) {
  if (!iso) return null;
  try {
    return new Date(iso).toLocaleString('zh-TW', {
      timeZone: 'Asia/Taipei', month: 'numeric', day: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: false,
    }) + ' 台北時間';
  } catch {
    return null;
  }
}

// 個股全量清單（stocks_full.json）沒有像 API 一樣先做頭尾裁切，
// 預設檢視（未選板塊）要自己重現「買超榜 + 賣超榜」各取 N 檔的邏輯，
// 對應 quadrant.py 的 rank_stocks()：兩端各取 limit 檔，中間略過。
function topAndBottom(rows, limit) {
  if (rows.length <= limit * 2) return rows;
  return rows.slice(0, limit).concat(rows.slice(-limit));
}

/* ---------- 四象限圖 ---------- */

const chart = echarts.init(document.getElementById('quadrant-chart'), null, {
  renderer: 'canvas',
});

/* ---------- 各區塊 ---------- */

// 空畫面的「下一步」在這個版本是排程自動處理，不是要使用者手動下指令
// ——render.js 的預設文字是寫給本機版的，這裡蓋掉。主題切換重畫時也要
// 用同一份 opts，不然重畫會掉回 render.js 的預設（本機版）文字。
function quadrantRenderOpts() {
  return {
    theme: currentTheme,
    emptyMessage: d => d.has_official
      ? `官方三大法人數據已就緒（${d.official_date}）—— 往下捲即可查看。\n`
        + '四象限需要盤中即時資料，而它只能在盤中累積、事後無法回補。\n'
        + 'GitHub Actions 會在下個交易日盤中自動輪詢累積，不需要手動操作，\n'
        + '過幾輪排程後這張圖就會開始長出來。'
      : '尚無任何資料。GitHub Actions 可能還沒執行過第一輪，\n'
        + '可以到 repo 的 Actions 分頁手動觸發一次「twflow · 盤後」試試看。',
  };
}

async function loadQuadrant() {
  const win = document.getElementById('window-select').value;
  const showTrail = document.getElementById('trail-toggle').checked;
  const data = await getJSON(`data/quadrant_${win}.json`);

  document.getElementById('trail-foot').hidden = !showTrail;

  // 靜態匯出一律把輪動軌跡算好放進檔案（不像 API 可以用查詢參數決定
  // 要不要算），勾選與否只是要不要把它畫出來。
  lastQuadrantData = showTrail ? data : { ...data, trail: {} };
  renderQuadrant(chart, lastQuadrantData, quadrantRenderOpts());
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

  // 板塊排行沿用同一份資料，不必再打一次
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
  const full = await getJSON('data/stocks_full.json');
  const all = full.stocks || [];
  const rows = selectedSector
    ? all.filter(s => s.sector === selectedSector)
    : topAndBottom(all, 15);

  document.getElementById('stock-rank-title').textContent =
    selectedSector ? `${selectedSector} · 成分股` : '個股資金流排行';
  document.getElementById('clear-sector').hidden = !selectedSector;

  const el = document.getElementById('stock-rank');
  if (selectedSector && !rows.length) {
    el.innerHTML = '<div class="empty">這個板塊今天還沒有成交資料。</div>';
    return;
  }
  renderRankList(el, rows, {
    label: s => `${s.code} ${s.name || ''} <small>${s.sector}</small>`,
    value: s => s.net_value,
  });
}

function selectSector(sector) {
  // 再點一次同一個板塊就取消篩選
  selectedSector = (selectedSector === sector) ? null : sector;
  loadStocks().catch(err => console.warn('載入成分股失敗:', err));
}

function watchlistRowHtml(it, { removable = false } = {}) {
  const o = it.official || {};
  const removeCell = removable
    ? `<td><button class="remove-row" data-remove="${it.code}" title="移除">×</button></td>`
    : '<td></td>';
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
    ${removeCell}
  </tr>`;
}

// 自訂清單存的是代號；實際資料每次都重新查（推估淨流來自 stocks_full.json，
// 官方數字來自 watchlist_lookup.json）。兩份都是全市場規模，一次載入後
// 給這一輪要查的所有代號共用，不要每個代號各自重新 fetch 一次。
async function fetchLookupSources() {
  const [stocksFull, lookup] = await Promise.all([
    getJSON('data/stocks_full.json'),
    getJSON('data/watchlist_lookup.json'),
  ]);
  return { stocks: stocksFull.stocks || [], lookup };
}

// 查不到的話回傳 null，呼叫端決定怎麼顯示。
function resolveFromSources(code, { stocks, lookup }) {
  const stock = stocks.find(s => s.code === code);
  const meta = lookup[code];
  if (!stock && !meta) return null;
  return {
    code,
    name: (stock && stock.name) || (meta && meta.name) || '',
    sector: (stock && stock.sector) || (meta && meta.sector) || '—',
    last_price: stock ? stock.last_price : 0,
    est_net_value: stock ? stock.net_value : 0,
    foreign_ratio: meta ? meta.foreign_ratio : null,
    official: meta ? meta.official : {},
  };
}

function renderWatchlistTable(customItems) {
  const tbody = document.querySelector('#watchlist-table tbody');
  const rows = [
    ...defaultWatchlistItems.map(it => watchlistRowHtml(it)),
    ...customItems.map(it => watchlistRowHtml(it, { removable: true })),
  ];
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="muted">自選股清單是空的，'
      + '用上面的輸入框加幾檔看看。</td></tr>';
    return;
  }
  tbody.innerHTML = rows.join('');
  tbody.querySelectorAll('[data-remove]').forEach(btn => {
    btn.addEventListener('click', () => removeCustomStock(btn.dataset.remove));
  });
}

async function loadWatchlist() {
  const data = await getJSON('data/watchlist.json');
  defaultWatchlistItems = data.items || [];

  if (!customWatchlist.length) {
    renderWatchlistTable([]);
    return;
  }
  const sources = await fetchLookupSources();
  // 查不到的代號（例如剛好卡在資料還沒同步、或代號打錯了）不要讓整張表
  // 消失，該檔先跳過，其餘照常顯示。
  const resolved = customWatchlist.map(code => resolveFromSources(code, sources)).filter(Boolean);
  renderWatchlistTable(resolved);
}

// 回傳是否真的加成功——輸入框只在成功時清空，失敗（重複／查無代號）
// 要留著原字讓使用者看得到自己打了什麼、方便修正。
async function addCustomStock(rawCode) {
  const msgEl = document.getElementById('watchlist-add-msg');
  const code = rawCode.trim();
  msgEl.hidden = true;

  if (!code) return false;
  const already = defaultWatchlistItems.some(it => it.code === code) || customWatchlist.includes(code);
  if (already) {
    msgEl.textContent = `${code} 已經在清單裡了。`;
    msgEl.hidden = false;
    return false;
  }

  const item = await fetchLookupSources()
    .then(sources => resolveFromSources(code, sources))
    .catch(() => null);
  if (!item) {
    msgEl.textContent = `找不到代號 ${code} 的資料——確認代號是否正確，`
      + '或這檔今天還沒有任何官方／推估資料。';
    msgEl.hidden = false;
    return false;
  }

  customWatchlist.push(code);
  saveCustomWatchlist(customWatchlist);
  await loadWatchlist();
  return true;
}

function removeCustomStock(code) {
  customWatchlist = customWatchlist.filter(c => c !== code);
  saveCustomWatchlist(customWatchlist);
  loadWatchlist().catch(err => console.warn('移除自選股後重新載入失敗:', err));
}

document.getElementById('watchlist-add-form').addEventListener('submit', e => {
  e.preventDefault();
  const input = document.getElementById('watchlist-add-input');
  addCustomStock(input.value)
    .then(ok => { if (ok) input.value = ''; })
    .catch(err => console.warn('加入自選股失敗:', err));
});

async function loadInstitutional() {
  const data = await getJSON('data/institutional.json');
  const el = document.getElementById('insti-rank');
  const rows = [...(data.buy || []), ...(data.sell || [])];
  if (!rows.length) {
    el.innerHTML = '<div class="empty">尚無官方三大法人資料——'
      + '等下一次 GitHub Actions 收盤後排程執行。</div>';
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
  const data = await getJSON('data/futures.json');
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
  const data = await getJSON('data/brokers.json');
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
  const m = await getJSON('data/meta.json');
  const pill = document.getElementById('session-pill');
  pill.textContent = m.session_open ? '● 盤中' : '○ 已收盤';
  pill.className = `pill ${m.session_open ? 'open' : 'closed'}`;

  const pipeline = m.pipeline || {};
  const generatedAt = formatGeneratedAt(pipeline.generated_at_utc);

  document.getElementById('meta-line').textContent =
    `${m.securities} 檔 · ${m.sectors} 板塊（${m.custom_classified} 檔細分）`
    + (m.latest_flow_date ? ` · 盤中資料 ${m.latest_flow_date}` : '')
    + (m.latest_insti_date ? ` · 官方數據 ${m.latest_insti_date}` : '')
    + (generatedAt ? ` · 產生於 ${generatedAt}` : '');

  // 兩個旗標都是 false 代表 Actions 連一次真實資料都沒抓到過——
  // 這跟「今天還沒開盤所以是空的」是完全不同的兩件事，必須分開講清楚。
  const neverFetched = document.getElementById('never-fetched-banner');
  if (neverFetched) {
    neverFetched.hidden = Boolean(pipeline.ever_fetched_intraday || pipeline.ever_fetched_official);
  }
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
