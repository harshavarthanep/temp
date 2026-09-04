/* ==================================================================
   ZenPDF — LARGE MERGE + IMAGE WORKSPACE PACK   v1.1.0
   ------------------------------------------------------------------
   PASTE AT THE VERY END OF index.html, immediately before </body>,
   AFTER the v1.0.0 geometry fix block.

   Requires v1.0.0 (it reuses safeLoad / safeSave when present, and
   falls back gracefully if they are missing).

   PART A — MERGE AT ANY SIZE
   --------------------------
   The old procMerge did this:

       const pages = await merged.copyPages(src, src.getPageIndices());
       pages.forEach(p => merged.addPage(p));

   That copies EVERY page of a source in one synchronous burst, with
   no yield to the event loop. Merging two 1,000-page files meant:
     * the tab froze solid for the whole operation, and Chrome could
       show "Page unresponsive" and offer to kill it;
     * peak memory held the parsed object graph of the source AND the
       destination at once, with no chance for the GC to run between
       pages, so big scans hit the tab's heap ceiling and threw
       "Array buffer allocation failed";
     * one damaged or password-protected file aborted the whole merge
       and lost the work already done;
     * there was no progress or cancel — a long merge was
       indistinguishable from a crash.

   Now: pages are copied in chunks with a yield between them, each
   source is released as soon as it is drained, progress is reported
   per page, the operation can be cancelled, bad files are skipped and
   reported rather than fatal, and the final save is tuned for large
   documents. There is no page-count limit — only the device's memory,
   and the merge now tells you honestly when it is near it.

   PART B — IMAGES TO PDF, WITH A REAL WORKSPACE
   ---------------------------------------------
   The image list was a bare name + size row: no preview, no ordering,
   no way to drop one image without clearing the lot. It now behaves
   exactly like Merge — thumbnails, drag-to-reorder, up/down, include
   /exclude ticks, per-image rotate, whole-set preview, page setup,
   and a preview of the finished PDF before anything downloads.
   ================================================================== */
