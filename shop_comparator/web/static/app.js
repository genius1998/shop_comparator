'use strict';
const $ = id => document.getElementById(id);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const won = v => v == null ? '—' : new Intl.NumberFormat('ko-KR').format(v) + '원';
const when = v => v ? new Date(v.includes('T') ? v : v.replace(' ', 'T') + 'Z').toLocaleString('ko-KR', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'}) : '';
const SITE = {musinsa: '무신사', kream: 'KREAM', poizon: 'POIZON'};
const STATE_CLASS = {logged_in: 'good', expired: 'warn', failed: 'bad', needs_user: 'warn', checking: 'warn', unknown: ''};
let state = null, products = [], matches = [], selected = new Set(), brands = [], logAfter = 0, logEvents = [], lastJobStates = {};

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, '');
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const child of children.flat()) if (child != null) node.append(child);
  return node;
}
function notify(message, ok = false) {
  const box = $('message');
  box.textContent = message; box.hidden = !message; box.classList.toggle('ok', ok);
  if (ok) setTimeout(() => { if (box.textContent === message) box.hidden = true; }, 4000);
}
async function api(path, body) {
  const options = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  let data = {};
  try { data = await response.json(); } catch (e) { /* empty body */ }
  if (!response.ok) throw new Error(data.error || `요청 실패 (${response.status})`);
  return data;
}
const run = promise => promise.then(d => { notify(''); return d; }).catch(e => { notify(e.message); });

