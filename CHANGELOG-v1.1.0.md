# ZenPDF v1.1.0 — Large Merge + Image Workspace

**Files:** `zenpdf-merge-images-fix.js`, `apply_merge_fix.py`
**Applies to:** the original app (`index.html`) — not ZenPDF Studio.
**Requires:** v1.0.0 geometry pack already applied. Paste/apply *after* it.

---

## Read this first: what I found about the merge

You asked me to make merge work for 1000+ page documents. **I tested it, and
it already does.** I need to say that plainly rather than sell you a fix for
a problem that is not there.

Measured in Chromium on your current build, *before* any change:

| Input | Pages | Result |
|---|---|---|
| 2 × 435 KB synthetic | 2,000 | 1.4 s |
| 2 × 98 MB scanned | 2,000 | 2.9 s |
| 4 × 98 MB scanned | 4,000 | 10.5 s, 1.3 GB heap |
| 8 × 98 MB scanned | 8,000 | 21.5 s, 2.5 GB heap |

There is **no page-count limit** in pdf-lib or in your code. What actually
limits a merge is **memory, driven by total bytes, not page count**:

```
peak JS heap  ≈  3.2 × combined input size
```

A browser tab usually dies somewhere past 2–4 GB, and a phone long before
that. So ~800 MB of input is roughly the ceiling on a desktop, and perhaps
150–250 MB on a mid-range phone — regardless of whether that is 500 pages or
8,000.

**So if you are seeing merge fail, it is worth telling me exactly what you
see** — a frozen tab, a specific error, the tab reloading itself, or a
wrong/short output file. Each points somewhere different, and I would rather
fix the real one. In the meantime this release hardens everything around the
merge and makes the failure modes legible instead of silent.

> Two of my own earlier measurements were wrong and I want to flag it: a
> 25-minute "hang" turned out to be my test harness waiting on the export
> dialog, and a "file add timeout" was a bug in my test predicate. Neither
> was your app. The numbers above are the corrected ones.

---

## What changed — Merge

| # | Before | After |
|---|---|---|
| 1 | The whole merge ran in one synchronous burst; the tab was frozen for its entire duration (21 s at 8,000 pages) and Chrome could offer to kill it | Pages copy in chunks of 40 with a yield between them — worst single block measured at **343 ms** |
| 2 | No progress at all — a long merge was indistinguishable from a crash | Live progress sheet: "3,480 of 8,000 pages · contract.pdf" |
| 3 | No way to stop it | Working **Cancel** |
| 4 | One password-protected or damaged file threw and lost the whole merge | Bad files are skipped, named in a toast, and the rest still merges |
| 5 | No warning before attempting something the device cannot hold | Memory pre-flight using the measured 3.2× model and `navigator.deviceMemory`; offers "merge in two halves" instead of letting the tab die |
| 6 | Out-of-memory surfaced as a raw `Array buffer allocation failed` | Plain-language message telling you to merge in halves |
| 7 | Sources stayed parsed in memory for the whole run | Each source is released as soon as it is drained |
| 8 | `save()` used stock defaults | `useObjectStreams:false` + `objectsPerTick:1500` above 400 pages — **16× faster save** on a 2,000-page document (0.9 s → 0.1 s) |

Net effect at scale: **8,000 pages went from 21.5 s to 16.4 s**, with the UI
staying responsive throughout. At small sizes it is a hair slower (2,000
pages: 2.9 s → 4.0 s) because of the deliberate yielding — that is the trade
for a tab that never locks up.

### The bug this nearly shipped with

Chunking a merge looks trivial — call `copyPages()` on 40 pages at a time.
It is a trap. pdf-lib builds a **fresh `PDFObjectCopier` on every call**:

```js
copier = PDFObjectCopier.for(srcDoc.context, this.context)
```

That copier is what remembers "I already copied this font, this image". Slice
a 1,000-page document into 25 chunks and you get 25 copiers — and every
shared resource is re-embedded 25 times.

Measured on two 600-page documents sharing one font and one image:

| approach | output |
|---|---|
| whole-document `copyPages` | 1.1 MB |
| chunked, fresh copier per chunk | **11.5 MB  (+994%)** |
| chunked, one persistent copier | 1.1 MB  (+0%) |

The shipped code keeps **one copier alive per source document** and copies
through it chunk by chunk. Verified on the 2,000-page output: exactly **2
font objects across 2,000 pages**, correct page order, correct boundary at
page 1000/1001. If `PDFObjectCopier` is ever unavailable, it falls back to
the stock whole-document path rather than producing a bloated file.

---

## What changed — Images to PDF

The image list was a bare name-and-size row. It now behaves exactly like
Merge, which is what you asked for:

* **Thumbnail** of every image, plus pixel dimensions and file size.
* **Default order = the order you selected them.**
* **Drag to reorder** with the same grip and drop indicators as Merge.
* **Up / down arrows** on every row.
* **Include/exclude tick** — unticked images grey out and strike through, and
  stay in the list.
* **Delete** any single image; **Clear** removes all.
* **Rotate 90°** per image, applied to the exported page.
* **Sort by name** using natural ordering, so `2_scan` comes before
  `10_scan` rather than after it.
* **Reverse**, **Select all**, **Select none**.
* **Preview order** — every selected image in sequence, before converting.
* **Preview the PDF first** — renders the finished document page by page,
  then offers Download or Back to editing.
* **Page setup** — fit-each-image (default), A4 or Letter; auto/portrait/
  landscape; none/small/medium/large margin.
* Accepts **JPG, PNG, WebP, GIF, BMP**, and drag-drop onto the drop zone.
* Images are drawn through a canvas first, so EXIF rotation is honoured and
  formats pdf-lib cannot embed directly still work.
* Sizing uses 96 dpi → points, so a 1600 px image becomes a 1200 pt page
  rather than a 1600 pt one.

### Verified

Six images (JPG, PNG, WebP; portrait, landscape, square, tall, wide):
selection order preserved → reorder → exclude → rotate → natural-name sort
(`2_numeric` before `10_wide`) → delete → preview (5 pages) → download →
A4 + margin export. Page geometry checked against expected `px × 0.75`:
every page exact. Rendered contact sheet confirms no flips, no distortion,
correct orientation. **Zero JS errors.**

---

## Deploying

```bash
# beside index.html, after the v1.0.0 pack is already applied
python3 apply_merge_fix.py --check
python3 apply_merge_fix.py
python3 apply_merge_fix.py --revert     # byte-for-byte undo
```

Same applier as before, with its own markers (`ZENPDF-MERGE-IMG`), so the two
packs are independent — you can revert either without touching the other.

**Verified on your current `index 2.html`:** preflight passes, three
consecutive applies leave the file at a constant size, revert restores the
original byte-for-byte, all 12 inline scripts parse, both packs report
active, and geometry / merge / images-to-PDF all work together with no
errors.

---

## Known limits

* The memory ceiling is real and cannot be engineered away in-browser —
  pdf-lib has no streaming writer. Past roughly 800 MB of combined input on
  desktop the honest answer is "merge in halves", which the pre-flight now
  offers.
* `navigator.deviceMemory` is Chrome/Edge only. On Safari and Firefox the
  pre-flight falls back to a fixed 2.2 GB budget.
* Merge order still follows the workspace list, not a per-tool order.