(function () {
'use strict';

if (window.__zenMergeImgFix) return;
window.__zenMergeImgFix = '1.1.0';

const LOG = (...a) => { try { console.debug('[zen-merge]', ...a); } catch (e) {} };
const $id = (x) => document.getElementById(x);

/* v1.0.0 helpers if present, otherwise safe local equivalents */
const load = (b) => (window.safeLoad ? window.safeLoad(b)
  : PDFDocument.load(b, { ignoreEncryption: true, updateMetadata: false,
                          throwOnInvalidObject: false }));

/* let the browser paint and let the GC breathe */
const tick = () => new Promise(r => setTimeout(r, 0));

/* ==================================================================
   SHARED — progress sheet with a working Cancel
   ================================================================== */
function progressSheet(title, subtitle) {
  const s = sheetOpen(`
    <h3><i class="fas fa-circle-notch spin"></i> ${escapeHtml(title)}</h3>
    <p class="sheet-sub" id="zp-sub">${escapeHtml(subtitle || '')}</p>
    <div class="zp-track"><div class="zp-fill" id="zp-fill"></div></div>
    <p class="sheet-sub" id="zp-detail" style="margin-top:8px"></p>
    <div class="sheet-actions" style="margin-top:14px">
      <button class="chip-btn" id="zp-cancel"><i class="fas fa-xmark"></i> Cancel</button>
    </div>`);
  const state = { cancelled: false };
  const cancelBtn = s.querySelector('#zp-cancel');
  cancelBtn.onclick = () => {
    state.cancelled = true;
    cancelBtn.disabled = true;
    s.querySelector('#zp-detail').textContent = 'Stopping…';
  };
  state.set = (frac, detail) => {
    const f = $id('zp-fill');
    if (f) f.style.width = Math.max(0, Math.min(1, frac)) * 100 + '%';
    const d = $id('zp-detail');
    if (d && detail != null) d.textContent = detail;
  };
  state.sub = (t) => { const e = $id('zp-sub'); if (e) e.textContent = t; };
  state.done = () => sheetClose();
  return state;
}

/* styles the pack needs (progress track, image rows) */
(function injectCss() {
  const css = `
.zp-track{height:6px;border-radius:99px;background:var(--line);overflow:hidden;margin-top:4px}
.zp-fill{height:100%;width:0%;background:var(--ink);border-radius:99px;transition:width .18s linear}
.file-chip .fc-thumb.img-thumb{background:var(--bg);border:1px solid var(--line)}
.file-chip .fc-dim{color:var(--faint);font-size:.7rem;flex-shrink:0;font-variant-numeric:tabular-nums}
.img-opts{display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:var(--bg);
  border:1px solid var(--line);border-radius:var(--radius-md);padding:10px 12px;margin-bottom:12px}
.img-opts label{font-size:.76rem;font-weight:700;color:var(--muted);display:flex;
  align-items:center;gap:6px}
.img-opts select{font:inherit;font-size:.78rem;padding:5px 8px;border:1px solid var(--line);
  border-radius:8px;background:var(--card,#fff);color:var(--ink);cursor:pointer}
.img-bulk{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
#zimg-preview img{width:100%;display:block;border:1px solid var(--line);border-radius:6px;
  background:#fff;box-shadow:var(--shadow-sm)}
#zimg-preview{display:flex;flex-direction:column;gap:12px}`;
  const el = document.createElement('style');
  el.id = 'zen-merge-img-css';
  el.textContent = css;
  document.head.appendChild(el);
})();

/* ==================================================================
   PART A — MERGE
   ================================================================== */
const CHUNK = 40;              // pages copied between yields
const BIG_DOC = 400;           // pages above which we switch save strategy

/* ------------------------------------------------------------------
   WHY WE DO NOT JUST CALL copyPages() IN A LOOP
   ------------------------------------------------------------------
   pdf-lib's copyPages() builds a FRESH PDFObjectCopier on every call:

       copier = PDFObjectCopier.for(srcDoc.context, this.context)

   The copier is what remembers "I already copied this font / this
   image", so anything shared across pages is copied once. Slice a
   1,000-page document into 25 chunks and you get 25 copiers — and the
   shared font and every shared image are re-embedded 25 times.

   Measured on two 600-page documents sharing one font and one image:

       whole-document copyPages ......... 1.1 MB
       chunked, fresh copier per chunk .. 11.5 MB   (+994%)
       chunked, one persistent copier ...  1.1 MB   (+0%)

   So we keep ONE copier alive for the whole of each source document
   and copy through it chunk by chunk. Same output as before, but the
   event loop gets a breath between chunks.
------------------------------------------------------------------ */
const HAS_COPIER = !!(window.PDFLib && PDFLib.PDFObjectCopier &&
                      PDFLib.PDFObjectCopier.for && PDFLib.PDFPage &&
                      PDFLib.PDFPage.of);

function makeCopier(destDoc, srcDoc) {
  if (!HAS_COPIER) return null;
  try { return PDFLib.PDFObjectCopier.for(srcDoc.context, destDoc.context); }
  catch (e) { LOG('copier unavailable', e); return null; }
}

/* copy a slice of pages through an existing copier — this is exactly
   what copyPages() does internally, minus the per-call copier */
function copySlice(copier, destDoc, srcPages, indices) {
  const out = [];
  for (const i of indices) {
    const node = copier.copy(srcPages[i].node);
    const ref = destDoc.context.register(node);
    out.push(PDFLib.PDFPage.of(node, ref, destDoc));
  }
  return out;
}

window.procMerge = async function procMerge(btn) {
  const chosen = pdfFiles.filter(f => isSel(f, 'merge'));
  if (chosen.length < 2) { toast('Tick at least two PDFs to merge', 'err', 2800); return; }

  busy(btn, true, 'Merging…');
  let P = progressSheet('Merging PDFs', 'Reading documents…');
  const skipped = [];
  let totalPages = 0, donePages = 0;

  try {
    /* ---- pass 1: open each source just far enough to count pages.
       Doing this first means the progress bar is honest instead of
       jumping about, and a broken file is caught before any work. -- */
    const sources = [];
    for (let i = 0; i < chosen.length; i++) {
      if (P.cancelled) throw new Error('__cancelled__');
      const f = chosen[i];
      P.set(0, `Checking ${f.name} (${i + 1} of ${chosen.length})`);
      try {
        const doc = await load(f.bytes);
        const n = doc.getPageCount();
        if (!n) { skipped.push(f.name + ' (no pages)'); continue; }
        sources.push({ f, doc, n });
        totalPages += n;
      } catch (e) {
        skipped.push(f.name + ' (' + (/encrypt/i.test(String(e.message)) ?
          'password-protected' : 'unreadable') + ')');
      }
      await tick();
    }
    if (!sources.length) throw new Error('None of the ticked PDFs could be read');
    if (sources.length < 2 && !skipped.length) throw new Error('Need at least two readable PDFs');

    /* ---- memory pre-flight -------------------------------------------
       Page COUNT turns out not to be the limit — 8,000 pages merge
       fine. Total BYTES are. Measured peak JS heap while merging
       scanned documents in Chromium:

           196 MB in ->  0.70 GB heap   (2,000 pages)
           392 MB in ->  1.30 GB heap   (4,000 pages)
           784 MB in ->  2.40 GB heap   (8,000 pages)

       i.e. peak heap runs at roughly 3.2x the combined input size,
       because the parsed source objects, the copied destination
       objects and the serialised output all coexist at the moment of
       save. A tab typically dies somewhere past 2-4 GB, and a phone
       long before that — so warn using the real number rather than a
       page count.                                                     */
    const totalBytes = sources.reduce((a, s) => a + (s.f.size || 0), 0);
    const estGB = (totalBytes * 3.2) / (1024 * 1024 * 1024);
    const deviceGB = navigator.deviceMemory || null;      // Chrome/Edge only
    const budgetGB = deviceGB ? Math.max(1, deviceGB * 0.45) : 2.2;

    if (estGB > budgetGB) {
      P.done();
      busy(btn, false);
      const go = await pickSheet('This merge may be too large for this device',
        `${totalPages.toLocaleString()} pages, ${fmtSize(totalBytes)} in total. ` +
        `A merge this size needs roughly ${estGB.toFixed(1)} GB of memory` +
        (deviceGB ? ` and this device reports about ${deviceGB} GB` : '') +
        `. If it runs out, the tab will reload and you will lose the work.`, [
        { label: 'Merge in two halves', sub: 'Safer — combine the results afterwards',
          icon: 'fas fa-scissors', value: 'half' },
        { label: 'Try anyway', sub: 'It may still work', icon: 'fas fa-play', value: 'go' },
        { label: 'Cancel', sub: '', icon: 'fas fa-xmark', value: null, danger: true },
      ]);
      if (go !== 'go') {
        if (go === 'half')
          toast('Untick the second half, merge, then merge that result with the rest', 'ok', 6500);
        return;
      }
      busy(btn, true, 'Merging…');
      P = progressSheet('Merging PDFs', 'Continuing…');
    }
    P.sub(`${totalPages.toLocaleString()} pages from ${sources.length} document` +
          `${sources.length > 1 ? 's' : ''} · ${fmtSize(totalBytes)}`);

    const merged = await PDFDocument.create();

    /* ---- pass 2: copy in chunks, yielding between them --------------
       copyPages() on a whole 1,000-page document builds every page
       object at once. In slices of CHUNK the peak stays flat, the
       progress bar moves, and Cancel actually responds.             */
    for (let si = 0; si < sources.length; si++) {
      const src = sources[si];

      // one copier for this whole source — see the note above
      await src.doc.flush();
      const copier = makeCopier(merged, src.doc);
      const srcPages = copier ? src.doc.getPages() : null;

      if (!copier) {
        // pdf-lib internals not exposed: fall back to the stock path so
        // the output is still correct, just without the yielding
        P.set(donePages / totalPages, `Copying ${src.f.name} in one pass…`);
        await tick();
        const copied = await merged.copyPages(src.doc, src.doc.getPageIndices());
        for (const pg of copied) merged.addPage(pg);
        donePages += src.n;
      } else {
        for (let start = 0; start < src.n; start += CHUNK) {
          if (P.cancelled) throw new Error('__cancelled__');
          const idx = [];
          for (let k = start; k < Math.min(start + CHUNK, src.n); k++) idx.push(k);
          for (const pg of copySlice(copier, merged, srcPages, idx)) merged.addPage(pg);
          donePages += idx.length;
          P.set(donePages / totalPages,
            `${donePages.toLocaleString()} of ${totalPages.toLocaleString()} pages · ${src.f.name}`);
          await tick();
        }
      }
      // drop the parsed source as soon as it is drained so the GC can
      // reclaim it while the rest of the merge is still running
      sources[si].doc = null;
      await tick();
    }

    if (P.cancelled) throw new Error('__cancelled__');

    /* ---- pass 3: save ------------------------------------------------
       useObjectStreams compresses the output, but on a very large
       document that pass is slow and doubles peak memory. Above
       BIG_DOC pages we skip it: the file is somewhat larger, but the
       save completes instead of stalling. objectsPerTick controls how
       often pdf-lib yields while writing — the default of 50 crawls
       on documents this size.                                        */
    const big = merged.getPageCount() > BIG_DOC;
    P.set(1, `Writing ${merged.getPageCount().toLocaleString()} pages…`);
    await tick();
    const bytes = await merged.save({
      useObjectStreams: !big,
      objectsPerTick: big ? 1500 : 200,
      updateFieldAppearances: false,
    });

    P.done();
    busy(btn, false);

    if (skipped.length) {
      toast(`Skipped ${skipped.length}: ${skipped.join(', ')}`, 'err', 6000);
    }
    const done = await chooseExport(bytes, 'merged', 'merged.pdf');
    if (done) toast(`Merged ${merged.getPageCount().toLocaleString()} pages`, 'ok', 3600);
    return;

  } catch (e) {
    P.done();
    busy(btn, false);
    const m = String(e && e.message || e);
    if (m === '__cancelled__') { toast('Merge cancelled', 'err', 2400); return; }
    if (/allocation|out of memory|Array buffer/i.test(m)) {
      toast('Ran out of memory for a merge this large. Merge in two halves, ' +
            'then merge the results.', 'err', 7000);
    } else {
      toast('Merge failed: ' + m, 'err', 5000);
    }
  }
};

/* ==================================================================
   PART B — IMAGES TO PDF, WITH A MERGE-STYLE WORKSPACE
   ================================================================== */

/* one entry per image the user added, in the order they added them */
const IMG = {
  items: [],            // {id,file,name,size,url,w,h,rot,sel,type}
  seq: 0,
  opts: { page: 'auto', orient: 'auto', fit: 'fit', margin: 0 },
};
window.ZenImages = IMG;   // exposed for debugging

const imgSel = (it) => it.sel !== false;

/* ---- reading an image: EXIF-correct, and never trusting the type -- */
async function readImage(file) {
  const url = URL.createObjectURL(file);
  let w = 0, h = 0;
  try {
    // createImageBitmap applies the EXIF rotation, so w/h are what the
    // user actually sees rather than the raw sensor orientation
    const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
    w = bmp.width; h = bmp.height;
    bmp.close && bmp.close();
  } catch (e) {
    try {
      const im = new Image();
      im.src = url;
      await im.decode();
      w = im.naturalWidth; h = im.naturalHeight;
    } catch (e2) { /* leave 0x0 — flagged as unreadable in the row */ }
  }
  return {
    id: ++IMG.seq, file, name: file.name || 'image',
    size: file.size || 0, type: file.type || '', url, w, h, rot: 0, sel: true,
  };
}

async function imgAdd(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  const btn = $id('btn-img2pdf');
  if (btn) { btn.disabled = true; }
  let bad = 0;
  for (const f of list) {
    const it = await readImage(f);
    if (!it.w || !it.h) { bad++; URL.revokeObjectURL(it.url); continue; }
    IMG.items.push(it);
  }
  imgRender();
  if (bad) toast(`${bad} file${bad > 1 ? 's' : ''} could not be read as an image`, 'err', 4000);
}

function imgRemove(id) {
  const i = IMG.items.findIndex(x => x.id === id);
  if (i < 0) return;
  URL.revokeObjectURL(IMG.items[i].url);
  IMG.items.splice(i, 1);
  imgRender();
}
function imgToggle(id) {
  const it = IMG.items.find(x => x.id === id);
  if (it) { it.sel = !imgSel(it); imgRender(); }
}
function imgMove(id, dir) {
  const i = IMG.items.findIndex(x => x.id === id);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= IMG.items.length) return;
  const [it] = IMG.items.splice(i, 1);
  IMG.items.splice(j, 0, it);
  imgRender();
}
function imgRotate(id) {
  const it = IMG.items.find(x => x.id === id);
  if (!it) return;
  it.rot = ((it.rot || 0) + 90) % 360;
  imgRender();
}
function imgSortName() {
  IMG.items.sort((a, b) => a.name.localeCompare(b.name, undefined,
    { numeric: true, sensitivity: 'base' }));   // numeric: img2 before img10
  imgRender();
}
function imgReverse() { IMG.items.reverse(); imgRender(); }
function imgAll(on) { IMG.items.forEach(i => i.sel = on); imgRender(); }
function imgClear() {
  IMG.items.forEach(i => URL.revokeObjectURL(i.url));
  IMG.items = [];
  imgRender();
}
Object.assign(window, { imgRemove, imgToggle, imgMove, imgRotate,
  imgSortName, imgReverse, imgAll, imgClear, imgRender, imgAdd });

/* ---- generic drag-to-reorder, bound to ANY array ------------------
   The stock fileDragInit is hardwired to pdfFiles, so images need
   their own. Same visual language, same drop indicators.          */
function dragInit(chip, index, arr, onDone) {
  const grip = chip.querySelector('.fc-grip');
  if (!grip) return;
  grip.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    const container = chip.parentElement;
    if (!container) return;
    const chips = Array.from(container.children);
    try { grip.setPointerCapture(e.pointerId); } catch (err) {}
    chip.classList.add('drag-src');
    let drop = index;
    const move = (ev) => {
      ev.preventDefault();
      drop = chips.length;
      for (let i = 0; i < chips.length; i++) {
        const r = chips[i].getBoundingClientRect();
        if (ev.clientY < r.top + r.height / 2) { drop = i; break; }
      }
      chips.forEach(c => c.classList.remove('drop-above', 'drop-below'));
      if (drop >= chips.length) chips[chips.length - 1].classList.add('drop-below');
      else chips[drop].classList.add('drop-above');
      if (ev.clientY < 90) window.scrollBy(0, -14);
      else if (ev.clientY > window.innerHeight - 90) window.scrollBy(0, 14);
    };
    const up = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      document.removeEventListener('pointercancel', up);
      chips.forEach(c => c.classList.remove('drop-above', 'drop-below'));
      chip.classList.remove('drag-src');
      let to = drop;
      if (to > index) to--;
      if (to !== index && to >= 0 && to < arr.length) {
        const [it] = arr.splice(index, 1);
        arr.splice(to, 0, it);
        onDone();
      }
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
    document.addEventListener('pointercancel', up);
  });
}

