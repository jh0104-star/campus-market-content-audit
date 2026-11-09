/* 首页交互：实时审核 / 提交发布 / 示例填充 */
const $ = (id) => document.getElementById(id);

function collectForm() {
  return {
    title: $('f-title').value.trim(),
    description: $('f-desc').value.trim(),
    price: $('f-price').value === '' ? null : parseFloat($('f-price').value),
    condition: $('f-cond').value,
    seller_id: $('f-sid').value.trim(),
    seller_name: $('f-sname').value.trim(),
    contact: $('f-contact').value.trim(),
    has_image: ($('f-img').checked || hasImageFile()) ? 1 : 0,
  };
}

/** 是否选择了图片文件：有图时改用 multipart 提交，让后端能做感知哈希查重。 */
function hasImageFile() {
  const el = $('f-image');
  return !!(el && el.files && el.files.length);
}

/** 组装请求体：有图片走 FormData（multipart），否则走 JSON。 */
function buildBody(data, save) {
  if (!hasImageFile()) {
    return { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
  }
  const fd = new FormData();
  Object.entries(data).forEach(([k, v]) => { if (v !== null && v !== '') fd.append(k, v); });
  fd.append('image', $('f-image').files[0]);
  return { headers: {}, body: fd };
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/** 在原文中高亮命中的关键词：先转义再整体替换，单次遍历避免标签嵌套与 XSS。 */
function highlight(text, terms) {
  const raw = String(text ?? '');
  const list = [...new Set((terms || []).map(t => String(t).trim()).filter(Boolean))]
    .sort((a, b) => b.length - a.length);        // 长词优先，避免短词截断长词
  if (!list.length) return esc(raw);
  const escTerm = (s) => esc(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp('(' + list.map(escTerm).join('|') + ')', 'g');
  return esc(raw).replace(re, '<mark>$1</mark>');
}

function renderResult(r, data) {
  const cls = { pass: 'ok', review: 'warn', reject: 'bad' }[r.decision] || 'info';
  const llm = r.llm || {}, rule = r.rule || {};
  const src = data || {};
  const hits = (rule.hits || []).map(h => `
    <div class="hit ${h.severity}">
      <div class="h-top"><span>${esc(h.rule)}</span><span>${h.severity === 'high' ? '高危' : h.severity === 'medium' ? '中度' : '轻微'} · ${h.score}分</span></div>
      <div class="h-ev">${esc(h.evidence)}</div>
      ${h.evasion ? `<div class="h-adv">🛡️ 反规避：通过「${esc(h.evasion)}」识别</div>` : ''}
      <div class="h-adv">💡 ${esc(h.advice)}</div>
    </div>`).join('');
  // 汇总所有命中关键词，用于在原文中做证据高亮
  const terms = (rule.hits || []).flatMap(h => h.terms || []);

  const conflicts = (r.conflicts || []).map(c => `<span class="tag amber">${esc(c)}</span>`).join('');

  $('empty').style.display = 'none';
  $('result').style.display = 'block';
  $('result').innerHTML = `
    <div class="verdict ${cls}">
      <div class="v-title">${esc(r.decision_label)}</div>
      <div class="v-sub">综合风险分 ${r.risk_score} / 100（${r.risk_level === 'high' ? '高风险' : r.risk_level === 'medium' ? '中风险' : '低风险'}）</div>
      <div class="scorebar"><i style="width:${r.risk_score}%"></i></div>
    </div>

    <div class="metric-row">
      <div class="metric"><div class="m-num">${rule.score ?? 0}</div><div class="m-label">规则引擎分</div></div>
      <div class="metric"><div class="m-num">${llm.risk_score ?? 0}</div><div class="m-label">大模型分</div></div>
      <div class="metric"><div class="m-num">${r.latency_ms ?? 0}ms</div><div class="m-label">审核耗时</div></div>
    </div>

    <div class="tags">
      <span class="tag">${esc(r.category_name)}${r.sub_category ? ' · ' + esc(r.sub_category) : ''}</span>
      <span class="tag gray">模型置信度 ${Math.round((llm.confidence || 0) * 100)}%</span>
      ${llm.price_verdict && llm.price_verdict !== 'unknown' ? `<span class="tag ${llm.price_verdict === 'normal' ? 'green' : 'red'}">价格判定：${llm.price_verdict === 'normal' ? '合理' : llm.price_verdict === 'suspicious_low' ? '异常偏低' : '异常偏高'}</span>` : ''}
      ${r.llm && r.llm.is_fake ? '<span class="tag red">疑似虚假描述</span>' : ''}
      ${conflicts}
    </div>

    <div class="reason-box"><b>审核结论：</b>${esc(llm.reason || '—')}</div>

    ${terms.length ? `
    <div style="margin-bottom:12px">
      <b style="font-size:13px">原文证据（<mark style="background:#ffe3e3">命中词已高亮</mark>）</b>
      <div class="evidence-text" style="margin-top:8px">${highlight(src.title, terms)}</div>
      <div class="evidence-text">${highlight(src.description, terms)}</div>
    </div>` : ''}

    <div style="margin-bottom:12px">
      <b style="font-size:13px">规则引擎命中（${(rule.hits || []).length}）</b>
      <div style="margin-top:8px">${hits || '<div class="hint">未命中任何风险规则</div>'}</div>
    </div>

    <div class="reason-box"><b>整改建议：</b>${esc((r.suggestions || []).join(' '))}</div>

    <details>
      <summary>查看大模型推理过程（${esc(llm.engine || '-')} / ${esc(llm.model || '-')}${llm.fallback ? ' · 已降级' : ''}）</summary>
      <pre>${esc(llm.reasoning || '—')}</pre>
    </details>`;
}

async function doAudit(save) {
  const data = collectForm();
  if (!data.title) { alert('请填写标题'); return; }
  const btn = save ? $('btnSubmit') : $('btnAudit');
  const old = btn.textContent;
  btn.textContent = '审核中…'; btn.disabled = true;
  try {
    const url = save ? '/api/items' : '/api/audit';
    const req = buildBody(data);
    const resp = await fetch(url, {
      method: 'POST', headers: req.headers, body: req.body
    });
    const json = await resp.json();
    if (!json.ok) { alert(json.error || '审核失败'); return; }
    renderResult(json.result || json.audit, data);
    if (save) {
      alert(`已发布：${json.audit.decision_label}（风险 ${json.audit.risk_score} 分）`);
      setTimeout(() => location.reload(), 400);
    }
  } catch (e) {
    alert('请求失败：' + e.message);
  } finally {
    btn.textContent = old; btn.disabled = false;
  }
}

/* 引擎切换：本地分析器（离线）↔ 远程大模型，即时生效无需重启 */
async function loadEngineStatus() {
  try {
    const json = await (await fetch('/api/engine')).json();
    renderEngine(json.status);
  } catch (e) {
    $('engineStatus').textContent = '引擎状态获取失败';
  }
}

function renderEngine(st) {
  const sel = $('engineSelect');
  sel.innerHTML = (st.available || []).map(p =>
    `<option value="${esc(p.key)}"${p.key === st.provider ? ' selected' : ''}${p.enabled ? '' : ' disabled'}>`
    + `${esc(p.label)}${p.needs_key && !st.has_key ? '（未配置密钥）' : ''}</option>`).join('');
  const remote = st.running !== 'mock';
  $('engineStatus').innerHTML = remote
    ? `远程大模型 <b>${esc(st.model)}</b> ｜ ${esc(st.base_url)}`
    : '本地分析器（离线模式，毫秒级、零成本）';
  $('engineBadge').className = 'engine-badge ' + (remote ? 'remote' : 'local');
  $('engineBadge').textContent = remote ? '🌐 联网模式' : '✈️ 离线模式';
}

async function switchEngine(provider) {
  const sel = $('engineSelect');
  sel.disabled = true;
  try {
    const resp = await fetch('/api/engine', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider })
    });
    const json = await resp.json();
    if (!json.ok) { alert(json.error || '切换失败'); return; }
    renderEngine(json.status);
    $('engineStatus').innerHTML = '✅ 已切换，下次审核即生效 — ' + $('engineStatus').innerHTML;
  } catch (e) {
    alert('切换失败：' + e.message);
  } finally {
    sel.disabled = false;
  }
}

