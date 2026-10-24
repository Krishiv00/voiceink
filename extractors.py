import io
import re
from dataclasses import dataclass
from typing import Callable, Dict, List
import docx
from pypdf import PdfReader


def extract_docx(file_bytes: bytes) -> List[dict]:
    def _looks_like_list_paragraph(para) -> bool:
        try:
            pPr = para._p.pPr
            return pPr is not None and pPr.numPr is not None
        except AttributeError:
            return False

    document = docx.Document(io.BytesIO(file_bytes))
    blocks: List[dict] = []

    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        style_name = (para.style.name or "").lower() if para.style else ""

        if style_name.startswith("heading"):
            level = None
            digits = "".join(ch for ch in style_name if ch.isdigit())
            if digits:
                level = int(digits)
            blocks.append({"type": "heading", "text": text, "level": level})

        elif style_name.startswith("quote") or style_name.startswith("intense quote"):
            blocks.append({"type": "quote", "text": text, "level": None})

        elif style_name.startswith("list") or _looks_like_list_paragraph(para):
            blocks.append({"type": "list_item", "text": text, "level": None})

        else:
            blocks.append({"type": "paragraph", "text": text, "level": None})

    return blocks


_PDF_BULLET_PREFIX_RE = re.compile(r"^[\-\*\u2022\u25CF\u25E6\u2023\u2043]\s+")
_PDF_NUMBERED_PREFIX_RE = re.compile(r"^(?:\d+|[a-zA-Z])[.)]\s+")


@dataclass
class _PdfLine:
    text: str
    y: float
    x: float
    size: float
    font: str
    page: int

    @property
    def is_bold(self) -> bool:
        f = (self.font or "").lower()
        return "bold" in f or "black" in f or "heavy" in f or "semibold" in f

    @property
    def is_italic(self) -> bool:
        f = (self.font or "").lower()
        return "italic" in f or "oblique" in f


def _collect_pdf_lines(reader: "PdfReader") -> List[_PdfLine]:
    lines: List[_PdfLine] = []

    for page_index, page in enumerate(reader.pages):
        page_fragments: List[_PdfLine] = []

        def visitor(text, cm, tm, font_dict, font_size, _page_fragments=page_fragments, _page_index=page_index):
            if not text or not text.strip("\n"):
                return
            fontname = font_dict.get("/BaseFont", "") if font_dict else ""
            _page_fragments.append(_PdfLine(
                text=text,
                y=round(tm[5], 1),
                x=tm[4],
                size=font_size or 0.0,
                font=fontname,
                page=_page_index,
            ))

        try:
            page.extract_text(visitor_text=visitor)
        except Exception:
            continue

        page_fragments.sort(key=lambda f: (-f.y, f.x))
        grouped: List[List[_PdfLine]] = []
        for frag in page_fragments:
            if grouped and abs(grouped[-1][0].y - frag.y) <= 1.5:
                grouped[-1].append(frag)
            else:
                grouped.append([frag])

        for group in grouped:
            group.sort(key=lambda f: f.x)
            text = "".join(f.text for f in group).replace("\n", " ")
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue

            dominant = max(group, key=lambda f: f.size)
            lines.append(_PdfLine(
                text=text,
                y=group[0].y,
                x=group[0].x,
                size=dominant.size,
                font=dominant.font,
                page=page_index,
            ))

    return lines


def _pdf_body_size(lines: List[_PdfLine]) -> float:
    if not lines:
        return 11.0
    counts: dict[float, int] = {}
    for ln in lines:
        key = round(ln.size, 1)
        counts[key] = counts.get(key, 0) + 1
    return max(counts, key=counts.get)


def _pdf_heading_levels(lines: List[_PdfLine], body_size: float) -> dict:
    bigger_sizes = sorted({round(ln.size, 1) for ln in lines if ln.size > body_size + 0.5}, reverse=True)
    level_for_size: dict[float, int] = {}
    level = 0
    group_anchor = None
    for size in bigger_sizes:
        if group_anchor is None or group_anchor - size > 0.5:
            level += 1
            group_anchor = size
        level_for_size[size] = level
    return level_for_size