/* ---------- tabs ---------- */
function showTab(name) {
  $$('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  $$('.tab').forEach(t => t.classList.toggle('on', t.id === 'tab-' + name));
  location.hash = name;
  if (name === 'products') loadProducts();
  if (name === 'match') loadMatches();
  if (name === 'monitor') loadMonitors();
  if (name === 'export') loadSettings();
  if (name === 'logs') loadLogs(true);
}
$$('#tabs button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));

/* ---------- state polling ---------- */
function renderSessions() {
  const box = $('session-badges'); box.replaceChildren();
  for (const [site, s] of Object.entries(state.sessions)) {
    box.append(el('span', {class: 'badge ' + (STATE_CLASS[s.state] || ''), text: `${SITE[site]} · ${s.label}`}));
    const card = document.querySelector(`.login-card[data-site="${site}"]`);
    if (!card) continue;
    const badge = card.querySelector('[data-state]');
    badge.textContent = s.label; badge.className = 'badge ' + (STATE_CLASS[s.state] || '');
    card.querySelector('[data-detail]').textContent = [s.account && `계정 ${s.account}`, s.verifiedAt && `마지막 인증 확인 ${when(s.verifiedAt)}`, s.detail].filter(Boolean).join(' · ');
    const guide = {expired: '세션이 만료되었습니다. 다시 로그인해 주세요.', needs_user: '열린 Chrome 창에서 추가 인증을 완료해 주세요.', failed: '로그인에 실패했습니다. 계정 정보를 확인하고 다시 시도해 주세요.'}[s.state] || '';
    card.querySelector('[data-guide]').textContent = guide;
    card.querySelector('[data-forget]').hidden = !s.remembered;
    card.querySelector('[data-env]').hidden = !(state.envAccounts && state.envAccounts[site]);
    const busy = Boolean(state.busy[site]);
    $$('button', card).forEach(b => { if (!b.hasAttribute('data-forget')) b.disabled = busy; });
  }
}
function renderJobs() {
  const bar = $('jobbar'); bar.replaceChildren();
  const recent = state.jobs.filter(j => ['running', 'stopping'].includes(j.state) || (Date.now() - new Date(j.finishedAt).getTime() < 60000)).slice(0, 5);
  for (const job of recent) {
    const active = ['running', 'stopping'].includes(job.state);
    const p = job.progress;
    const summary = Object.entries(job.summary).filter(([k]) => !['goods', 'byStatus', 'stageErrors'].includes(k)).map(([k, v]) => `${k} ${v}`).join(' · ');
    bar.append(el('div', {class: 'job ' + job.state},
      el('strong', {text: job.title}), el('span', {class: 'badge', text: job.stateLabel}),
      active && p.total ? el('progress', {max: p.total, value: p.done}) : null,
      el('span', {class: 'grow', text: [p.stage && (p.total ? `${p.stage} ${p.done}/${p.total}` : p.stage), summary, job.error].filter(Boolean).join(' · ')}),
      el('button', {text: '로그', onclick: () => { showTab('logs'); $('log-job').value = job.id; loadLogs(true); }}),
      active ? el('button', {text: job.state === 'stopping' ? '중단 중…' : '작업 중단', disabled: job.state === 'stopping', onclick: () => run(api('/api/stop', {id: job.id})).then(poll)}) : null));
  }
  for (const job of state.jobs) {
    if (lastJobStates[job.id] && lastJobStates[job.id] !== job.state && !['running', 'stopping'].includes(job.state)) {
      if (job.state === 'failed') notify(`${job.title} 실패: ${job.error || ''}`);
      if ($('tab-products').classList.contains('on')) loadProducts();
      if ($('tab-match').classList.contains('on')) loadMatches();
      if ($('tab-monitor').classList.contains('on')) loadMonitors();
      if ($('tab-export').classList.contains('on')) loadHistory();
    }
    lastJobStates[job.id] = job.state;
  }
  const select = $('log-job'), current = select.value;
  select.replaceChildren(el('option', {value: '', text: '작업 전체'}), ...state.jobs.map(j => el('option', {value: j.id, text: `${j.title} · ${when(j.startedAt)}`})),
    el('option', {value: 'auto-event', text: '자동화'}), ...['restock', 'price', 'limit'].map(k => el('option', {value: 'monitor-' + k, text: '모니터링 ' + k})));
  select.value = current;
}
function renderPrereq() {
  const c = state.counts, s = state.sessions, lines = [];
  if (s.musinsa.state !== 'logged_in') lines.push('무신사 회원가 수집에는 무신사 로그인이 필요합니다.');
  if (!c.products) lines.push('KREAM 매칭·가격 수집·비교는 무신사 수집 결과가 있어야 실행됩니다.');
  else lines.push(`저장된 무신사 상품 ${c.products}개(사이즈 ${c.variants}개), KREAM 매칭 ${c.matched}개.`);
  if (s.kream.state !== 'logged_in') lines.push('KREAM 매칭·가격 수집은 로그인 없이 실행됩니다. 거래량 필터만 KREAM 로그인이 필요합니다.');
  $('prereq').textContent = lines.join(' ');
  const last = state.lastCollection || {count: 0};
  $('scope-hint').textContent = $('scope').value === 'all'
    ? `상품 목록 전체 ${c.products}개가 대상입니다(이전에 저장된 상품 포함).`
    : (last.count ? `방금 수집한 ${last.count}개가 대상입니다 (${when(last.at)} 수집). 특정 상품만 하려면 상품·비교 탭에서 선택하세요.` : '아직 이번에 수집한 상품이 없습니다. 먼저 [무신사만 수집]을 실행하세요.');
  $('auto-status').textContent = state.auto.status; $('auto-status').className = 'badge ' + (state.auto.running ? 'good' : '');
}
async function poll() {
  try {
    state = await api('/api/state');
    renderSessions(); renderJobs(); renderPrereq();
    if ($('tab-logs').classList.contains('on') && state.logSeq > logAfter) loadLogs();
    if ($('tab-monitor').classList.contains('on')) loadMonitors();
    // Live view: while any job runs, keep the open list in step with what has been stored so far.
    const busy = state.jobs.some(j => ['running', 'stopping'].includes(j.state));
    if (busy && $('tab-products').classList.contains('on') && $('detail').hidden) loadProducts();
    if (busy && $('tab-match').classList.contains('on') && !document.activeElement?.closest('#match-rows')) loadMatches();
  } catch (e) { notify('서버에 연결할 수 없습니다. 웹 서버 실행 상태를 확인해 주세요.'); }
}

/* ---------- login ---------- */
$$('.login-card').forEach(card => {
  const site = card.dataset.site;
  const send = manual => {
    const account = card.account.value.trim(), password = card.password.value;
    const body = manual ? {manual: true} : {account, password, remember: card.remember.checked};
    card.password.value = '';
    run(api(`/api/login/${site}`, body)).then(d => { if (d) notify(manual ? '열린 Chrome 창에서 로그인해 주세요.' : '로그인을 시작했습니다. Chrome 창이 열립니다.', true); poll(); });
  };
  card.addEventListener('submit', e => { e.preventDefault(); send(false); });
  card.querySelector('[data-manual]')?.addEventListener('click', () => send(true));
  card.querySelector('[data-env]').addEventListener('click', () => run(api(`/api/login/${site}`, {env: true, remember: card.remember.checked}))
    .then(d => { if (d) notify('.env 계정으로 로그인을 시작했습니다. Chrome 창이 열립니다.', true); poll(); }));
  card.querySelector('[data-check]')?.addEventListener('click', () => run(api(`/api/session/${site}/check`, {})).then(poll));
  card.querySelector('[data-forget]').addEventListener('click', () => run(api(`/api/session/${site}/forget`, {})).then(poll));
});

/* ---------- collect ---------- */
function modeChanged() {
  const mode = $('mode').value;
  $$('#collect-form [data-mode]').forEach(d => d.hidden = !d.dataset.mode.split(' ').includes(mode));
  $('limit-label').textContent = mode === 'brand' ? '브랜드당 수집 개수' : '상품 수 (TopN)';
  if (mode === 'brand' && $('limit').value === '100') $('limit').value = 30;
  if (mode === 'brand' && !brands.length) loadSettings();
}
$('mode').addEventListener('change', modeChanged);
function renderBrands() {
  const query = $('brand-search').value.toLowerCase(), box = $('brand-list');
  const checked = new Set($$('input:checked', box).map(i => i.value));
  box.replaceChildren(...brands.filter(b => b.toLowerCase().includes(query) || checked.has(b)).map(b =>
    el('label', {class: 'check'}, el('input', {type: 'checkbox', name: 'brands', value: b, checked: checked.has(b)}), ' ' + b)));
  $('brand-hint').textContent = brands.length ? `브랜드 ${brands.length}개 (kream_overlapping_brands CSV 기준)` : '브랜드 CSV(kream_overlapping_brands*.csv)를 찾을 수 없습니다.';
}
$('brand-search').addEventListener('input', renderBrands);
function collectSettings() {
  const checked = name => $$(`#collect-form input[name="${name}"]:checked`).map(i => i.value);
  return {mode: $('mode').value, products: $('products-input').value, limit: $('limit').value, rankings: checked('rankings'),
          categories: checked('categories'), brands: checked('brands'), price_mode: $('price-mode').value, replace_list: $('replace-list').checked};
}
$$('[data-job]').forEach(button => button.addEventListener('click', e => {
  e.preventDefault();
  const kind = button.dataset.job;
  let body = button.hasAttribute('data-form') ? collectSettings() : {scope: $('scope').value};
  if (kind === 'filter') body = {scope: $('scope').value, min_total: $('min-total').value, min_monthly: $('min-monthly').value};
  if (!button.hasAttribute('data-form') && $('scope').value === 'all' &&
      !confirm(`상품 목록 전체(${state.counts.products}개)를 대상으로 실행합니다. 계속할까요?`)) return;
  if (kind === 'poizon' || kind === 'full_poizon') {
    const card = document.querySelector('.login-card[data-site="poizon"]');
    body.account = card.account.value.trim(); body.password = card.password.value;
  }
  run(api(`/api/jobs/${kind}`, body)).then(d => { if (d) notify(`${button.firstChild.textContent.trim()} 작업을 시작했습니다.`, true); poll(); });
}));

/* ---------- products ---------- */
function statusClass(status) {
  if (status === '비교 완료') return 'good';
  if (status === '품절' || status === '무신사에 없는 사이즈') return 'muted';
  if (/실패|오류/.test(status)) return 'bad';
  return 'warn';
}
function diffCell(value) {
  const td = el('td', {class: 'num'});
  if (value == null) td.textContent = '—';
  else td.append(el('span', {class: value > 0 ? 'pos' : value < 0 ? 'neg' : '', text: (value > 0 ? '+' : '') + won(value)}));
  return td;
}
function renderProducts() {
  const query = $('product-search').value.toLowerCase(), filter = $('status-filter').value, showOnly = $('show-kream-only').checked;
  const body = $('product-rows'); body.replaceChildren();
  let rows = 0, compared = 0, attention = 0;
  const statuses = new Set();
  for (const p of products) {
    let sizes = p.sizes.filter(s => showOnly || !s.kreamOnly);
    sizes.forEach(s => statuses.add(s.status));
    const text = `${p.goodsName} ${p.brandName} ${p.goodsNo} ${p.serial}`.toLowerCase();
    if (query && !text.includes(query)) sizes = sizes.filter(s => `${s.size} ${s.kreamSize}`.toLowerCase().includes(query));
    if (filter) sizes = sizes.filter(s => s.status === filter);
    if ((query || filter) && !sizes.length && !(query && !filter && text.includes(query))) continue;
    rows += sizes.length; compared += sizes.filter(s => s.status === '비교 완료').length;
    attention += sizes.filter(s => !['비교 완료', '품절', '무신사에 없는 사이즈'].includes(s.status)).length;
    const box = el('input', {type: 'checkbox', checked: selected.has(p.goodsNo), 'aria-label': '선택'});
    box.addEventListener('click', e => e.stopPropagation());
    box.addEventListener('change', () => { box.checked ? selected.add(p.goodsNo) : selected.delete(p.goodsNo); selectionChanged(); });
    const m = p.match;
    const info = [p.brandName, p.goodsNo, p.serial && `품번 ${p.serial}`, m.kreamId ? `KREAM ${m.kreamId} (${m.status}${m.confidence != null ? ' ' + Math.round(m.confidence * 100) + '%' : ''})` : 'KREAM 매칭 없음',
                  p.totalTrades ? `거래 ${p.totalTrades}건` : null].filter(Boolean).join(' · ');
    const errors = [p.musinsaStatus === 'error' && `무신사 오류: ${p.musinsaError}`, p.kreamStatus === 'error' && `KREAM 오류: ${p.kreamError}`].filter(Boolean).join(' / ');
    const links = el('small', {},
      el('a', {href: `https://www.musinsa.com/products/${encodeURIComponent(p.goodsNo)}`, target: '_blank', rel: 'noopener noreferrer', text: '무신사 ↗'}), ' ',
      m.kreamId ? el('a', {href: `https://kream.co.kr/products/${encodeURIComponent(m.kreamId)}`, target: '_blank', rel: 'noopener noreferrer', text: 'KREAM ↗'}) : null, ' ',
      p.poizonLink && /^https:\/\/seller\.poizon\.com\//.test(p.poizonLink) ? el('a', {href: p.poizonLink, target: '_blank', rel: 'noopener noreferrer', text: 'POIZON ↗'}) : null);
    links.addEventListener('click', e => e.stopPropagation());
    const tr = el('tr', {class: 'product' + (p.active ? '' : ' hidden-item')},
      el('td', {}, box), el('td', {text: p.rank ?? ''}),
      el('td', {colspan: 7}, p.goodsName || p.goodsNo, el('small', {text: info}), errors ? el('small', {class: 'neg', text: errors}) : null, links),
      el('td', {}, el('small', {text: [p.musinsaCheckedAt && `무신사 ${when(p.musinsaCheckedAt)}`, p.kreamCheckedAt && `KREAM ${when(p.kreamCheckedAt)}`].filter(Boolean).join(' / ') || '미수집'}), p.active ? null : el('span', {class: 'badge', text: '숨김'})));
    tr.addEventListener('click', () => openDetail(p.goodsNo));
    body.append(tr);
    for (const s of sizes) {
      const sizeText = s.kreamOnly ? `(KREAM ${s.kreamSize})` : s.size + (s.kreamSize && s.kreamSize !== s.size ? ` ↔ ${s.kreamSize}` : '');
      body.append(el('tr', {class: 'size'}, el('td'), el('td'), el('td', {}, el('small', {text: [s.coupon && `쿠폰 ${s.coupon} −${won(s.couponDiscount)}`, s.memberDiscount ? `등급 −${won(s.memberDiscount)}` : null, s.pointDiscount ? `적립금 선할인 −${won(s.pointDiscount)}` : null, s.priceSource, s.musinsaError].filter(Boolean).join(' · ')})),
        el('td', {text: sizeText}),
        el('td', {class: 'num'}, won(s.musinsaPrice), s.salePrice != null && s.salePrice !== s.musinsaPrice ? el('small', {text: `판매가 ${won(s.salePrice)}`}) : null),
        el('td', {class: 'num'}, won(s.kreamBuy), s.kreamBuyDelivery ? el('small', {text: s.kreamBuyDelivery}) : null),
        el('td', {class: 'num', text: won(s.kreamSell)}), diffCell(s.difference),
        el('td', {class: 'num'}, won(s.poizonPrice)),
        el('td', {}, el('span', {class: 'badge ' + statusClass(s.status), text: s.status}), el('small', {text: [s.musinsaCheckedAt && when(s.musinsaCheckedAt), s.kreamCheckedAt && `K ${when(s.kreamCheckedAt)}`].filter(Boolean).join(' / ')}))));
    }
  }
  $('product-empty').hidden = products.length > 0;
  $('m-products').textContent = products.length; $('m-rows').textContent = rows; $('m-compared').textContent = compared; $('m-attention').textContent = attention;
  const select = $('status-filter'), current = select.value;
  select.replaceChildren(el('option', {value: '', text: '상태 전체'}), ...[...statuses].sort().map(s => el('option', {text: s})));
  select.value = current;
  selectionChanged();
}
function selectionChanged() {
  $('selected-count').textContent = `선택 ${selected.size}개`;
  $$('[data-selected]').forEach(b => b.disabled = !selected.size);
  document.querySelector('[data-selected="restore"]').hidden = !$('show-hidden').checked;
  $('select-all').checked = products.length > 0 && products.every(p => selected.has(p.goodsNo));
}
async function loadProducts() {
  const query = new URLSearchParams({scope: $('show-all').checked ? 'all' : 'last'});
  if ($('show-hidden').checked) query.set('hidden', '1');
  const data = await run(api('/api/products?' + query));
  if (!data) return;
  products = data.products;
  const last = data.lastCollection || {count: 0};
  $('view-hint').textContent = $('show-all').checked || !last.count
    ? `저장된 상품 전체 ${data.total}개를 표시 중입니다(이전 실행과 데스크톱 프로그램이 저장한 상품 포함).`
    : `방금 수집한 ${products.length}개만 표시 중입니다 (${when(last.at)} 수집). 저장된 전체 ${data.total}개를 보려면 "이전에 저장된 상품도 표시"를 켜세요.`;
  const known = new Set(products.map(p => p.goodsNo));
  selected = new Set([...selected].filter(g => known.has(g)));
  renderProducts();
}
['product-search', 'status-filter', 'show-kream-only'].forEach(id => $(id).addEventListener('input', renderProducts));
$('show-hidden').addEventListener('change', loadProducts);
$('show-all').addEventListener('change', loadProducts);
$('select-all').addEventListener('change', e => { products.forEach(p => e.target.checked ? selected.add(p.goodsNo) : selected.delete(p.goodsNo)); renderProducts(); });
$$('[data-selected]').forEach(button => button.addEventListener('click', () => {
  const ids = [...selected], action = button.dataset.selected;
  let request;
  if (action === 'musinsa') request = api('/api/jobs/musinsa', {mode: 'selected', ids, price_mode: $('price-mode').value});
  else if (action === 'match') request = api('/api/jobs/match', {ids, force: true});
  else if (action === 'kream') request = api('/api/jobs/kream', {ids});
  else request = api('/api/products/active', {ids, active: action === 'restore'});
  run(request).then(d => { if (d) notify('요청을 처리했습니다.', true); poll(); if (action === 'hide' || action === 'restore') loadProducts(); });
}));
async function openDetail(goodsNo) {
  const data = await run(api(`/api/products/${encodeURIComponent(goodsNo)}`));
  if (!data) return;
  const p = data.product, yn = v => v == null ? '' : v ? 'Y' : 'N';
  const fields = [['브랜드', p.brandName], ['상품번호', p.goodsNo], ['품번', p.serial_number], ['링크', p.linkUrl], ['카테고리', p.baseCategoryFullName],
    ['정가', won(p.normalPrice)], ['판매가(최초 옵션)', won(p.salePrice)], ['쿠폰할인', won(p.couponDiscount)], ['쿠폰', p.couponName], ['최종가(최초 옵션)', won(p.finalPrice)],
    ['멤버할인%', p.memberDiscountRate], ['적립률%', p.memberSavePointRate], ['멤버 할인 가능', yn(p.isMemberDiscount)], ['적립 선할인', yn(p.isUsePointDiscount)],
    ['goodsSavePoint', won(p.goodsSavePoint)], ['가능한 사이즈', p.availableSizes], ['선택 옵션코드', p.managedCode], ['마지막 관측가', won(p.lastObservedFinalPrice)],
    ['KREAM 상품', data.match.kreamId ? `${data.match.kreamId} · ${data.match.status}` : '매칭 없음'], ['매칭 근거', data.match.reason],
    ['크림 전체 거래건수', p.kream_total_transactions != null ? p.kream_total_transactions + '건' : ''], ['크림 한달간 거래수', p.kream_monthly_transactions != null ? p.kream_monthly_transactions + '건' : ''],
    ['무신사 조회', when(p.musinsaCheckedAt)], ['KREAM 조회', when(p.kreamCheckedAt)]];
  const dl = el('dl');
  for (const [k, v] of fields) if (v !== '' && v != null && v !== '—') dl.append(el('dt', {text: k}), el('dd', {text: String(v)}));
  const image = p.imageUrl && /^https:\/\/[a-z0-9.-]*msscdn\.net\//.test(p.imageUrl) ? el('img', {src: p.imageUrl, alt: '', referrerpolicy: 'no-referrer'}) : el('p', {class: 'hint', text: '이미지 없음'});
  const table = el('table', {}, el('thead', {}, el('tr', {}, ...['사이즈', '회원가', '즉시구매', '즉시판매', '상태'].map(t => el('th', {text: t})))),
    el('tbody', {}, ...data.sizes.map(s => el('tr', {}, el('td', {text: s.size || `(KREAM ${s.kreamSize})`}), el('td', {class: 'num', text: won(s.musinsaPrice)}), el('td', {class: 'num', text: won(s.kreamBuy)}), el('td', {class: 'num', text: won(s.kreamSell)}), el('td', {text: s.status})))));
  $('detail-body').replaceChildren(el('h2', {text: `${p.rank ? `[${p.rank}위] ` : ''}${p.goodsName || p.goodsNo}`}), image, dl, table);
  $('detail').hidden = false;
}
$('detail-close').addEventListener('click', () => $('detail').hidden = true);

/* ---------- matches ---------- */
async function loadMatches() {
  const data = await run(api('/api/matches'));
  if (!data) return;
  matches = data.matches; renderMatches();
}
function renderMatches() {
  const query = $('match-search').value.toLowerCase(), filter = $('match-filter').value;
  const kept = new Set($$('#match-rows input[data-goods]:checked').map(i => i.dataset.goods));
  $('match-rows').replaceChildren(...matches.filter(m => (!filter || m.status === filter) && `${m.goodsName} ${m.goodsNo} ${m.serial}`.toLowerCase().includes(query)).map(m => {
    const input = el('input', {type: 'text', placeholder: 'KREAM URL 또는 번호', value: ''});
    const save = el('button', {text: '지정', onclick: () => run(api('/api/matches', {goodsNo: m.goodsNo, kream: input.value})).then(d => { if (d) { notify('직접 지정했습니다.', true); loadMatches(); } })});
    return el('tr', {}, el('td', {}, el('input', {type: 'checkbox', 'data-goods': m.goodsNo, checked: kept.has(m.goodsNo)})),
      el('td', {}, m.goodsName, el('small', {text: `${m.brandName} · ${m.goodsNo}`})), el('td', {text: m.serial}),
      el('td', {}, m.kreamId ? el('a', {href: `https://kream.co.kr/products/${encodeURIComponent(m.kreamId)}`, target: '_blank', rel: 'noopener noreferrer', text: m.kreamId + ' ↗'}) : '—', el('small', {text: [m.kreamBrand, m.kreamName].filter(Boolean).join(' ')})),
      el('td', {}, el('span', {class: 'badge ' + ({'직접 지정': 'good', '모델번호 일치': 'good', '이미지 검토': 'good', '자동 검증': 'good', '미검증': 'warn'}[m.status] || ''), text: m.status}), m.blacklisted ? el('small', {text: '검색 제외 목록'}) : null),
      el('td', {}, m.confidence != null ? Math.round(m.confidence * 100) + '%' : '', el('small', {text: [m.method, m.reason].filter(Boolean).join(' · ')})),
      el('td', {class: 'inline'}, input, save));
  }));
}
['match-search', 'match-filter'].forEach(id => $(id).addEventListener('input', renderMatches));
$$('[data-match]').forEach(button => button.addEventListener('click', () => {
  const ids = $$('#match-rows input[data-goods]:checked').map(i => i.dataset.goods);
  if (!ids.length) return notify('상품을 선택해 주세요.');
  const action = button.dataset.match;
  const request = action === 'search' ? api('/api/jobs/match', {ids, force: true}) : api(`/api/matches/${action}`, {ids});
  run(request).then(d => { if (d) { notify('요청을 처리했습니다.', true); loadMatches(); poll(); } });
}));

/* ---------- monitoring ---------- */
async function loadMonitors() {
  const data = await api('/api/monitors').catch(() => null);
  if (!data) return;
  for (const [kind, m] of Object.entries(data.monitors)) {
    const card = document.querySelector(`.monitor[data-kind="${kind}"]`);
    const badge = card.querySelector('[data-running]');
    badge.textContent = m.running ? `실행 중 · ${m.interval}초` : '중지'; badge.className = 'badge ' + (m.running ? 'good' : '');
    const checked = new Set($$('input[data-goods]:checked', card).map(i => i.dataset.goods));
    card.querySelector('[data-rows]').replaceChildren(...m.items.map(i => {
      const s = i.state || {};
      const value = kind === 'restock' ? (s.text || '') : kind === 'price' ? `${won(s.original)} → ${won(s.current)}` : (s.limited == null ? '' : s.limited ? '제한' : '없음');
      return el('tr', {}, el('td', {}, el('input', {type: 'checkbox', 'data-goods': i.goodsNo, checked: checked.has(i.goodsNo)})), el('td', {text: i.goodsNo}), el('td', {text: i.brandName}), el('td', {text: i.goodsName}),
        el('td', {}, el('span', {class: 'badge ' + ({good: 'good', bad: 'bad', warn: 'warn', down: 'good', muted: 'muted'}[s.flag] || ''), text: i.status})), el('td', {text: value}), el('td', {text: when(i.updatedAt)}));
    }));
    card.querySelector('[data-start]').disabled = m.running; card.querySelector('[data-stop]').disabled = !m.running;
  }
}
$$('.monitor').forEach(card => {
  const kind = card.dataset.kind, act = (action, body = {}) => run(api(`/api/monitors/${kind}/${action}`, body)).then(d => { if (d) notify('요청을 처리했습니다.', true); loadMonitors(); poll(); });
  card.querySelector('[data-add]').addEventListener('click', () => { act('add', {ids: card.querySelector('[data-id]').value.split(/[\s,]+/).filter(Boolean)}); card.querySelector('[data-id]').value = ''; });
  card.querySelector('[data-add-current]').addEventListener('click', () => act('add', {current: true}));
  card.querySelector('[data-remove]').addEventListener('click', () => act('remove', {ids: $$('input[data-goods]:checked', card).map(i => i.dataset.goods)}));
  card.querySelector('[data-start]').addEventListener('click', () => act('start', {interval: card.querySelector('[data-interval]').value}));
  card.querySelector('[data-stop]').addEventListener('click', () => act('stop'));
});
$$('[data-auto]').forEach(button => button.addEventListener('click', () => {
  const mode = button.dataset.auto;
  const request = mode === 'stop' ? api('/api/auto/stop', {}) : api('/api/auto/start', {...collectSettings(), mode, day: $('auto-day').value, night: $('auto-night').value, seed: $('auto-seed').value});
  run(request).then(d => { if (d) notify(mode === 'stop' ? '중지를 요청했습니다.' : '자동화를 시작했습니다.', true); poll(); });
}));

/* ---------- export / settings ---------- */
async function loadSettings() {
  const data = await api('/api/settings').catch(() => null);
  if (!data) return;
  brands = data.brands; renderBrands();
  $('webhook-state').textContent = data.webhookSet ? `(저장됨 ${data.webhookMasked})` : '(미설정)';
  $('settings-form').alert_margin.value = data.alertMargin || 0;
  $('alert-basis').replaceChildren(...Object.entries(data.alertBases || {}).map(([k, v]) => el('option', {value: k, text: v})));
  $('alert-basis').value = data.alertBasis;
  loadHistory();
}
async function loadHistory() {
  const data = await api('/api/jobs/history').catch(() => null);
  if (!data) return;
  $('history-rows').replaceChildren(...data.jobs.map(j => el('tr', {}, el('td', {text: when(j.startedAt)}), el('td', {text: j.title}), el('td', {text: j.state}),
    el('td', {text: Object.entries(j.summary || {}).filter(([k, v]) => v != null && !['goods', 'byStatus'].includes(k)).map(([k, v]) => `${k} ${Array.isArray(v) ? v.join(' / ') : v}`).join(' · ')}))));
}
$('settings-form').addEventListener('submit', e => {
  e.preventDefault();
  const form = e.target, body = {alert_margin: form.alert_margin.value, alert_basis: form.alert_basis.value};
  if (form.webhook_url.value.trim()) body.webhook_url = form.webhook_url.value.trim();
  form.webhook_url.value = '';
  run(api('/api/settings', body)).then(d => { if (d) { notify('저장했습니다.', true); loadSettings(); } });
});
$('webhook-test').addEventListener('click', () => run(api('/api/settings/test-webhook', {})).then(d => d && notify('테스트 알림을 보냈습니다.', true)));
$('webhook-clear').addEventListener('click', () => run(api('/api/settings', {webhook_url: ''})).then(loadSettings));
async function download(url) {
  const ids = $('export-selected').checked && selected.size ? [...selected].join(',') : '';
  const full = url + (ids ? (url.includes('?') ? '&' : '?') + 'ids=' + encodeURIComponent(ids) : '');
  const response = await fetch(full);
  if (!response.ok) { let d = {}; try { d = await response.json(); } catch (e) { /* not json */ } return notify(d.error || '다운로드에 실패했습니다.'); }
  const name = /filename\*?=(?:UTF-8'')?"?([^";]+)/i.exec(response.headers.get('Content-Disposition') || '');
  const link = el('a', {href: URL.createObjectURL(await response.blob()), download: name ? decodeURIComponent(name[1]) : 'download'});
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(link.href), 5000);
  notify('파일을 내려받았습니다.', true);
}
$$('[data-download]').forEach(a => a.addEventListener('click', e => { e.preventDefault(); download(a.getAttribute('href')); }));
$('bid-form').addEventListener('submit', e => { e.preventDefault(); download('/api/export/bid.xlsx?' + new URLSearchParams(new FormData(e.target))); });

