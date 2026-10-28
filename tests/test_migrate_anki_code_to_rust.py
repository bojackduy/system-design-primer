import json
import html
import re
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.migrate_anki_code_to_rust import (
    TS_AVAILABLE,
    apply_inventory_replacements,
    extract_code_candidates,
    extract_codemirror_blocks,
    extract_courier_blocks,
    extract_deck_notes,
    field_checksum,
    highlight_rust_html,
    neutralize_deck_container_widths,
    _replace_candidate,
    validate_migrated_deck,
    write_inventory,
)


_STRUCT_PREFIXES = ('pub ', 'fn ', 'struct ', 'enum ', 'impl ', 'impl<', 'use ',
                    'const ', 'static ', 'trait ', 'mod ', '#[', 'extern ',
                    'unsafe ', '/*', '* ', '};')


def _is_structural_part(part):
    for line in part.split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('//'):
            continue
        return stripped.startswith(_STRUCT_PREFIXES) or stripped.startswith('}')
    return True


def _brace_delta(text):
    depth = 0
    for line in text.split('\n'):
        code = line.split('//')[0]
        code = re.sub(r'"[^"]*"', '""', code)
        code = re.sub(r"'[^']*'", "''", code)
        depth += code.count('{') - code.count('}')
    return depth


def assemble_note_parts(parts):
    """Join Rust replacements for compilation.

    Display-only fragments (bare statements like `queue.enqueue(1);`) are
    valid card snippets but not top-level items. Keep them only inside an
    already-open item; otherwise skip them for compilation.
    """
    kept = []
    depth = 0
    for part in parts:
        if _is_structural_part(part):
            kept.append(part)
            depth += _brace_delta(part)
        elif depth > 0:
            kept.append(part)
            depth += _brace_delta(part)
    return kept


