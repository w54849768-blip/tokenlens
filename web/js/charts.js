/* TokenLens 手写 SVG 图表库 —— 无依赖，支持响应式与悬浮游标 */
(function () {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';

  function E(tag, attrs) {
    const n = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) {
      if (attrs[k] === undefined || attrs[k] === null) continue;
      n.setAttribute(k, attrs[k]);
    }
    return n;
  }
  function txt(x, y, s, opts) {
    const t = E('text', Object.assign({ x, y, 'font-size': 11, fill: '#7C8798' }, opts || {}));
    t.textContent = s;
    return t;
  }
  function niceMax(v) {
    if (!v || v <= 0) return 1;
    const e = Math.pow(10, Math.floor(Math.log10(v)));
    const f = v / e;
    const m = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
    return m * e;
  }
  function compact(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 0 : 1) + 'B';
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + 'M';
    if (a >= 1e3) return (v / 1e3).toFixed(a >= 1e4 ? 0 : 1) + 'k';
    if (a >= 10) return Math.round(v) + '';
    return (Math.round(v * 100) / 100) + '';
  }

  const TIP = () => document.getElementById('chart-tip');
  function showTip(html, x, y) {
    const t = TIP();
    if (!t) return;
    t.innerHTML = html;
    t.classList.add('show');
    const w = t.offsetWidth, h = t.offsetHeight;
    let left = x + 14, top = y - h - 12;
    if (left + w > window.innerWidth - 12) left = x - w - 14;
    if (top < 8) top = y + 18;
    t.style.left = left + 'px';
    t.style.top = top + 'px';
  }
  function hideTip() {
    const t = TIP();
    if (t) t.classList.remove('show');
  }

  function smooth(points) {
    if (points.length < 2) return points.length ? `M${points[0][0]},${points[0][1]}` : '';
    let d = `M${points[0][0]},${points[0][1]}`;
    for (let i = 0; i < points.length - 1; i++) {
      const p0 = points[i - 1] || points[i];
      const p1 = points[i];
      const p2 = points[i + 1];
      const p3 = points[i + 2] || p2;
      const t = 0.18;
      const c1x = p1[0] + (p2[0] - p0[0]) * t;
      const c1y = p1[1] + (p2[1] - p0[1]) * t;
      const c2x = p2[0] - (p3[0] - p1[0]) * t;
      const c2y = p2[1] - (p3[1] - p1[1]) * t;
      d += `C${c1x},${c1y} ${c2x},${c2y} ${p2[0]},${p2[1]}`;
    }
    return d;
  }

  function base(container, height, pad) {
    container.innerHTML = '';
    const W = Math.max(260, container.clientWidth || 640);
    const H = height;
    const svg = E('svg', {
      viewBox: `0 0 ${W} ${H}`, width: '100%', height: H,
      style: 'display:block;overflow:visible', preserveAspectRatio: 'none'
    });
    container.appendChild(svg);
    return { svg, W, H, pad };
  }

  function axisY(svg, W, H, pad, max, fmt, right) {
    const n = 4;
    for (let i = 0; i <= n; i++) {
      const v = (max / n) * i;
      const y = H - pad.b - ((H - pad.t - pad.b) * i) / n;
      svg.appendChild(E('line', {
        x1: pad.l, x2: W - pad.r, y1: y, y2: y,
        stroke: 'rgba(255,255,255,.055)', 'stroke-width': 1
      }));
      const t = txt(right ? W - pad.r + 6 : pad.l - 8, y + 3.5, fmt(v), {
        'text-anchor': right ? 'start' : 'end', 'font-size': 10.5, fill: '#657080'
      });
      svg.appendChild(t);
    }
  }

  /* ---------------- 折线 / 面积（支持双轴） ---------------- */
  function area(container, o) {
    const H = o.height || 260;
    const pad = { l: 44, r: o.rightAxis ? 48 : 14, t: 16, b: 26 };
    const { svg, W } = base(container, H, pad);
    const labels = o.labels || [];
    const series = (o.series || []).filter(s => s && s.values && s.values.length);
    if (!labels.length || !series.length) {
      container.innerHTML = `<div class="empty-mini">暂无数据</div>`;
      return;
    }
    const leftSeries = series.filter(s => !s.right);
    const rightSeries = series.filter(s => s.right);
    const maxL = o.max || niceMax(Math.max(1, ...leftSeries.flatMap(s => s.values.map(v => v || 0))));
    const maxR = niceMax(Math.max(1, ...rightSeries.flatMap(s => s.values.map(v => v || 0))));
    const iw = W - pad.l - pad.r;
    const ih = H - pad.t - pad.b;
    const X = i => pad.l + (labels.length === 1 ? iw / 2 : (iw * i) / (labels.length - 1));
    const Y = (v, right) => H - pad.b - (ih * (v || 0)) / (right ? maxR : maxL);

    const defs = E('defs');
    svg.appendChild(defs);
    series.forEach((s, i) => {
      const g = E('linearGradient', { id: `ag${i}-${Math.random().toString(36).slice(2, 7)}`, x1: 0, y1: 0, x2: 0, y2: 1 });
      g.appendChild(E('stop', { offset: '0%', 'stop-color': s.color, 'stop-opacity': s.right ? 0 : 0.22 }));
      g.appendChild(E('stop', { offset: '100%', 'stop-color': s.color, 'stop-opacity': 0 }));
      defs.appendChild(g);
      s._gid = g.id;
    });

    axisY(svg, W, H, pad, maxL, o.yFormat || compact, false);
    if (rightSeries.length) axisY(svg, W, H, pad, maxR, o.y2Format || compact, true);

    // 面积 + 线
    series.forEach((s) => {
      const pts = s.values.map((v, i) => (v === null || v === undefined) ? null : [X(i), Y(v, s.right)]).filter(Boolean);
      if (!pts.length) return;
      if (!s.right && s.fill !== false) {
        const areaD = smooth(pts) + `L${pts[pts.length - 1][0]},${H - pad.b}L${pts[0][0]},${H - pad.b}Z`;
        svg.appendChild(E('path', { d: areaD, fill: `url(#${s._gid})`, stroke: 'none' }));
      }
      const path = E('path', {
        d: smooth(pts), fill: 'none', stroke: s.color,
        'stroke-width': s.width || 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
        'stroke-dasharray': s.dash || null
      });
      if (s.animate !== false) {
        const len = path.getTotalLength ? path.getTotalLength() : 0;
        svg.appendChild(path);
      } else svg.appendChild(path);
    });

    // x 轴标签
    const step = Math.max(1, Math.ceil(labels.length / 8));
    labels.forEach((lb, i) => {
      if (i % step && i !== labels.length - 1) return;
      svg.appendChild(txt(X(i), H - 8, lb, { 'text-anchor': 'middle', 'font-size': 10.5, fill: '#657080' }));
    });

    // 游标
    const cursor = E('g', { opacity: 0 });
    const vline = E('line', { y1: pad.t, y2: H - pad.b, stroke: 'rgba(255,255,255,.22)', 'stroke-width': 1, 'stroke-dasharray': '3 3' });
    cursor.appendChild(vline);
    const dots = series.map(s => {
      const c = E('circle', { r: 3.6, fill: '#0B0F14', stroke: s.color, 'stroke-width': 2 });
      cursor.appendChild(c);
      return c;
    });
    svg.appendChild(cursor);

    const hit = E('rect', { x: pad.l, y: pad.t, width: iw, height: ih, fill: 'transparent', style: 'cursor:crosshair' });
    svg.appendChild(hit);

    hit.addEventListener('mousemove', (ev) => {
      const rect = svg.getBoundingClientRect();
      const scale = W / rect.width;
      const mx = (ev.clientX - rect.left) * scale;
      let i = Math.round(((mx - pad.l) / iw) * (labels.length - 1));
      i = Math.max(0, Math.min(labels.length - 1, i));
      const x = X(i);
      cursor.setAttribute('opacity', 1);
      vline.setAttribute('x1', x); vline.setAttribute('x2', x);
      dots.forEach((c, k) => {
        const v = series[k].values[i];
        if (v === null || v === undefined) { c.setAttribute('opacity', 0); return; }
        c.setAttribute('opacity', 1);
        c.setAttribute('cx', x);
        c.setAttribute('cy', Y(v, series[k].right));
      });
      let html = `<div class="tip-title">${labels[i]}</div>`;
      series.forEach((s) => {
        const v = s.values[i];
        html += `<div class="tip-row"><i style="background:${s.color}"></i>
                 <span>${s.name}</span><b>${v === null || v === undefined ? '—' : (s.fmt || compact)(v)}</b></div>`;
      });
      showTip(html, ev.clientX, ev.clientY);
    });
    hit.addEventListener('mouseleave', () => { cursor.setAttribute('opacity', 0); hideTip(); });

    observe(container, () => area(container, o));
  }

  /* ---------------- 堆叠面积 ---------------- */
  function stacked(container, o) {
    const H = o.height || 220;
    const pad = { l: 44, r: 14, t: 16, b: 26 };
    const { svg, W } = base(container, H, pad);
    const labels = o.labels || [];
    const series = o.series || [];
    if (!labels.length || !series.length) { container.innerHTML = '<div class="empty-mini">暂无数据</div>'; return; }
    const totals = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
    const max = niceMax(Math.max(1, ...totals));
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const X = i => pad.l + (labels.length === 1 ? iw / 2 : (iw * i) / (labels.length - 1));
    const Y = v => H - pad.b - (ih * (v || 0)) / max;

    const defs = E('defs'); svg.appendChild(defs);
    axisY(svg, W, H, pad, max, o.yFormat || compact, false);

    let acc = labels.map(() => 0);
    const bands = series.map((s) => {
      const lower = acc.slice();
      acc = acc.map((v, i) => v + (s.values[i] || 0));
      return { s, lower, upper: acc.slice() };
    });
    bands.forEach((b, idx) => {
      const up = labels.map((_, i) => [X(i), Y(b.upper[i])]);
      const lo = labels.map((_, i) => [X(i), Y(b.lower[i])]).reverse();
      const d = smooth(up) + 'L' + lo.map(p => p.join(',')).join('L') + 'Z';
      svg.appendChild(E('path', { d, fill: b.s.color, 'fill-opacity': 0.18, stroke: 'none' }));
      svg.appendChild(E('path', {
        d: smooth(up), fill: 'none', stroke: b.s.color, 'stroke-width': 1.6,
        'stroke-linejoin': 'round'
      }));
    });

    const step = Math.max(1, Math.ceil(labels.length / 8));
    labels.forEach((lb, i) => {
      if (i % step && i !== labels.length - 1) return;
      svg.appendChild(txt(X(i), H - 8, lb, { 'text-anchor': 'middle', 'font-size': 10.5, fill: '#657080' }));
    });

    const hit = E('rect', { x: pad.l, y: pad.t, width: iw, height: ih, fill: 'transparent' });
    svg.appendChild(hit);
    hit.addEventListener('mousemove', (ev) => {
      const rect = svg.getBoundingClientRect();
      const scale = W / rect.width;
      const mx = (ev.clientX - rect.left) * scale;
      let i = Math.round(((mx - pad.l) / iw) * (labels.length - 1));
      i = Math.max(0, Math.min(labels.length - 1, i));
      let html = `<div class="tip-title">${labels[i]}</div>`;
      series.forEach(s => {
        html += `<div class="tip-row"><i style="background:${s.color}"></i><span>${s.name}</span>
                 <b>${(o.fmt || compact)(s.values[i] || 0)}</b></div>`;
      });
      html += `<div class="tip-total">合计 ${(o.fmt || compact)(totals[i])}</div>`;
      showTip(html, ev.clientX, ev.clientY);
    });
    hit.addEventListener('mouseleave', hideTip);
    observe(container, () => stacked(container, o));
  }

  /* ---------------- 柱状（横向/纵向） ---------------- */
  function bars(container, o) {
    const items = (o.items || []).filter(x => x && (x.value !== undefined || x.count !== undefined));
    container.innerHTML = '';
    if (!items.length || items.every(x => !(x.value || x.count))) {
      container.innerHTML = `<div class="empty-mini">${o.emptyText || '暂无数据'}</div>`;
      return;
    }
    const horizontal = o.horizontal !== false;
    if (horizontal) {
      const max = Math.max(1, ...items.map(x => x.value || x.count || 0));
      const wrap = document.createElement('div');
      wrap.className = 'hbars';
      items.forEach(it => {
        const v = it.value || it.count || 0;
        const row = document.createElement('div');
        row.className = 'hbar-row';
        row.innerHTML = `<span class="hbar-label" title="${it.label}">${it.label}</span>
          <span class="hbar-track"><i style="width:${Math.max(2, (v / max) * 100)}%;background:${it.color || 'var(--accent)'}"></i></span>
          <span class="hbar-val">${(o.fmt || compact)(v)}</span>`;
        wrap.appendChild(row);
      });
      container.appendChild(wrap);
      return;
    }
    const H = o.height || 220;
    const pad = { l: 30, r: 10, t: 16, b: 30 };
    const { svg, W } = base(container, H, pad);
    const max = niceMax(Math.max(1, ...items.map(x => x.value || x.count || 0)));
    const ih = H - pad.t - pad.b;
    const bw = (W - pad.l - pad.r) / items.length;
    items.forEach((it, i) => {
      const v = it.value || it.count || 0;
      const h = (ih * v) / max;
      const x = pad.l + i * bw + bw * 0.18;
      const w = bw * 0.64;
      const g = E('linearGradient', { id: `bg${i}`, x1: 0, y1: 0, x2: 0, y2: 1 });
      g.appendChild(E('stop', { offset: '0%', 'stop-color': it.color || '#35E0A1', 'stop-opacity': .95 }));
      g.appendChild(E('stop', { offset: '100%', 'stop-color': it.color || '#35E0A1', 'stop-opacity': .35 }));
      const defs = E('defs'); defs.appendChild(g); svg.appendChild(defs);
      svg.appendChild(E('rect', { x, y: H - pad.b - h, width: w, height: Math.max(1, h), rx: 4, fill: `url(#bg${i})` }));
      svg.appendChild(txt(x + w / 2, H - pad.b - h - 6, (o.fmt || compact)(v), {
        'text-anchor': 'middle', 'font-size': 10, fill: '#9AA6B6'
      }));
      svg.appendChild(txt(x + w / 2, H - 10, it.label, {
        'text-anchor': 'middle', 'font-size': 10, fill: '#657080'
      }));
    });
  }

  /* ---------------- 环形 ---------------- */
  function donut(container, o) {
    const items = (o.items || []).filter(x => (x.value || 0) > 0);
    container.innerHTML = '';
    const size = o.size || 168;
    const thickness = o.thickness || 14;
    const svg = E('svg', { viewBox: `0 0 ${size} ${size}`, width: size, height: size });
    container.appendChild(svg);
    const total = items.reduce((a, b) => a + b.value, 0);
    const R = (size - thickness) / 2;
    const C = size / 2;
    svg.appendChild(E('circle', { cx: C, cy: C, r: R, fill: 'none', stroke: 'rgba(255,255,255,.06)', 'stroke-width': thickness }));
    let ang = -Math.PI / 2;
    items.forEach((it) => {
      const frac = it.value / total;
      const a0 = ang, a1 = ang + frac * Math.PI * 2;
      ang = a1;
      if (frac >= 0.9999) {
        svg.appendChild(E('circle', { cx: C, cy: C, r: R, fill: 'none', stroke: it.color, 'stroke-width': thickness }));
        return;
      }
      const large = a1 - a0 > Math.PI ? 1 : 0;
      const x0 = C + R * Math.cos(a0), y0 = C + R * Math.sin(a0);
      const x1 = C + R * Math.cos(a1), y1 = C + R * Math.sin(a1);
      svg.appendChild(E('path', {
        d: `M${x0},${y0}A${R},${R} 0 ${large} 1 ${x1},${y1}`,
        fill: 'none', stroke: it.color, 'stroke-width': thickness, 'stroke-linecap': 'butt'
      }));
    });
    const val = document.createElement('div');
    val.className = 'donut-center';
    val.innerHTML = `<b>${o.centerValue || compact(total)}</b><span>${o.centerLabel || '合计'}</span>`;
    val.style.width = val.style.height = size + 'px';
    container.appendChild(val);
  }

  /* ---------------- 迷你折线 ---------------- */
  function spark(container, o) {
    container.innerHTML = '';
    const vals = (o.values || []).map(v => v || 0);
    if (!vals.length) return;
    const W = Math.max(60, container.clientWidth || 120), H = o.height || 34;
    const svg = E('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: H, preserveAspectRatio: 'none' });
    container.appendChild(svg);
    const max = Math.max(1, ...vals), min = Math.min(...vals, 0);
    const pts = vals.map((v, i) => [
      (W * i) / Math.max(1, vals.length - 1),
      H - 3 - ((H - 6) * (v - min)) / (max - min || 1)
    ]);
    const id = 'sp' + Math.random().toString(36).slice(2, 8);
    const defs = E('defs');
    const g = E('linearGradient', { id, x1: 0, y1: 0, x2: 0, y2: 1 });
    g.appendChild(E('stop', { offset: '0%', 'stop-color': o.color || '#35E0A1', 'stop-opacity': .32 }));
    g.appendChild(E('stop', { offset: '100%', 'stop-color': o.color || '#35E0A1', 'stop-opacity': 0 }));
    defs.appendChild(g); svg.appendChild(defs);
    svg.appendChild(E('path', {
      d: smooth(pts) + `L${W},${H}L0,${H}Z`, fill: `url(#${id})`, stroke: 'none'
    }));
    svg.appendChild(E('path', {
      d: smooth(pts), fill: 'none', stroke: o.color || '#35E0A1', 'stroke-width': 1.6,
      'stroke-linecap': 'round'
    }));
  }

  const observers = new WeakMap();
  function observe(container, rerender) {
    if (observers.has(container)) return;
    let t = null;
    const ro = new ResizeObserver(() => {
      clearTimeout(t);
      t = setTimeout(() => { if (container.clientWidth) rerender(); }, 160);
    });
    ro.observe(container);
    observers.set(container, ro);
  }

  window.Charts = { area, stacked, bars, donut, spark, compact, niceMax, hideTip };
})();