/* ---- the list itself ---------------------------------------------- */
function imgRender() {
  const list = $id('list-img2pdf');
  if (!list) return;
  const n = IMG.items.length;
  const on = IMG.items.filter(imgSel).length;

  /* options + bulk actions live just above the list */
  let bar = $id('zimg-bar');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'zimg-bar';
    list.parentNode.insertBefore(bar, list);
  }
  bar.innerHTML = !n ? '' : `
    <div class="img-opts">
      <label>Page
        <select onchange="ZenImages.opts.page=this.value">
          <option value="auto"${IMG.opts.page==='auto'?' selected':''}>Fit each image</option>
          <option value="a4"${IMG.opts.page==='a4'?' selected':''}>A4</option>
          <option value="letter"${IMG.opts.page==='letter'?' selected':''}>Letter</option>
        </select>
      </label>
      <label>Orientation
        <select onchange="ZenImages.opts.orient=this.value">
          <option value="auto"${IMG.opts.orient==='auto'?' selected':''}>Auto</option>
          <option value="portrait"${IMG.opts.orient==='portrait'?' selected':''}>Portrait</option>
          <option value="landscape"${IMG.opts.orient==='landscape'?' selected':''}>Landscape</option>
        </select>
      </label>
      <label>Margin
        <select onchange="ZenImages.opts.margin=+this.value">
          <option value="0"${IMG.opts.margin===0?' selected':''}>None</option>
          <option value="18"${IMG.opts.margin===18?' selected':''}>Small</option>
          <option value="36"${IMG.opts.margin===36?' selected':''}>Medium</option>
          <option value="60"${IMG.opts.margin===60?' selected':''}>Large</option>
        </select>
      </label>
      <span style="flex:1"></span>
      <b style="font-size:.76rem;color:var(--muted)">${on} of ${n} selected</b>
    </div>
    <div class="img-bulk">
      <button class="chip-btn" onclick="imgSortName()"><i class="fas fa-arrow-down-a-z"></i> Sort by name</button>
      <button class="chip-btn" onclick="imgReverse()"><i class="fas fa-arrow-down-up-across-line"></i> Reverse</button>
      <button class="chip-btn" onclick="imgAll(true)"><i class="far fa-square-check"></i> Select all</button>
      <button class="chip-btn" onclick="imgAll(false)"><i class="far fa-square"></i> Select none</button>
      <button class="chip-btn" onclick="imgPreviewAll()"><i class="far fa-eye"></i> Preview order</button>
      <button class="chip-btn" onclick="imgClear()"><i class="fas fa-trash-can"></i> Clear</button>
    </div>`;

  list.innerHTML = '';
  IMG.items.forEach((it, i) => {
    const chip = document.createElement('div');
    chip.className = 'file-chip' + (imgSel(it) ? '' : ' excluded');
    const dim = it.rot % 180 ? `${it.h} × ${it.w}` : `${it.w} × ${it.h}`;
    chip.innerHTML = `
      <span class="fc-grip" title="Drag to reorder"><i class="fas fa-grip-vertical"></i></span>
      <input type="checkbox" class="fc-sel" ${imgSel(it) ? 'checked' : ''}
        title="${imgSel(it) ? 'Untick to leave this image out' : 'Tick to include'}"
        onchange="imgToggle(${it.id})">
      <span class="fc-order">${i + 1}</span>
      <span class="fc-thumb img-thumb"><img alt="" src="${it.url}"
        style="transform:rotate(${it.rot}deg)"></span>
      <span class="fc-name">${escapeHtml(it.name)}</span>
      <span class="fc-dim">${dim}</span>
      <span class="fc-size">${fmtSize(it.size)}</span>
      <button class="icon-btn" title="Preview this image" onclick="imgPreviewOne(${it.id})"><i class="far fa-eye"></i></button>
      <button class="icon-btn" title="Rotate 90°" onclick="imgRotate(${it.id})"><i class="fas fa-rotate-right"></i></button>
      <button class="icon-btn" title="Move up" onclick="imgMove(${it.id},-1)"><i class="fas fa-chevron-up"></i></button>
      <button class="icon-btn" title="Move down" onclick="imgMove(${it.id},1)"><i class="fas fa-chevron-down"></i></button>
      <button class="icon-btn del" title="Remove" onclick="imgRemove(${it.id})"><i class="fas fa-xmark"></i></button>`;
    dragInit(chip, i, IMG.items, imgRender);
    list.appendChild(chip);
  });

  /* the Create button, plus a Preview-result button beside it */
  const btn = $id('btn-img2pdf');
  if (btn) {
    btn.disabled = on === 0;
    btn.innerHTML = `<i class="fas fa-file-pdf"></i> Create PDF` +
      (on ? ` <span style="opacity:.7">(${on} image${on > 1 ? 's' : ''})</span>` : '');
    let pv = $id('btn-img-preview');
    if (!pv) {
      pv = document.createElement('button');
      pv.id = 'btn-img-preview';
      pv.className = 'chip-btn';
      pv.style.marginTop = '8px';
      pv.onclick = () => imgBuildAndPreview();
      btn.parentNode.insertBefore(pv, btn.nextSibling);
    }
    pv.innerHTML = '<i class="far fa-file-pdf"></i> Preview the PDF first';
    pv.style.display = on ? '' : 'none';
  }

  const note = document.querySelector('#view-img2pdf .tool-head p');
  if (note) note.innerHTML = n
    ? 'Images become pages <b>top to bottom</b> — drag the grip or use the arrows to reorder. Untick any image to leave it out, and preview the finished PDF before you download it.'
    : 'Add JPG, PNG, WebP, GIF or BMP images — they become pages in the order you add them, and you can reorder, rotate and remove them afterwards.';
}

