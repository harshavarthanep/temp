#!/usr/bin/env python3
# =============================================================================
#  ZenPDF — production fix patch  v2.1.0
# =============================================================================
#  Fixes the "signature / stamp lands in the wrong place and points the wrong
#  way" bug on rotated and cropped PDFs, plus a batch of related export,
#  typography and layout defects.
#
#  USAGE (GitHub Codespaces / any shell):
#      python3 zenpdf_geometry_fix.py                 # patches ./index.html
#      python3 zenpdf_geometry_fix.py path/to/index.html
#      python3 zenpdf_geometry_fix.py --check         # dry run, changes nothing
#      python3 zenpdf_geometry_fix.py --restore       # roll back from .bak
#      python3 zenpdf_geometry_fix.py --no-ui         # skip the CSS/UX layer
#      python3 zenpdf_geometry_fix.py --no-hidpi-ink  # skip the ink-sharpness fix
#
#  SAFETY
#    * Idempotent - running it twice is a no-op.
#    * All-or-nothing - every anchor is located before anything is written.
#      A missing anchor aborts with a clear message and leaves the file alone.
#    * Writes <file>.bak once, before the first successful patch.
#
#  No third-party Python packages required. Python 3.8+.
# =============================================================================

import argparse
import datetime
import hashlib
import os
import re
import shutil
import sys
from html.parser import HTMLParser

VERSION = "2.1.0"
MARKER = "/* ZP-FIX v2 GEOMETRY CORE */"
UI_MARKER = "/* ZP-FIX v2 UI LAYER */"
INK_MARKER = "/* ZP-FIX v2 HIDPI INK */"


