"""Inventory, rewrite, and validate programming code inside Anki decks."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

FIELD_SEPARATOR = "\x1f"
CODE_BLOCK_RE = re.compile(r"<pre\b[^>]*>(?P<body>.*?)</pre>", re.IGNORECASE | re.DOTALL)
INLINE_CODE_RE = re.compile(r"<code\b[^>]*>(?P<body>.*?)</code>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
LANGUAGE_RE = re.compile(r"(?:language|lang|highlight)[-_]([a-zA-Z0-9+#-]+)", re.IGNORECASE)
COURIER_P_RE = re.compile(r"<p\b[^>]*>(?P<body>.*?)</p>", re.IGNORECASE | re.DOTALL)
CODEMIRROR_PRE_RE = re.compile(
    r"<pre\b[^>]*\bCodeMirror-line\b[^>]*>(?P<body>.*?)</pre>", re.IGNORECASE | re.DOTALL
)
_BLANK_TEXT_RE = re.compile(r"^[\s\u200b\xa0]*$")


def _is_courier_paragraph(match: re.Match[str]) -> bool:
    opening = match.group(0)
    opening = opening[: opening.find(">") + 1]
    if "Courier" in opening:
        return True
    # Coding deck solutions use p6-p13 (bare or styled); p1-p5 and
    # classless paragraphs are headers/labels/prose across the deck.
    if re.search(r'class="p(?:[6-9]|1[0-3])"', opening):
        return True
    return (
        'class="p2"' in opening
        or 'class="p3"' in opening
        or 'class="p4"' in opening
    )


def _courier_text(body: str) -> str:
    return html.unescape(TAG_RE.sub("", body)).strip().strip("\u200b").strip()


def extract_courier_blocks(field: str) -> list[tuple[str, str]]:
    """Group consecutive Courier <p> paragraphs into (source_html, source_text).

    Blank paragraphs and non-Courier paragraphs end a block.
    """
    blocks: list[tuple[str, str]] = []
    cur_spans: list[tuple[int, int]] = []
    cur_text: list[str] = []

    def flush() -> None:
        if not cur_spans:
            return
        # Split leading prose / code core / trailing prose, then slice the
        # exact field span (including inter-tag whitespace) per part.
        idx = list(range(len(cur_text)))
        codey = [bool(_CODE_LINE_RE.search(cur_text[k])) for k in idx]
        if not any(codey):
            parts = [idx]
        else:
            first = codey.index(True)
            last = len(codey) - 1 - codey[::-1].index(True)
            parts = []
            if first > 0:
                parts.append(idx[:first])
            parts.append(idx[first:last + 1])
            if last + 1 < len(cur_text):
                parts.append(idx[last + 1:])
        for part in parts:
            start = cur_spans[part[0]][0]
            end = cur_spans[part[-1]][1]
            blocks.append((field[start:end], "\n".join(cur_text[k] for k in part)))
        cur_spans.clear()
        cur_text.clear()

    for match in COURIER_P_RE.finditer(field):
        if not _is_courier_paragraph(match):
            flush()
            continue
        text = _courier_text(match.group("body"))
        if text:
            cur_spans.append(match.span(0))
            cur_text.append(text)
        else:
            flush()
    flush()
    return blocks


def extract_codemirror_blocks(field: str) -> list[tuple[str, str]]:
    """Merge consecutive CodeMirror-line <pre> blocks, stripping cm-* spans."""
    blocks: list[tuple[str, str]] = []
    cur_spans: list[tuple[int, int]] = []
    cur_text: list[str] = []
    matches = list(CODEMIRROR_PRE_RE.finditer(field))

    def flush() -> None:
        if not cur_spans:
            return
        idx = list(range(len(cur_text)))
        codey = [bool(_CODE_LINE_RE.search(cur_text[k])) for k in idx]
        if not any(codey):
            parts = [idx]
        else:
            first = codey.index(True)
            last = len(codey) - 1 - codey[::-1].index(True)
            parts = []
            if first > 0:
                parts.append(idx[:first])
            parts.append(idx[first:last + 1])
            if last + 1 < len(cur_text):
                parts.append(idx[last + 1:])
        for part in parts:
            start = cur_spans[part[0]][0]
            end = cur_spans[part[-1]][1]
            blocks.append((field[start:end], "\n".join(cur_text[k] for k in part)))
        cur_spans.clear()
        cur_text.clear()

    for index, match in enumerate(matches):
        text = _courier_text(match.group("body"))
        if text:
            if cur_spans:
                gap = field[matches[index - 1].end():match.start()]
                # Notebook gutter line numbers are digits outside the <pre>;
                # only real text between blocks ends a run.
                gap_text = re.sub(r"[\d\s\u200b\xa0]", "", TAG_RE.sub("", html.unescape(gap)))
                if gap_text:
                    flush()
            cur_spans.append(match.span(0))
            cur_text.append(text)
        else:
            flush()
    flush()
    return blocks


_CODE_LINE_RE = re.compile(
    r"%%writefile\b|\b(def |class |import |return |for .+:|while .+:|if .+:|elif |else:|self\.|lambda |print\(|assert|raise |try:|except\b|==|!=|>=|<=|\w+\(.*\).*:|=)"
)


@dataclass(frozen=True)
class NoteSnapshot:
    note_id: int
    fields: tuple[str, ...]
    checksum: str


@dataclass(frozen=True)
class DeckSnapshot:
    deck_path: Path
    notes: tuple[NoteSnapshot, ...]
    note_count: int
    card_count: int
    deck_count: int
    model_count: int
    members: tuple[str, ...]
    media_manifest: dict[str, str]


@dataclass(frozen=True)
class CodeCandidate:
    deck_path: str
    note_id: int
    field_index: int
    snippet_id: str
    source_html: str
    source_text: str
    language: str | None
    classification: str
    rust_replacement: str | None = None
    validation_status: str = "pending"
    notes: str = ""
    occurrence: int = 0
    start: int = 0
    end: int = 0


def field_checksum(first_field: str) -> int:
    return int(hashlib.sha1(first_field.encode("utf-8")).hexdigest()[:8], 16)


def _read_archive(deck_path: Path) -> tuple[Path, list[str], dict[str, str]]:
    temp_dir = Path(tempfile.mkdtemp(prefix="anki-rust-")).resolve()
    try:
        with zipfile.ZipFile(deck_path) as archive:
            for member in archive.infolist():
                target = (temp_dir / member.filename).resolve()
                if target != temp_dir and temp_dir not in target.parents:
                    raise ValueError(f"unsafe archive member: {member.filename}")
                archive.extract(member, temp_dir)
        database = temp_dir / "collection.anki2"
        if not database.exists():
            raise ValueError("archive missing collection.anki2")
        manifest_path = temp_dir / "media"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        return temp_dir, [member.filename for member in zipfile.ZipFile(deck_path).infolist()], manifest
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def extract_deck_notes(deck_path: Path) -> DeckSnapshot:
    temp_dir, members, manifest = _read_archive(deck_path)
    try:
        connection = sqlite3.connect(temp_dir / "collection.anki2")
        notes = tuple(
            NoteSnapshot(note_id=row[0], fields=tuple(row[1].split(FIELD_SEPARATOR)), checksum=row[2])
            for row in connection.execute("SELECT id, flds, csum FROM notes ORDER BY id")
        )
        counts = {}
        for table in ("notes", "cards", "decks", "models"):
            try:
                counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = 0
        connection.close()
        return DeckSnapshot(
            deck_path=deck_path,
            notes=notes,
            note_count=counts["notes"],
            card_count=counts["cards"],
            deck_count=counts["decks"],
            model_count=counts["models"],
            members=tuple(sorted(members)),
            media_manifest=manifest,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _visible_text(value: str) -> str:
    return html.unescape(TAG_RE.sub("", value)).strip()


def _language(fragment: str) -> str | None:
    match = LANGUAGE_RE.search(fragment)
    if match:
        return match.group(1).lower()
    visible = _visible_text(fragment)
    prefix = re.match(r"(?is)^\s*(sql|json|yaml|shell|bash)\s*[:：]", visible)
    if prefix:
        return prefix.group(1).lower()
    if re.search(r"(?m)^(?:%%writefile\b|(?:from|import)\s+\w+|class\s+\w+|def\s+\w+)|\b(?:yield|self\.)\b", visible):
        return "python"
    return None


def _classification(language: str | None, source_text: str) -> tuple[str, str]:
    lowered = source_text.lower().strip()
    if language in {"sql", "json", "yaml", "yml", "html", "xml", "shell", "bash", "sh", "通用"}:
        return "exclude", "non-Rust artifact"
    if re.match(r"^(select|insert|update|delete|create table|alter table|drop table)\b", lowered):
        return "exclude", "SQL example"
    if lowered.startswith(("$ ", "sudo ", "curl ", "git ", "docker ")):
        return "exclude", "instructional shell command"
    if re.match(r"^(?:\[?\s*)?(?:\{\s*)?[\"']?[a-z_]+[\"']?\s*:", lowered):
        return "exclude", "JSON or data example"
    if not source_text or len(source_text) < 3:
        return "manual-review", "empty or too short"
    if language is None and re.search(r"(?m)^(?:%%writefile\b|(?:from|import)\s+\w+|class\s+\w+|def\s+\w+|#include\s*[<\"])|\b(yield|self\.)\b", source_text):
        return "convert", "detected Python-like programming example"
    if language is None:
        return "manual-review", "language or boundary unclear"
    return "convert", ""


def extract_code_candidates(snapshot: DeckSnapshot) -> list[CodeCandidate]:
    candidates: list[CodeCandidate] = []
    deck_name = snapshot.deck_path.stem.lower().replace(" ", "-")
    for note in snapshot.notes:
        for field_index, field in enumerate(note.fields):
            cm_blocks = extract_codemirror_blocks(field)
            if cm_blocks:
                for occurrence, (source_html, source_text) in enumerate(cm_blocks):
                    classification, notes = _classification("python", source_text)
                    candidates.append(CodeCandidate(
                        deck_path=str(snapshot.deck_path), note_id=note.note_id,
                        field_index=field_index,
                        snippet_id=f"{deck_name}-{note.note_id}-{field_index}-courier-{occurrence}",
                        source_html=source_html, source_text=source_text,
                        language="python", classification=classification,
                        notes=notes or "CodeMirror solution block", occurrence=occurrence,
                    ))
                continue
            matches = list(CODE_BLOCK_RE.finditer(field))
            if not matches:
                matches = list(INLINE_CODE_RE.finditer(field))
            standalone = None
            if not matches and re.match(r"(?is)^\s*(sql|json|yaml|shell|bash)\s*[:：]", field):
                standalone = re.match(r"(?is)^(?P<body>.*)$", field)
            if standalone is not None:
                matches = [standalone]
            for occurrence, match in enumerate(matches):
                source_html = match.group(0)
                source_text = _visible_text(match.group("body"))
                language = _language(source_html)
                classification, notes = _classification(language, source_text)
                candidates.append(CodeCandidate(
                    deck_path=str(snapshot.deck_path), note_id=note.note_id,
                    field_index=field_index,
                    snippet_id=f"{deck_name}-{note.note_id}-{field_index}-{occurrence}",
                    source_html=source_html, source_text=source_text,
                    language=language, classification=classification, notes=notes,
                    occurrence=occurrence, start=match.start(), end=match.end(),
                ))
            if matches:
                continue
            for occurrence, (source_html, source_text) in enumerate(extract_courier_blocks(field)):
                language = _language(source_html)
                classification, notes = _classification(language, source_text)
                candidates.append(CodeCandidate(
                    deck_path=str(snapshot.deck_path), note_id=note.note_id,
                    field_index=field_index,
                    snippet_id=f"{deck_name}-{note.note_id}-{field_index}-courier-{occurrence}",
                    source_html=source_html, source_text=source_text,
                    language=language, classification=classification, notes=notes,
                    occurrence=occurrence,
                ))
    return candidates


def write_inventory(candidates: Iterable[CodeCandidate], output_path: Path) -> None:
    payload = []
    for candidate in candidates:
        item = asdict(candidate)
        item.pop("start", None)
        item.pop("end", None)
        payload.append(item)
    payload.sort(key=lambda item: item["snippet_id"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def load_inventory(path: Path) -> list[dict]:
    return json.loads(path.read_text())


_RUST_TOKEN_RE = re.compile(
    r"(?P<comment>//[^\n]*|/\*.*?\*/)"
    r"|(?P<string>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
    r"|(?P<attribute>#\[[^\]]*\])"
    r"|(?P<lifetime>'[a-z_][a-z0-9_]*|'_)"
    r"|(?P<macro>\b[a-z_][a-z0-9_]*!)"
    r"|(?P<keyword>\b(?:fn|let|mut|struct|enum|trait|impl|use|pub|mod|match|if|else|for|while|loop|return|self|Self|true|false|Some|None|Ok|Err|as|ref|move|where|type|const|static|crate|in|dyn|async|await)\b)"
    r"|(?P<type>\b(?:[A-Z][A-Za-z0-9_]*|u8|u16|u32|u64|u128|usize|i8|i16|i32|i64|i128|isize|f32|f64|bool|char|str|String|Vec|Option|Result|Box|Rc|Arc|Self)\b)"
    r"|(?P<number>\b\d[\w\.]*\b)"
    r"|(?P<function>\b[a-z_][a-z0-9_]*(?=\s*\())",
    re.DOTALL,
)
_RUST_TOKEN_STYLES = {
    "rust-keyword": "#c586c0",
    "rust-string": "#ce9178",
    "rust-comment": "#6a9955",
    "rust-number": "#b5cea8",
    "rust-type": "#4ec9b0",
    "rust-function": "#dcdcaa",
    "rust-macro": "#dcdcaa",
    "rust-lifetime": "#569cd6",
    "rust-attribute": "#9cdcfe",
}

try:
    from tree_sitter import Language as _TsLanguage
    from tree_sitter import Parser as _TsParser
    import tree_sitter_rust as _ts_rust_grammar

    _TS_LANGUAGE = _TsLanguage(_ts_rust_grammar.language())
    _TS_PARSER = _TsParser(_TS_LANGUAGE)
    TS_AVAILABLE = True
except ImportError:
    TS_AVAILABLE = False

# Catppuccin Mocha palette for Rust tokens.
_CATPPUCCIN_MOCHA = {
    "rust-keyword": "#cba6f7",
    "rust-string": "#a6e3a1",
    "rust-comment": "#7f849c",
    "rust-number": "#fab387",
    "rust-type": "#f9e2af",
    "rust-function": "#89b4fa",
    "rust-macro": "#94e2d5",
    "rust-lifetime": "#74c7ec",
    "rust-attribute": "#b4befe",
    "rust-property": "#b4befe",
    "rust-operator": "#89dceb",
    "rust-escape": "#f5c2e7",
    "rust-builtin": "#f38ba8",
    "rust-constant": "#fab387",
    "base": "#1e1e2e",
    "text": "#cdd6f4",
}

_TS_KEYWORDS = frozenset(
    "fn let mut struct enum trait impl use pub mod match if else for while loop "
    "return as ref move where type const static crate in dyn async await".split()
)
_TS_OPERATORS = frozenset(
    ["=", "==", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/", "%", "&&", "||",
     "!", "->", "=>", "::", ".", "..", "..=", "&", "|", "^", "<<", ">>", "?"]
)
_TS_PRIMITIVES = frozenset(
    "u8 u16 u32 u64 u128 usize i8 i16 i32 i64 i128 isize f32 f64 bool char str".split()
)


def _ts_span(kind: str, text: str) -> str:
    return f'<span class="{kind}" style="color:{_CATPPUCCIN_MOCHA[kind]}">{html.escape(text, quote=False)}</span>'


def _ts_leaf_class(node_type: str, text: str, parent_type: str, field: str | None) -> str | None:
    if node_type in ("integer_literal", "float_literal", "boolean_literal"):
        return "rust-number"
    if node_type == "char_literal":
        return "rust-string"
    if node_type == "identifier":
        if parent_type == "macro_invocation" and field == "macro":
            return "rust-macro"
        if parent_type == "function_item" and field == "name":
            return "rust-function"
        if parent_type == "attribute":
            return "rust-attribute"
        if text in _TS_PRIMITIVES:
            return "rust-type"
        if len(text) > 1 and text.isupper() and text.replace("_", "").isalnum():
            return "rust-constant"
        if text[:1].isupper():
            return "rust-type"
        return None
    if node_type in ("type_identifier", "primitive_type"):
        return "rust-type"
    if node_type == "field_identifier":
        return "rust-property"
    if node_type in ("self", "super", "crate"):
        return "rust-builtin"
    if node_type == "escape_sequence":
        return "rust-escape"
    if node_type in _TS_KEYWORDS:
        return "rust-keyword"
    if node_type in _TS_OPERATORS:
        return "rust-operator"
    return None


def _ts_children(node):
    for index, child in enumerate(node.children):
        try:
            field = node.field_name_for_child(index)
        except Exception:
            field = None
        yield child, field


def _ts_emit(node, source: str, out: list[str], parent_type: str = "", field: str | None = None, func_path: bool = False) -> None:
    node_type = node.type
    text = source[node.start_byte:node.end_byte]
    if node.child_count == 0:
        if node_type == "!" and parent_type == "macro_invocation":
            out.append(_ts_span("rust-macro", text))
            return
        kind = _ts_leaf_class(node_type, text, parent_type, field)
        if kind == "rust-property" and func_path:
            kind = "rust-function"
        out.append(_ts_span(kind, text) if kind else html.escape(text, quote=False))
        return
    if node_type in ("line_comment", "block_comment"):
        out.append(_ts_span("rust-comment", text))
        return
    if node_type in ("integer_literal", "float_literal", "boolean_literal"):
        out.append(_ts_span("rust-number", text))
        return
    if node_type == "lifetime":
        out.append(_ts_span("rust-lifetime", text))
        return
    if node_type in ("char_literal", "raw_string_literal"):
        out.append(_ts_span("rust-string", text))
        return
    if node_type == "string_literal":
        def render_string(child, _field):
            child_text = source[child.start_byte:child.end_byte]
            if child.type == "escape_sequence":
                out.append(_ts_span("rust-escape", child_text))
            elif child.child_count == 0:
                out.append(_ts_span("rust-string", child_text))
            else:
                _ts_emit(child, source, out, node_type, None)
        _ts_emit_children(node, source, out, render_string)
        return
    if node_type == "attribute_item":
        def render_attr_item(child, _field):
            if child.child_count == 0:
                out.append(_ts_span("rust-attribute", source[child.start_byte:child.end_byte]))
            else:
                _ts_emit(child, source, out, node_type, None)
        _ts_emit_children(node, source, out, render_attr_item)
        return
    if node_type == "macro_invocation":
        def render_macro(child, child_field):
            _ts_emit(child, source, out, node_type, child_field)
        _ts_emit_children(node, source, out, render_macro)
        return
    if node_type == "call_expression":
        def render_call(child, child_field):
            _ts_emit(child, source, out, node_type, child_field, func_path=(child_field == "function"))
        _ts_emit_children(node, source, out, render_call)
        return
    if node_type == "field_expression":
        def render_field(child, child_field):
            if child_field == "field":
                _ts_emit(child, source, out, node_type, child_field, func_path=func_path)
            else:
                _ts_emit(child, source, out, node_type, child_field)
        _ts_emit_children(node, source, out, render_field)
        return
    if node_type == "scoped_identifier":
        def render_scoped(child, child_field):
            if child_field == "name" and func_path and child.type == "identifier":
                out.append(_ts_span("rust-function", source[child.start_byte:child.end_byte]))
            else:
                _ts_emit(child, source, out, node_type, child_field, func_path=func_path)
        _ts_emit_children(node, source, out, render_scoped)
        return
    def render_default(child, child_field):
        _ts_emit(child, source, out, node_type, child_field)
    _ts_emit_children(node, source, out, render_default)


def _ts_emit_children(node, source: str, out: list[str], render) -> None:
    """Emit children, preserving skipped whitespace between siblings."""
    cursor = node.start_byte
    for child, child_field in _ts_children(node):
        if child.start_byte > cursor:
            out.append(html.escape(source[cursor:child.start_byte], quote=False))
        render(child, child_field)
        cursor = child.end_byte
    if cursor < node.end_byte:
        out.append(html.escape(source[cursor:node.end_byte], quote=False))


def _highlight_treesitter(source: str) -> str:
    """Render Rust source with Tree-sitter tokens in Catppuccin Mocha."""
    tree = _TS_PARSER.parse(source.encode("utf-8"))
    out: list[str] = [_code_open_tag()]
    _ts_emit(tree.root_node, source, out)
    out.append("</code>")
    return "".join(out)


def _code_open_tag() -> str:
    base = _CATPPUCCIN_MOCHA["base"]
    text = _CATPPUCCIN_MOCHA["text"]
    return (
        '<code class="language-rust" style="display:block;box-sizing:border-box;max-width:100%;'
        f"background-color:{base};color:{text};padding:12px;font-family:monospace;white-space:pre;"
        'overflow-x:auto;line-height:1.5;text-align:left;border-radius:4px;">'
    )


def _highlight_regex(source: str) -> str:
    """Fallback highlighter when Tree-sitter is unavailable."""
    parts: list[str] = [_code_open_tag()]
    cursor = 0
    for match in _RUST_TOKEN_RE.finditer(source):
        parts.append(html.escape(source[cursor:match.start()], quote=False))
        token = match.group(0)
        kind = f"rust-{match.lastgroup}"
        parts.append(f'<span class="{kind}" style="color:{_RUST_TOKEN_STYLES[kind]}">{html.escape(token, quote=False)}</span>')
        cursor = match.end()
    parts.append(html.escape(source[cursor:], quote=False))
    parts.append("</code>")
    return "".join(parts)


def highlight_rust_html(source: str) -> str:
    """Render Rust source with inline colors, so Anki needs no external highlighter.

    Prefers Tree-sitter (Catppuccin Mocha); falls back to regex colors when
    Tree-sitter is not installed.
    """
    if TS_AVAILABLE:
        return _highlight_treesitter(source)
    return _highlight_regex(source)


def _neutralize_fixed_width_containers(field: str) -> str:
    """Remove desktop-only fixed widths inherited from notebook HTML.

    Field-wide pass touches only ancestor container widths (e.g. the 928px
    notebook div), never <pre> tags: excluded/manual snippets share fields
    with converted blocks and their source HTML must stay byte-identical.
    """
    return re.sub(r"(?<![-\w])width\s*:\s*928px", "max-width: 100%", field)


def _ensure_wrap_style(attrs: str) -> str:
    style_match = re.search(r'''(?is)\bstyle\s*=\s*(["'])(.*?)\1''', attrs)
    wrap = "max-width:100%;white-space:pre;overflow-x:auto;"
    if not style_match:
        return f'{attrs} style="{wrap}"'
    quote, style = style_match.group(1), style_match.group(2)
    if "max-width" not in style:
        style += "max-width:100%;"
    if "white-space" not in style:
        style += "white-space:pre;"
    if "overflow-x" not in style:
        style += "overflow-x:auto;"
    start, end = style_match.span(2)
    return attrs[:start] + style + attrs[end:]


_WRAP_TOGGLE_SCRIPT = (
    "var c=this.parentNode.parentNode.querySelector('code');"
    "var on=c.style.whiteSpace!=='pre';"
    "if(on){c.style.whiteSpace='pre';c.style.overflowX='auto';this.textContent='wrap: off';}"
    "else{c.style.whiteSpace='pre-wrap';c.style.overflowX='visible';this.textContent='wrap: on';}"
)


def _wrap_with_toggle(code_html: str) -> str:
    """Prepend a tap-to-toggle wrap button above a highlighted code block.

    Inline onclick works in Anki desktop and AnkiDroid reviewers with no
    card-template changes. Default state is wrap-off (unwrapped + horizontal
    scroll) for narrow screens.
    """
    return (
        '<div class="rust-snippet" style="max-width:100%">'
        '<div style="max-width:100%;text-align:right">'
        '<button class="rust-wrap-toggle" style="font-size:12px;padding:2px 10px;margin:0 0 4px 0;'
        "background:#313244;color:#cdd6f4;border:1px solid #45475a;border-radius:4px;font-family:sans-serif;\""
        f' onclick="{_WRAP_TOGGLE_SCRIPT}">wrap: off</button></div>'
        + code_html
        + "</div>"
    )


def _replace_candidate(field: str, candidate: dict, replacement: str) -> str:
    source_html = candidate["source_html"]
    occurrence = int(candidate.get("occurrence", int(candidate["snippet_id"].rsplit("-", 1)[-1])))
    if candidate.get("match") == "exact":
        start = -1
        for _ in range(occurrence + 1):
            start = field.find(source_html, start + 1)
            if start < 0:
                raise ValueError(f"source boundary missing for {candidate['snippet_id']}")
        end = start + len(source_html)
    else:
        regions = list(CODE_BLOCK_RE.finditer(field)) or list(INLINE_CODE_RE.finditer(field))
        if regions:
            if occurrence < 0 or occurrence >= len(regions) or regions[occurrence].group(0) != source_html:
                raise ValueError(f"source boundary mismatch for {candidate['snippet_id']}")
            start, end = regions[occurrence].span()
        elif occurrence == 0 and field == source_html:
            start, end = 0, len(field)
        else:
            raise ValueError(f"source boundary missing for {candidate['snippet_id']}")
    highlighted = highlight_rust_html(replacement)
    if source_html.lower().startswith("<pre"):
        updated = re.sub(r"(?is)(<pre\b[^>]*>).*?(</pre>)", lambda match: "<pre" + _ensure_wrap_style(match.group(1)[4:-1]) + ">" + highlighted + match.group(2), source_html, count=1)
    else:
        updated = highlighted
    return _neutralize_fixed_width_containers(field[:start] + _wrap_with_toggle(updated) + field[end:])


def apply_inventory_replacements(deck_path: Path, inventory: list[dict], output_path: Path) -> None:
    by_note: dict[tuple[int, int], list[dict]] = {}
    for entry in inventory:
        if entry["classification"] == "convert":
            by_note.setdefault((entry["note_id"], entry["field_index"]), []).append(entry)
    temp_dir, _, _ = _read_archive(deck_path)
    try:
        database = temp_dir / "collection.anki2"
        connection = sqlite3.connect(database)
        for note_id, fields_value in connection.execute("SELECT id, flds FROM notes ORDER BY id"):
            fields = fields_value.split(FIELD_SEPARATOR)
            for field_index, field in enumerate(fields):
                entries = by_note.get((note_id, field_index), [])
                for entry in sorted(entries, key=lambda item: int(item.get("occurrence", int(item["snippet_id"].rsplit("-", 1)[-1]))), reverse=True):
                    replacement = entry.get("rust_replacement")
                    if not replacement:
                        raise ValueError(f"missing Rust replacement for {entry['snippet_id']}")
                    field = _replace_candidate(field, entry, replacement)
                fields[field_index] = field
            new_fields = FIELD_SEPARATOR.join(fields)
            if new_fields != fields_value:
                connection.execute("UPDATE notes SET flds = ?, sfld = ?, csum = ? WHERE id = ?", (new_fields, fields[0], field_checksum(fields[0]), note_id))
        connection.commit()
        connection.close()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for source in sorted(temp_dir.rglob("*")):
                if source.is_file():
                    archive.write(source, source.relative_to(temp_dir).as_posix())
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def neutralize_deck_container_widths(deck_path: Path) -> int:
    """Replace desktop-only 928px container widths in every field.

    Content-preserving layout fix: only the width declaration changes, so
    notebook-derived cards fit narrow phone screens. Returns touched fields.
    """
    temp_dir, _, _ = _read_archive(deck_path)
    try:
        database = temp_dir / "collection.anki2"
        connection = sqlite3.connect(database)
        touched = 0
        for note_id, fields_value in connection.execute("SELECT id, flds FROM notes ORDER BY id"):
            fields = fields_value.split(FIELD_SEPARATOR)
            normalized = [_neutralize_fixed_width_containers(field) for field in fields]
            if normalized != fields:
                touched += sum(1 for before, after in zip(fields, normalized) if before != after)
                new_fields = FIELD_SEPARATOR.join(normalized)
                connection.execute("UPDATE notes SET flds = ?, sfld = ?, csum = ? WHERE id = ?", (new_fields, normalized[0], field_checksum(normalized[0]), note_id))
        connection.commit()
        connection.close()
        with zipfile.ZipFile(deck_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for source in sorted(temp_dir.rglob("*")):
                if source.is_file():
                    archive.write(source, source.relative_to(temp_dir).as_posix())
        return touched
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def validate_inventory(inventory: list[dict]) -> None:
    ids = [entry["snippet_id"] for entry in inventory]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate inventory snippet ID")
    for entry in inventory:
        if entry["classification"] == "convert" and not entry.get("rust_replacement"):
            raise ValueError(f"missing replacement: {entry['snippet_id']}")


def validate_migrated_deck(source_path: Path, output_path: Path, inventory: list[dict]) -> None:
    validate_inventory(inventory)
    source = extract_deck_notes(source_path)
    output = extract_deck_notes(output_path)
    if (source.note_count, source.card_count, source.deck_count, source.model_count) != (output.note_count, output.card_count, output.deck_count, output.model_count):
        raise ValueError("Anki counts changed")
    if source.members != output.members:
        raise ValueError("archive members changed")
    source_fields = {(note.note_id, index): value for note in source.notes for index, value in enumerate(note.fields)}
    output_fields = {(note.note_id, index): value for note in output.notes for index, value in enumerate(note.fields)}
    expected_fields = dict(source_fields)
    for entry in sorted(inventory, key=lambda item: int(item.get("occurrence", item["snippet_id"].rsplit("-", 1)[-1])), reverse=True):
        if entry["classification"] == "convert":
            key = (entry["note_id"], entry["field_index"])
            expected_fields[key] = _replace_candidate(expected_fields[key], entry, entry["rust_replacement"])
    for key, value in expected_fields.items():
        expected_fields[key] = _neutralize_fixed_width_containers(value)
    if output_fields != expected_fields:
        raise ValueError("note fields differ from approved replacements")
    with zipfile.ZipFile(source_path) as original, zipfile.ZipFile(output_path) as rebuilt:
        for member in source.members:
            if member != "collection.anki2" and original.read(member) != rebuilt.read(member):
                raise ValueError(f"non-database archive member changed: {member}")
    converted_keys = {(entry["note_id"], entry["field_index"]) for entry in inventory if entry["classification"] == "convert"}
    for entry in inventory:
        key = (entry["note_id"], entry["field_index"])
        before = source_fields[key]
        after = output_fields[key]
        if entry["classification"] != "convert" and key not in converted_keys and after != _neutralize_fixed_width_containers(before):
            raise ValueError(f"out-of-scope field changed: {entry['snippet_id']}")
        if entry["classification"] != "convert" and entry["source_html"] not in after:
            raise ValueError(f"excluded or manual snippet changed: {entry['snippet_id']}")
        if entry["classification"] == "convert" and entry["source_html"] in after:
            raise ValueError(f"source snippet remains: {entry['snippet_id']}")
    source_full = {note.note_id: FIELD_SEPARATOR.join(note.fields) for note in source.notes}
    source_csum = {note.note_id: note.checksum for note in source.notes}
    with zipfile.ZipFile(output_path) as archive:
        with tempfile.TemporaryDirectory() as directory:
            archive.extract("collection.anki2", directory)
            connection = sqlite3.connect(Path(directory) / "collection.anki2")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite quick_check failed")
            for note_id, fields, checksum in connection.execute("SELECT id, flds, csum FROM notes"):
                first_field = fields.split(FIELD_SEPARATOR)[0]
                if int(checksum) != field_checksum(first_field):
                    # Untouched notes keep the deck's original checksum, which
                    # may be based on a sort field that differs from field 0
                    # (as in Coding.apkg). Only enforce the original value then.
                    if fields != source_full[note_id] or int(checksum) != int(source_csum[note_id]):
                        raise ValueError(f"checksum mismatch: {note_id}")
            connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deck", action="append", required=True, type=Path)
    parser.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args()
    candidates = []
    for deck in args.deck:
        snapshot = extract_deck_notes(deck)
        candidates.extend(extract_code_candidates(snapshot))
    write_inventory(candidates, args.inventory)
    print(f"inventoried {len(candidates)} code candidates")


if __name__ == "__main__":
    main()