def extract_pdf(file_bytes: bytes) -> List[dict]:
    reader = PdfReader(io.BytesIO(file_bytes))
    raw_lines = _collect_pdf_lines(reader)
    if not raw_lines:
        return []

    body_size = _pdf_body_size(raw_lines)
    heading_level_for_size = _pdf_heading_levels(raw_lines, body_size)

    body_line_gaps = []
    for page_lines in _group_by_page(raw_lines):
        for prev, cur in zip(page_lines, page_lines[1:]):
            if abs(prev.size - body_size) < 0.5 and abs(cur.size - body_size) < 0.5:
                gap = prev.y - cur.y
                if 0 < gap < body_size * 3:
                    body_line_gaps.append(gap)
    typical_gap = sorted(body_line_gaps)[len(body_line_gaps) // 2] if body_line_gaps else body_size * 1.3

    blocks: List[dict] = []
    prev_line: _PdfLine | None = None
    in_quote = False

    def _close_quote_if_open():
        nonlocal in_quote
        in_quote = False

    for i, ln in enumerate(raw_lines):
        text = ln.text
        new_page = prev_line is not None and ln.page != prev_line.page
        same_block_gap = (
            not new_page
            and prev_line is not None
            and 0 < (prev_line.y - ln.y) <= typical_gap * 1.25
        )

        bullet_m = _PDF_BULLET_PREFIX_RE.match(text)
        numbered_m = _PDF_NUMBERED_PREFIX_RE.match(text)
        is_heading_size = round(ln.size, 1) in heading_level_for_size
        is_quote_line = ln.is_italic and not is_heading_size

        if bullet_m or numbered_m:
            _close_quote_if_open()
            clean = (bullet_m or numbered_m).re.sub("", text, count=1).strip()
            blocks.append({"type": "list_item", "text": clean, "level": None})
            prev_line = ln
            continue

        if (
            same_block_gap
            and blocks
            and blocks[-1]["type"] == "list_item"
            and not is_heading_size
            and not is_quote_line
        ):
            blocks[-1]["text"] = (blocks[-1]["text"] + " " + text).strip()
            prev_line = ln
            continue

        if is_heading_size:
            _close_quote_if_open()
            level = heading_level_for_size[round(ln.size, 1)]
            if (
                blocks
                and blocks[-1]["type"] == "heading"
                and blocks[-1]["level"] == level
                and same_block_gap
            ):
                blocks[-1]["text"] = (blocks[-1]["text"] + " " + text).strip()
            else:
                blocks.append({"type": "heading", "text": text, "level": level})
            prev_line = ln
            continue

        if is_quote_line:
            if (
                in_quote
                and blocks
                and blocks[-1]["type"] == "quote"
                and same_block_gap
            ):
                blocks[-1]["text"] = (blocks[-1]["text"] + " " + text).strip()
            else:
                blocks.append({"type": "quote", "text": text, "level": None})
                in_quote = True
            prev_line = ln
            continue

        _close_quote_if_open()
        if (
            same_block_gap
            and blocks
            and blocks[-1]["type"] == "paragraph"
        ):
            blocks[-1]["text"] = (blocks[-1]["text"] + " " + text).strip()
        else:
            blocks.append({"type": "paragraph", "text": text, "level": None})
        prev_line = ln

    return blocks


def _group_by_page(lines: List[_PdfLine]) -> List[List[_PdfLine]]:
    pages: dict[int, List[_PdfLine]] = {}
    for ln in lines:
        pages.setdefault(ln.page, []).append(ln)
    return [pages[k] for k in sorted(pages)]


def extract_plain_text(text: str) -> List[dict]:
    def _starts_with_number_dot(line: str) -> bool:
        head = line.split(".", 1)[0]
        return head.isdigit() and "." in line[: len(head) + 1]

    blocks: List[dict] = []
    for raw_para in text.split("\n\n"):
        raw_para = raw_para.strip()
        if not raw_para:
            continue
        for line in raw_para.split("\n"):
            line = line.strip()
            if not line:
                continue
            stripped = line.lstrip("-*• ")
            if line[:2] in ("- ", "* ", "• ") or _starts_with_number_dot(line):
                blocks.append({"type": "list_item", "text": stripped, "level": None})
            elif (line.startswith('"') and line.endswith('"')) or (
                line.startswith("\u201c") and line.endswith("\u201d")
            ):
                blocks.append({"type": "quote", "text": line.strip('"\u201c\u201d'), "level": None})
            else:
                blocks.append({"type": "paragraph", "text": line, "level": None})
    return blocks


def _decode_bytes(file_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("latin-1")


def extract_txt(file_bytes: bytes) -> List[dict]:
    return extract_plain_text(_decode_bytes(file_bytes))


def extract_md(file_bytes: bytes) -> List[dict]:
    _MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
    _MD_BULLET_RE = re.compile(r"^[\-\*\+]\s+(.*)$")
    _MD_NUMBERED_RE = re.compile(r"^\d+[.)]\s+(.*)$")
    _MD_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")
    _MD_INLINE_RE = re.compile(
        r"\*\*(.+?)\*\*"
        r"|__(.+?)__"
        r"|\*(.+?)\*"
        r"|_(.+?)_"
        r"|`(.+?)`"
        r"|\[(.+?)\]\([^)]*\)"
    )

    def _md_strip_inline(text: str) -> str:
        def _repl(m: "re.Match") -> str:
            return next((g for g in m.groups() if g is not None), "")
        prev = None

        while prev != text:
            prev = text
            text = _MD_INLINE_RE.sub(_repl, text)
        return text

    blocks: List[dict] = []
    in_code_fence = False
    paragraph_lines: List[str] = []

    def _flush_paragraph():
        if paragraph_lines:
            text = _md_strip_inline(" ".join(paragraph_lines)).strip()
            if text:
                blocks.append({"type": "paragraph", "text": text, "level": None})
            paragraph_lines.clear()

    for raw_line in _decode_bytes(file_bytes).split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_fence = not in_code_fence
            _flush_paragraph()
            continue
        if in_code_fence:
            continue

        if not stripped:
            _flush_paragraph()
            continue

        m = _MD_HEADING_RE.match(stripped)
        if m:
            _flush_paragraph()
            level = len(m.group(1))
            text = _md_strip_inline(m.group(2)).strip()
            if text:
                blocks.append({"type": "heading", "text": text, "level": level})
            continue

        m = _MD_BULLET_RE.match(stripped) or _MD_NUMBERED_RE.match(stripped)
        if m:
            _flush_paragraph()
            text = _md_strip_inline(m.group(1)).strip()
            if text:
                blocks.append({"type": "list_item", "text": text, "level": None})
            continue

        m = _MD_BLOCKQUOTE_RE.match(stripped)
        if m:
            _flush_paragraph()
            text = _md_strip_inline(m.group(1)).strip()
            if text:
                blocks.append({"type": "quote", "text": text, "level": None})
            continue

        if re.fullmatch(r"([-*_])\1{2,}", stripped):
            _flush_paragraph()
            continue

        paragraph_lines.append(stripped)

    _flush_paragraph()
    return blocks


EXTRACTORS: Dict[str, Callable[[bytes], List[dict]]] = {
    "txt": extract_txt,
    "docx": extract_docx,
    "pdf": extract_pdf,
    "md": extract_md,
}


def extract_file(filename: str, file_bytes: bytes) -> List[dict]:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in EXTRACTORS:
        supported = ", ".join(sorted(EXTRACTORS))
        raise ValueError(f"Unsupported file type '.{ext}'. Supported: {supported}")
    return EXTRACTORS[ext](file_bytes)
