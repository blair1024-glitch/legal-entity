/* GitHub Pages 靜態版的資料載入邏輯。
 *
 * 純渲染函式（money/renderQuadrant/renderRankList/…）在 shared/render.js，
 * 與本機版（web/app.js）共用——這裡只負責「資料從哪裡來」：固定檔名的
 * 靜態 JSON（由 GitHub Actions 定時算好發布），不是即時 API。
 *
 * 因此這裡刻意不做任何跨次呼叫的快取：資料本來就是每 10-15 分鐘才更新
 * 一次，每次載入都直接 fetch 最新的靜態檔（帶時間戳避免瀏覽器快取到
 * 上一輪），邏輯上比本機版更單純，不必煩惱「快取何時該失效」。
 */

const REFRESH_MS = 5 * 60 * 1000; // 資料本身更新頻率遠低於本機版，30 秒轮询沒有意義

// 目前鑽取的板塊（null = 看全市場個股）。點板塊泡泡或板塊排行列即可切換。
let selectedSector = null;

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

async function loadQuadrant() {
  const win = document.getElementById('window-select').value;
  const showTrail = document.getElementById('trail-toggle').checked;
  const data = await getJSON(`data/quadrant_${win}.json`);

  document.getElementById('trail-foot').hidden = !showTrail;

  // 靜態匯出一律把輪動軌跡算好放進檔案（不像 API 可以用查詢參數決定
  // 要不要算），勾選與否只是要不要把它畫出來。
  renderQuadrant(chart, showTrail ? data : { ...data, trail: {} }, {
    // 空畫面的「下一步」在這個版本是排程自動處理，不是要使用者手動下指令
    // ——render.js 的預設文字是寫給本機版的，這裡蓋掉。
    emptyMessage: d => d.has_official
      ? `官方三大法人數據已就緒（${d.official_date}）—— 往下捲即可查看。\n`
        + '四象限需要盤中即時資料，而它只能在盤中累積、事後無法回補。\n'
        + 'GitHub Actions 會在下個交易日盤中自動輪詢累積，不需要手動操作，\n'
        + '過幾輪排程後這張圖就會開始長出來。'
      : '尚無任何資料。GitHub Actions 可能還沒執行過第一輪，\n'
        + '可以到 repo 的 Actions 分頁手動觸發一次「twflow · 盤後」試試看。',
  });
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

async function loadWatchlist() {
  const data = await getJSON('data/watchlist.json');
  const tbody = document.querySelector('#watchlist-table tbody');
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="muted">自選股清單是空的，'
      + '請編輯 ci/config.ci.yaml 的 watchlist。</td></tr>';
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