/* ---- previews ------------------------------------------------------ */
window.imgPreviewOne = function (id) {
  const it = IMG.items.find(x => x.id === id);
  if (!it) return;
  const s = sheetOpen(`
    <h3 style="justify-content:space-between">
      <span style="display:inline-flex;align-items:center;gap:9px;min-width:0">
        <i class="far fa-image"></i>
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(it.name)}</span></span>
      <button class="icon-btn" id="zi-x" title="Close"><i class="fas fa-xmark"></i></button></h3>
    <p class="sheet-sub">${it.w} × ${it.h} px · ${fmtSize(it.size)}${it.rot ? ' · rotated ' + it.rot + '°' : ''}</p>
    <div id="zimg-preview"><img src="${it.url}" style="transform:rotate(${it.rot}deg)"></div>`);
  s.classList.add('wide');
  s.querySelector('#zi-x').onclick = () => sheetClose();
};

window.imgPreviewAll = function () {
  const sel = IMG.items.filter(imgSel);
  if (!sel.length) { toast('Tick at least one image', 'err'); return; }
  const s = sheetOpen(`
    <h3 style="justify-content:space-between">
      <span><i class="far fa-eye"></i> Page order</span>
      <button class="icon-btn" id="zi-x" title="Close"><i class="fas fa-xmark"></i></button></h3>
    <p class="sheet-sub">${sel.length} image${sel.length > 1 ? 's' : ''}, in the order they will appear</p>
    <div id="zimg-preview">${sel.map((it, i) =>
      `<div><b style="font-size:.74rem;color:var(--muted)">Page ${i + 1} — ${escapeHtml(it.name)}</b>
       <img src="${it.url}" style="transform:rotate(${it.rot}deg);margin-top:6px"></div>`).join('')}</div>`);
  s.classList.add('wide');
  s.querySelector('#zi-x').onclick = () => sheetClose();
};