class MigrationExtractionTests(unittest.TestCase):
    def test_missing_source_boundary_refuses_already_migrated_field(self):
        entry = {"source_html": "<pre>print(1)</pre>", "snippet_id": "fixture-1-0-0"}
        with self.assertRaisesRegex(ValueError, "source boundary"):
            _replace_candidate("<pre>fn main() {}</pre>", entry, "fn main() {}")

    def test_source_boundary_must_match_requested_ordinal(self):
        field = "<pre>print(1)</pre><pre>print(2)</pre>"
        entry = {"source_html": "<pre>print(1)</pre>", "snippet_id": "fixture-1-0-1"}
        with self.assertRaisesRegex(ValueError, "source boundary"):
            _replace_candidate(field, entry, "fn main() {}")

    def test_all_highlighted_inventory_sources_roundtrip_exactly(self):
        inventory = json.loads(Path("docs/superpowers/rust-flashcard-code-inventory.json").read_text())
        for entry in inventory:
            if entry["classification"] != "convert":
                continue
            source = entry["rust_replacement"]
            rendered = highlight_rust_html(source)
            self.assertEqual(html.unescape(re.sub(r"<[^>]+>", "", rendered)), source)
            self.assertIn("background-color:#1e1e2e", rendered)
            self.assertIn('class="rust-keyword"', rendered)

    @unittest.skipUnless(TS_AVAILABLE, "tree-sitter not installed")
    def test_treesitter_renders_catppuccin_mocha(self):
        source = (
            "#[derive(Debug)]\n"
            "const MAX: u32 = u32::MAX; // set limit\n"
            "fn get(&'a self, path: &'x str) -> Vec<u32> {\n"
            "    let ch = 'a'; let s = \"hi\\n\";\n"
            "    self.items(len + 0x1F) + self.limit\n"
            "}\n"
            "println!(\"v={}\", v);\n"
        )
        rendered = highlight_rust_html(source)
        self.assertEqual(html.unescape(re.sub(r"<[^>]+>", "", rendered)), source)
        for kind in ("rust-keyword", "rust-string", "rust-comment", "rust-number",
                     "rust-type", "rust-function", "rust-macro", "rust-lifetime",
                     "rust-attribute", "rust-property", "rust-operator",
                     "rust-escape", "rust-builtin", "rust-constant"):
            self.assertIn(f'class="{kind}"', rendered)
        for mocha in ("#cba6f7", "#a6e3a1", "#7f849c", "#fab387", "#f9e2af",
                      "#89b4fa", "#94e2d5", "#74c7ec", "#b4befe", "#89dceb",
                      "#f5c2e7", "#f38ba8", "#1e1e2e", "#cdd6f4"):
            self.assertIn(mocha, rendered)

    def make_deck(self, root: Path) -> Path:
        database = root / "collection.anki2"
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE notes (id INTEGER PRIMARY KEY, guid TEXT, mid INTEGER, mod INTEGER,
                usn INTEGER, tags TEXT, flds TEXT, sfld TEXT, csum TEXT, flags INTEGER, data TEXT);
            CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER, ord INTEGER,
                mod INTEGER, usn INTEGER, type INTEGER, queue INTEGER, due INTEGER, ivl INTEGER,
                factor INTEGER, reps INTEGER, lapses INTEGER, left INTEGER, odue INTEGER,
                odid INTEGER, flags INTEGER, data TEXT);
            CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE models (id INTEGER PRIMARY KEY, name TEXT);
            """
        )
        first = '<p>Example:</p><pre class="language-python"><code><span>print(&amp;quot;hi&amp;quot;)</span></code></pre><p><a href="https://example.com">link</a><img src="local.png"></p>'
        fields = first + "\x1f" + "SQL: SELECT * FROM users;"
        connection.execute(
            "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "guid", 1, 0, 0, "", fields, first, field_checksum(first), 0, ""),
        )
        connection.execute("INSERT INTO cards VALUES (1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, '')")
        connection.execute("INSERT INTO decks VALUES (1, 'Default')")
        connection.execute("INSERT INTO models VALUES (1, 'Basic')")
        connection.commit()
        connection.close()
        deck = root / "fixture.apkg"
        with zipfile.ZipFile(deck, "w") as archive:
            archive.write(database, "collection.anki2")
            archive.writestr("media", "{}")
            archive.writestr("local.png", b"image")
        return deck

    def test_highlights_rust_code_inside_code_element(self):
        highlighted = highlight_rust_html('fn main() { println!("hi"); // comment\n}')
        self.assertIn('<code class="language-rust"', highlighted)
        self.assertIn('class="rust-keyword"', highlighted)
        self.assertIn('class="rust-string"', highlighted)
        self.assertIn('class="rust-comment"', highlighted)
        self.assertIn("white-space:pre;", highlighted)
        self.assertIn("overflow-x:auto", highlighted)
        self.assertIn("max-width:100%", highlighted)
        self.assertNotIn("pre-wrap", highlighted)

    def test_highlights_rust_token_kinds(self):
        source = "#[derive(Debug)]\nfn get(&'a self) -> Vec<u32> { self.items(len + 0x1F) }\nprintln!(\"v\"); // done\n"
        highlighted = highlight_rust_html(source)
        for kind in ("rust-attribute", "rust-lifetime", "rust-type", "rust-function", "rust-macro", "rust-number"):
            self.assertIn(f'class="{kind}"', highlighted)
        self.assertEqual(html.unescape(re.sub(r"<[^>]+>", "", highlighted)), source)

    def test_replacement_adds_wrap_toggle_button(self):
        field = '<pre><code>print(1)</code></pre>'
        entry = {"source_html": field, "snippet_id": "fixture-1-0-0"}
        changed = _replace_candidate(field, entry, 'fn main() { println!("hi"); }')
        self.assertIn('class="rust-wrap-toggle"', changed)
        self.assertIn('onclick="', changed)
        self.assertIn("whiteSpace='pre'", changed)
        self.assertIn("whiteSpace='pre-wrap'", changed)
        self.assertIn(">wrap: off<", changed)
        self.assertIn('class="rust-snippet"', changed)
        text = html.unescape(re.sub(r"<[^>]+>", "", changed))
        self.assertIn('fn main() { println!("hi"); }', text.replace("wrap: off", ""))

    def test_extracts_courier_blocks_groups_and_splits(self):
        field = (
            '<p class="p2">Hash Function</p>'
            '<p class="p3">def f(x):</p><p class="p3">return x</p>'
            '<p class="p3"><br></p>'
            '<p class="p4">Complexity:</p>'
        )
        blocks = extract_courier_blocks(field)
        self.assertEqual(len(blocks), 3)
        self.assertIn("def f(x):", blocks[1][1])
        self.assertTrue(blocks[1][0].startswith('<p class="p3">def f(x):</p>'))

    def test_extracts_codemirror_blocks_strips_spans(self):
        field = (
            '<pre class=" CodeMirror-line "><span class="cm-keyword">class</span> A:</pre>'
            '<pre class=" CodeMirror-line "><span>\u200b</span></pre>'
            '<pre class=" CodeMirror-line ">    <span class="cm-def">f</span>():</pre>'
        )
        blocks = extract_codemirror_blocks(field)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0][1], "class A:")
        self.assertIn("f():", blocks[1][1])
        self.assertNotIn("cm-keyword", blocks[1][0])

    def test_exact_match_replacement(self):
        field = '<p class="p3">def f(x):</p><p class="p3">return x</p><p>prose</p>'
        entry = {"source_html": '<p class="p3">def f(x):</p><p class="p3">return x</p>',
                 "snippet_id": "coding-1-1-courier-0", "occurrence": 0, "match": "exact"}
        changed = _replace_candidate(field, entry, "fn f(x: i32) -> i32 { x }")
        self.assertIn('class="language-rust"', changed)
        self.assertIn('class="rust-wrap-toggle"', changed)
        self.assertIn('<p>prose</p>', changed)
        with self.assertRaisesRegex(ValueError, "source boundary"):
            _replace_candidate("<p>other</p>", entry, "fn g() {}")

    def test_replacement_neutralizes_fixed_width_containers(self):
        field = '<div style="width: 928px"><pre><code>print(1)</code></pre></div>'
        entry = {"source_html": "<pre><code>print(1)</code></pre>", "snippet_id": "fixture-1-0-0"}
        changed = _replace_candidate(field, entry, 'fn main() { println!("hi"); }')
        self.assertNotIn("928px", changed)
        self.assertIn("max-width", changed)
        self.assertIn('class="language-rust"', changed)

    def test_coding_inventory_assemblies_compile(self):
        import shutil
        import subprocess

        if shutil.which("rustc") is None:
            self.skipTest("rustc not available")
        path = Path("docs/superpowers/rust-flashcard-code-inventory-coding.json")
        if not path.exists():
            self.skipTest("coding inventory absent")
        inventory = json.loads(path.read_text(encoding="utf-8"))
        groups: dict[tuple[int, int], list[str]] = {}
        for entry in inventory:
            if entry["classification"] != "convert":
                continue
            self.assertTrue(entry["rust_replacement"], entry["snippet_id"])
            groups.setdefault((entry["note_id"], entry["field_index"]), []).append(entry["rust_replacement"])
        self.assertTrue(groups, "no coding converts")
        with tempfile.TemporaryDirectory() as directory:
            for (note_id, field_index), parts in sorted(groups.items()):
                source = Path(directory) / f"coding_{note_id}_{field_index}.rs"
                kept = assemble_note_parts(parts)
                body = "\n\n".join(kept)
                if not re.search(r"(?m)^fn main\b", body):
                    body += "\nfn main() {}\n"
                source.write_text(body, encoding="utf-8")
                binary = Path(directory) / "t"
                compiled = subprocess.run(
                    ["rustc", "--edition=2021", "--test", str(source), "-o", str(binary)],
                    capture_output=True, text=True, timeout=180,
                )
                self.assertEqual(compiled.returncode, 0, f"note {note_id} field {field_index}:\n{compiled.stderr}")
                ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=120)
                self.assertEqual(ran.returncode, 0, f"note {note_id} field {field_index}:\n{ran.stdout}")

    def test_deck_width_pass_preserves_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deck = self.make_deck(root)
            database = root / "collection.anki2"
            connection = sqlite3.connect(database)
            fields = connection.execute("SELECT flds FROM notes WHERE id = 1").fetchone()[0]
            wrapped = f'<div style="width: 928px">{fields}</div>'
            connection.execute("UPDATE notes SET flds = ?, sfld = ?, csum = ? WHERE id = 1", (wrapped, wrapped.split("\x1f")[0], field_checksum(wrapped.split("\x1f")[0])))
            connection.commit()
            connection.close()
            with zipfile.ZipFile(deck, "w") as archive:
                archive.write(database, "collection.anki2")
                archive.writestr("media", "{}")
                archive.writestr("local.png", b"image")
            touched = neutralize_deck_container_widths(deck)
            self.assertGreater(touched, 0)
            changed = extract_deck_notes(deck).notes[0].fields[0]
            self.assertNotIn("928px", changed)
            self.assertIn('href="https://example.com"', changed)
            self.assertIn('src="local.png"', changed)
            self.assertIn("print(", html.unescape(re.sub(r"<[^>]+>", "", changed)))

    def test_extracts_code_candidates_and_preserves_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            deck = self.make_deck(Path(directory))
            snapshot = extract_deck_notes(deck)
            candidates = extract_code_candidates(snapshot)

        self.assertEqual(snapshot.note_count, 1)
        self.assertEqual(len(candidates), 2)
        programming, sql = candidates
        self.assertEqual(programming.classification, "convert")
        self.assertEqual(programming.language, "python")
        self.assertIn("print", programming.source_text)
        self.assertEqual(sql.classification, "exclude")
        self.assertIn('href="https://example.com"', snapshot.notes[0].fields[0])
        self.assertIn('src="local.png"', snapshot.notes[0].fields[0])

    def test_replaces_multiple_code_blocks_in_one_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_deck(root)
            snapshot = extract_deck_notes(source)
            original = snapshot.notes[0].fields[0]
            second = '<pre class="language-python"><code>print(&amp;quot;bye&amp;quot;)</code></pre>'
            database_dir = root / "db"
            database_dir.mkdir()
            with zipfile.ZipFile(source) as archive:
                archive.extract("collection.anki2", database_dir)
            database = database_dir / "collection.anki2"
            connection = sqlite3.connect(database)
            fields = original.replace("</p>", "</p>" + second, 1)
            connection.execute("UPDATE notes SET flds = ?, sfld = ?, csum = ? WHERE id = ?", (fields + "\x1fSQL: SELECT 1", fields, field_checksum(fields), 1))
            connection.commit()
            connection.close()
            rebuilt = root / "two.apkg"
            with zipfile.ZipFile(rebuilt, "w") as archive:
                archive.write(database, "collection.anki2")
                archive.writestr("media", "{}")
                archive.writestr("local.png", b"image")
            candidates = extract_code_candidates(extract_deck_notes(rebuilt))
            entries = []
            for candidate, replacement in zip(candidates, ['fn main() { println!("hi"); }', 'fn bye() {}']):
                item = candidate.__dict__.copy()
                item["rust_replacement"] = replacement
                item["validation_status"] = "compiled"
                entries.append(item)
            output = root / "output.apkg"
            apply_inventory_replacements(rebuilt, entries, output)
            changed = extract_deck_notes(output).notes[0].fields[0]
            self.assertIn('class="language-rust"', changed)
            self.assertIn(">main<", changed)
            self.assertIn(">bye<", changed)

    def test_applies_only_approved_code_and_validates_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_deck(root)
            snapshot = extract_deck_notes(source)
            candidate = extract_code_candidates(snapshot)[0]
            entry = {
                "deck_path": str(source), "note_id": candidate.note_id,
                "field_index": candidate.field_index, "snippet_id": candidate.snippet_id,
                "source_html": candidate.source_html, "source_text": candidate.source_text,
                "language": candidate.language, "classification": "convert",
                "rust_replacement": 'fn main() { println!("hi"); }',
                "validation_status": "compiled", "notes": "",
            }
            output = root / "output.apkg"
            apply_inventory_replacements(source, [entry], output)
            validate_migrated_deck(source, output, [entry])
            changed = extract_deck_notes(output).notes[0].fields[0]
            self.assertIn('class="language-rust"', changed)
            self.assertIn('>main<', changed)
            self.assertIn('href="https://example.com"', changed)
            self.assertIn('src="local.png"', changed)

    def test_validation_rejects_corrupted_rust_even_with_valid_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_deck(root)
            entry = extract_code_candidates(extract_deck_notes(source))[0].__dict__.copy()
            entry["rust_replacement"] = "fn main() {}"
            output = root / "output.apkg"
            apply_inventory_replacements(source, [entry], output)
            with zipfile.ZipFile(output) as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            database = root / "corrupted.anki2"
            database.write_bytes(members["collection.anki2"])
            connection = sqlite3.connect(database)
            fields = connection.execute("SELECT flds FROM notes").fetchone()[0].replace(">main<", ">wrong<")
            connection.execute("UPDATE notes SET flds = ?, csum = ?", (fields, field_checksum(fields.split("\x1f")[0])))
            connection.commit()
            connection.close()
            members["collection.anki2"] = database.read_bytes()
            with zipfile.ZipFile(output, "w") as archive:
                for name, contents in members.items():
                    archive.writestr(name, contents)
            with self.assertRaisesRegex(ValueError, "approved replacements"):
                validate_migrated_deck(source, output, [entry])

    def test_every_convert_inventory_entry_has_utf8_replacement(self):
        inventory_path = Path("docs/superpowers/rust-flashcard-code-inventory.json")
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        converted = [entry for entry in inventory if entry["classification"] == "convert"]
        self.assertEqual(len(converted), 29)
        for entry in converted:
            replacement = entry["rust_replacement"]
            self.assertIsInstance(replacement, str)
            self.assertTrue(replacement)
            replacement.encode("utf-8")

    def test_inventory_is_deterministic_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deck = self.make_deck(root)
            inventory_path = root / "inventory.json"
            candidates = extract_code_candidates(extract_deck_notes(deck))
            write_inventory(candidates, inventory_path)
            payload = json.loads(inventory_path.read_text())

        self.assertEqual([entry["snippet_id"] for entry in payload], ["fixture-1-0-0", "fixture-1-1-0"])
        self.assertIsNone(payload[0]["rust_replacement"])
        self.assertEqual(payload[1]["classification"], "exclude")


if __name__ == "__main__":
    unittest.main()
