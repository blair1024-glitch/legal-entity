/* 純渲染函式：只負責「給定資料畫出畫面」，不含任何資料載入或 API 呼叫。
 *
 * 本機版（web/app.js）與 GitHub Pages 靜態版（docs/app.js）共用這份檔案，
 * 保證兩邊視覺呈現一致——「去哪裡抓資料」兩邊不同（打 API vs 讀靜態 JSON），
 * 但「資料長這樣要怎麼畫」不該有兩份、也不該有機會兜出不一樣的結果。
 */

const QUADRANT_COLORS = {
  '加速流入': '#f85149',
  '流入但放緩': '#d29922',
  '加速流出': '#3fb950',
  '流出但放緩': '#58a6ff',
  // 開盤初期歷史不足，算不出加速度。灰色代表「還不知道」，不是第五種狀態。
  '動能待觀察': '#6e7b8a',
};

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

/* ---------- 四象限圖 ---------- */

function renderQuadrant(chart, data) {
  const points = data.points || [];

  if (!points.length) {
    // 空白畫面要說清楚三件事：為什麼空的、已經有什麼、下一步做什麼。
    // 只說「尚無資料」會讓人以為是壞掉了。
    const sub = data.has_official
      ? `官方三大法人數據已就緒（${data.official_date}）—— 往下捲即可查看。\n`
        + '四象限需要盤中即時資料，而它只能在盤中累積、事後無法回補。\n'
        + '下個交易日 09:00 前執行 ./twflow auto，這張圖就會開始長出來。'
      : '盤中執行 ./twflow auto 開始累積，或用 ./twflow demo 產生合成資料先看介面。';

    chart.clear();
    chart.setOption({
      title: {
        text: data.has_official ? '盤中資金流尚未開始累積' : '尚無資料',
        subtext: sub,
        // top:'center' 會讓標題與多行 subtext 疊在一起（ECharts 是以整個
        // 標題區塊的中心對齊，不是逐行堆疊）。改用固定比例 + itemGap。
        left: 'center',
        top: '34%',
        itemGap: 14,
        textStyle: { color: '#9aa7b4', fontSize: 15, fontWeight: 600, align: 'center' },
        subtextStyle: { color: '#6e7b8a', fontSize: 12.5, lineHeight: 23, align: 'center' },
      },
    });
    return;
  }

  // 泡泡大小依成交值開根號縮放——線性縮放會讓權值板塊大到蓋住整張圖
  const maxTurnover = Math.max(...points.map(p => p.turnover_value)) || 1;
  const bubble = t => 12 + 34 * Math.sqrt(t / maxTurnover);

  // 座標軸範圍取對稱，讓原點永遠在正中央，四個象限面積相等
  const trailPts = Object.values(data.trail || {}).flat();
  const allX = points.map(p => Math.abs(p.strength)).concat(trailPts.map(p => Math.abs(p.strength)));
  const allY = points.map(p => Math.abs(p.momentum)).concat(trailPts.map(p => Math.abs(p.momentum)));
  const maxX = Math.max(...allX, 0.02) * 1.25;
  const maxY = Math.max(...allY, 0.01) * 1.25;

  // 輪動軌跡：每個板塊一條線，串起它過去走過的位置。線的末端是現在。
  // 用板塊「當下」的象限顏色，這樣一眼看得出它是往哪個狀態移動。
  const trails = data.trail || {};
  const quadrantOf = Object.fromEntries(points.map(p => [p.sector, p.quadrant]));
  const trailSeries = Object.entries(trails)
    .filter(([, path]) => path.length > 1)
    .map(([sector, path]) => {
      const color = QUADRANT_COLORS[quadrantOf[sector]] || '#6e7b8a';
      return {
        name: `軌跡-${sector}`,
        type: 'line',
        silent: true,
        // 動能是兩個視窗相減，本來就會上下跳動。稍微平滑化只是讓「走向」
        // 看得出來，端點座標仍然是實際算出來的值。
        smooth: 0.4,
        showSymbol: true,
        z: 1,
        data: path.map((pt, i) => ({
          value: [pt.strength, pt.momentum],
          // 點越靠近現在越大越實，讓時間方向一眼看得出來
          symbolSize: 2 + (i / (path.length - 1)) * 4,
          itemStyle: { color, opacity: 0.25 + (i / (path.length - 1)) * 0.55 },
        })),
        lineStyle: { color, width: 1.4, opacity: 0.4 },
        tooltip: { show: false },
      };
    });

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
      // 只列四象限＋待觀察，軌跡線不進圖例（會塞爆）
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
        // 動能不可信時顯示 +0.00% 會讓人以為「力道剛好持平」，
        // 但實情是「還算不出來」——兩者意思差很多。
        const momentum = d.momentum_known
          ? `${d.momentum >= 0 ? '+' : ''}${(d.momentum * 100).toFixed(2)}%`
          + ` <span style="color:#6e7b8a">(${d.momentum_window_minutes}分)</span>`
          : '<span style="color:#6e7b8a">歷史不足，尚無法判定</span>';
        return `<b>${d.sector}</b> <span style="color:#6e7b8a">(${src})</span><br>
          <span style="color:${QUADRANT_COLORS[d.quadrant]}">${d.quadrant}</span><br>
          強度　${d.strength >= 0 ? '+' : ''}${(d.strength * 100).toFixed(2)}%<br>
          動能　${momentum}<br>
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
      ...trailSeries,
      ...series,
    ],
  }, { notMerge: true });
}

/* ---------- 排行清單 ---------- */

function renderRankList(el, rows, { label, value, badge, key } = {}) {
  if (!rows.length) {
    el.innerHTML = '<div class="empty">尚無資料</div>';
    return;
  }
  const max = Math.max(...rows.map(r => Math.abs(value(r)))) || 1;
  el.innerHTML = rows.map((r, i) => {
    const v = value(r);
    const width = (Math.abs(v) / max) * 100;
    const b = badge ? badge(r) : '';
    const k = key ? ` data-key="${key(r).replace(/"/g, '&quot;')}"` : '';
    return `<div class="rank-row ${signClass(v)}"${k}>
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