/* 批量预检：一次提交多条走同一条审核链路，只审核不入库 */
function parseBatch(text) {
  return text.split('\n').map(l => l.trim()).filter(Boolean).slice(0, 50).map(line => {
    const parts = line.split('|').map(s => s.trim());
    const price = parts[2] ? parseFloat(parts[2]) : null;
    return { title: parts[0] || '', description: parts[1] || '',
             price: (price !== null && !isNaN(price)) ? price : null };
  }).filter(x => x.title || x.description);
}

async function runBatch() {
  const items = parseBatch($('batchInput').value);
  if (!items.length) { alert('请先输入待审核内容（每行一条）'); return; }
  const btn = $('btnBatch');
  btn.disabled = true; btn.textContent = '审核中…';
  try {
    const resp = await fetch('/api/batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items })
    });
    const json = await resp.json();
    if (!json.ok) { alert(json.error || '批量审核失败'); return; }
    const s = json.summary;
    $('batchSummary').textContent =
      `共 ${s.total} 条，通过 ${s.counts.pass} / 复核 ${s.counts.review} / 拒绝 ${s.counts.reject}`
      + `，拦截率 ${(s.reject_rate * 100).toFixed(0)}%，平均 ${s.avg_ms} ms/条（合计 ${s.total_ms} ms）`;
    $('batchResult').innerHTML = json.rows.map(r => r.error
      ? `<div class="hint">第 ${r.index + 1} 行：${esc(r.error)}</div>`
      : `<div class="hint"><span class="pill ${r.decision === 'pass' ? 'ok' : r.decision === 'review' ? 'warn' : 'bad'}">`
        + `${esc(r.decision_label)}</span> ${esc(r.title)} · ${r.risk_score} 分 · ${esc(r.category)}`
        + `${r.top_rule ? ' · 命中：' + esc(r.top_rule) : ''}</div>`).join('');
  } catch (e) {
    alert('批量审核失败：' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = '批量审核';
  }
}

