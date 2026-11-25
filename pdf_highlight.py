"""Locate passages in a PDF and turn them into Zotero highlight annotations.

Used by the `create_highlights` MCP tool. The caller passes the passages to
highlight as text. This module finds them in the PDF, tolerating differences
such as ligatures, line-end hyphenation, curly quotes and small typos. It
snaps each match to whole words and computes the page rectangles. A passage
that crosses a page break becomes one annotation per page, because Zotero
highlights are single-page.

Positions follow the format of highlights created in Zotero's own reader:
  annotationPosition  = {"pageIndex": n, "rects": [[x1, y1, x2, y2], ...]}
                        (PDF user space, origin bottom-left)
  annotationSortIndex = "PPPPP|OOOOOO|YYYYY" (page index | char offset | top)
"""
from __future__ import annotations

import bisect
import json
import re
import unicodedata
from collections import Counter, defaultdict

import pymupdf

pymupdf.TOOLS.set_small_glyph_heights(True)  # tight boxes: no overlap between lines

ZOTERO_COLORS = {
    "yellow": "#ffd400", "red": "#ff6666", "green": "#5fb236", "blue": "#2ea8e5",
    "purple": "#a28ae5", "magenta": "#e56eee", "orange": "#f19837", "gray": "#aaaaaa",
}
MAX_COST = 0.15  # max edit distance per character for a match to count as found


# --------------------------------------------------------------------------
# Broken ligature ToUnicode maps (e.g. Ghostscript output: "The" -> "he").
# Repaired in memory only, so that matching and annotationText are right.
# --------------------------------------------------------------------------
_STD_LIGS = {"fi": "fi", "fl": "fl", "ff": "ff", "ffi": "ffi", "ffl": "ffl", "ft": "ft",
             "st": "st", "uniFB00": "ff", "uniFB01": "fi", "uniFB02": "fl",
             "uniFB03": "ffi", "uniFB04": "ffl"}


def _lig_text(glyph):
    base, _, suffix = glyph.partition(".")
    if base in _STD_LIGS:
        txt = _STD_LIGS[base]
    elif "_" in base and all(len(p) == 1 and p.isalpha() for p in base.split("_")):
        txt = base.replace("_", "")
    else:
        return None
    return txt.lower() if suffix == "sc" else txt


def _parse_cmap(data):
    out = {}

    def u(h):
        return bytes.fromhex(h).decode("utf-16-be", errors="replace")

    for block in re.findall(r"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(r"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]*)>", block):
            out[int(src, 16)] = u(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, rest in re.findall(
                r"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*(\[[^\]]*\]|<[0-9a-fA-F]*>)", block):
            lo, hi = int(lo, 16), int(hi, 16)
            if rest.startswith("["):
                for k, d in enumerate(re.findall(r"<([0-9a-fA-F]*)>", rest)):
                    out[lo + k] = u(d)
            else:
                b = bytes.fromhex(rest[1:-1])
                start = int.from_bytes(b, "big")
                for k in range(hi - lo + 1):
                    out[lo + k] = (start + k).to_bytes(len(b), "big").decode("utf-16-be", "replace")
    return out


def _write_cmap(mapping):
    lines = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
             "1 begincodespacerange", "<00> <FF>", "endcodespacerange"]
    items = sorted(mapping.items())
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        lines += [f"<{c:02X}> <{t.encode('utf-16-be').hex().upper()}>" for c, t in chunk]
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode()


def fix_ligatures(doc) -> int:
    groups = defaultdict(list)
    for x in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(x, "Type") != ("name", "/Font"):
                continue
            kind, tu = doc.xref_get_key(x, "ToUnicode")
            if kind != "xref":
                continue
            kind, val = doc.xref_get_key(x, "Encoding/Differences")
        except Exception:
            continue
        if kind != "array":
            continue
        diffs, code = {}, 0
        for tok in re.findall(r"/[^\s/\[\]]+|\d+", val):
            if tok.startswith("/"):
                diffs[code] = tok[1:]
                code += 1
            else:
                code = int(tok)
        ligs = {c: _lig_text(g) for c, g in diffs.items() if _lig_text(g)}
        if ligs:
            groups[(int(tu.split()[0]), tuple(sorted(ligs.items())))].append(x)
    shared = Counter(tu for tu, _ in groups)
    fixed = 0
    for (tu_xref, ligs), fonts in groups.items():
        try:
            mapping = _parse_cmap(doc.xref_stream(tu_xref).decode("latin-1"))
        except Exception:
            continue
        if all(mapping.get(c) == t for c, t in ligs) or any(c > 0xFF for c in mapping):
            continue
        mapping.update(dict(ligs))
        data = _write_cmap(mapping)
        if shared[tu_xref] == 1:
            doc.update_stream(tu_xref, data)
        else:
            nx = doc.get_new_xref()
            doc.update_object(nx, "<<>>")
            doc.update_stream(nx, data)
            for f in fonts:
                doc.xref_set_key(f, "ToUnicode", f"{nx} 0 R")
        fixed += len(fonts)
    return fixed