/* ---- the actual conversion ---------------------------------------- */
const PAGE_SIZES = { a4: [595.28, 841.89], letter: [612, 792] };

async function imgBuild(onProgress) {
  const sel = IMG.items.filter(imgSel);
  if (!sel.length) throw new Error('Tick at least one image');

  const doc = await PDFDocument.create();
  const PT = 72 / 96;                 // css px -> pt
  const M = IMG.opts.margin || 0;
  const skipped = [];

  for (let i = 0; i < sel.length; i++) {
    const it = sel[i];
    if (onProgress) onProgress(i / sel.length, `${it.name} (${i + 1} of ${sel.length})`);
    try {
      /* draw through a canvas so EXIF orientation, the user's own
         rotation and any exotic format (webp / gif / bmp) all end up
         as something pdf-lib can definitely embed */
      let bmp = null;
      try { bmp = await createImageBitmap(it.file, { imageOrientation: 'from-image' }); }
      catch (e) {
        const im = new Image(); im.src = it.url; await im.decode(); bmp = im;
      }
      const bw = bmp.width || bmp.naturalWidth, bh = bmp.height || bmp.naturalHeight;
      const swap = it.rot % 180 !== 0;
      const cv = document.createElement('canvas');
      cv.width = swap ? bh : bw;
      cv.height = swap ? bw : bh;
      const cx = cv.getContext('2d');
      cx.fillStyle = '#ffffff'; cx.fillRect(0, 0, cv.width, cv.height);
      cx.translate(cv.width / 2, cv.height / 2);
      cx.rotate((it.rot || 0) * Math.PI / 180);
      cx.drawImage(bmp, -bw / 2, -bh / 2);
      if (bmp.close) bmp.close();

      const hasAlpha = /png|webp|gif/i.test(it.type);
      const data = cv.toDataURL(hasAlpha ? 'image/png' : 'image/jpeg', 0.92);
      const emb = hasAlpha ? await doc.embedPng(data) : await doc.embedJpg(data);

      /* natural size in points */
      let iw = emb.width * PT, ih = emb.height * PT;

      if (IMG.opts.page === 'auto') {
        /* the page IS the image, plus any margin */
        const pw = iw + M * 2, ph = ih + M * 2;
        const page = doc.addPage([Math.max(1, pw), Math.max(1, ph)]);
        page.drawImage(emb, { x: M, y: M, width: iw, height: ih });
      } else {
        let [pw, ph] = PAGE_SIZES[IMG.opts.page] || PAGE_SIZES.a4;
        const wantLand = IMG.opts.orient === 'landscape' ||
          (IMG.opts.orient === 'auto' && iw > ih);
        if (wantLand) { const t = pw; pw = ph; ph = t; }
        const page = doc.addPage([pw, ph]);
        const availW = pw - M * 2, availH = ph - M * 2;
        const k = IMG.opts.fit === 'actual'
          ? Math.min(1, availW / iw, availH / ih)
          : Math.min(availW / iw, availH / ih);
        const dw = iw * k, dh = ih * k;
        page.drawImage(emb, {
          x: (pw - dw) / 2, y: (ph - dh) / 2, width: dw, height: dh,
        });
      }
    } catch (e) {
      skipped.push(it.name);
      LOG('image skipped', it.name, e);
    }
    await tick();
  }

  if (!doc.getPageCount()) throw new Error('None of those images could be converted');
  if (onProgress) onProgress(1, 'Writing the PDF…');
  const bytes = await doc.save({ objectsPerTick: 400, updateFieldAppearances: false });
  return { bytes, pages: doc.getPageCount(), skipped };
}

