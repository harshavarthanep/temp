#!/usr/bin/env python3
"""
apply_merge_fix.py — production patch applier for ZenPDF v1.1.0
==========================================================

Injects the rotation & geometry fix pack into index.html, idempotently
and reversibly. Safe to run in GitHub Codespaces or any CI step.

WHAT IT ADDS
    * Merge: chunked page copying with a persistent PDFObjectCopier, so
      a large merge keeps the tab responsive and shows real progress
      WITHOUT re-embedding shared fonts and images per chunk.
    * Merge: memory pre-flight, cancel, and per-file fault tolerance.
    * Images to PDF: a full merge-style workspace — thumbnails, drag to
      reorder, include/exclude, rotate, natural-name sort, whole-set
      preview, page setup, and a preview of the finished PDF.

    Apply this AFTER the v1.0.0 geometry pack. The two are independent
    and can be reverted separately.

USAGE
    python3 apply_merge_fix.py                     # patch ./index.html
    python3 apply_merge_fix.py --html path/to/index.html
    python3 apply_merge_fix.py --check             # report only, no write
    python3 apply_merge_fix.py --revert            # remove the patch
    python3 apply_merge_fix.py --diagnose file.pdf # is this PDF affected?

EXIT CODES
    0 success / already applied      1 error      2 --check found work to do
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import re
import shutil
import sys
from pathlib import Path

PATCH_FILE = "zenpdf-merge-images-fix.js"
BEGIN = "<!-- ZENPDF-MERGE-IMG:BEGIN v1.1.0 -->"
END = "<!-- ZENPDF-MERGE-IMG:END -->"
MARKER = "__zenMergeImgFix"

C = {
    "g": "\033[32m", "r": "\033[31m", "y": "\033[33m",
    "b": "\033[1m", "d": "\033[2m", "x": "\033[0m",
}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def say(sym: str, msg: str, col: str = "x") -> None:
    print(f"{C[col]}{sym}{C['x']} {msg}")


# ----------------------------------------------------------------------
# preflight
# ----------------------------------------------------------------------
def preflight(html: str) -> list[str]:
    """Confirm the file really is the ZenPDF app before touching it."""
    problems: list[str] = []
    required = {
        "pdf-lib":                r"pdf-lib(?:@[\d.]+)?[/.]",
        "pdf.js":                 r"pdf\.js/[\d.]+/pdf\.min\.js|pdfjsLib",
        "buildEditedPdfBytes()":  r"function\s+buildEditedPdfBytes|buildEditedPdfBytes\s*=",
        "editor state object":    r"\bed\.pages\b",
        "PDFLib destructure":     r"const\s*\{\s*PDFDocument\s*,\s*rgb\s*,\s*degrees",
        "closing </body>":        r"</body>",
    }
    for label, pat in required.items():
        if not re.search(pat, html):
            problems.append(f"expected to find {label} and did not")
    return problems


def detect_state(html: str) -> str:
    if BEGIN in html and END in html:
        return "applied"
    if MARKER in html:
        return "partial"
    return "clean"


# ----------------------------------------------------------------------
# apply / revert
# ----------------------------------------------------------------------
def _strip_block(html: str) -> str:
    """Remove the patch block AND the newline the injector added, so a
    revert restores the file byte-for-byte."""
    return re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?",
                  "", html, flags=re.S)


def build_block(js: str) -> str:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    digest = hashlib.sha256(js.encode("utf-8")).hexdigest()[:12]
    return (
        f"{BEGIN}\n"
        f"<!-- applied {stamp} | sha256:{digest}\n"
        f"     revert with:  python3 apply_zenpdf_fix.py --revert -->\n"
        f"<script>\n{js}\n</script>\n"
        f"{END}\n"
    )


def apply(html_path: Path, patch_path: Path, dry: bool) -> int:
    html = html_path.read_text(encoding="utf-8")

    problems = preflight(html)
    if problems:
        say("x", f"{html_path} does not look like the ZenPDF app:", "r")
        for p in problems:
            print(f"    - {p}")
        say("!", "refusing to patch. Point --html at the right file.", "y")
        return 1
    say("+", "preflight passed — this is the ZenPDF app", "g")

    state = detect_state(html)
    if state == "applied":
        say("=", "patch already applied — replacing it with this version", "y")
        html = _strip_block(html)
    elif state == "partial":
        say("!", f"found a bare {MARKER} guard but no patch markers.", "y")
        say(" ", "  A previous hand-paste is present. Remove it first, or the")
        say(" ", "  guard will make this patch no-op. Aborting.")
        return 1

    js = patch_path.read_text(encoding="utf-8")
    block = build_block(js)

    # inject immediately before the LAST </body>
    idx = html.rfind("</body>")
    if idx == -1:
        say("x", "no </body> found", "r")
        return 1
    out = html[:idx] + block + html[idx:]

    if dry:
        say("i", f"--check: would insert {len(js):,} bytes before </body>", "y")
        return 2

    backup = html_path.with_suffix(
        html_path.suffix + "." + _dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak")
    shutil.copy2(html_path, backup)
    say("+", f"backup written -> {backup.name}", "d")

    html_path.write_text(out, encoding="utf-8")
    say("+", f"patched {html_path.name}  (+{len(js):,} bytes)", "g")

    verify = html_path.read_text(encoding="utf-8")
    ok = (BEGIN in verify and END in verify
          and verify.count(MARKER) >= 1
          and verify.rstrip().endswith("</html>"))
    if not ok:
        say("x", "post-write verification failed — restoring backup", "r")
        shutil.copy2(backup, html_path)
        return 1
    say("+", "verified: markers present, document still well-formed", "g")
    return 0


def revert(html_path: Path) -> int:
    html = html_path.read_text(encoding="utf-8")
    if BEGIN not in html:
        say("=", "patch is not present — nothing to revert", "y")
        return 0
    backup = html_path.with_suffix(html_path.suffix + ".prerevert.bak")
    shutil.copy2(html_path, backup)
    out = _strip_block(html)
    html_path.write_text(out, encoding="utf-8")
    say("+", f"patch removed (backup: {backup.name})", "g")
    return 0


# ----------------------------------------------------------------------
# diagnose — tell the user WHICH of their PDFs will misalign
# ----------------------------------------------------------------------
def diagnose(paths: list[str]) -> int:
    """Report what a merge of these files would cost in memory.

    Peak JS heap while merging in Chromium was measured at roughly 3.2x
    the combined input size (196MB->0.70GB, 392MB->1.30GB, 784MB->2.40GB).
    Page COUNT is not the limit; total BYTES are.
    """
    try:
        import pikepdf
    except ImportError:
        say("x", "pip install pikepdf   (needed only for --diagnose)", "r")
        return 1

    total_pages = total_bytes = 0
    rc = 0
    for path in paths:
        p = Path(path)
        if not p.exists():
            say("x", f"{p}: not found", "r"); rc = 1; continue
        try:
            pdf = pikepdf.open(str(p))
        except Exception as e:
            say("x", f"{p.name}: cannot open — {e}", "r"); rc = 1; continue
        n = len(pdf.pages)
        b = p.stat().st_size
        rot = sum(1 for pg in pdf.pages if int(pg.get("/Rotate", 0) or 0) % 360)
        total_pages += n; total_bytes += b
        print(f"  {p.name[:44]:<44} {n:>6} pages  {b/1048576:>7.1f} MB"
              + (f"  ({rot} rotated)" if rot else ""))

    if not total_pages:
        return rc

    est = total_bytes * 3.2 / (1024 ** 3)
    print()
    say("i", f"combined: {total_pages:,} pages, {total_bytes/1048576:.0f} MB", "b")
    say("i", f"estimated peak browser memory: {est:.1f} GB "
             f"(measured model: 3.2x input size)")
    if est > 3.0:
        say("!", "Too large for one pass on most machines — merge in halves.", "r")
        rc = max(rc, 2)
    elif est > 1.6:
        say("!", "Fine on a desktop; likely to fail on a phone or a 4 GB laptop.", "y")
    else:
        say("+", "Comfortably within a normal browser tab.", "g")
    say(" ", "  Page count is not the limit — total bytes are.")
    return rc


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Apply the ZenPDF rotation & geometry fix pack.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html", default="index.html", help="path to index.html")
    ap.add_argument("--patch", default=PATCH_FILE, help=f"path to {PATCH_FILE}")
    ap.add_argument("--check", action="store_true", help="report only, do not write")
    ap.add_argument("--revert", action="store_true", help="remove a previously applied patch")
    ap.add_argument("--diagnose", nargs="+", metavar="PDF",
                    help="report page count, size and the memory a merge would need")
    a = ap.parse_args()

    print(f"{C['b']}ZenPDF merge + image pack applier v1.1.0{C['x']}")

    if a.diagnose:
        return diagnose(a.diagnose)

    html_path = Path(a.html)
    if not html_path.exists():
        say("x", f"{html_path} not found (use --html to point at it)", "r")
        return 1

    if a.revert:
        return revert(html_path)

    patch_path = Path(a.patch)
    if not patch_path.exists():
        say("x", f"{patch_path} not found — keep it beside this script", "r")
        return 1

    return apply(html_path, patch_path, a.check)


if __name__ == "__main__":
    sys.exit(main())
