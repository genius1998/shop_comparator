'use strict';
/* Clickable system diagram: every node opens the matching <article class="detail"> in a side panel. */
(function () {
  document.documentElement.classList.add('js');
  const panel = document.getElementById('panel'), backdrop = document.getElementById('backdrop');
  const body = document.getElementById('panel-body'), title = document.getElementById('panel-title');
  const lightbox = document.getElementById('lightbox');
  const details = {};
  document.querySelectorAll('.details-source .detail').forEach(a => { details[a.dataset.detail] = a; });
  let lastFocus = null, current = null;

  function select(id) {
    document.querySelectorAll('[data-node]').forEach(n => {
      const on = n.dataset.node === id;
      n.classList.toggle('selected', on);
      n.setAttribute('aria-pressed', String(on));
    });
  }
  function open(id, focusPanel) {
    const source = details[id];
    if (!source) return;
    if (!current) lastFocus = document.activeElement;
    current = id;
    title.textContent = source.dataset.title;
    body.replaceChildren(source.cloneNode(true));
    body.querySelectorAll('img').forEach(img => {
      img.addEventListener('error', () => { img.alt = '이미지를 불러오지 못했습니다: ' + img.alt; img.style.cursor = 'default'; img.dataset.broken = '1'; });
      img.setAttribute('tabindex', '0');
      const zoom = () => { if (!img.dataset.broken) { lightbox.querySelector('img').src = img.src; lightbox.querySelector('img').alt = img.alt; lightbox.hidden = false; } };
      img.addEventListener('click', zoom);
      img.addEventListener('keydown', e => { if (e.key === 'Enter') zoom(); });
    });
    body.querySelectorAll('[data-goto]').forEach(b => b.addEventListener('click', () => open(b.dataset.goto, true)));
    panel.hidden = false; backdrop.hidden = false;
    panel.scrollTop = 0;
    select(id);
    if (location.hash !== '#' + id) history.replaceState(null, '', '#' + id);
    if (focusPanel !== false) document.getElementById('panel-close').focus();
  }
  function close() {
    if (!lightbox.hidden) { lightbox.hidden = true; return; }
    if (!current) return;
    current = null;
    panel.hidden = true; backdrop.hidden = true;
    select(null);
    history.replaceState(null, '', location.pathname + location.search);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  document.querySelectorAll('[data-node]').forEach(node => {
    node.addEventListener('click', () => open(node.dataset.node));
    node.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(node.dataset.node); } });
  });
  document.getElementById('panel-close').addEventListener('click', close);
  backdrop.addEventListener('click', close);
  lightbox.addEventListener('click', () => { lightbox.hidden = true; });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  document.getElementById('panel-copy').addEventListener('click', e => {
    const url = location.href;
    (navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject()).then(() => { e.target.textContent = '링크 복사됨'; }, () => { e.target.textContent = url; });
  });
  const fromHash = () => { const id = location.hash.slice(1); if (details[id]) open(id, false); };
  window.addEventListener('hashchange', fromHash);
  fromHash();
})();