/** 用户举报：机器放过但用户认为有问题时，交回人工复核并计入漏放反馈 */
async function reportItem(id) {
  const reason = prompt('举报理由（不少于 5 个字）：', '');
  if (!reason || reason.trim().length < 5) return;
  try {
    const resp = await fetch('/api/report', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: id, reason: reason.trim() })
    });
    const json = await resp.json();
    if (!json.ok) { alert(json.error || '举报失败'); return; }
    alert(json.to_review ? '已举报，该信息已转为人工复核' : '已举报，管理员会尽快处理');
    if (json.to_review) setTimeout(() => location.reload(), 300);
  } catch (e) {
    alert('举报失败：' + e.message);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  $('btnAudit').addEventListener('click', () => doAudit(false));
  $('btnSubmit').addEventListener('click', () => doAudit(true));
  $('engineSelect').addEventListener('change', (e) => switchEngine(e.target.value));
  $('f-image').addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    $('imgHint').textContent = f ? `已选择：${f.name}（提交时会做盗图查重）` : '未选择图片';
    if (f) $('f-img').checked = true;
  });
  $('btnBatch').addEventListener('click', runBatch);
  loadEngineStatus();
  document.querySelectorAll('#samples button').forEach(b => {
    b.addEventListener('click', () => {
      $('f-title').value = b.dataset.t;
      $('f-desc').value = b.dataset.d;
      $('f-price').value = b.dataset.p;
      doAudit(false);
    });
  });
});
