#!/usr/bin/env python3
"""
verify_alignment.py - inspect a PDF for the conditions that broke stamping,
and (optionally) prove a stamp lands where it was asked to.

    python3 verify_alignment.py contract.pdf          # report the page geometry
    python3 verify_alignment.py contract.pdf --probe  # stamp + render + measure

--probe needs `pip install pypdfium2 pikepdf`; the plain report needs neither
(it parses the page tree directly).

Use it on any file a user reports as "the signature is in the wrong place":
if `rotate` is non-zero or `cropbox` differs from `mediabox`, that page is
exactly the case this fix addresses.
"""
import sys
import re
import zlib


def _report_pure(path):
    """Page geometry with no third-party dependencies."""
    try:
        import pikepdf
    except ImportError:
        print("  (install pikepdf for a full report: pip install pikepdf)")
        return _report_raw(path)

    with pikepdf.open(path) as pdf:
        print("  pages: %d   pdf version: %s" % (len(pdf.pages), pdf.pdf_version))
        print()
        hdr = "  %-5s %-26s %-26s %-7s %-14s %s"
        print(hdr % ("page", "MediaBox", "CropBox", "Rotate", "displayed", "note"))
        print("  " + "-" * 96)
        flagged = 0
        for i, page in enumerate(pdf.pages):
            o = page.obj
            mb = [float(x) for x in (o.get("/MediaBox") or [0, 0, 612, 792])]
            cbo = o.get("/CropBox")
            cb = [float(x) for x in cbo] if cbo is not None else None
            rot = int(o.get("/Rotate") or 0)
            rot = ((round(rot / 90) * 90) % 360 + 360) % 360

            x1, y1, x2, y2 = min(mb[0], mb[2]), min(mb[1], mb[3]), max(mb[0], mb[2]), max(mb[1], mb[3])
            if cb:
                cx1, cy1 = min(cb[0], cb[2]), min(cb[1], cb[3])
                cx2, cy2 = max(cb[0], cb[2]), max(cb[1], cb[3])
                nx1, ny1 = max(x1, cx1), max(y1, cy1)
                nx2, ny2 = min(x2, cx2), min(y2, cy2)
                if nx2 - nx1 > 1 and ny2 - ny1 > 1:
                    x1, y1, x2, y2 = nx1, ny1, nx2, ny2
            bw, bh = x2 - x1, y2 - y1
            dw, dh = (bh, bw) if rot in (90, 270) else (bw, bh)

            notes = []
            if rot:
                notes.append("ROTATED")
            if cb and [round(v, 2) for v in cb] != [round(v, 2) for v in mb]:
                notes.append("CROPPED")
            if mb[0] or mb[1]:
                notes.append("OFFSET-ORIGIN")
            if notes:
                flagged += 1

            print(hdr % (i + 1,
                         "[%g %g %g %g]" % tuple(mb),
                         ("[%g %g %g %g]" % tuple(cb)) if cb else "-",
                         rot,
                         "%g x %g" % (dw, dh),
                         " ".join(notes) or "plain"))
        print()
        if flagged:
            print("  >> %d of %d page(s) need rotation/crop-aware stamping." % (flagged, len(pdf.pages)))
            print("     Unpatched ZenPDF places signatures wrongly on these pages.")
        else:
            print("  >> All pages are plain, upright and uncropped.")
        return flagged


def _report_raw(path):
    data = open(path, "rb").read()
    n = len(re.findall(rb"/Type\s*/Page[^s]", data))
    print("  raw scan: ~%d page object(s); /Rotate present: %s"
          % (n, b"/Rotate" in data))
    return 0


def _probe(path):
    """Stamp a marker through the corrected matrix and measure where it lands."""
    try:
        import pikepdf
        import pypdfium2 as pdfium
        import numpy as np
    except ImportError as e:
        print("  --probe needs pypdfium2, pikepdf and numpy (%s)" % e)
        return 1

    L, T, W, H = 60, 120, 100, 40          # target, in display points from top-left

    with pikepdf.open(path) as pdf:
        page = pdf.pages[0]
        o = page.obj
        mb = [float(x) for x in (o.get("/MediaBox") or [0, 0, 612, 792])]
        cbo = o.get("/CropBox")
        cb = [float(x) for x in cbo] if cbo is not None else None
        rot = int(o.get("/Rotate") or 0)
        rot = ((round(rot / 90) * 90) % 360 + 360) % 360

        x1, y1, x2, y2 = min(mb[0], mb[2]), min(mb[1], mb[3]), max(mb[0], mb[2]), max(mb[1], mb[3])
        if cb:
            nx1, ny1 = max(x1, min(cb[0], cb[2])), max(y1, min(cb[1], cb[3]))
            nx2, ny2 = min(x2, max(cb[0], cb[2])), min(y2, max(cb[1], cb[3]))
            if nx2 - nx1 > 1 and ny2 - ny1 > 1:
                x1, y1, x2, y2 = nx1, ny1, nx2, ny2
        bw, bh = x2 - x1, y2 - y1
        Dw, Dh = (bh, bw) if rot in (90, 270) else (bw, bh)

        ctm = {0:   (1, 0, 0, 1, x1, y1),
               90:  (0, 1, -1, 0, x2, y1),
               180: (-1, 0, 0, -1, x2, y2),
               270: (0, -1, 1, 0, x1, y2)}[rot]

        ops = ("q %g %g %g %g %g %g cm 1 0 0 RG 1 0 0 rg "
               "%g %g %g %g re f Q\n" % (ctm + (L, Dh - (T + H), W, H)))
        new = pikepdf.Stream(pdf, ops.encode())
        cur = o.get("/Contents")
        arr = pikepdf.Array(list(cur) if isinstance(cur, pikepdf.Array) else [cur])
        arr.append(new)
        o["/Contents"] = arr
        pdf.save("/tmp/_zp_probe.pdf")

    doc = pdfium.PdfDocument("/tmp/_zp_probe.pdf")
    a = doc[0].render(scale=1.0).to_numpy()[:, :, :3]      # pdfium gives BGR
    b, g, r = a[:, :, 0].astype(int), a[:, :, 1].astype(int), a[:, :, 2].astype(int)
    ys, xs = np.nonzero((r > 180) & (g < 80) & (b < 80))
    print()
    print("  probe: page 1 renders %d x %d, /Rotate %d" % (a.shape[1], a.shape[0], rot))
    if len(xs) == 0:
        print("  probe: MARKER NOT FOUND - something is wrong")
        return 1
    got = (xs.min(), ys.min(), xs.max() - xs.min() + 1, ys.max() - ys.min() + 1)
    print("  probe: asked for  left=%d top=%d w=%d h=%d" % (L, T, W, H))
    print("  probe: landed at  left=%d top=%d w=%d h=%d" % got)
    good = all(abs(a_ - b_) <= 2 for a_, b_ in zip(got, (L, T, W, H)))
    print("  probe: %s" % ("PASS - display-space stamping is exact" if good else "FAIL"))
    return 0 if good else 1


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print(__doc__)
        return 1
    path = args[0]
    print()
    print("=" * 100)
    print(" %s" % path)
    print("=" * 100)
    _report_pure(path)
    if "--probe" in sys.argv:
        return _probe(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
