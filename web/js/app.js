/* TokenLens 前端 —— 原生 JS，无构建步骤 */
(function () {
  'use strict';

  const API = '/api';
  const view = document.getElementById('view');
  const state = {
    page: 'overview',
    range: 14,
    filters: { app: '', model: '', status: '', q: '' },
    meta: null,
    overview: null,
    reqPage: 0,
    reqLimit: 40,
    reqOrder: '-ts',
    chats: [], chatId: null, chatCache: {},
    balTimer: null, autoTimer: null,
  };

  /* ================= 工具 ================= */
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  const nf = (n) => (n == null ? '—' : Number(n).toLocaleString('zh-CN'));
  const compact = (n) => (n == null ? '—' : Charts.compact(Number(n)));
  const pct = (n, d) => (n == null ? '—' : (n * 100).toFixed(d == null ? 1 : d) + '%');
  const cost = (n) => {
    if (n == null) return '—';
    const v = Number(n);
    if (v === 0) return '$0';
    if (v < 0.01) return '$' + v.toFixed(4);
    if (v < 1) return '$' + v.toFixed(3);
    return '$' + v.toFixed(2);
  };
  const ms = (n) => (n == null || n === 0 ? '—' : n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 's' : n + 'ms');
  const dtime = (t) => t ? new Date(t).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '—';
  const dshort = (t) => t ? new Date(t).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '—';
  const APP_COLOR = {
    dsh: '#4D6BFE', claude: '#D4915D', codex: '#9B8CFF', opencode: '#35E0A1',
    antigravity: '#F5C451', cursor: '#8A95A6', grokbot: '#FF7AC6',
  };
  const appColor = (a) => APP_COLOR[a] || '#4CC9F0';

  function toast(msg, msDelay) {
    const t = $('#toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.remove('show'), msDelay || 2200);
  }

  async function api(path, params) {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => { if (v !== '' && v != null) qs.set(k, v); });
    const url = API + path + (qs.toString() ? '?' + qs : '');
    const res = await fetch(url);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((data.error && data.error.message) || res.statusText);
    return data;
  }

  function rangeParams() {
    const f = state.filters;
    if (state.range === 'all') {
      const min = state.meta && state.meta.range && state.meta.range.min;
      return { from: min || undefined, to: Date.now(), app: f.app, model: f.model, status: f.status, q: f.q };
    }
    const to = Date.now();
    const from = to - state.range * 86400 * 1000;
    return { from, to, app: f.app, model: f.model, status: f.status, q: f.q };
  }

  /* ================= 导航 ================= */
  const PAGE_META = {
    overview: { title: '总览', sub: '全部应用与供应商的用量、成本与缓存表现' },
    products: { title: '各产品用量', sub: '每个 AI 客户端分别用了多少 token、多少钱' },
    requests: { title: '请求日志', sub: '每一次调用的 token、成本与延迟明细' },
    chats: { title: '聊天记录', sub: '各 AI 客户端的会话，可查看与导出' },
    balances: { title: '账号余额', sub: '从供应商配置读取凭据，实时查询剩余额度' },
    providers: { title: '供应商', sub: 'CC Switch 里配置的供应商与接入地址' },
    settings: { title: '设置', sub: '数据来源、刷新频率与服务信息' },
  };

  function setPage(p, push) {
    if (!PAGE_META[p]) p = 'overview';
    state.page = p;
    $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.page === p));
    const m = PAGE_META[p];
    $('#page-title').textContent = m.title;
    $('#page-sub').textContent = m.sub;
    if (push !== false) location.hash = p;
    Charts.hideTip();
    render();
  }

  async function render() {
    try {
      if (state.page === 'overview') await renderOverview();
      else if (state.page === 'products') await renderProducts();
      else if (state.page === 'requests') await renderRequests();
      else if (state.page === 'chats') await renderChats();
      else if (state.page === 'balances') await renderBalances();
      else if (state.page === 'providers') await renderProviders();
      else if (state.page === 'settings') await renderSettings();
    } catch (e) {
      view.innerHTML = `<div class="empty"><div class="big">⚠</div><div>加载失败：${esc(e.message)}</div>
        <div style="font-size:11.5px;color:var(--dim)">确认服务仍在运行，或点右上角「刷新」重试</div></div>`;
    }
  }

  /* ================= 总览 ================= */
  function deltaHTML(cur, prev, kind) {
    if (prev == null || cur == null) return '<span class="delta flat">—</span>';
    if (kind === 'points') {
      const d = (cur - prev) * 100;
      const cls = Math.abs(d) < 0.05 ? 'flat' : d > 0 ? 'up' : 'down';
      return `<span class="delta ${cls}">${d > 0 ? '+' : ''}${d.toFixed(1)}pt</span>`;
    }
    if (!prev || prev < 5) return '<span class="delta flat">—</span>';
    const d = ((cur - prev) / prev) * 100;
    if (!isFinite(d) || Math.abs(d) > 999) return '<span class="delta flat">—</span>';
    const cls = Math.abs(d) < 0.5 ? 'flat' : d > 0 ? 'up' : 'down';
    return `<span class="delta ${cls}">${d > 0 ? '+' : ''}${d.toFixed(1)}%</span>`;
  }

  function kpiCard(id, label, value, foot, color) {
    return `<div class="kpi">
      <div class="k-label">${label}</div>
      <div class="k-value">${value}</div>
      <div class="k-foot">${foot || '<span></span>'}<div class="k-spark" id="${id}"></div></div>
    </div>`;
  }

  async function renderOverview() {
    view.innerHTML = `<div class="wrap"><div class="kpis">
      ${Array(6).fill('<div class="kpi"><div class="skeleton" style="height:62px"></div></div>').join('')}
      </div><div class="card" style="height:300px"><div class="card-body"><div class="skeleton" style="height:250px"></div></div></div></div>`;

    const d = await api('/overview', rangeParams());
    state.overview = d;
    state._ovCache = d;
    state._ovKey = JSON.stringify(rangeParams());
    const t = d.totals, p = d.prev || {};
    const s = d.series || [];
    const labels = s.map(x => x.label);
    const hitSeries = s.map(x => {
      const denom = (x.input_tokens || 0) + (x.cache_read_tokens || 0) + (x.cache_creation_tokens || 0);
      return denom ? x.cache_read_tokens / denom : null;
    });

    const kpis = [
      kpiCard('sp-req', '请求数', nf(t.requests), deltaHTML(t.requests, p.requests)),
      kpiCard('sp-cost', '总成本', cost(t.cost_usd), deltaHTML(t.cost_usd, p.cost_usd)),
      kpiCard('sp-tok', 'Token 总量', compact(t.total_tokens),
        `<span class="delta flat">${compact(t.input_tokens)} 入 / ${compact(t.output_tokens)} 出</span>`),
      kpiCard('sp-hit', '缓存命中率', pct(t.cache_hit_rate),
        deltaHTML(t.cache_hit_rate, p.cache_hit_rate, 'points')),
      kpiCard('sp-lat', '平均延迟', ms(t.avg_latency_ms),
        `<span class="delta flat">P95 ${ms(t.p95_latency_ms)}</span>`),
      kpiCard('sp-err', '成功率', pct(t.success_rate),
        `<span class="delta ${t.errors ? 'down' : 'flat'}">${t.errors} 错误</span>`),
    ];

    const rangeLabel = d.range ? d.range.label + '（按' + (d.range.granularity === 'hour' ? '小时' : '天') + '）' : '';

    view.innerHTML = `<div class="wrap fade-in">
      <div class="kpis">${kpis.join('')}</div>

      <div class="grid g-2 mb">
        <div class="card">
          <div class="card-head">
            <div><h3>请求量与成本</h3><div class="sub">${esc(rangeLabel)}</div></div>
            <div class="legend"><span><i style="background:var(--accent)"></i>请求数</span>
              <span><i style="background:var(--cyan)"></i>成本 (USD)</span></div>
          </div>
          <div class="card-body"><div id="ch-main" style="height:250px"></div></div>
        </div>
        <div class="card">
          <div class="card-head"><div><h3>应用分布</h3><div class="sub">按请求数</div></div></div>
          <div class="card-body">
            <div class="donut-wrap" id="ch-donut"></div>
            <div class="legend" style="justify-content:center;margin-top:10px">
              ${(d.by_app || []).map(a => `<span><i style="background:${appColor(a.key)}"></i>${esc(a.name)} · ${nf(a.requests)}</span>`).join('')}
            </div>
          </div>
        </div>
      </div>

      <div class="grid g-2 mb">
        <div class="card">
          <div class="card-head">
            <div><h3>各产品用量</h3>
              <div class="sub">按${state.appMetric === 'requests' ? '请求数' : state.appMetric === 'cost' ? '成本' : 'Token'}堆叠</div></div>
            <div class="seg" id="app-metric">
              <button data-m="tokens" class="${(state.appMetric || 'tokens') === 'tokens' ? 'active' : ''}">Token</button>
              <button data-m="requests" class="${state.appMetric === 'requests' ? 'active' : ''}">请求</button>
              <button data-m="cost" class="${state.appMetric === 'cost' ? 'active' : ''}">成本</button>
            </div>
          </div>
          <div class="card-body"><div id="ch-apps" style="height:236px"></div></div>
        </div>
        <div class="card">
          <div class="card-head"><div><h3>产品排行</h3><div class="sub">点一行看该产品明细</div></div></div>
          <div class="card-body tight" id="app-list"><div class="skeleton" style="height:180px"></div></div>
        </div>
      </div>

      <div class="grid g-2 mb">
        <div class="card">
          <div class="card-head">
            <div><h3>Token 构成</h3><div class="sub">输入 / 输出 / 缓存读取</div></div>
            <div class="legend"><span><i style="background:#4CC9F0"></i>输入</span>
              <span><i style="background:#35E0A1"></i>输出</span>
              <span><i style="background:#9B8CFF"></i>缓存读</span></div>
          </div>
          <div class="card-body"><div id="ch-token" style="height:210px"></div></div>
        </div>
        <div class="card">
          <div class="card-head"><div><h3>延迟分布</h3><div class="sub">请求耗时区间</div></div></div>
          <div class="card-body"><div id="ch-lat" style="height:210px"></div></div>
        </div>
      </div>

      <div class="grid g-2b">
        <div class="card">
          <div class="card-head"><div><h3>模型用量</h3><div class="sub">Top ${(d.by_model || []).length}</div></div></div>
          <div class="card-body tight"><div class="table-wrap"><table class="tbl">
            <thead><tr><th>模型</th><th class="t-right">请求</th><th class="t-right">Token</th>
              <th class="t-right">缓存命中</th><th class="t-right">成本</th></tr></thead>
            <tbody>${(d.by_model || []).map(m => `<tr data-model="${esc(m.key)}">
              <td><span class="trunc" style="max-width:170px">${esc(m.key)}</span></td>
              <td class="t-right num">${nf(m.requests)}</td>
              <td class="t-right num dim">${compact(m.tokens)}</td>
              <td class="t-right num ${m.cache_hit_rate > 0.5 ? '' : 'dim'}">${pct(m.cache_hit_rate, 0)}</td>
              <td class="t-right num">${cost(m.cost_usd)}</td></tr>`).join('') || '<tr><td colspan="5" class="t-muted">暂无</td></tr>'}
            </tbody></table></div></div>
        </div>
        <div class="card">
          <div class="card-head"><div><h3>供应商 / 来源</h3><div class="sub">按请求数</div></div></div>
          <div class="card-body tight"><div class="table-wrap"><table class="tbl">
            <thead><tr><th>来源</th><th class="t-right">请求</th><th class="t-right">成本</th></tr></thead>
            <tbody>${(d.by_provider || []).map(m => `<tr>
              <td><span class="trunc" style="max-width:190px">${esc(m.name || m.key)}</span></td>
              <td class="t-right num">${nf(m.requests)}</td>
              <td class="t-right num">${cost(m.cost_usd)}</td></tr>`).join('') || '<tr><td colspan="3" class="t-muted">暂无</td></tr>'}
            </tbody></table></div>
            <div style="padding:10px 4px 2px;font-size:11px;color:var(--dim)">
              状态码：${(d.status_codes || []).map(c => `<span class="badge ${c.code >= 400 ? 'err' : 'ok'}" style="margin-right:4px">${c.code} · ${nf(c.count)}</span>`).join('')}
            </div>
          </div>
        </div>
      </div>
    </div>`;

    // 图表
    Charts.spark($('#sp-req'), { values: s.map(x => x.requests), color: '#35E0A1' });
    Charts.spark($('#sp-cost'), { values: s.map(x => x.cost_usd), color: '#4CC9F0' });
    Charts.spark($('#sp-tok'), { values: s.map(x => x.input_tokens + x.output_tokens + x.cache_read_tokens), color: '#9B8CFF' });
    Charts.spark($('#sp-hit'), { values: hitSeries, color: '#F5C451' });
    Charts.spark($('#sp-lat'), { values: s.map(x => x.avg_latency_ms || 0), color: '#FF7AC6' });
    Charts.spark($('#sp-err'), { values: s.map(x => x.requests - x.errors), color: '#35E0A1' });

    Charts.area($('#ch-main'), {
      labels, height: 250, rightAxis: true,
      series: [
        { name: '请求数', color: '#35E0A1', values: s.map(x => x.requests), fmt: nf },
        { name: '成本', color: '#4CC9F0', values: s.map(x => x.cost_usd), right: true, fill: false, fmt: cost },
      ],
      yFormat: compact, y2Format: (v) => '$' + (v < 1 ? v.toFixed(2) : v.toFixed(1)),
    });
    Charts.donut($('#ch-donut'), {
      items: (d.by_app || []).map(a => ({ label: a.name, value: a.requests, color: appColor(a.key) })),
      centerValue: nf(t.requests), centerLabel: '总请求',
    });
    // 各产品用量（可切换 Token / 请求 / 成本）
    const paintApps = () => {
      const m = state.appMetric || 'tokens';
      const fmtFn = m === 'cost' ? cost : m === 'tokens' ? compact : nf;
      const total = m === 'tokens' ? t.total_tokens : m === 'requests' ? t.requests : t.cost_usd;
      Charts.stacked($('#ch-apps'), {
        labels, height: 236, fmt: fmtFn,
        series: (d.series_by_app || []).map(a => ({
          name: a.name, color: appColor(a.app), values: a[m] || [],
        })),
      });
      const list = (d.by_app || []);
      $('#app-list').innerHTML = list.map(a => {
        const v = m === 'tokens' ? a.tokens : m === 'requests' ? a.requests : a.cost_usd;
        const share = total ? v / total : 0;
        return `<div class="app-row" data-app="${esc(a.key)}">
          <div style="display:flex;align-items:center;gap:8px">
            <i style="width:8px;height:8px;border-radius:3px;background:${appColor(a.key)};flex:none"></i>
            <b style="font-size:12.5px">${esc(a.name)}</b>
            <span style="flex:1"></span>
            <span class="num" style="font-size:12.5px">${fmtFn(v)}</span>
            <span class="num dim" style="font-size:10.5px;width:46px;text-align:right">${(share * 100).toFixed(1)}%</span>
          </div>
          <div class="hbar-track" style="margin-top:6px"><i style="width:${Math.max(1.5, share * 100)}%;background:${appColor(a.key)}"></i></div>
          <div class="num" style="display:flex;gap:11px;margin-top:5px;font-size:10.5px;color:var(--dim)">
            <span>请求 ${nf(a.requests)}</span><span>成本 ${cost(a.cost_usd)}</span>
            <span>缓存命中 ${pct(a.cache_hit_rate, 0)}</span>
          </div>
        </div>`;
      }).join('') || '<div class="empty-mini">暂无</div>';
      $$('#app-list .app-row').forEach(el => {
        el.onclick = () => { state.appSel = el.dataset.app; setPage('products'); };
      });
      $$('#app-metric button').forEach(b => {
        b.onclick = () => {
          state.appMetric = b.dataset.m;
          $$('#app-metric button').forEach(x => x.classList.toggle('active', x.dataset.m === b.dataset.m));
          paintApps();
        };
      });
    };
    paintApps();

    Charts.stacked($('#ch-token'), {
      labels, height: 210, fmt: compact,
      series: [
        { name: '缓存读', color: '#9B8CFF', values: s.map(x => x.cache_read_tokens) },
        { name: '输入', color: '#4CC9F0', values: s.map(x => x.input_tokens) },
        { name: '输出', color: '#35E0A1', values: s.map(x => x.output_tokens) },
      ],
    });
    Charts.bars($('#ch-lat'), {
      horizontal: false, height: 210, emptyText: '这段时间没有耗时数据（本地会话日志不含延迟）',
      items: (d.latency_buckets || []).map(b => ({ label: b.label, value: b.count, color: '#4CC9F0' })),
      fmt: nf,
    });

    $$('#view tbody tr[data-model]').forEach(tr => {
      tr.onclick = () => { state.filters.model = tr.dataset.model; state.page = 'requests'; setPage('requests'); };
    });
  }

  /* ================= 各产品用量 ================= */
  async function renderProducts() {
    const rp = rangeParams();
    const key = JSON.stringify(rp);
    const d = (state._ovKey === key && state._ovCache) ? state._ovCache : await api('/overview', rp);
    state._ovKey = key;
    state._ovCache = d;

    const apps = d.by_app || [];
    if (!apps.length) {
      view.innerHTML = '<div class="empty"><div class="big">📊</div><div>这个时间范围内没有数据</div></div>';
      return;
    }
    const sel = apps.some(a => a.key === state.appSel) ? state.appSel : apps[0].key;
    state.appSel = sel;
    const labels = (d.series || []).map(x => x.label);
    const totalTok = d.totals.total_tokens || 1;
    const cur = apps.find(a => a.key === sel) || apps[0];
    const sa = (d.series_by_app || []).find(x => x.app === sel) || { tokens: [], requests: [], cost: [] };
    const uns = (state.meta && state.meta.sources && state.meta.sources.unsupported) || [];

    view.innerHTML = `<div class="wrap fade-in">
      <div class="prod-grid">
        ${apps.map(a => `<div class="prod-card ${a.key === sel ? 'active' : ''}" data-app="${esc(a.key)}">
          <div class="prod-top"><i style="background:${appColor(a.key)}"></i><b>${esc(a.name)}</b>
            <span class="badge gray">${a.token_share == null ? '—' : (a.token_share * 100).toFixed(1) + '%'}</span></div>
          <div class="prod-num">${compact(a.tokens)}<small> token</small></div>
          <div class="prod-metrics">
            <span>请求 <b class="num">${nf(a.requests)}</b></span>
            <span>成本 <b class="num">${cost(a.cost_usd)}</b></span>
            <span>缓存 <b class="num">${pct(a.cache_hit_rate, 0)}</b></span>
          </div>
          <div class="hbar-track"><i style="width:${Math.max(1.5, (a.tokens / totalTok) * 100)}%;background:${appColor(a.key)}"></i></div>
        </div>`).join('')}
      </div>

      <div class="grid g-2 mb">
        <div class="card">
          <div class="card-head">
            <div><h3>${esc(cur.name)} · 用量趋势</h3><div class="sub">${esc(d.range.label)}</div></div>
            <div class="legend"><span><i style="background:${appColor(sel)}"></i>Token</span>
              <span><i style="background:#4CC9F0"></i>请求数</span></div>
          </div>
          <div class="card-body"><div id="pd-chart" style="height:250px"></div></div>
        </div>
        <div class="card">
          <div class="card-head"><div><h3>${esc(cur.name)} · 用量结构</h3><div class="sub">该产品在此区间的构成</div></div></div>
          <div class="card-body">
            <div class="prod-break">
              <div><span>输入（未命中缓存）</span><b>${compact(cur.input)}</b></div>
              <div><span>输出</span><b>${compact(cur.output)}</b></div>
              <div><span>缓存读取</span><b>${compact(cur.cache_read)}</b></div>
              <div><span>缓存写入</span><b>${compact(cur.cache_creation)}</b></div>
            </div>
            <dl class="kv" style="margin-top:16px">
              <dt>Token 占比</dt><dd>${pct(cur.token_share)}</dd>
              <dt>成本占比</dt><dd>${pct(cur.cost_share)}</dd>
              <dt>平均每次请求</dt><dd class="num">${nf(Math.round(cur.tokens / Math.max(1, cur.requests)))} token</dd>
              <dt>缓存命中率</dt><dd>${pct(cur.cache_hit_rate)}</dd>
              <dt>最近活跃</dt><dd>${dshort(cur.last_ts)}</dd>
            </dl>
          </div>
        </div>
      </div>

      <div class="grid g-2b">
        <div class="card">
          <div class="card-head"><div><h3>${esc(cur.name)} · 模型分布</h3><div class="sub">该产品调用的模型</div></div></div>
          <div class="card-body tight"><div class="table-wrap"><table class="tbl">
            <thead><tr><th>模型</th><th class="t-right">请求</th><th class="t-right">Token</th><th class="t-right">成本</th></tr></thead>
            <tbody>${(cur.models || []).map(m => `<tr>
              <td><span class="trunc" style="max-width:170px">${esc(m.model)}</span></td>
              <td class="t-right num">${nf(m.requests)}</td>
              <td class="t-right num dim">${compact(m.tokens)}</td>
              <td class="t-right num">${cost(m.cost_usd)}</td></tr>`).join('')
              || '<tr><td colspan="4" class="t-muted">暂无</td></tr>'}</tbody>
          </table></div></div>
        </div>
        <div class="card">
          <div class="card-head"><div><h3>未接入的客户端</h3>
            <div class="sub">在你桌面上但本地拿不到 token，已逐项核实</div></div></div>
          <div class="card-body tight">
            ${uns.length ? uns.map(u => `<div class="src-row">
              <span class="dot warn"></span>
              <div><div><b>${esc(u.name)}</b><span class="badge gray" style="margin-left:6px">${esc(u.reason)}</span></div>
                <div class="path">${esc(u.detail)}</div></div>
              <span class="badge warn">无数据</span>
            </div>`).join('') : '<div class="empty-mini">全部客户端都已接入</div>'}
            <div style="padding:12px 4px 2px;font-size:11px;color:var(--dim);line-height:1.7">
              想把这些也统计进来，需要给它配一个本地代理（走代理的请求都能记录），
              这也是 Helicone 原本的工作方式。
            </div>
          </div>
        </div>
      </div>
    </div>`;

    Charts.area($('#pd-chart'), {
      labels, height: 250, rightAxis: true,
      series: [
        { name: 'Token', color: appColor(sel), values: sa.tokens || [], fmt: compact },
        { name: '请求数', color: '#4CC9F0', values: sa.requests || [], right: true, fill: false, fmt: nf },
      ],
      yFormat: compact, y2Format: compact,
    });
    $$('.prod-card').forEach(el => {
      el.onclick = () => { state.appSel = el.dataset.app; renderProducts(); };
    });
  }

  /* ================= 请求日志 ================= */
  async function renderRequests() {
    const f = state.filters;
    const params = Object.assign(rangeParams(), { limit: state.reqLimit, offset: state.reqPage * state.reqLimit, order: state.reqOrder });
    const d = await api('/requests', params);
    const models = (state.meta && state.meta.models) || [];
    const pages = Math.max(1, Math.ceil(d.total / state.reqLimit));

    view.innerHTML = `<div class="wrap fade-in">
      <div class="toolbar">
        <input class="input search" id="f-q" placeholder="搜索模型 / 会话 / 错误…" value="${esc(f.q || '')}">
        <select class="input" id="f-app">
          <option value="">全部应用</option>
          ${((state.meta && state.meta.apps) || []).map(a => `<option value="${esc(a)}" ${f.app === a ? 'selected' : ''}>${esc(a)}</option>`).join('')}
        </select>
        <select class="input" id="f-model">
          <option value="">全部模型</option>
          ${models.map(m => `<option value="${esc(m)}" ${f.model === m ? 'selected' : ''}>${esc(m)}</option>`).join('')}
        </select>
        <select class="input" id="f-status">
          <option value="">全部状态</option>
          <option value="ok" ${f.status === 'ok' ? 'selected' : ''}>仅成功</option>
          <option value="error" ${f.status === 'error' ? 'selected' : ''}>仅失败</option>
        </select>
        <div class="spacer"></div>
        <span class="badge gray">共 ${nf(d.total)} 条</span>
        <button class="btn btn-sm" id="btn-export">
          <svg viewBox="0 0 24 24" class="ico"><path d="M12 3v10l4-4 1.4 1.4L12 16l-5.4-5.6L8 9l4 4V3h0ZM4 19h16v2H4v-2Z"/></svg>导出 CSV</button>
      </div>

      <div class="card">
        <div class="card-body tight"><div class="table-wrap"><table class="tbl">
          <thead><tr>
            <th>时间</th><th>应用</th><th>模型</th><th>来源</th>
            <th class="t-right">输入</th><th class="t-right">输出</th><th class="t-right">缓存读</th>
            <th class="t-right">成本</th><th class="t-right">延迟</th><th class="t-right">状态</th>
          </tr></thead>
          <tbody id="req-body">
          ${(d.rows || []).map(r => `<tr data-id="${esc(r.id)}">
            <td class="num dim">${dshort(r.ts)}</td>
            <td><span class="badge ${esc(r.app)}"><i></i>${esc(r.app_label || r.app)}</span></td>
            <td><span class="trunc" style="max-width:150px" title="${esc(r.model)}">${esc(r.model)}</span></td>
            <td><span class="trunc dim" style="max-width:130px" title="${esc(r.provider_name)}">${esc(r.provider_name)}</span></td>
            <td class="t-right num">${nf(r.input_tokens)}</td>
            <td class="t-right num">${nf(r.output_tokens)}</td>
            <td class="t-right num ${r.cache_read_tokens ? '' : 'dim'}">${r.cache_read_tokens ? nf(r.cache_read_tokens) : '—'}</td>
            <td class="t-right num">${cost(r.cost_usd)}</td>
            <td class="t-right num ${r.latency_ms ? '' : 'dim'}">${ms(r.latency_ms)}</td>
            <td class="t-right">${r.status == null ? '<span class="badge gray">—</span>'
              : `<span class="badge ${r.status >= 400 ? 'err' : 'ok'}">${r.status}</span>`}</td>
          </tr>`).join('') || '<tr><td colspan="10"><div class="empty">没有匹配的请求</div></td></tr>'}
          </tbody>
        </table></div></div>
        <div class="toolbar" style="justify-content:space-between;padding:10px 16px;margin:0;border-top:1px solid var(--line)">
          <span style="font-size:11.5px;color:var(--dim)">第 ${state.reqPage + 1} / ${pages} 页</span>
          <div style="display:flex;gap:6px">
            <button class="btn btn-sm" id="pg-prev" ${state.reqPage === 0 ? 'disabled' : ''}>上一页</button>
            <button class="btn btn-sm" id="pg-next" ${state.reqPage >= pages - 1 ? 'disabled' : ''}>下一页</button>
          </div>
        </div>
      </div>
    </div>`;

    const apply = () => { state.reqPage = 0; state.filters.q = $('#f-q').value; state.filters.app = $('#f-app').value; state.filters.model = $('#f-model').value; state.filters.status = $('#f-status').value; render(); };
    $('#f-app').onchange = apply; $('#f-model').onchange = apply; $('#f-status').onchange = apply;
    let qt = null;
    $('#f-q').oninput = () => { clearTimeout(qt); qt = setTimeout(apply, 350); };
    const qs = new URLSearchParams();
    Object.entries(rangeParams()).forEach(([k, v]) => { if (v !== '' && v != null) qs.set(k, v); });
    $('#btn-export').onclick = () => window.open(`${API}/requests/export?${qs}`, '_blank');
    $('#pg-prev').onclick = () => { state.reqPage = Math.max(0, state.reqPage - 1); render(); };
    $('#pg-next').onclick = () => { state.reqPage++; render(); };
    $$('#req-body tr[data-id]').forEach(tr => { tr.onclick = () => openDrawer(tr.dataset.id); });
  }

  async function openDrawer(id) {
    const drawer = $('#drawer');
    drawer.classList.add('open');
    $('#drawer-mask').classList.add('show');
    $('#drawer-title').textContent = '请求详情';
    $('#drawer-body').innerHTML = '<div class="skeleton" style="height:200px"></div>';
    try {
      const all = await api('/requests', Object.assign(rangeParams(), { limit: 1, offset: 0, q: id }));
      const r = (all.rows || []).find(x => x.id === id) || (all.rows || [])[0];
      if (!r) throw new Error('未找到该请求');
      const prompt = (r.input_tokens || 0) + (r.cache_read_tokens || 0) + (r.cache_creation_tokens || 0);
      $('#drawer-body').innerHTML = `
        <dl class="kv">
          <dt>时间</dt><dd>${dtime(r.ts)}</dd>
          <dt>应用</dt><dd><span class="badge ${esc(r.app)}"><i></i>${esc(r.app_label || r.app)}</span></dd>
          <dt>模型</dt><dd><span class="num">${esc(r.model)}</span></dd>
          <dt>供应商</dt><dd>${esc(r.provider_name)}</dd>
          <dt>数据来源</dt><dd>${esc(r.source_label || r.source)}</dd>
          <dt>会话</dt><dd><span class="num" style="font-size:11px">${esc(r.session_id || '—')}</span></dd>
          <dt>状态</dt><dd><span class="badge ${r.status >= 400 ? 'err' : 'ok'}">${r.status == null ? '—' : r.status}</span>
            ${r.error ? `<span class="badge err" style="margin-left:6px">${esc(String(r.error).slice(0, 40))}</span>` : ''}</dd>
        </dl>
        <h4 style="margin:20px 0 10px;font-size:12px;color:var(--muted)">Token 明细</h4>
        <table class="tbl">
          <tbody>
            <tr><td>输入（未命中缓存）</td><td class="t-right num">${nf(r.input_tokens)}</td></tr>
            <tr><td>缓存读取</td><td class="t-right num" style="color:#A99CFF">${nf(r.cache_read_tokens)}</td></tr>
            <tr><td>缓存写入</td><td class="t-right num">${nf(r.cache_creation_tokens)}</td></tr>
            <tr><td>输出</td><td class="t-right num" style="color:var(--accent)">${nf(r.output_tokens)}</td></tr>
            <tr><td><b>提示词合计</b></td><td class="t-right num"><b>${nf(prompt)}</b></td></tr>
            <tr><td><b>总计</b></td><td class="t-right num"><b>${nf(prompt + (r.output_tokens || 0))}</b></td></tr>
            <tr><td>缓存命中率</td><td class="t-right num">${prompt ? pct(r.cache_read_tokens / prompt) : '—'}</td></tr>
          </tbody>
        </table>
        <h4 style="margin:20px 0 10px;font-size:12px;color:var(--muted)">成本与耗时</h4>
        <dl class="kv">
          <dt>输入成本</dt><dd class="num">${cost(r.input_cost_usd)}</dd>
          <dt>输出成本</dt><dd class="num">${cost(r.output_cost_usd)}</dd>
          <dt>缓存成本</dt><dd class="num">${cost(r.cache_cost_usd)}</dd>
          <dt>总成本</dt><dd class="num" style="color:var(--accent)"><b>${cost(r.cost_usd)}</b></dd>
          <dt>延迟</dt><dd class="num">${ms(r.latency_ms)}</dd>
          <dt>首字延迟</dt><dd class="num">${ms(r.first_token_ms)}</dd>
          <dt>流式</dt><dd>${r.streaming ? '是' : '否'}</dd>
          <dt>计价模型</dt><dd class="num" style="font-size:11px">${esc(r.pricing_model || '—')}</dd>
        </dl>
        <h4 style="margin:20px 0 10px;font-size:12px;color:var(--muted)">请求 ID</h4>
        <div class="code">${esc(r.id)}</div>`;
    } catch (e) {
      $('#drawer-body').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
  }

  function closeDrawer() {
    $('#drawer').classList.remove('open');
    $('#drawer-mask').classList.remove('show');
  }

  /* ================= 聊天记录 ================= */
  async function renderChats() {
    view.innerHTML = `<div class="wrap fade-in"><div class="chat-layout">
      <div class="card chat-list">
        <div class="chat-list-head">
          <div style="display:flex;gap:6px">
            <input class="input" id="chat-q" placeholder="搜索标题 / 项目…" style="flex:1;min-width:0">
            <button class="btn btn-sm" id="chat-export-all" title="导出全部会话为 Markdown 压缩包">⤓</button>
          </div>
          <div class="seg" id="chat-src" style="margin-top:8px;display:flex">
            <button data-s="" class="${!state.chatSource ? 'active' : ''}" style="flex:1">全部</button>
            <button data-s="claude" class="${state.chatSource === 'claude' ? 'active' : ''}" style="flex:1">Claude</button>
            <button data-s="dsh" class="${state.chatSource === 'dsh' ? 'active' : ''}" style="flex:1">DeepSeek</button>
          </div>
          <div style="font-size:10.5px;color:var(--dim);margin-top:7px" id="chat-count"></div>
        </div>
        <div class="chat-items" id="chat-items"><div class="skeleton" style="height:60px;margin:8px"></div></div>
      </div>
      <div class="card chat-panel" id="chat-panel">
        <div class="empty" style="height:100%"><div class="big">💬</div><div>从左侧选择一个会话</div></div>
      </div>
    </div></div>`;

    const d = await api('/chats', { limit: 300, q: $('#chat-q').value || '', source: state.chatSource || '' });
    state.chats = d.rows || [];
    const items = $('#chat-items');
    const paint = (rows) => {
      items.innerHTML = rows.map(c => `<div class="chat-item ${c.id === state.chatId ? 'active' : ''}" data-id="${esc(c.id)}">
        <div class="ci-title">${esc(c.title)}</div>
        <div class="ci-meta">
          <span class="badge ${esc(c.source || 'claude')}">${esc(c.source_label || 'Claude Code')}</span>
          <span class="num">${dshort(c.updated)}</span>
          <span class="num">${compact(c.prompt_tokens + c.output)} tok</span>
          ${c.cost_usd ? `<span class="num">${cost(c.cost_usd)}</span>` : ''}
        </div>
      </div>`).join('') || '<div class="empty-mini">没有会话</div>';
      $$('#chat-items .chat-item').forEach(el => { el.onclick = () => openChat(el.dataset.id); });
    };
    paint(state.chats);
    $('#chat-count').textContent = `共 ${d.total} 个会话`;
    $('#chat-export-all').onclick = () => window.open(`${API}/chats/export-all`, '_blank');
    $$('#chat-src button').forEach(b => {
      b.onclick = () => { state.chatSource = b.dataset.s; state.chatId = null; render(); };
    });
    let t = null;
    $('#chat-q').oninput = () => {
      clearTimeout(t);
      t = setTimeout(() => {
        const q = $('#chat-q').value.trim().toLowerCase();
        paint(state.chats.filter(c => !q || c.title.toLowerCase().includes(q) || (c.project || '').toLowerCase().includes(q)));
      }, 200);
    };
    if (state.chatId) openChat(state.chatId); else if (state.chats.length) openChat(state.chats[0].id);
  }

  async function openChat(id) {
    state.chatId = id;
    $$('#chat-items .chat-item').forEach(el => el.classList.toggle('active', el.dataset.id === id));
    const panel = $('#chat-panel');
    panel.innerHTML = '<div class="empty" style="height:100%"><div class="spinner skeleton" style="width:36px;height:36px;border-radius:50%"></div></div>';
    try {
      const d = state.chatCache[id] || await api('/chats/' + encodeURIComponent(id));
      state.chatCache[id] = d;
      const u = d.usage || {};
      const total = (u.input || 0) + (u.output || 0) + (u.cache_read || 0) + (u.cache_creation || 0);
      panel.innerHTML = `
        <div class="chat-panel-head">
          <div style="min-width:0">
            <h2>${esc(d.title || id.slice(0, 8))}</h2>
            <div class="meta">
              <span>${esc(d.project || '—')}</span>
              <span>·</span><span>${dtime(d.created)}</span>
              <span>·</span><span class="num">${nf(total)} tok</span>
              <span>·</span><span class="num">缓存读 ${compact(u.cache_read)}</span>
              <span>·</span><span>${(d.models || []).map(m => `<span class="msg-model">${esc(m)}</span>`).join(' ')}</span>
            </div>
          </div>
          <div class="chat-panel-actions">
            <button class="btn btn-sm" id="ex-md">导出 MD</button>
            <button class="btn btn-sm" id="ex-json">JSON</button>
          </div>
        </div>
        <div class="chat-scroll" id="chat-scroll">
          ${(d.messages || []).map(renderMessage).join('') || '<div class="empty">没有消息</div>'}
        </div>`;
      $('#ex-md').onclick = () => window.open(`${API}/chats/${encodeURIComponent(id)}/export?format=md`, '_blank');
      $('#ex-json').onclick = () => window.open(`${API}/chats/${encodeURIComponent(id)}/export?format=json`, '_blank');
      const sc = $('#chat-scroll');
      sc.scrollTop = 0;
    } catch (e) {
      panel.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
    }
  }

  function renderMessage(m) {
    const isUser = m.role === 'user';
    const blocks = m.blocks || [];
    let inner = '';
    const texts = blocks.filter(b => b.type === 'text');
    const thinks = blocks.filter(b => b.type === 'thinking');
    const tools = blocks.filter(b => b.type === 'tool_use');
    const results = blocks.filter(b => b.type === 'tool_result');
    const imgs = blocks.filter(b => b.type === 'image');

    if (isUser) {
      inner = texts.map(b => `<div class="bubble">${esc(b.text)}</div>`).join('');
    } else {
      if (thinks.length) {
        inner += thinks.map(b => `<details class="block-think"><summary>💭 思考过程</summary>${esc(b.text)}</details>`).join('');
      }
      if (texts.length) inner += `<div class="bubble md">${texts.map(b => mdToHtml(b.text)).join('')}</div>`;
    }
    tools.forEach(t => {
      const pv = (() => { try { return JSON.stringify(t.input || {}); } catch (e) { return ''; } })();
      inner += `<details class="tool"><summary>🔧 <span class="tname">${esc(t.name)}</span>
        <span class="tprev">${esc(pv.slice(0, 160))}</span></summary>
        <pre>${esc(pv.length > 20000 ? pv.slice(0, 20000) + '\n… (已截断)' : pv)}</pre></details>`;
    });
    results.forEach(r => {
      const txt = r.text || '';
      inner += `<details class="tool ${r.is_error ? 'err' : ''}"><summary>${r.is_error ? '⚠ 工具报错' : '📄 工具结果'}
        <span class="tprev">${esc(txt.slice(0, 120).replace(/\n/g, ' '))}</span></summary>
        <pre>${esc(txt.length > 20000 ? txt.slice(0, 20000) + '\n… (已截断)' : txt)}</pre></details>`;
    });
    if (imgs.length) inner += `<div class="msg-img">🖼 ${imgs.length} 张图片（未保存原始数据）</div>`;

    const usage = !isUser && m.usage && (m.usage.input || m.usage.output)
      ? `<div class="msg-usage">
           <span>输入 <b>${nf(m.usage.input)}</b></span>
           <span>输出 <b>${nf(m.usage.output)}</b></span>
           ${m.usage.cache_read ? `<span>缓存读 <b>${nf(m.usage.cache_read)}</b></span>` : ''}
         </div>` : '';

    return `<div class="msg ${isUser ? 'u' : 'a'}">
      <div class="msg-head">
        <span class="avatar ${isUser ? 'u' : 'a'}">${isUser ? '你' : 'AI'}</span>
        <span class="msg-role">${isUser ? '用户' : '助手'}</span>
        ${m.model ? `<span class="msg-model">${esc(m.model)}</span>` : ''}
        <span class="msg-time">${m.ts ? dshort(m.ts) : ''}</span>
      </div>
      <div class="msg-body"><div class="blocks">${inner}</div></div>
      ${usage}
    </div>`;
  }

  /* 轻量 Markdown 渲染（先转义，再处理块级/行内） */
  function mdToHtml(src) {
    const lines = String(src).split('\n');
    const out = [];
    let i = 0;
    const inline = (s) => esc(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
      .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;
        out.push(`<pre class="md-code"><code>${esc(buf.join('\n'))}</code></pre>`);
        continue;
      }
      if (/^#{1,4}\s/.test(line)) {
        const lvl = Math.min(4, line.match(/^#+/)[0].length);
        out.push(`<h${lvl + 1}>${inline(line.replace(/^#+\s*/, ''))}</h${lvl + 1}>`);
        i++;
        continue;
      }
      if (/^\s*[-*+]\s/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*+]\s/.test(lines[i])) {
          items.push(`<li>${inline(lines[i].replace(/^\s*[-*+]\s/, ''))}</li>`);
          i++;
        }
        out.push(`<ul>${items.join('')}</ul>`);
        continue;
      }
      if (/^\s*\d+[.)]\s/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+[.)]\s/.test(lines[i])) {
          items.push(`<li>${inline(lines[i].replace(/^\s*\d+[.)]\s/, ''))}</li>`);
          i++;
        }
        out.push(`<ol>${items.join('')}</ol>`);
        continue;
      }
      if (/^>\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) {
          buf.push(inline(lines[i].replace(/^>\s?/, '')));
          i++;
        }
        out.push(`<blockquote>${buf.join('<br>')}</blockquote>`);
        continue;
      }
      if (!line.trim()) { i++; continue; }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|>\s?|\s*[-*+]\s|\s*\d+[.)]\s)/.test(lines[i])) {
        buf.push(inline(lines[i]));
        i++;
      }
      out.push(`<p>${buf.join('<br>')}</p>`);
    }
    return out.join('');
  }

  /* ================= 余额 ================= */
  async function renderBalances(refresh) {
    view.innerHTML = `<div class="wrap fade-in">
      <div class="toolbar">
        <span class="badge gray" id="bal-time">查询中…</span>
        <div class="spacer"></div>
        <button class="btn btn-sm" id="btn-bal-refresh">重新查询</button>
      </div>
      <div id="bal-body"><div class="bal-grid">
        ${Array(4).fill('<div class="bal-card"><div class="skeleton" style="height:90px"></div></div>').join('')}
      </div></div>
      <div class="card" style="margin-top:12px"><div class="card-body">
        <div style="font-size:11.5px;color:var(--dim);line-height:1.8">
          <b style="color:var(--muted)">说明</b>：余额通过各供应商的官方余额接口查询，凭据读取自本机 CC Switch 配置，仅在本地内存使用、不会上传。
          订阅制产品（如 Claude Pro / ChatGPT Plus）没有公开的余额接口，这里只能显示 API 余额；
          暂不支持查询的平台会标记为「无接口」。
        </div>
      </div></div>
    </div>`;
    const load = async (force) => {
      try {
        const d = await api('/balances', force ? { refresh: 1 } : {});
        $('#bal-time').textContent = d.checked_at ? '数据时间 ' + dshort(d.checked_at) : '—';
        const items = d.items || [];
        if (!items.length) {
          $('#bal-body').innerHTML = `<div class="card"><div class="empty">
            <div class="big">💳</div><div>还没有可查询的供应商</div>
            <div style="font-size:11.5px">${esc((d.error || '请在「供应商」页确认已配置') + '')}</div></div></div>`;
          return;
        }
        $('#bal-body').innerHTML = `<div class="bal-grid">${items.map(it => {
          const official = it.category === 'official';
          const ok = it.ok && it.balance != null;
          const sym = it.currency === 'CNY' ? '¥' : it.currency === 'USD' ? '$' : (it.currency || '');
          const apps = String(it.app_type || '').split(' / ').filter(Boolean);
          const appLabel = apps.length > 2 ? apps.slice(0, 2).join(' / ') + ` +${apps.length - 2}` : apps.join(' / ');
          const badge = ok ? esc(it.adapter) : (official ? '官方订阅' : '无接口');
          const amountText = ok ? sym + Number(it.balance).toFixed(2) : (official ? '订阅制' : '暂不可查');
          const detailText = ok ? (it.detail || '')
            : official ? '官方订阅账号没有公开余额接口，这里只统计 API 用量'
            : (it.detail || it.error || '');
          return `<div class="bal-card ${ok ? 'ok' : ''}">
            <div class="bal-top">
              <div style="min-width:0">
                <div class="bal-name">${esc(it.name || it.id)}</div>
                <div class="bal-app" title="${esc(it.app_type || '')}">${esc(appLabel)}${it.base_url ? ' · ' + esc(String(it.base_url).replace(/^https?:\/\//, '').slice(0, 30)) : ''}</div>
              </div>
              <span class="badge ${ok ? 'ok' : 'gray'}">${badge}</span>
            </div>
            <div class="bal-amount ${ok ? '' : 'na'}">${amountText}</div>
            <div class="bal-detail" title="${esc(detailText)}">${esc(detailText)}</div>
            ${it.latency_ms ? `<div class="bal-foot"><span class="bal-detail">${it.latency_ms}ms</span></div>` : ''}
          </div>`;
        }).join('')}</div>`;
      } catch (e) {
        $('#bal-body').innerHTML = `<div class="card"><div class="empty">查询失败：${esc(e.message)}</div></div>`;
      }
    };
    $('#btn-bal-refresh').onclick = () => { $('#bal-body').innerHTML = '<div class="skeleton" style="height:160px"></div>'; load(true); };
    load(!!refresh);
  }

  /* ================= 供应商 ================= */
  async function renderProviders() {
    const d = await api('/providers');
    const items = d.items || [];
    const groups = {};
    items.forEach(it => { (groups[it.app_type] = groups[it.app_type] || []).push(it); });
    view.innerHTML = `<div class="wrap fade-in">
      <div class="card">
        <div class="card-head"><div><h3>已配置供应商</h3>
          <div class="sub">共 ${items.length} 个，来自 CC Switch（只读展示，不修改你的配置）</div></div></div>
        <div class="card-body tight"><div class="table-wrap"><table class="tbl">
          <thead><tr><th>名称</th><th>应用</th><th>类型</th><th>接入地址</th><th class="t-right">状态</th></tr></thead>
          <tbody>
          ${Object.keys(groups).map(app => groups[app].map(it => `<tr>
            <td><b>${esc(it.name)}</b></td>
            <td><span class="badge ${esc(app)}"><i></i>${esc(it.app_label || app)}</span></td>
            <td><span class="badge ${it.category === 'official' ? 'gray' : 'warn'}">${esc(it.category || '自定义')}</span></td>
            <td class="num dim" style="max-width:320px;overflow:hidden;text-overflow:ellipsis">
              ${esc((it.endpoints && it.endpoints[0]) || it.website_url || '—')}</td>
            <td class="t-right">${it.is_current ? '<span class="badge ok">使用中</span>' : ''}</td>
          </tr>`).join('')).join('')}
          </tbody>
        </table></div></div>
      </div>
      <div class="card" style="margin-top:12px"><div class="card-body">
        <div style="font-size:11.5px;color:var(--dim);line-height:1.8">
          这里只是镜像展示 CC Switch 的配置，切换供应商仍请使用 CC Switch 本身。
          本应用不会写入 <span class="num">~/.claude</span> 或 <span class="num">~/.codex</span> 下的任何文件。
        </div>
      </div></div>
    </div>`;
  }

  /* ================= 设置 ================= */
  // 数据源模式说明：单机版 / 示例数据 / 云端（见 docs/CLOUD.md）
  function sourceModeLabel(m) {
    const s = (m && m.source) || {};
    const mode = s.mode || (s.name === 'demo' ? 'demo' : 'local');
    if (mode === 'demo') return '内置示例数据 · 数值均为合成，不代表真实用量';
    if (mode === 'cloud') return '云端数据源 · 只读' + (s.name ? '（' + s.name + '）' : '');
    return '全部为本地只读访问' + (s.name && s.name !== 'local' ? '（' + s.name + '）' : '');
  }

  async function renderSettings() {
    const m = state.meta || await api('/meta');
    const s = m.settings || {};
    const src = m.sources || {};
    const ccMax = src.cc_switch && src.cc_switch.range && src.cc_switch.range.max;
    const ccLag = ccMax ? Math.floor((Date.now() - ccMax) / 86400000) : null;
    const row = (ok, name, path, extra) => `<div class="src-row">
      <span class="dot ${ok ? '' : 'err'}"></span>
      <div><div><b>${esc(name)}</b> ${extra ? `<span class="badge gray" style="margin-left:6px">${esc(extra)}</span>` : ''}</div>
        <div class="path">${esc(path || '')}</div></div>
      <span class="badge ${ok ? 'ok' : 'err'}">${ok ? '正常' : '不可用'}</span></div>`;

    view.innerHTML = `<div class="wrap fade-in">
      <div class="card mb">
        <div class="card-head"><div><h3>数据来源</h3><div class="sub">${sourceModeLabel(m)}</div></div></div>
        <div class="card-body tight">
          ${row(src.cc_switch && src.cc_switch.ok, 'CC Switch 代理日志', src.cc_switch && src.cc_switch.path,
            src.cc_switch && src.cc_switch.rows != null ? src.cc_switch.rows + ' 条' : '')}
          ${row(src.claude && src.claude.ok, 'Claude Code 会话记录', src.claude && src.claude.path,
            src.claude && src.claude.files != null ? src.claude.files + ' 个文件' : '')}
          ${row(src.dsh && src.dsh.ok, 'DeepSeek Harness 会话流', src.dsh && src.dsh.path,
            src.dsh && src.dsh.events != null ? src.dsh.events + ' 条用量' : '')}
          ${row(src.antigravity && src.antigravity.ok, 'Antigravity 会话库', src.antigravity && src.antigravity.path,
            src.antigravity && src.antigravity.events != null ? src.antigravity.events + ' 条用量' : '尚未解出')}
          ${row(src.native && src.native.ok, '原生解析（补实时用量）', '直接解析本地会话获取每条消息的 token',
            src.native && src.native.events != null ? src.native.events + ' 条' : '')}
          <div class="src-row">
            <span class="dot ${ccLag != null && ccLag <= 1 ? '' : 'warn'}"></span>
            <div><div><b>数据覆盖区间</b></div>
              <div class="path">${m.range && m.range.min ? dshort(m.range.min) + ' → ' + dshort(m.range.max) : '—'}</div></div>
            <span class="badge ${ccLag != null && ccLag <= 1 ? 'ok' : 'warn'}">CC Switch ${ccLag != null ? '滞后 ' + ccLag + ' 天' : '无数据'}</span>
          </div>
        </div>
      </div>

      <div class="card mb">
        <div class="card-head"><div><h3>未接入的客户端</h3>
          <div class="sub">本地确实没有 token 数据，已逐项核实原因</div></div></div>
        <div class="card-body tight">
          ${(src.unsupported || []).map(u => `<div class="src-row">
            <span class="dot warn"></span>
            <div><div><b>${esc(u.name)}</b><span class="badge gray" style="margin-left:6px">${esc(u.reason)}</span></div>
              <div class="path">${esc(u.detail)}</div></div>
            <span class="badge warn">无数据</span></div>`).join('')
            || '<div class="empty-mini">全部已接入</div>'}
        </div>
      </div>

      <div class="card mb">
        <div class="card-head"><div><h3>刷新与端口</h3><div class="sub">改端口后需重启程序</div></div></div>
        <div class="card-body">
          <div class="setting-row">
            <div><div class="lbl">界面自动刷新</div><div class="desc">停留在页面上时多久重新拉一次数据（秒，0 为关闭）</div></div>
            <div class="ctl"><input class="input" id="s-auto" type="number" min="0" max="3600" value="${s.auto_refresh_sec || 0}" style="width:100px"></div>
          </div>
          <div class="setting-row">
            <div><div class="lbl">余额缓存时间</div><div class="desc">避免频繁请求各平台余额接口（秒）</div></div>
            <div class="ctl"><input class="input" id="s-bal" type="number" min="60" max="86400" value="${s.balance_refresh_sec || 600}" style="width:100px"></div>
          </div>
          <div class="setting-row">
            <div><div class="lbl">监听端口</div><div class="desc">当前 ${s.port || 8787}，仅监听 127.0.0.1</div></div>
            <div class="ctl"><input class="input" id="s-port" type="number" min="1024" max="65535" value="${s.port || 8787}" style="width:100px"></div>
          </div>
          <div class="setting-row">
            <div><div class="lbl"></div></div>
            <div class="ctl"><button class="btn btn-accent" id="s-save">保存设置</button></div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>关于</h3></div></div>
        <div class="card-body">
          <dl class="kv">
            <dt>版本</dt><dd>TokenLens ${esc((m.app && m.app.version) || '1.0.0')}</dd>
            <dt>运行方式</dt><dd>本地 Python 服务 + 浏览器界面，零第三方依赖</dd>
            <dt>隐私</dt><dd>数据不离开本机；余额查询使用你已配置的密钥直连官方接口</dd>
            <dt>授权</dt><dd>仅供本机个人使用</dd>
          </dl>
          <div style="margin-top:14px;display:flex;gap:8px">
            <button class="btn btn-sm" id="s-reload">重新扫描数据</button>
            <button class="btn btn-sm" id="s-refresh-meta">刷新元信息</button>
          </div>
        </div>
      </div>
    </div>`;

    $('#s-save').onclick = async () => {
      try {
        const body = {
          auto_refresh_sec: Number($('#s-auto').value) || 0,
          balance_refresh_sec: Number($('#s-bal').value) || 600,
          port: Number($('#s-port').value) || 8787,
        };
        const r = await fetch(API + '/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (!r.ok) throw new Error('保存失败');
        const j = await r.json();
        state.meta.settings = j.settings;
        setupAutoRefresh();
        toast('设置已保存' + (body.port !== (s.port || 8787) ? '，端口修改需重启程序生效' : ''));
      } catch (e) { toast('保存失败：' + e.message); }
    };
    $('#s-reload').onclick = async () => {
      await api('/chats', { refresh: 1, limit: 1 }).catch(() => {});
      state.chatCache = {};
      await loadMeta();
      toast('已重新扫描');
      render();
    };
    $('#s-refresh-meta').onclick = async () => { await loadMeta(); toast('元信息已更新'); render(); };
  }

  /* ================= 初始化 ================= */
  async function loadMeta() {
    state.meta = await api('/meta');
    renderSourceStatus();
    return state.meta;
  }

  function renderSourceStatus() {
    const m = state.meta || {};
    const src = m.sources || {};
    const cc = src.cc_switch || {};
    const cl = src.claude || {};
    const dsh = src.dsh || {};
    const latest = m.range && m.range.max;
    const days = latest ? Math.floor((Date.now() - latest) / 86400000) : null;
    $('#source-status').innerHTML = `
      <div class="src"><span class="dot ${cc.ok ? '' : 'err'}"></span><b>CC Switch</b> · ${cc.ok ? nf(cc.rows) + ' 条' : '不可用'}</div>
      <div class="src"><span class="dot ${cl.ok ? '' : 'err'}"></span><b>Claude Code</b> · ${cl.ok ? (cl.files || 0) + ' 会话' : '不可用'}</div>
      <div class="src"><span class="dot ${dsh.ok ? '' : 'err'}"></span><b>DeepSeek Harness</b> · ${dsh.ok ? (dsh.sessions || 0) + ' 会话' : '不可用'}</div>
      <div class="src"><span class="dot ${days != null && days <= 1 ? '' : 'warn'}"></span>
        ${latest ? '最新数据 ' + dshort(latest) : '暂无数据'}</div>`;
  }

  function setupAutoRefresh() {
    clearInterval(state.autoTimer);
    const sec = (state.meta && state.meta.settings && state.meta.settings.auto_refresh_sec) || 0;
    if (sec > 0) {
      state.autoTimer = setInterval(() => {
        if (document.hidden) return;
        if (state.page === 'overview' || state.page === 'requests') render();
      }, Math.max(10, sec) * 1000);
    }
  }

  function bindUI() {
    $$('#nav a').forEach(a => { a.onclick = (e) => { e.preventDefault(); setPage(a.dataset.page); }; });
    $$('#range-pills button').forEach(b => {
      b.onclick = () => {
        $$('#range-pills button').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        const v = b.dataset.range;
        state.range = v === 'all' ? 'all' : Number(v);
        state.reqPage = 0;
        render();
      };
    });
    $('#btn-refresh').onclick = async () => {
      state.chatCache = {};
      const btn = $('#btn-refresh');
      btn.classList.add('spin');
      try { await loadMeta(); await render(); toast('已刷新'); }
      finally { btn.classList.remove('spin'); }
    };
    $('#drawer-close').onclick = closeDrawer;
    $('#drawer-mask').onclick = closeDrawer;
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeDrawer();
      if (e.key === 'r' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); $('#btn-refresh').click(); }
    });
    window.addEventListener('hashchange', () => {
      const p = location.hash.replace('#', '') || 'overview';
      if (p !== state.page) setPage(p, false);
    });
  }

  async function init() {
    bindUI();
    try {
      await loadMeta();
      if (state.meta.settings && state.meta.settings.auto_refresh_sec) state.autoTimer = null;
      setupAutoRefresh();
    } catch (e) {
      view.innerHTML = `<div class="empty"><div class="big">⚠</div><div>无法连接本地服务：${esc(e.message)}</div></div>`;
      return;
    }
    const p = location.hash.replace('#', '') || 'overview';
    setPage(p, false);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