# =============================================================================
#  1. THE GEOMETRY CORE  — injected right after the pdf-lib <script> tag
# =============================================================================
#
#  THE BUG
#  -------
#  pdf.js `page.getViewport()` returns the page **as displayed**: it applies
#  /Rotate and it uses the CropBox.  pdf-lib `page.getSize()` returns the raw,
#  unrotated **MediaBox**.  The editor measured objects against the first and
#  stamped them with the second:
#
#      const { width: pw, height: ph } = page.getSize();   // 792 x 612
#      const sx = pw / p.w, sy = ph / p.h;                 // p.w/p.h are 612 x 792
#
#  On the Molina contract (/Rotate 270 on all 15 pages) that produces
#  sx = 1.29, sy = 0.77 and no rotation at all, so a 100x40 pt signature placed
#  at (60,120) came out as a 31x130 pt smear at (93,585) - rotated a quarter
#  turn and pushed down the page.  Measured, not guessed: see CHANGELOG.
#
#  THE FIX
#  -------
#  Draw in "display space" - the coordinate system the user actually sees -
#  and let the PDF do the rotation.  We push one transformation matrix onto
#  the page before drawing and pop it afterwards.  Every existing draw call
#  then works unchanged, for every rotation, and sx === sy so nothing is ever
#  squashed.  Derivation of the four matrices is in the CHANGELOG.
#
GEOMETRY_CORE = r"""
<script>
/* ZP-FIX v2 GEOMETRY CORE */
/* =========================================================================
   ZenPDF geometry core - rotation & CropBox aware stamping.

   Everything the editor draws is measured in "editor css px" against the
   pdf.js viewport, which already applies /Rotate and the CropBox.  pdf-lib
   draws in raw, unrotated MediaBox user space.  These helpers bridge the two
   by pushing a single transformation matrix, so page.drawText/drawImage/...
   can keep using simple top-left-ish coordinates on ANY page.

   zpBeginPage(page, pageState) -> { Dw, Dh, k, rot }
       Dw,Dh : the page size AS DISPLAYED, in points
       k     : editor css px -> points  (uniform: one factor for both axes)
   ...draw...
   zpEndPage(page)
   ========================================================================= */
(function () {
  'use strict';
  if (window.zpPageGeom) return;                 // already installed
  var L = function () { return window.PDFLib; };

  /* /Rotate may legally be any multiple of 90, and may be negative or
     absurd (720, -90, 45 from a broken generator). Normalise hard. */
  function zpNormRot(a) {
    a = Math.round((Number(a) || 0) / 90) * 90;
    return ((a % 360) + 360) % 360;
  }

  /* The visible box is CropBox INTERSECTED with MediaBox (PDF 32000-1 14.11.2).
     A CropBox that is missing, degenerate or entirely outside the MediaBox is
     ignored - that is what every real viewer does. */
  function zpPageGeom(page) {
    var mb = page.getMediaBox();
    var cb = null;
    try { cb = page.getCropBox(); } catch (e) { cb = null; }

    var x1 = mb.x, y1 = mb.y, x2 = mb.x + mb.width, y2 = mb.y + mb.height;
    if (cb && isFinite(cb.x) && isFinite(cb.y) &&
        isFinite(cb.width) && isFinite(cb.height) &&
        cb.width > 0 && cb.height > 0) {
      x1 = Math.max(x1, cb.x);
      y1 = Math.max(y1, cb.y);
      x2 = Math.min(x2, cb.x + cb.width);
      y2 = Math.min(y2, cb.y + cb.height);
    }
    if (!(x2 - x1 > 1) || !(y2 - y1 > 1)) {      // degenerate -> fall back
      x1 = mb.x; y1 = mb.y; x2 = mb.x + mb.width; y2 = mb.y + mb.height;
    }

    var bw = x2 - x1, bh = y2 - y1;
    var rot = zpNormRot(page.getRotation().angle);
    var swap = (rot === 90 || rot === 270);

    /* Maps DISPLAY space (origin = bottom-left of the page as shown, y up,
       units = pt) onto unrotated PDF user space. Verified against pdfium for
       all four rotations x {CropBox, no CropBox} x {offset MediaBox, origin}. */
    var ctm = rot === 0   ? [ 1,  0,  0,  1, x1, y1]
            : rot === 90  ? [ 0,  1, -1,  0, x2, y1]
            : rot === 180 ? [-1,  0,  0, -1, x2, y2]
            : /* 270 */     [ 0, -1,  1,  0, x1, y2];

    return {
      Dw: swap ? bh : bw,
      Dh: swap ? bw : bh,
      rot: rot, ctm: ctm,
      box: { x1: x1, y1: y1, x2: x2, y2: y2 }
    };
  }

  /* Some PDFs ship an unbalanced content stream (more `q` than `Q`, a stray
     `cm`, an unclosed clip). Anything we append then inherits that leftover
     state and lands somewhere unpredictable. Wrapping the ORIGINAL content in
     its own q/Q pair isolates it, so our overlay always starts from a clean
     identity CTM. Cheap insurance; runs once per page. */
  function zpArmor(page) {
    if (page.__zpArmored) return true;
    page.__zpArmored = true;
    try {
      var lib = L();
      var PDFName = lib.PDFName, PDFArray = lib.PDFArray;
      var ctx = page.node.context;
      var K = PDFName.of('Contents');
      var raw = page.node.get(K);
      if (!raw) return true;                       // empty page, nothing to guard
      var resolved = ctx.lookup(raw);
      var mk = function (s) { return ctx.register(ctx.stream(s)); };
      var arr = ctx.obj([]);
      arr.push(mk('q\n'));
      if (resolved instanceof PDFArray) {
        for (var i = 0; i < resolved.size(); i++) arr.push(resolved.get(i));
      } else {
        arr.push(raw);
      }
      arr.push(mk('\nQ\n'));
      page.node.set(K, arr);
      return true;
    } catch (e) {
      /* Never let hardening break an export - worst case we are exactly as
         correct as before. */
      if (window.console) console.warn('[ZenPDF] content-stream guard skipped:', e && e.message);
      return false;
    }
  }

  function zpBeginPage(page, p) {
    var g = zpPageGeom(page);
    zpArmor(page);
    var lib = L();
    page.pushOperators(
      lib.pushGraphicsState(),
      lib.concatTransformationMatrix.apply(null, g.ctm)
    );
    page.__zpOpen = (page.__zpOpen || 0) + 1;

    /* editor css px -> points. Because rotation is handled by the matrix,
       both axes share ONE factor; averaging guards against a 1px rounding
       difference in the canvas size. */
    var k = 1;
    if (p && p.w > 0 && p.h > 0) k = (g.Dw / p.w + g.Dh / p.h) / 2;
    return { Dw: g.Dw, Dh: g.Dh, k: k, rot: g.rot, ctm: g.ctm };
  }

  function zpEndPage(page) {
    if (!page || !page.__zpOpen) return;
    page.pushOperators(L().popGraphicsState());
    page.__zpOpen--;
  }

  /* ---- text encoding ---------------------------------------------------
     The 14 standard PDF fonts speak WinAnsi. The old winAnsiSafe() flattened
     curly quotes to ', em dashes to "--" and everything non-Latin to "?".
     WinAnsi actually DOES carry the smart-quote/dash/euro block, so keep
     those; anything genuinely outside it gets drawn as a raster instead of
     silently becoming "?". */
  var ZP_WINANSI_EXTRA =
    '€‚ƒ„…†‡ˆ‰Š‹Œ' +
    'Ž‘’“”•–—˜™š›' +
    'œžŸ';

  function zpIsWinAnsi(ch) {
    var c = ch.codePointAt(0);
    return (c >= 0x20 && c <= 0x7E) || (c >= 0xA0 && c <= 0xFF) ||
           c === 0x0A || c === 0x0D || c === 0x09 ||
           ZP_WINANSI_EXTRA.indexOf(ch) >= 0;
  }
  function zpNeedsRaster(s) {
    if (!s) return false;
    for (var i = 0; i < s.length; i++) {
      var ch = s.charAt(i);
      var c = s.codePointAt(i);
      if (c > 0xFFFF) { i++; return true; }        // astral: emoji etc.
      if (!zpIsWinAnsi(ch)) return true;
    }
    return false;
  }

  /* Draw a text object exactly as the browser lays it out, at 4x, and hand
     back a PNG plus its box in editor css px. Used only for text the standard
     fonts cannot encode, so ordinary text stays real, selectable PDF text. */
  function zpRasterText(obj, t, p) {
    try {
      var hr = p.holder.getBoundingClientRect();
      var k2 = hr.width > 0 ? (p.w / hr.width) : 1;   // visual px -> editor css px
      var r = t.getBoundingClientRect();
      var w = r.width * k2, h = r.height * k2;
      if (!(w > 0) || !(h > 0)) return null;

      var S = 4;
      var cv = document.createElement('canvas');
      cv.width = Math.max(1, Math.round(w * S));
      cv.height = Math.max(1, Math.round(h * S));
      var c = cv.getContext('2d');
      c.scale(S, S);

      var cs = getComputedStyle(t);
      var fs = parseFloat(cs.fontSize) || 16;
      var lh = parseFloat(cs.lineHeight) || fs * 1.25;
      c.font = [cs.fontStyle, cs.fontWeight, fs + 'px', cs.fontFamily].join(' ');
      c.fillStyle = cs.color || '#000';
      c.textBaseline = 'alphabetic';

      var padL = parseFloat(cs.paddingLeft) || 0;
      var padR = parseFloat(cs.paddingRight) || 0;
      var padT = parseFloat(cs.paddingTop) || 0;
      var innerW = w - padL - padR;
      var align = cs.textAlign || 'left';
      var underline = (cs.textDecorationLine || cs.textDecoration || '').indexOf('underline') >= 0;

      t.innerText.split('\n').forEach(function (ln, i) {
        var tw = c.measureText(ln).width;
        var x = padL;
        if (align === 'center') x = padL + (innerW - tw) / 2;
        else if (align === 'right') x = padL + innerW - tw;
        var y = padT + lh * i + fs * 0.82;
        c.fillText(ln, x, y);
        if (underline && ln.trim()) c.fillRect(x, y + fs * 0.12, tw, Math.max(0.6, fs / 16));
      });

      return {
        dataUrl: cv.toDataURL('image/png'),
        x: (r.left - hr.left) * k2,
        y: (r.top - hr.top) * k2,
        w: w, h: h
      };
    } catch (e) { return null; }
  }

  /* Measure a DOM object against its page holder. Immune to browser zoom,
     borders, padding and non-proportional resizing - the old code divided
     bounding rects by a holder ratio and then re-derived height from the
     image's natural aspect, which drifted whenever either changed. */
  function zpMeasure(el, p) {
    try {
      var hr = p.holder.getBoundingClientRect();
      var k2 = hr.width > 0 ? (p.w / hr.width) : 1;
      var r = el.getBoundingClientRect();
      var cs = getComputedStyle(el);
      var bl = parseFloat(cs.borderLeftWidth) || 0;
      var bt = parseFloat(cs.borderTopWidth) || 0;
      var br = parseFloat(cs.borderRightWidth) || 0;
      var bb = parseFloat(cs.borderBottomWidth) || 0;
      var w = r.width * k2 - bl - br;
      var h = r.height * k2 - bt - bb;
      if (!(w > 0) || !(h > 0)) return null;
      return { x: (r.left - hr.left) * k2 + bl, y: (r.top - hr.top) * k2 + bt, w: w, h: h };
    } catch (e) { return null; }
  }

  window.zpNormRot = zpNormRot;
  window.zpPageGeom = zpPageGeom;
  window.zpBeginPage = zpBeginPage;
  window.zpEndPage = zpEndPage;
  window.zpArmor = zpArmor;
  window.zpNeedsRaster = zpNeedsRaster;
  window.zpRasterText = zpRasterText;
  window.zpMeasure = zpMeasure;
  window.zpIsWinAnsi = zpIsWinAnsi;
})();
</script>
"""


