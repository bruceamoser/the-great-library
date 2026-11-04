/* Media popups and hover previews for the file lists.
 *
 * Buttons in the lists carry data-media="image|audio|video", data-url and
 * data-name. There is exactly one popup element (in base.html); it is reused,
 * and audio/video play in place.
 *
 * Nothing is fetched until a button is clicked or a row is hovered on purpose:
 * a 500-row result page would otherwise pull 500 files off a single spinning
 * disk just to render.
 */
(() => {
  'use strict';

  const modal = document.getElementById('mediamodal');
  if (!modal) return;

  const stage = modal.querySelector('.mm-stage');
  const titleEl = modal.querySelector('.mm-title');
  const openRaw = modal.querySelector('.mm-open');
  const closeBtn = modal.querySelector('.mm-close');
  const peek = document.getElementById('hoverpeek');
  let lastFocus = null;

  function closeModal() {
    modal.hidden = true;
    document.body.classList.remove('modal-open');
    stage.replaceChildren();   // detaching the element stops playback at once
    if (lastFocus && lastFocus.isConnected) lastFocus.focus();
    lastFocus = null;
  }

  function openModal(btn) {
    const kind = btn.dataset.media;
    const url = btn.dataset.url;
    if (!url) return;

    const name = btn.dataset.name || url;
    titleEl.textContent = name;
    titleEl.title = name;
    // 'open raw' always points at the original, even when the popup shows a
    // downscaled thumbnail (some map scans are 184 MB).
    openRaw.href = btn.dataset.raw || url;
    stage.replaceChildren();

    let el;
    if (kind === 'image') {
      el = document.createElement('img');
      el.className = 'mm-img';
      el.alt = name;
    } else if (kind === 'audio') {
      el = document.createElement('audio');
      el.className = 'mm-audio';
      el.controls = true;
      el.autoplay = true;
    } else if (kind === 'video') {
      el = document.createElement('video');
      el.className = 'mm-video';
      el.controls = true;
      el.autoplay = true;
    } else {
      return;
    }
    el.src = url;
    stage.appendChild(el);

    lastFocus = btn;
    modal.hidden = false;
    document.body.classList.add('modal-open');
    closeBtn.focus();
  }

  document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-media]');
    if (btn) { ev.preventDefault(); openModal(btn); return; }
    if (ev.target.closest('[data-close]')) { ev.preventDefault(); closeModal(); }
  });

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !modal.hidden) closeModal();
  });

  /* ---- hover preview ----------------------------------------------------
   * One shared element; the image is swapped per row and only ever requested
   * on hover, so scrolling a large list stays cheap.
   */
  const PEEK_DELAY = 220;
  let peekTimer = null;
  let peekFor = null;

  function hidePeek() {
    if (peekTimer) clearTimeout(peekTimer);
    peekTimer = null;
    peekFor = null;
    if (peek) peek.hidden = true;
  }

  function placePeek(anchor) {
    const r = anchor.getBoundingClientRect();
    const box = peek.getBoundingClientRect();
    let left = r.right + 14;
    let top = r.top;
    if (left + box.width > window.innerWidth - 8) left = r.left - box.width - 14;
    if (left < 8) left = 8;
    if (top + box.height > window.innerHeight - 8) {
      top = Math.max(8, window.innerHeight - box.height - 8);
    }
    peek.style.left = left + 'px';
    peek.style.top = top + 'px';
  }

  if (peek) {
    document.addEventListener('mouseover', (ev) => {
      const row = ev.target.closest('[data-preview]');
      if (!row || peekFor === row) return;
      hidePeek();
      peekFor = row;
      peekTimer = setTimeout(() => {
        const url = row.dataset.preview;
        if (!url || peekFor !== row) return;
        const img = peek.querySelector('img');
        img.alt = row.dataset.previewName || '';
        const show = () => {
          if (peekFor !== row) return;
          peek.hidden = false;
          placePeek(row);
        };
        img.onload = show;
        img.onerror = hidePeek;
        img.src = url;
        if (img.complete && img.naturalWidth) show();
      }, PEEK_DELAY);
    });

    document.addEventListener('mouseout', (ev) => {
      const row = ev.target.closest('[data-preview]');
      if (!row) return;
      // ignore movement that stays inside the same row
      if (ev.relatedTarget && row.contains(ev.relatedTarget)) return;
      hidePeek();
    });

    window.addEventListener('scroll', hidePeek, { passive: true });
  }
})();