/* ---------- logs ---------- */
async function loadLogs(reset = false) {
  if (reset) { logAfter = 0; logEvents = []; }
  const params = new URLSearchParams({after: logAfter, job: $('log-job').value, shop: $('log-shop').value, level: $('log-level').value, q: $('log-search').value});
  const data = await api('/api/logs?' + params).catch(() => null);
  if (!data) return;
  logEvents = logEvents.concat(data.events).slice(-800); logAfter = data.last;
  $('log-file').textContent = `파일: ${data.file} (5MB × 6개 순환 보관)`;
  $('log-rows').replaceChildren(...logEvents.slice().reverse().map(e => el('tr', {class: 'lv-' + e.level},
    ...[when(e.ts), e.level, e.job || '', SITE[e.shop] || e.shop || '', e.goods || '', e.stage || '', e.error || '', e.message].map(t => el('td', {text: t})))));
}
['log-job', 'log-shop', 'log-level'].forEach(id => $(id).addEventListener('change', () => loadLogs(true)));
$('log-search').addEventListener('change', () => loadLogs(true));

/* ---------- boot ---------- */
modeChanged();
const tabFromHash = () => (location.hash || '#login').slice(1).replace(/[^a-z]/g, '') || 'login';
showTab(tabFromHash());
window.addEventListener('hashchange', () => { if (!$('tab-' + tabFromHash())?.classList.contains('on')) showTab(tabFromHash()); });
(async function loop() { await poll(); setTimeout(loop, document.hidden ? 10000 : 2500); })();