# =============================================================================
#  2. THE UI / RESPONSIVE LAYER  — injected before </head>
# =============================================================================
#  Purely additive CSS + a tiny viewport shim. It cannot break any JS. Targets
#  the six real form factors: foldable-closed, phone, large phone, tablet,
#  laptop, desktop - plus notch safe areas, coarse-pointer hit targets,
#  keyboard focus rings, reduced motion, forced colors and print.
UI_LAYER = r"""
<style>
/* ZP-FIX v2 UI LAYER */
/* =========================================================================
   ZenPDF professional layout layer.
   Loaded last so it wins on ties, but every rule is deliberately scoped -
   nothing here changes behaviour, only presentation and ergonomics.
   ========================================================================= */

/* ---- 1. never let the shell scroll sideways -------------------------- */
html, body { max-width: 100%; overflow-x: hidden; }
body { text-rendering: optimizeLegibility; -webkit-font-smoothing: antialiased; }
*, *::before, *::after { box-sizing: border-box; }

/* ---- 2. respect notches, home bars and hinge cutouts ------------------ */
header { padding-left: max(12px, env(safe-area-inset-left));
         padding-right: max(12px, env(safe-area-inset-right)); }
main   { padding-left: max(12px, env(safe-area-inset-left));
         padding-right: max(12px, env(safe-area-inset-right)); }

/* ---- 3. real focus rings (keyboard only) ----------------------------- */
:where(button, a, input, select, textarea, [tabindex]):focus-visible {
  outline: 2px solid var(--sel, #0a84ff);
  outline-offset: 2px;
  border-radius: 6px;
}
:where(button, a, input, select, textarea):focus:not(:focus-visible) { outline: none; }

/* ---- 4. the document canvas — the part that must feel like a reader --- */
.editor-pages {
  /* a calm neutral trough behind the paper, exactly like Acrobat/iLovePDF */
  background: var(--reader-bg, #55555a);
  border-radius: var(--radius-md, 14px);
  padding: 26px 16px 48px;
  scroll-padding-top: 96px;
  overscroll-behavior-x: contain;
}
html[data-theme="dark"] .editor-pages { background: #48484d; }
.page-holder {
  box-shadow: 0 2px 6px rgba(0,0,0,.24), 0 12px 32px rgba(0,0,0,.22);
  border-radius: 3px;
  outline: 1px solid rgba(0,0,0,.10);
  outline-offset: -1px;
  transition: box-shadow .18s var(--ease, ease);
}
.page-holder:hover { box-shadow: 0 3px 8px rgba(0,0,0,.28), 0 16px 40px rgba(0,0,0,.26); }
.page-num-badge {
  backdrop-filter: saturate(1.6) blur(8px);
  -webkit-backdrop-filter: saturate(1.6) blur(8px);
  background: rgba(20,20,22,.72);
  letter-spacing: .02em;
}

/* ---- 5. toolbar: sticky, scrollable, never clipped ------------------- */
.editor-bar {
  position: sticky;
  z-index: 30;
  backdrop-filter: saturate(1.8) blur(20px);
  -webkit-backdrop-filter: saturate(1.8) blur(20px);
  border-bottom: 1px solid var(--line, #e4e4e8);
}
.bar-row { scroll-behavior: smooth; }
.bar-row::-webkit-scrollbar { height: 0; }

/* ---- 6. touch ergonomics: 44px minimum on any coarse pointer --------- */
@media (pointer: coarse) {
  .tool-btn, .chip-btn, .icon-btn, .color-dot, .crumb-btn,
  .back-btn, .sheet-opt, .org-thumb {
    min-height: 44px;
  }
  .tool-btn, .icon-btn, .color-dot, .chip-btn { min-width: 44px; }
  /* form controls in the toolbar were 16-32px tall - unusable with a thumb */
  .bar-select, .bar-num, .bar-color { min-height: 40px; }
  .bar-color { min-width: 40px; }
  .bar-range { height: 40px; }                  /* the track stays thin... */
  .bar-range::-webkit-slider-thumb { width: 26px; height: 26px; }
  .bar-range::-moz-range-thumb { width: 26px; height: 26px; }
  .ov-resize { width: 22px; height: 22px; }     /* draggable with a thumb */
  .ov-del, .ov-dup { min-width: 32px; min-height: 32px; }
}

/* ---- 7. FOLDABLE CLOSED / very narrow  (<= 360px) -------------------- */
@media (max-width: 360px) {
  :root { --radius-lg: 14px; --radius-md: 11px; }
  header { padding: 8px 10px; }
  main { padding-left: 8px; padding-right: 8px; }
  .editor-pages { padding: 14px 6px 36px; border-radius: 10px; }
  .action-card { padding: 14px 12px; }
  .action-card b { font-size: .92rem; }
  .action-card p { font-size: .74rem; }
  .tool-btn .tl { display: none; }              /* icons only - room is scarce */
  .sheet { padding: 16px 14px calc(16px + env(safe-area-inset-bottom)); }
  .primary-btn { width: 100%; }
}

/* ---- 8. PHONE  (361 - 480px) ---------------------------------------- */
@media (min-width: 361px) and (max-width: 480px) {
  .editor-pages { padding: 16px 8px 40px; }
  .primary-btn { width: 100%; }
}

/* ---- 9. LARGE PHONE / small tablet portrait (481 - 640px) ----------- */
@media (max-width: 640px) {
  .editor-pages { scroll-padding-top: 120px; }
  /* a bottom-anchored toolbar is reachable one-handed */
  .editor-bar.dock-bottom {
    position: fixed; left: 0; right: 0; bottom: 0; top: auto;
    padding-bottom: calc(7px + env(safe-area-inset-bottom));
    border-top: 1px solid var(--line, #e4e4e8);
    border-bottom: none;
    box-shadow: 0 -6px 22px rgba(0,0,0,.13);
  }
}

/* ---- 10. TABLET  (641 - 1024px) ------------------------------------- */
@media (min-width: 641px) and (max-width: 1024px) {
  .editor-pages { padding: 22px 14px 44px; }
  .actions-grid, .tool-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
}

/* ---- 11. LAPTOP  (1025 - 1440px) ------------------------------------ */
@media (min-width: 1025px) and (max-width: 1440px) {
  main { max-width: 1120px; margin-inline: auto; }
  .actions-grid, .tool-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 14px;
  }
}

/* ---- 12. DESKTOP / wide (>= 1441px) --------------------------------- */
@media (min-width: 1441px) {
  main { max-width: 1320px; margin-inline: auto; }
  .actions-grid, .tool-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 16px;
  }
  .editor-pages { padding: 32px 24px 56px; }
}

/* ---- 13. FOLDABLE UNFOLDED / dual screen ---------------------------- */
@media (horizontal-viewport-segments: 2) {
  .editor-pages { padding-inline: 8px; }
  main { max-width: none; }
}

/* ---- 14. LANDSCAPE PHONE: vertical space is the scarce axis ---------- */
@media (max-height: 460px) and (orientation: landscape) {
  header { padding-top: 5px; padding-bottom: 5px; }
  .editor-bar { padding-top: 4px; padding-bottom: 4px; }
  .tool-btn .tl { display: none; }
  .editor-pages { padding-top: 10px; }
}

/* ---- 15. accessibility & system preferences ------------------------- */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .001ms !important;
    scroll-behavior: auto !important;
  }
}
@media (forced-colors: active) {
  .tool-btn, .chip-btn, .action-card, .page-holder { border: 1px solid CanvasText; }
  .ov-obj.selected .ov-text, .ov-obj.selected img { outline: 2px solid Highlight; }
}

/* ---- 16. print: the page, nothing else ------------------------------ */
@media print {
  header, .editor-bar, .page-num-badge, .ov-del, .ov-dup, .ov-resize,
  #pwa-card, .toast, .sheet, .back-btn { display: none !important; }
  .editor-pages { background: none; padding: 0; }
  .page-holder { box-shadow: none; outline: none; break-inside: avoid; page-break-inside: avoid; }
}
</style>
"""


