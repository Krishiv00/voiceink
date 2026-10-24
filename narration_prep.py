import re
from dataclasses import dataclass
from typing import List

PUNCT_NAMES = {
    ",": "comma",
    ".": "full stop",
    "...": "ellipsis",
    "\u2026": "ellipsis",
    "?": "question mark",
    "!": "exclamation mark",
    ":": "colon",
    ";": "semicolon",
    "\u201c": "open quote",
    "\u201d": "close quote",
    "\u2018": "open single quote",
    "\u2019": "close single quote",
    "(": "open bracket",
    ")": "close bracket",
    "[": "open square bracket",
    "]": "close square bracket",
    "-": "dash",
    "--": "dash",
    "\u2013": "dash",
    "\u2014": "dash",
}

OPENERS = {"(", "[", "\u201c", "\u2018"}
CLOSERS = {")", "]", "\u201d", "\u2019"}

HARD_BREAK = {".", "...", "\u2026", "?", "!", ":", ";"}

TOKEN_RE = re.compile(
    r"[0-9]+(?:\.[0-9]+)+"
    r"|[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?"
    r"|[^\W_]+(?:['\u2019][^\W_]+)*(?:-[^\W_]+)*"
    r"|\.\.\.|\u2026"
    r"|--|[\u2013\u2014]"
    r"|(?<=\s)-(?=\s)"
    r"|[,.?!:;\"\u201c\u201d\u2018\u2019'()\[\]]"
)

CLUSTER_MIN_CHARS = 15
CLUSTER_MAX_CHARS = 25


@dataclass
class NarrationUnit:
    spoken_text: str
    display_text: str
    kind: str
    pause_after_chars: int = 0
    block_index: int = 0
    is_new_block: bool = False
    spoken_segments: List[tuple] = None

    def __post_init__(self):
        if self.spoken_segments is None:
            self.spoken_segments = [(self.spoken_text, False)] if self.spoken_text else []


STRUCTURE_CUES = {
    "paragraph": "New paragraph.",
    "heading": "Heading.",
    "list_item": "List item.",
    "quote_start": "Quote start.",
    "quote_end": "Quote end.",
}

HEADING_LEVEL_CUE_TEMPLATE = "Heading level {level}."

CUSTOMIZABLE_PUNCT_KEYS = [",", ".", "...", "?", "!", ":", ";", "\u2014", "(", ")"]


def apply_punctuation_overrides(overrides: dict) -> dict:
    merged = dict(PUNCT_NAMES)
    for key, value in (overrides or {}).items():
        if key in merged:
            merged[key] = value if value else ""
    return merged


def apply_structure_cue_overrides(overrides: dict) -> dict:
    merged = dict(STRUCTURE_CUES)
    for key, value in (overrides or {}).items():
        if key in merged and isinstance(value, str):
            merged[key] = value
    return merged


def _quote_is_opening(prev: str, nxt: str) -> bool:
    if prev.isspace() or prev in "([\u2014\u2013-\u2018":
        return True
    return (not prev.isalnum()) and nxt.isalnum()


def _tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    single_open = False
    for m in TOKEN_RE.finditer(text):
        tok = m.group(0)
        prev = text[m.start() - 1] if m.start() > 0 else " "
        nxt = text[m.end()] if m.end() < len(text) else " "

        if tok == '"':
            tok = "\u201c" if _quote_is_opening(prev, nxt) else "\u201d"
        elif tok in ("'", "\u2018", "\u2019"):
            if tok == "\u2018" or (_quote_is_opening(prev, nxt) and nxt.isalnum()):
                tok = "\u2018"
                single_open = True
            elif single_open and not prev.isspace():
                tok = "\u2019"
                single_open = False
            else:
                continue
        tokens.append(tok)
    return tokens


def _display_form(token: str) -> str:
    return token


def _letter_count(tokens: List[str]) -> int:
    return sum(len(t) for t in tokens if t not in PUNCT_NAMES)


def _split_on_punctuation(tokens: List[str]) -> List[List[str]]:
    pieces: List[List[str]] = []
    current: List[str] = []
    current_chars = 0
    n = len(tokens)
    i = 0

    while i < n:
        tok = tokens[i]

        if (
            tok in OPENERS
            and current_chars >= CLUSTER_MIN_CHARS
            and current
        ):
            pieces.append(current)
            current = []
            current_chars = 0

        current.append(tok)
        if tok not in PUNCT_NAMES:
            current_chars += len(tok)
        i += 1

        if tok in HARD_BREAK and current_chars > 0:
            while i < n and (tokens[i] in CLOSERS or tokens[i] in HARD_BREAK):
                current.append(tokens[i])
                i += 1
            pieces.append(current)
            current = []
            current_chars = 0
        elif (
            tok in PUNCT_NAMES
            and tok not in HARD_BREAK
            and tok not in OPENERS
            and current_chars >= CLUSTER_MIN_CHARS
        ):
            pieces.append(current)
            current = []
            current_chars = 0

    if current:
        pieces.append(current)

    return pieces


def _merge_tiny_pieces(pieces: List[List[str]]) -> List[List[str]]:
    merged: List[List[str]] = []
    for piece in pieces:
        chars = _letter_count(piece)
        is_tiny = chars == 0 or chars < CLUSTER_MIN_CHARS // 2
        if is_tiny and merged and not _ends_with_hard_break(merged[-1]):
            merged[-1].extend(piece)
        else:
            merged.append(piece)

    if len(merged) >= 2 and _letter_count(merged[0]) < CLUSTER_MIN_CHARS // 2:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)
    return merged