# --------------------------------------------------------------------------
# Text model
# --------------------------------------------------------------------------
_SUP_RE = re.compile(r"^[\d,\-–*†‡§]+$")


def keyify(s: str) -> str:
    return "".join(c.lower() for c in unicodedata.normalize("NFKD", s) if c.isalnum())


class PdfText:
    """Reading-order text (R) of selected pages, mapped back to glyph boxes.
    Running headers/footers are skipped and footnote markers are kept out of R."""

    def __init__(self, doc, pages=None):
        self.doc = doc
        pages = sorted(pages) if pages is not None else list(range(len(doc)))
        self.stream = []          # (page, line_id, bbox, char, offset_on_page)
        R, rmap = [], []
        page_lines = {p: self._lines(doc[p]) for p in pages}
        heads = self._running_heads(page_lines, doc)
        line_id = 0
        for p in pages:
            offset = 0
            for li, (bbox, chars) in enumerate(page_lines[p]):
                if (p, li) in heads or not chars:
                    continue
                if R:
                    while R and R[-1] == " ":
                        R.pop(); rmap.pop()
                    first = next((c for _, c, s in chars if not s and not c.isspace()), "")
                    if R and R[-1] in "-­" and len(R) > 1 and R[-2].isalpha() and first.islower():
                        R.pop(); rmap.pop()       # line-end hyphenation
                    else:
                        R.append(" "); rmap.append(-1)
                for cb, ch, sup in chars:
                    self.stream.append((p, line_id, cb, ch, offset))
                    offset += 1
                    if sup:
                        continue                  # footnote marker: geometry only
                    if ch.isspace():
                        if R and R[-1] == " ":
                            continue
                        ch = " "
                    R.append(ch); rmap.append(len(self.stream) - 1)
                line_id += 1
        self.R = "".join(R)
        self.rmap = rmap
        K, kmap = [], []
        for i, ch in enumerate(self.R):
            for c in unicodedata.normalize("NFKD", ch):
                if c.isalnum():
                    K.append(c.lower()); kmap.append(i)
        self.K, self.kmap = "".join(K), kmap

    @staticmethod
    def _lines(page):
        out = []
        for b in page.get_text("rawdict")["blocks"]:
            for l in b.get("lines", []):
                spans = l["spans"]
                if not spans:
                    continue
                maxsize = max(s["size"] for s in spans)
                chars = []
                for s in spans:
                    txt = "".join(c["c"] for c in s["chars"])
                    sup = s["size"] < 0.8 * maxsize and bool(_SUP_RE.match(txt.strip()))
                    chars += [(pymupdf.Rect(c["bbox"]), c["c"], sup) for c in s["chars"]]
                out.append((pymupdf.Rect(l["bbox"]), chars))
        return out

    @staticmethod
    def _running_heads(page_lines, doc):
        cand, count = [], defaultdict(set)
        for p, lines in page_lines.items():
            h = doc[p].rect.height
            for li, (bbox, chars) in enumerate(lines):
                if bbox.y1 < 0.08 * h or bbox.y0 > 0.92 * h:
                    key = re.sub(r"\s+", " ", re.sub(r"\d+", "#", "".join(c for _, c, _ in chars).strip().lower()))
                    cand.append((p, li, key))
                    count[key].add(p)
        need = 3 if len(page_lines) >= 6 else 2
        return {(p, li) for p, li, k in cand if len(count[k]) >= need or re.fullmatch(r"\W*#\W*", k)}


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
def _fit_align(S, T):
    """Best substring T[a:b] matching all of S by edit distance -> (cost, a, b)."""
    n = len(T)
    prev, prev_st = [0] * (n + 1), list(range(n + 1))
    for i, si in enumerate(S, 1):
        cur, cur_st = [i] * (n + 1), [0] * (n + 1)
        for j in range(1, n + 1):
            c, st = prev[j - 1] + (si != T[j - 1]), prev_st[j - 1]
            if prev[j] + 1 < c:
                c, st = prev[j] + 1, prev_st[j]
            if cur[j - 1] + 1 < c:
                c, st = cur[j - 1] + 1, cur_st[j - 1]
            cur[j], cur_st[j] = c, st
        prev, prev_st = cur, cur_st
    j = min(range(n + 1), key=lambda j: (prev[j], -j))
    return prev[j], prev_st[j], j