# =============================================================================
#  3. THE EDITS
# =============================================================================
#  Each entry: (id, description, anchor, replacement)
#  The anchor must appear EXACTLY ONCE. Anything else aborts the run.

def core_edits():
    E = []

    # -- F1 ------------------------------------------------------------------
    E.append((
        "F1-editor-geometry",
        "Editor export: use rotation/CropBox-aware display geometry",
        """      const { width: pw, height: ph } = page.getSize();
      const sx = pw / p.w, sy = ph / p.h;   // css px -> pdf pt
""",
        """      /* ZP-FIX F1: page.getSize() is the raw, UNROTATED MediaBox, but every
         object on screen was measured against pdf.js's viewport, which
         applies /Rotate and the CropBox. Mixing the two put stamps a quarter
         turn out and off-page on rotated scans. zpBeginPage() opens a
         display-space transform instead: pw/ph are now the page AS SHOWN and
         sx === sy, so nothing is squashed, flipped or displaced. */
      const _zg = zpBeginPage(page, p);
      const pw = _zg.Dw, ph = _zg.Dh;
      const sx = _zg.k, sy = _zg.k;         // editor css px -> display pt
""",
    ))

    # -- F1b -----------------------------------------------------------------
    E.append((
        "F1b-editor-close",
        "Editor export: close the display-space transform per page",
        """      }
    }

    return doc.save();
}
""",
        """      }

      zpEndPage(page);      /* ZP-FIX F1: balance the transform for this page */
    }

    return doc.save();
}
""",
    ))

    # -- F2 ------------------------------------------------------------------
    E.append((
        "F2-dot-geometry",
        "Dot stamp export: same rotation/CropBox fix",
        """    const { width: pw, height: ph } = page.getSize();
    const sx = pw / p.w, sy = ph / p.h;
""",
        """    /* ZP-FIX F2: the dot-stamp pass had its own copy of the broken math. */
    const _zg = zpBeginPage(page, p);
    const pw = _zg.Dw, ph = _zg.Dh;
    const sx = _zg.k, sy = _zg.k;
""",
    ))

    E.append((
        "F2b-dot-close",
        "Dot stamp export: close the transform",
        """      page.drawEllipse({ x: X + W / 2, y: Ytop - H / 2, xScale: r, yScale: r,
        color: rgb(c[0], c[1], c[2]) });
    }
  }
  return doc.save();""",
        """      page.drawEllipse({ x: X + W / 2, y: Ytop - H / 2, xScale: r, yScale: r,
        color: rgb(c[0], c[1], c[2]) });
    }
    zpEndPage(page);        /* ZP-FIX F2 */
  }
  return doc.save();""",
    ))

    # -- F3 ------------------------------------------------------------------
    E.append((
        "F3-watermark-image",
        "Watermark (logo): fit and centre against the page as displayed",
        """        pages.forEach(page => {
          const { width, height } = page.getSize();
          let w = Math.min(width * 0.5 * scalePct, width * 0.92);""",
        """        pages.forEach(page => {
          /* ZP-FIX F3: on a rotated page getSize() reports the landscape
             MediaBox, so the logo was fitted to the wrong axis and came out
             oversized, clipped and lying on its side. */
          const _zg = zpBeginPage(page, null);
          const width = _zg.Dw, height = _zg.Dh;
          let w = Math.min(width * 0.5 * scalePct, width * 0.92);""",
    ))

    E.append((
        "F3b-watermark-image-close",
        "Watermark (logo): close the transform",
        """          page.drawImage(img, {
            x: (width - w) / 2, y: (height - h) / 2, width: w, height: h, opacity,
          });
        });""",
        """          page.drawImage(img, {
            x: (width - w) / 2, y: (height - h) / 2, width: w, height: h, opacity,
          });
          zpEndPage(page);   /* ZP-FIX F3 */
        });""",
    ))

    E.append((
        "F4-watermark-text",
        "Watermark (text): diagonal follows the page as displayed",
        """        pages.forEach(page => {
          const { width, height } = page.getSize();
          let size = 50 * scalePct;""",
        """        pages.forEach(page => {
          /* ZP-FIX F4: the 45 degree diagonal was measured on the unrotated
             box, so on a rotated page it ran across the wrong diagonal and
             the auto-shrink used the wrong page width. */
          const _zg = zpBeginPage(page, null);
          const width = _zg.Dw, height = _zg.Dh;
          let size = 50 * scalePct;""",
    ))

    E.append((
        "F4b-watermark-text-close",
        "Watermark (text): close the transform",
        """            color: rgb(0.45, 0.45, 0.47), opacity, rotate: degrees(45),
          });
        });""",
        """            color: rgb(0.45, 0.45, 0.47), opacity, rotate: degrees(45),
          });
          zpEndPage(page);   /* ZP-FIX F4 */
        });""",
    ))

    # -- F5 ------------------------------------------------------------------
    E.append((
        "F5-page-numbers",
        "Page numbers: land on the visual bottom edge, upright",
        """    pages.forEach((page, i) => {
      const { width, height } = page.getSize();
      const label = fmt === 'pn'""",
        """    pages.forEach((page, i) => {
      /* ZP-FIX F5: "bottom centre" was the bottom of the UNROTATED box. On a
         /Rotate 270 page that is the left-hand margin, with the number lying
         on its side. Display space puts it where the user expects. */
      const _zg = zpBeginPage(page, null);
      const width = _zg.Dw, height = _zg.Dh;
      const label = fmt === 'pn'""",
    ))

    E.append((
        "F5b-page-numbers-close",
        "Page numbers: close the transform",
        """      page.drawText(label, { x, y, size, font, color: rgb(0.25, 0.25, 0.27) });
    });""",
        """      page.drawText(label, { x, y, size, font, color: rgb(0.25, 0.25, 0.27) });
      zpEndPage(page);       /* ZP-FIX F5 */
    });""",
    ))

    # -- F6 ------------------------------------------------------------------
    E.append((
        "F6-image-measure",
        "Signatures/images: measure the real element box, not a derived aspect",
        """        const w = obj.getBoundingClientRect().width / (p.holder.getBoundingClientRect().width / p.w);
        const h = w * (img.naturalHeight / img.naturalWidth);""",
        """        /* ZP-FIX F6: width came from the WRAPPER's bounding rect (which
           includes the dashed selection border) and height was re-derived
           from the image's natural aspect ratio. Any border, browser zoom or
           non-proportional resize therefore shifted the signature vertically,
           because y is computed from (top + h). Measure the <img> itself,
           relative to its page holder, and use its real height. */
        const _zm = zpMeasure(img, p);
        let w, h;
        if (_zm) {
          w = _zm.w; h = _zm.h;
          rect.l = _zm.x; rect.t = _zm.y;
        } else {                                  // not laid out - best effort
          w = parseFloat(obj.style.width) || img.naturalWidth || 1;
          h = w * ((img.naturalHeight / img.naturalWidth) || 1);
        }""",
    ))

    # -- F7 ------------------------------------------------------------------
    E.append((
        "F7-winansi",
        "Text export: stop flattening typography WinAnsi can encode",
        'function winAnsiSafe(s) {\n  return s\n    .replace(/[\\u2018\\u2019\\u201A\\u2032]/g, "\'")\n    .replace(/[\\u201C\\u201D\\u201E\\u2033]/g, \'"\')\n    .replace(/[\\u2013\\u2212]/g, \'-\').replace(/\\u2014/g, \'--\')\n    .replace(/\\u2026/g, \'...\').replace(/\\u00a0/g, \' \')\n    .replace(/\\u2022/g, \'\\xb7\')\n    .replace(/[^\\x20-\\x7E\\xA0-\\xFF\\n]/g, \'?\');\n}',
        'function winAnsiSafe(s) {\n  /* ZP-FIX F7: this used to flatten typography the standard PDF fonts can\n     encode perfectly well - curly quotes became straight ones, an em dash\n     became "--", the bullet became a middot and the ellipsis became three\n     periods. Verified against pdf-lib 1.17.1: WinAnsi carries the whole\n     0x80-0x9F block (curly quotes, en/em dash, ellipsis, bullet, trademark,\n     euro, S-caron, Z-caron, OE) plus all of Latin-1, so those now survive\n     the round trip untouched.\n\n     Code points WinAnsi genuinely cannot hold - the rupee sign, Tamil,\n     Devanagari, CJK, emoji - are caught earlier by zpNeedsRaster() and drawn\n     as a 4x raster, so they no longer become a row of "?" either. What is\n     left here is only the last-resort net. */\n  return s\n    .replace(/′/g, "\'")      // prime -> apostrophe (not in WinAnsi)\n    .replace(/″/g, \'"\')      // double prime\n    .replace(/−/g, \'-\')      // minus sign -> hyphen\n    .replace(/\xa0/g, \' \')      // nbsp -> space\n    .replace(/[^\\x20-\\x7E\\xA0-\\xFF\\n€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ]/g, \'?\');\n}',
    ))

    E.append((
        "F7b-text-raster",
        "Text export: draw non-WinAnsi text as a crisp raster instead of '?'",
        """      for (const obj of p.texts) {
        const t = obj.querySelector('.ov-text');
        const content = winAnsiSafe(t.innerText);
        if (!content.trim()) continue;""",
        """      for (const obj of p.texts) {
        const t = obj.querySelector('.ov-text');
        const _raw = t.innerText;
        if (!_raw.trim()) continue;
        /* ZP-FIX F7: the 14 standard PDF fonts are WinAnsi-only, so an
           accented name, a rupee sign, Tamil, Hindi, CJK or an emoji used to
           export as a row of "?". Draw those objects at 4x as a raster - the
           export then looks exactly like the screen. Everything WinAnsi can
           encode still exports as real, selectable, searchable PDF text. */
        if (zpNeedsRaster(_raw)) {
          const _im = zpRasterText(obj, t, p);
          if (_im) {
            try {
              const _png = await doc.embedPng(_im.dataUrl);
              page.drawImage(_png, {
                x: _im.x * sx, y: ph - (_im.y + _im.h) * sy,
                width: _im.w * sx, height: _im.h * sy,
              });
              continue;
            } catch (_e) { /* fall through to the text path */ }
          }
        }
        const content = winAnsiSafe(_raw);
        if (!content.trim()) continue;""",
    ))

    return E