def _ends_with_hard_break(cluster: List[str]) -> bool:
    for tok in reversed(cluster):
        if tok in HARD_BREAK:
            return True
        if tok not in CLOSERS:
            return False
    return False


def _split_point(tokens: List[str]) -> int:
    total = _letter_count(tokens)
    half = total / 2.0
    running = 0
    best_i = None
    best_diff = None
    for idx, tok in enumerate(tokens):
        if tok not in PUNCT_NAMES:
            running += len(tok)
        if 0 < idx < len(tokens) - 1:
            diff = abs(running - half)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_i = idx + 1
    return best_i if best_i is not None else max(1, len(tokens) // 2)


def _rebalance_piece(tokens: List[str]) -> List[List[str]]:
    if _letter_count(tokens) <= CLUSTER_MAX_CHARS or len(tokens) <= 1:
        return [tokens]

    split_i = _split_point(tokens)
    if split_i <= 0 or split_i >= len(tokens):
        return [tokens]

    left, right = tokens[:split_i], tokens[split_i:]
    if not left or not right:
        return [tokens]

    return _rebalance_piece(left) + _rebalance_piece(right)


def _clusters_for_block_text(text: str) -> List[List[str]]:
    tokens = _tokenize(text)
    if not tokens:
        return []

    pieces = _split_on_punctuation(tokens)
    pieces = _merge_tiny_pieces(pieces)

    clusters: List[List[str]] = []
    for piece in pieces:
        clusters.extend(_rebalance_piece(piece))
    return clusters


NO_SPACE_BEFORE = {",", ".", "...", "\u2026", "?", "!", ":", ";", ")", "]", "\u201d", "\u2019"}
NO_SPACE_AFTER = {"(", "[", "\u201c", "\u2018"}


def _cluster_to_spoken_segments(tokens: List[str], punct_names: dict = None) -> List[tuple]:
    punct_names = punct_names if punct_names is not None else PUNCT_NAMES
    segments: List[tuple] = []
    word_run: List[str] = []

    def _flush():
        if word_run:
            segments.append((" ".join(word_run), False))
            word_run.clear()

    for tok in tokens:
        if tok in punct_names:
            _flush()
            segments.append((punct_names[tok], True))
        else:
            word_run.append(tok)
    _flush()
    return segments


def _cluster_to_text(tokens: List[str], form_fn) -> str:
    is_display = form_fn is _display_form
    out = ""
    prev = None
    for tok in tokens:
        piece = form_fn(tok)
        if prev is None:
            out = piece
        elif is_display and (tok in NO_SPACE_BEFORE or prev in NO_SPACE_AFTER):
            out += piece
        else:
            out += " " + piece
        prev = tok
    return out


def build_narration_units(
    blocks: List[dict],
    punct_names: dict = None,
    structure_cues: dict = None,
    heading_level_cue_template: str = None,
) -> List[NarrationUnit]:
    punct_names = punct_names if punct_names is not None else PUNCT_NAMES
    structure_cues = structure_cues if structure_cues is not None else STRUCTURE_CUES
    heading_level_cue_template = (
        heading_level_cue_template if heading_level_cue_template is not None else HEADING_LEVEL_CUE_TEMPLATE
    )

    units: List[NarrationUnit] = []
    in_quote = False

    for block_index, block in enumerate(blocks):
        btype = block["type"]
        text = block["text"]

        if btype == "heading":
            level = block.get("level")
            if level:
                try:
                    cue_text = heading_level_cue_template.format(level=level)
                except (KeyError, IndexError, ValueError):
                    cue_text = HEADING_LEVEL_CUE_TEMPLATE.format(level=level)
            else:
                cue_text = structure_cues["heading"]
            units.append(NarrationUnit(cue_text, "", "cue", 0, block_index, True))
        elif btype == "list_item":
            units.append(NarrationUnit(structure_cues["list_item"], "", "cue", 0, block_index, True))
        elif btype == "quote":
            if not in_quote:
                units.append(NarrationUnit(structure_cues["quote_start"], "", "cue", 0, block_index, True))
                in_quote = True
        else:
            if in_quote:
                units.append(NarrationUnit(structure_cues["quote_end"], "", "cue", 0, block_index, True))
                in_quote = False
            units.append(NarrationUnit(structure_cues["paragraph"], "", "cue", 0, block_index, True))

        clusters = _clusters_for_block_text(text)
        for i, cluster_tokens in enumerate(clusters):
            spoken = _cluster_to_text(cluster_tokens, lambda tok: punct_names.get(tok, tok))
            display = _cluster_to_text(cluster_tokens, _display_form)
            letter_chars = sum(len(t) for t in cluster_tokens if t not in punct_names)
            units.append(NarrationUnit(
                spoken_text=spoken,
                display_text=display,
                kind="cluster",
                pause_after_chars=letter_chars,
                block_index=block_index,
                is_new_block=(i == 0 and btype not in ("heading", "list_item", "quote")),
                spoken_segments=_cluster_to_spoken_segments(cluster_tokens, punct_names),
            ))

    if in_quote:
        units.append(NarrationUnit(structure_cues["quote_end"], "", "cue", 0, len(blocks) - 1, True))

    return units


def pause_seconds_for_chars(char_count: int, chars_per_minute: float) -> float:
    if chars_per_minute <= 0:
        chars_per_minute = 150.0

    sec_per_char = 60.0 / chars_per_minute
    pause = char_count * sec_per_char

    return pause


CUE_BREATH_PAUSE_SECONDS = 0.5
