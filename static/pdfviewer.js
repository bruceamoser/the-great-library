import * as pdfjsLib from '/static/pdfjs/build/pdf.mjs';

pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/pdfjs/build/pdf.worker.mjs';

const root = document.getElementById('pdfroot');
const url = root.dataset.url;
const desk=window.LibraryDesk;
const saved=desk?.load().reading.find(r=>r.path===root.dataset.path);
const startPage = new URLSearchParams(location.search).has('page') ? (parseInt(root.dataset.page,10)||1) : (saved?.page||1);
const canvas = document.getElementById('pdfcanvas');
const ctx = canvas.getContext('2d');
const pageInput = document.getElementById('pdfpage');
const pageCount = document.getElementById('pdfcount');
const status = document.getElementById('pdfstatus');

let pdfDoc = null;
let pageNum = 1;
let scale = 1.0;
let fitWidth = true;
let rendering = false;
let pending = null;

function setStatus(msg) {
  if (status) status.textContent = msg || '';
}

async function render(num) {
  if (!pdfDoc) return;
  if (rendering) { pending = num; return; }
  rendering = true;
  try {
    const page = await pdfDoc.getPage(num);

    let effectiveScale = scale;
    if (fitWidth) {
      const unscaled = page.getViewport({ scale: 1 });
      const avail = root.clientWidth - 4;
      effectiveScale = avail / unscaled.width;
    }
    const viewport = page.getViewport({ scale: effectiveScale });

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(viewport.width * dpr);
    canvas.height = Math.floor(viewport.height * dpr);
    canvas.style.width = Math.floor(viewport.width) + 'px';
    canvas.style.height = Math.floor(viewport.height) + 'px';

    await page.render({
      canvasContext: ctx,
      viewport,
      transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null,
    }).promise;

    pageInput.value = num;
    if(desk){const d=desk.load();d.reading=d.reading.filter(r=>r.path!==root.dataset.path);d.reading.unshift({path:root.dataset.path,name:root.dataset.name,id:Number(root.dataset.id),page:num,total:pdfDoc.numPages});d.reading=d.reading.slice(0,60);desk.save(d);}
    const readingUrl=new URL(location.href);readingUrl.searchParams.set('page',num);history.replaceState(null,'',readingUrl);
    pageCount.textContent = pdfDoc.numPages;
    setStatus('');
  } catch (err) {
    setStatus('render error: ' + err.message);
  } finally {
    rendering = false;
    if (pending !== null) {
      const n = pending; pending = null;
      render(n);
    }
  }
}

function go(n) {
  if (!pdfDoc) return;
  pageNum = Math.min(Math.max(1, n), pdfDoc.numPages);
  render(pageNum);
}

document.getElementById('pdfprev').onclick = () => go(pageNum - 1);
document.getElementById('pdfnext').onclick = () => go(pageNum + 1);
document.getElementById('pdfzin').onclick = () => {
  fitWidth = false; scale = Math.min(scale * 1.25, 6); render(pageNum);
};
document.getElementById('pdfzout').onclick = () => {
  fitWidth = false; scale = Math.max(scale / 1.25, 0.15); render(pageNum);
};
document.getElementById('pdffit').onclick = () => {
  fitWidth = !fitWidth; render(pageNum);
};
pageInput.onchange = () => go(parseInt(pageInput.value, 10) || 1);

document.addEventListener('keydown', (e) => {
  if (['INPUT','TEXTAREA','SELECT','BUTTON'].includes(e.target.tagName) || e.target.isContentEditable) return;
  if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); go(pageNum + 1); }
  if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); go(pageNum - 1); }
  if (e.key === 'Home') go(1);
  if (e.key === 'End') go(pdfDoc ? pdfDoc.numPages : 1);
});

let resizeTimer = null;
window.addEventListener('resize', () => {
  if (!fitWidth) return;
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => render(pageNum), 200);
});

(async () => {
  try {
    setStatus('loading…');
    pdfDoc = await pdfjsLib.getDocument({
      url,
      cMapUrl: '/static/pdfjs/cmaps/',
      cMapPacked: true,
      standardFontDataUrl: '/static/pdfjs/standard_fonts/',
    }).promise;
    pageCount.textContent = pdfDoc.numPages;
    pageNum = Math.min(Math.max(1, startPage), pdfDoc.numPages);
    setStatus('');
    render(pageNum);
  } catch (err) {
    setStatus('failed to load PDF: ' + err.message);
  }
})();

const notes=document.getElementById('book-notes');
if(notes && desk){notes.value=desk.load().notes[root.dataset.path]||'';let timer;notes.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>{const d=desk.load();d.notes[root.dataset.path]=notes.value;desk.save(d);},300);});notes.addEventListener('change',()=>{const d=desk.load();d.notes[root.dataset.path]=notes.value;desk.save(d);});}
document.getElementById('pdfbookmark')?.addEventListener('click',()=>{if(!desk||!pdfDoc)return;const d=desk.load();if(!d.bookmarks.some(b=>b.path===root.dataset.path&&b.page===pageNum)){d.bookmarks.unshift({path:root.dataset.path,name:root.dataset.name,id:Number(root.dataset.id),page:pageNum,total:pdfDoc.numPages});if(desk.save(d))desk.toast('A ribbon placed at page '+pageNum+'.');}else desk.toast('This page is already bookmarked.');});