def extra_edits():
    """Robustness fixes that are independent of the geometry work."""
    E = []

    # -- F8 ------------------------------------------------------------------
    E.append((
        "F8-images-to-pdf",
        "Images to PDF: sane page sizes, and formats pdf-lib cannot embed",
        """    for (const f of aux.img2pdf) {
      const buf = await f.arrayBuffer();
      const img = f.type.includes('png') ? await doc.embedPng(buf) : await doc.embedJpg(buf);
      const page = doc.addPage([img.width, img.height]);
      page.drawImage(img, { x: 0, y: 0, width: img.width, height: img.height });
    }""",
        """    /* ZP-FIX F8: pixel dimensions were used directly as PDF points, so a
       12 MP phone photo became a 4000 x 3000 pt page - roughly 55 x 41
       inches, which no printer or viewer handles sensibly. Treat the pixels
       as 96 DPI (the CSS reference) and cap the result at A3, keeping the
       aspect ratio. Formats pdf-lib cannot embed natively (WebP, AVIF, HEIC
       from an iPhone) are re-encoded through a canvas instead of throwing. */
    const ZP_MAX_PT = 1191;              // A3 long edge, in points
    for (const f of aux.img2pdf) {
      let img = null;
      const buf = await f.arrayBuffer();
      const isPng = (f.type || '').includes('png');
      try {
        img = isPng ? await doc.embedPng(buf) : await doc.embedJpg(buf);
      } catch (e) {
        img = await (async () => {         // transcode anything else to JPEG
          const url = URL.createObjectURL(f);
          try {
            const bmp = await new Promise((res, rej) => {
              const im = new Image();
              im.onload = () => res(im);
              im.onerror = () => rej(new Error('Unsupported image: ' + f.name));
              im.src = url;
            });
            const cv = document.createElement('canvas');
            cv.width = bmp.naturalWidth || bmp.width;
            cv.height = bmp.naturalHeight || bmp.height;
            const cx = cv.getContext('2d');
            cx.fillStyle = '#ffffff';
            cx.fillRect(0, 0, cv.width, cv.height);
            cx.drawImage(bmp, 0, 0);
            return await doc.embedJpg(cv.toDataURL('image/jpeg', 0.92));
          } finally { URL.revokeObjectURL(url); }
        })();
      }
      let w = img.width * 0.75, h = img.height * 0.75;   // 96 DPI px -> pt
      const over = Math.max(w, h) / ZP_MAX_PT;
      if (over > 1) { w /= over; h /= over; }
      if (!(w > 0) || !(h > 0)) { w = 612; h = 792; }
      const page = doc.addPage([w, h]);
      page.drawImage(img, { x: 0, y: 0, width: w, height: h });
    }""",
    ))

    # -- F9 ------------------------------------------------------------------
    E.append((
        "F9-rotate-normalise",
        "Rotate all: normalise angles that are not multiples of 90",
        "    doc.getPages().forEach(p => p.setRotation(degrees(((p.getRotation().angle + deg) % 360 + 360) % 360)));",
        """    /* ZP-FIX F9: /Rotate must be a multiple of 90. Files written by broken
       generators carry 45, -90 or 720; adding to those produced a page no
       viewer agrees on. zpNormRot snaps to the nearest legal quarter turn. */
    doc.getPages().forEach(p => p.setRotation(degrees(zpNormRot(p.getRotation().angle + deg))));""",
    ))

    # -- F10 -----------------------------------------------------------------
    E.append((
        "F10-editor-doc-leak",
        "Editor: release the previous pdf.js document before loading another",
        "  ed.strokeLog = []; ed.redoLog = []; ed.pdfDoc = null;",
        """  ed.strokeLog = []; ed.redoLog = [];
  /* ZP-FIX F10: the old pdf.js document (and its worker-side page cache) was
     dropped on the floor on every re-open, so opening a large file a few
     times in one session grew memory until the tab was killed. */
  try { if (ed.pdfDoc && ed.pdfDoc.destroy) ed.pdfDoc.destroy(); } catch (e) {}
  ed.pdfDoc = null;""",
    ))

    # -- F11 -----------------------------------------------------------------
    E.append((
        "F11-fit-narrow-screens",
        "Editor: always fit the page to the available width on small screens",
        """    const vw = Math.min(wrap.clientWidth || 900, 900);
    ed.scale = Math.min(1.6, Math.max(0.6, vw / first.getViewport({ scale: 1 }).width));""",
        """    /* ZP-FIX F11: two bugs in one line.
         1) clientWidth includes the wrapper's horizontal padding, so the
            available width was over-estimated and the page ran under the
            gutter.
         2) The 0.6 floor beat the fit-to-width calculation, so on a folded
            phone (280 css px) a Letter page still rendered 367 px wide and
            had to be scrolled sideways. Measured on a Galaxy Fold profile:
            page 367 px inside a 264 px column.
       Subtract the real padding and let the scale go as low as the screen
       needs; the 1.6 cap and the zoom controls are unchanged. */
    const _wcs = getComputedStyle(wrap);
    const _wpad = (parseFloat(_wcs.paddingLeft) || 0) + (parseFloat(_wcs.paddingRight) || 0);
    const vw = Math.max(160, Math.min((wrap.clientWidth || 900) - _wpad, 900));
    ed.scale = Math.min(1.6, Math.max(0.2, vw / first.getViewport({ scale: 1 }).width));""",
    ))

    return E