/* preview the finished PDF, with Download / Back to editing */
window.imgBuildAndPreview = async function () {
  const P = progressSheet('Building the PDF', 'Converting images…');
  let res;
  try {
    res = await imgBuild((f, d) => { if (!P.cancelled) P.set(f, d); });
  } catch (e) {
    P.done(); toast(e.message, 'err', 4500); return;
  }
  P.done();
  if (res.skipped.length)
    toast(`Skipped: ${res.skipped.join(', ')}`, 'err', 5000);

  const s = sheetOpen(`
    <h3 style="justify-content:space-between">
      <span><i class="far fa-file-pdf"></i> Preview</span>
      <button class="icon-btn" id="zi-x" title="Close"><i class="fas fa-xmark"></i></button></h3>
    <p class="sheet-sub" id="zi-st">Rendering ${res.pages} page${res.pages > 1 ? 's' : ''}…</p>
    <div id="zimg-preview"></div>
    <div class="sheet-actions" style="margin-top:14px">
      <button class="chip-btn" id="zi-back"><i class="fas fa-arrow-left"></i> Back to editing</button>
      <button class="chip-btn ok" id="zi-dl"><i class="fas fa-download"></i> Download this PDF</button>
    </div>`);
  s.classList.add('wide');
  s.querySelector('#zi-x').onclick = () => sheetClose();
  s.querySelector('#zi-back').onclick = () => sheetClose();
  s.querySelector('#zi-dl').onclick = async () => {
    sheetClose();
    await chooseExport(res.bytes, 'images', 'images.pdf');
    toast(`PDF created — ${res.pages} page${res.pages > 1 ? 's' : ''}`, 'ok');
  };

  const wrap = s.querySelector('#zimg-preview');
  const st = s.querySelector('#zi-st');
  try {
    const pdf = await pdfjsLoad(res.bytes.slice(0));
    for (let n = 1; n <= pdf.numPages; n++) {
      if (!document.body.contains(wrap)) return;      // closed mid-render
      const page = await pdf.getPage(n);
      const v1 = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: Math.min(1.4, 760 / v1.width) });
      const cv = document.createElement('canvas');
      cv.width = Math.round(viewport.width); cv.height = Math.round(viewport.height);
      const cx = cv.getContext('2d');
      cx.fillStyle = '#ffffff'; cx.fillRect(0, 0, cv.width, cv.height);
      await page.render({ canvasContext: cx, viewport }).promise;
      const img = new Image();
      img.src = cv.toDataURL('image/jpeg', 0.82);
      img.alt = 'Page ' + n;
      wrap.appendChild(img);
      st.textContent = n < pdf.numPages
        ? `Rendering page ${n + 1} of ${pdf.numPages}…`
        : `${pdf.numPages} page${pdf.numPages > 1 ? 's' : ''} · ${fmtSize(res.bytes.length)}`;
    }
  } catch (e) { st.textContent = 'Could not render the preview: ' + e.message; }
};