def locate(pt: PdfText, text: str):
    """-> (cost_per_char, r_start, r_end) snapped to whole words, or None."""
    S, K = keyify(text), pt.K
    if len(S) < 4 or not K:
        return None
    k = 8 if len(S) >= 24 else max(4, len(S) // 3)
    votes = Counter()
    for i in range(0, len(S) - k + 1, 2):
        g = S[i:i + k]
        occ, p = [], K.find(g)
        while p != -1 and len(occ) <= 200:
            occ.append(p)
            p = K.find(g, p + 1)
        if occ and len(occ) <= 200:
            for p in occ:
                votes[(p - i) // 32] += 1.0 / len(occ)
    if not votes:
        return None
    best = None
    for bucket, _ in votes.most_common(3):
        lo = max(0, bucket * 32 - 64)
        hi = min(len(K), bucket * 32 + len(S) + 128)
        cost, a, b = _fit_align(S, K[lo:hi])
        if b > a and (best is None or cost < best[0]):
            best = (cost, lo + a, lo + b)
    if best is None:
        return None
    cost, a, b = best
    R = pt.R
    rs, re_ = pt.kmap[a], pt.kmap[b - 1] + 1
    while rs > 0 and not R[rs - 1].isspace():
        rs -= 1
    while re_ < len(R) and not R[re_].isspace():
        re_ += 1
    return cost / len(S), rs, re_


def _pdf_rect(page, r):
    """MuPDF (top-left origin) rect -> PDF user-space [x1, y1, x2, y2]."""
    q = r * ~page.transformation_matrix
    x0, x1 = sorted((q.x0, q.x1))
    y0, y1 = sorted((q.y0, q.y1))
    return [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)]


def page_parts(pt: PdfText, rs: int, re_: int):
    """Split R[rs:re_] into per-page parts with text, rects and sort index."""
    idx = [(k, pt.rmap[k]) for k in range(rs, re_) if pt.rmap[k] >= 0]
    if not idx:
        return []
    by_page = defaultdict(list)   # page -> R positions
    for k, si in idx:
        by_page[pt.stream[si][0]].append(k)
    a, b = idx[0][1], idx[-1][1]
    parts = []
    for pno in sorted(by_page):
        page = pt.doc[pno]
        lines, order, first = {}, [], None
        for si in range(a, b + 1):
            p, lid, bbox, ch, off = pt.stream[si]
            if p != pno or ch.isspace():
                continue
            if first is None:
                first = off
            if lid not in lines:
                order.append(lid)
                lines[lid] = pymupdf.Rect(bbox)
            else:
                lines[lid] |= bbox
        rects = [lines[l] for l in order]
        ks = by_page[pno]
        text = re.sub(r"\s+", " ", pt.R[ks[0]:ks[-1] + 1]).strip()
        top = min(r.y0 for r in rects)
        parts.append(dict(
            pageIndex=pno,
            pageLabel=page.get_label() or str(pno + 1),
            text=text,
            rects=[_pdf_rect(page, r) for r in rects],
            sortIndex=f"{pno:05d}|{min(first or 0, 999999):06d}|{min(int(max(top, 0)), 99999):05d}",
        ))
    return parts


def find_passages(pdf_bytes: bytes, passages: list[dict]) -> list[dict]:
    """Locate each passage ({"text", "page"?}) -> list of results with per-page parts."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    fix_ligatures(doc)
    labels = [doc[i].get_label() or str(i + 1) for i in range(len(doc))]
    by_label = defaultdict(list)
    for i, lab in enumerate(labels):
        by_label[lab].append(i)

    # With page hints for every passage, only extract the pages around them (fast on big books)
    subset = None
    hints = [str(p.get("page") or "").strip() for p in passages]
    if hints and all(h in by_label for h in hints):
        subset = set()
        for h in hints:
            for i in by_label[h]:
                subset |= {j for j in range(i - 1, i + 3) if 0 <= j < len(doc)}
    full = None
    pt = PdfText(doc, subset) if subset else None

    results = []
    for p in passages:
        loc = locate(pt, p["text"]) if pt else None
        if loc is None or loc[0] > MAX_COST:
            if full is None:
                full = pt if subset is None and pt is not None else PdfText(doc)
            loc, used = locate(full, p["text"]), full
        else:
            used = pt
        if loc is None or loc[0] > MAX_COST:
            results.append(dict(found=False, cost=loc and round(loc[0], 3)))
            continue
        cost, rs, re_ = loc
        results.append(dict(found=True, cost=round(cost, 3), matched=used.R[rs:re_],
                            parts=page_parts(used, rs, re_)))
    return results


def color_hex(c: str | None) -> str:
    if not c:
        return ZOTERO_COLORS["yellow"]
    c = c.strip().lower()
    if c in ZOTERO_COLORS:
        return ZOTERO_COLORS[c]
    if re.fullmatch(r"#[0-9a-f]{6}", c):
        return c
    raise ValueError(f"unknown colour {c!r}; use a #rrggbb value or one of {', '.join(ZOTERO_COLORS)}")


def annotation_payload(attachment_key, part, color, comment="", tags=()):
    return {
        "itemType": "annotation",
        "parentItem": attachment_key,
        "annotationType": "highlight",
        "annotationText": part["text"],
        "annotationComment": comment or "",
        "annotationColor": color,
        "annotationPageLabel": part["pageLabel"],
        "annotationSortIndex": part["sortIndex"],
        "annotationPosition": json.dumps({"pageIndex": part["pageIndex"], "rects": part["rects"]},
                                         separators=(",", ":")),
        "tags": [{"tag": t} for t in tags],
    }