# =============================================================================
#  4. DRIVER
# =============================================================================

def _real_end_tag_offset(html, tag):
    """Byte offset of the DOCUMENT's closing </tag>, ignoring look-alikes.

    index.html builds whole HTML documents inside JavaScript string literals,
    so the raw text contains "</head>", "</body>" and "</html>" in places that
    are NOT markup. A plain .replace() (or even .rsplit()) lands in one of
    those and silently destroys a 228 KB script block. HTMLParser treats the
    contents of <script> and <style> as raw text, so it only ever reports the
    real tags - that is what we anchor to.
    """
    class _F(HTMLParser):
        def __init__(self):
            HTMLParser.__init__(self, convert_charrefs=False)
            self.pos = None
        def handle_endtag(self, t):
            if t == tag and self.pos is None:
                self.pos = self.getpos()

    f = _F()
    try:
        f.feed(html)
        f.close()
    except Exception:
        pass
    if not f.pos:
        return None
    line, col = f.pos
    lines = html.split("\n")
    if line - 1 >= len(lines):
        return None
    off = sum(len(x) + 1 for x in lines[:line - 1]) + col
    if html[off:off + len(tag) + 3].lower() == "</%s>" % tag:
        return off
    return None


def fail(msg):
    print("\n  ABORTED - nothing was written.\n  " + msg + "\n", file=sys.stderr)
    sys.exit(2)