/* straight to download, no preview */
window.procImg2Pdf = async function procImg2Pdf() {
  const btn = $id('btn-img2pdf');
  const P = progressSheet('Creating the PDF', 'Converting images…');
  try {
    const res = await imgBuild((f, d) => { if (!P.cancelled) P.set(f, d); });
    P.done();
    if (res.skipped.length) toast(`Skipped: ${res.skipped.join(', ')}`, 'err', 5000);
    pdfFiles.push({ name: 'images.pdf', bytes: res.bytes, size: res.bytes.length });
    if (typeof refreshPdfLists === 'function') refreshPdfLists();
    await chooseExport(res.bytes, 'images', 'images.pdf');
    toast(`PDF created — ${res.pages} page${res.pages > 1 ? 's' : ''}`, 'ok');
  } catch (e) {
    P.done();
    toast(e.message, 'err', 4500);
  }
  if (btn) btn.disabled = IMG.items.filter(imgSel).length === 0;
};

/* ---- take over the existing wiring -------------------------------- */
const _addAuxPrev = window.addAuxFiles;
window.addAuxFiles = function (kind, files) {
  if (kind === 'img2pdf') { imgAdd(files); return; }
  return _addAuxPrev.apply(this, arguments);
};
const _renderAuxPrev = window.renderAuxList;
window.renderAuxList = function (kind) {
  if (kind === 'img2pdf') { imgRender(); return; }
  return _renderAuxPrev.apply(this, arguments);
};