def apply_edits(html, edits, applied, skipped):
    """Apply every edit or raise. Returns the new html."""
    for eid, desc, anchor, repl in edits:
        if repl in html:                     # already patched
            skipped.append((eid, desc))
            continue
        n = html.count(anchor)
        if n == 0:
            fail("Could not find the code for %s (%s).\n"
                 "  This usually means index.html has already been edited by hand,\n"
                 "  or it is a different version than this patch targets.\n"
                 "  Nothing has been changed. Please share your index.html." % (eid, desc))
        if n > 1:
            fail("The anchor for %s (%s) appears %d times - refusing to guess."
                 % (eid, desc, n))
        html = html.replace(anchor, repl, 1)
        applied.append((eid, desc))
    return html


def main():
    ap = argparse.ArgumentParser(
        description="ZenPDF production fix patch v%s - rotation/CropBox stamping "
                    "alignment and related export defects." % VERSION)
    ap.add_argument("target", nargs="?", default="index.html",
                    help="path to index.html (default: ./index.html)")
    ap.add_argument("--check", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--restore", action="store_true",
                    help="restore the .bak written by a previous run")
    ap.add_argument("--no-ui", action="store_true",
                    help="skip the responsive/professional CSS layer")
    ap.add_argument("--no-extras", action="store_true",
                    help="skip the non-geometry robustness fixes (F8-F10)")
    args = ap.parse_args()

    target = os.path.abspath(args.target)
    bak = target + ".bak"

    if args.restore:
        if not os.path.exists(bak):
            fail("No backup at %s" % bak)
        shutil.copyfile(bak, target)
        print("Restored %s from %s" % (target, bak))
        return

    if not os.path.exists(target):
        fail("%s not found. Pass the path: python3 %s path/to/index.html"
             % (target, os.path.basename(__file__)))

    with open(target, "r", encoding="utf-8") as fh:
        original = fh.read()

    before = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    html = original
    applied, skipped = [], []

    print("=" * 72)
    print(" ZenPDF fix patch v%s" % VERSION)
    print(" target : %s" % target)
    print(" sha256 : %s (first 12)" % before)
    print(" size   : %s bytes, %s lines" % (len(original), original.count("\n") + 1))
    print("=" * 72)

    # ---- 1. the geometry core --------------------------------------------
    if MARKER in html:
        skipped.append(("CORE", "geometry core already installed"))
    else:
        tag = '<script src="https://unpkg.com/pdf-lib@1.17.1/dist/pdf-lib.min.js"></script>'
        if html.count(tag) != 1:
            # tolerate a self-hosted / different-CDN pdf-lib
            m = re.search(r'<script[^>]+src="[^"]*pdf-lib[^"]*"[^>]*>\s*</script>', html)
            if not m:
                fail("Could not find the pdf-lib <script> tag to anchor the "
                     "geometry core to.")
            tag = m.group(0)
        html = html.replace(tag, tag + GEOMETRY_CORE, 1)
        applied.append(("CORE", "geometry core injected after pdf-lib"))

    # ---- 2. correctness edits --------------------------------------------
    html = apply_edits(html, core_edits(), applied, skipped)
    if not args.no_extras:
        html = apply_edits(html, extra_edits(), applied, skipped)

    # ---- 3. the UI layer -------------------------------------------------
    if not args.no_ui:
        if UI_MARKER in html:
            skipped.append(("UI", "ui layer already installed"))
        else:
            off = _real_end_tag_offset(html, "head")
            if off is None:
                fail("Could not locate the document's real </head> tag.")
            html = html[:off] + UI_LAYER + "\n" + html[off:]
            applied.append(("UI", "responsive/professional CSS layer injected"))

    # ---- 4. stamp the build ---------------------------------------------
    stamp = ("\n<!-- ZenPDF fix patch v%s applied %s -->\n"
             % (VERSION, datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
    if "ZenPDF fix patch v" not in html:
        # NB: index.html contains the literal text "</body>" inside JavaScript
        # string literals (the HTML-export code builds a document as a string).
        # A naive .replace() lands inside one of those and breaks the whole
        # script block, so always target the LAST occurrence - the real tag.
        off = _real_end_tag_offset(html, "body")
        if off is None:
            off = _real_end_tag_offset(html, "html")
        if off is None:
            html = html + stamp
        else:
            html = html[:off] + stamp + html[off:]

    # ---- report ----------------------------------------------------------
    for eid, desc in applied:
        print("  [apply] %-24s %s" % (eid, desc))
    for eid, desc in skipped:
        print("  [ skip] %-24s %s" % (eid, desc))

    if html == original:
        print("\n  Already fully patched - nothing to do.\n")
        return

    if args.check:
        print("\n  --check: %d edit(s) would be applied. Nothing written.\n" % len(applied))
        return

    if not os.path.exists(bak):
        shutil.copyfile(target, bak)
        print("\n  backup -> %s" % bak)
    else:
        print("\n  backup already exists, left alone -> %s" % bak)

    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.replace(tmp, target)

    after = hashlib.sha256(html.encode("utf-8")).hexdigest()[:12]
    print("  written. new sha256 %s, %s bytes (+%s)"
          % (after, len(html), len(html) - len(original)))
    print("\n  %d edit(s) applied. Reload the page with a hard refresh\n"
          "  (Ctrl/Cmd+Shift+R) so the browser drops the cached copy.\n" % len(applied))


if __name__ == "__main__":
    main()