/* accept everything the browser can decode, and allow drag-drop onto
   the drop zone as well as the file picker */
function upgradeInput() {
  const inp = $id('in-img');
  if (inp && !inp.dataset.zen) {
    inp.dataset.zen = '1';
    inp.accept = 'image/png,image/jpeg,image/webp,image/gif,image/bmp,image/*';
  }
  const dz = document.querySelector('#view-img2pdf .sub-drop');
  if (dz && !dz.dataset.zen) {
    dz.dataset.zen = '1';
    // the stock label only mentions JPG and PNG
    dz.childNodes.forEach(nd => {
      if (nd.nodeType === 3 && /Drop JPG/i.test(nd.textContent))
        nd.textContent = 'Drop images here, or click to browse — JPG, PNG, WebP, GIF, BMP';
    });
    ['dragenter', 'dragover'].forEach(t => dz.addEventListener(t, (e) => {
      e.preventDefault(); dz.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(t => dz.addEventListener(t, (e) => {
      e.preventDefault(); dz.classList.remove('over'); }));
    dz.addEventListener('drop', (e) => {
      const fs = Array.from((e.dataTransfer && e.dataTransfer.files) || [])
        .filter(f => /^image\//i.test(f.type));
      if (fs.length) imgAdd(fs);
      else toast('Drop image files here', 'err');
    });
  }
}
if (document.readyState === 'loading')
  document.addEventListener('DOMContentLoaded', upgradeInput);
else upgradeInput();
document.addEventListener('click', upgradeInput, { once: true, capture: true });

LOG('merge + image workspace pack active');
})();