#!/usr/bin/env python3
"""Embed repository media into Anki package files."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from pathlib import PurePosixPath
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

_MEDIA_TAG = re.compile(
    r"(<(?:img|video|source)\b[^>]*?(?<![-\w])src\s*=\s*)([\"'])(.*?)(\2)",
    re.IGNORECASE | re.DOTALL,
)
_CANONICAL_ATTR = re.compile(
    r"\bdata-canonical-src\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL
)


@dataclass
class LocalizationReport:
    deck_path: Path
    localized_filenames: list[str]
    unresolved_urls: list[str]
    original_note_count: int
    final_note_count: int


def field_checksum(first_field: str) -> int:
    """Return Anki's checksum for the first note field."""
    return int(hashlib.sha1(first_field.encode("utf-8")).hexdigest()[:8], 16)


_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class DeckAssetConfig:
    """Per-deck media source override: asset dir plus referenced-name aliases."""

    asset_dir: Path | None
    aliases: dict[str, str]


PER_DECK_ASSETS: dict[str, DeckAssetConfig] = {
    # Coding.apkg notes reference algorithm gifs that live in the
    # interactive-coding-challenges submodule, not in images/.
    # eratosthenes.gif is referenced but no such file exists; sieve.gif
    # depicts the same Sieve of Eratosthenes algorithm, so it is copied
    # under the referenced name. Never rename files inside the submodule.
    "Coding.apkg": DeckAssetConfig(
        asset_dir=_REPO_ROOT / "third_party" / "interactive-coding-challenges" / "images",
        aliases={"eratosthenes.gif": "sieve.gif"},
    ),
}


def deck_asset_config(deck_path: Path | str, asset_dir: Path) -> tuple[Path, dict[str, str]]:
    """Return (asset_dir, aliases) for a deck, applying PER_DECK_ASSETS."""
    config = PER_DECK_ASSETS.get(Path(deck_path).name)
    if config is None:
        return asset_dir, {}
    if config.asset_dir is not None:
        return config.asset_dir, dict(config.aliases)
    return asset_dir, dict(config.aliases)


def _asset_name(url: str) -> str | None:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    if not name or name in {".", ".."}:
        return None
    return name


def _canonical_origin(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    payload = path.rsplit("/", 1)[-1]
    if "camo.githubusercontent.com" not in url:
        return url
    try:
        return bytes.fromhex(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return url


def _referenced_name(url: str, canonical_url: str | None) -> str | None:
    candidate_url = html.unescape(canonical_url or "") if canonical_url else ""
    if not candidate_url:
        candidate_url = _canonical_origin(html.unescape(url))
    return _asset_name(candidate_url)


def resolve_local_asset(
    url: str, canonical_url: str | None, asset_dir: Path, aliases: dict[str, str] | None = None
) -> Path | None:
    """Resolve a media URL to an existing repository asset by filename.

    With aliases, the returned file may live under a different on-disk name
    than the referenced one; use _referenced_name for the display name.
    """
    name = _referenced_name(url, canonical_url)
    if not name:
        return None
    source = (aliases or {}).get(name, name)
    asset = asset_dir / source
    return asset if asset.is_file() else None


def rewrite_media_html(
    html_text: str,
    resolver: Callable[[str, str | None], str | None],
    register: Callable[[str], None],
) -> tuple[str, list[str]]:
    """Rewrite media tag sources while preserving all other HTML attributes."""
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        prefix, quote, raw_url, closing_quote = match.groups()
        url = html.unescape(raw_url)
        tag_start = html_text.rfind("<", 0, match.start() + 1)
        tag_end = html_text.find(">", match.end())
        tag_text = html_text[tag_start : tag_end + 1] if tag_start >= 0 and tag_end >= 0 else match.group(0)
        canonical_match = _CANONICAL_ATTR.search(tag_text)
        canonical = html.unescape(canonical_match.group(2)) if canonical_match else None
        local_name = resolver(url, canonical)
        if local_name is None:
            unresolved.append(url)
            return match.group(0)
        register(local_name)
        return f"{prefix}{quote}{local_name}{closing_quote}"

    return _MEDIA_TAG.sub(replace, html_text), unresolved


def _rewrite_field(
    field: str, asset_dir: Path, filenames: set[str], aliases: dict[str, str] | None = None
) -> tuple[str, list[str]]:
    aliases = aliases or {}

    def resolver(url: str, canonical: str | None) -> str | None:
        name = _referenced_name(url, canonical)
        if name is None:
            return None
        if resolve_local_asset(url, canonical, asset_dir, aliases) is None:
            return None
        return name

    return rewrite_media_html(field, resolver, filenames.add)


def _normalize_media_manifest(raw_manifest: dict[str, str]) -> dict[str, str]:
    """Return Anki's numeric-member-to-filename legacy media mapping."""
    if all(str(key).isdigit() for key in raw_manifest):
        return {str(key): str(name) for key, name in raw_manifest.items()}
    if all(str(value).isdigit() for value in raw_manifest.values()):
        return {str(value): str(name) for name, value in raw_manifest.items()}
    raise ValueError("invalid legacy media manifest")


def _extract_archive(archive: zipfile.ZipFile, destination: Path) -> list[str]:
    members = archive.infolist()
    for member in members:
        path = PurePosixPath(member.filename)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe archive member: {member.filename}")
        if stat.S_ISLNK(member.external_attr >> 16):
            raise ValueError(f"unsafe archive member: {member.filename}")
    archive.extractall(destination)
    return [member.filename for member in members]


def localize_deck(
    deck_path: Path, asset_dir: Path, output_path: Path, aliases: dict[str, str] | None = None
) -> LocalizationReport:
    """Create a localized copy of one Anki package."""
    localized: set[str] = set()
    unresolved: list[str] = []
    with tempfile.TemporaryDirectory() as temporary:
        extracted = Path(temporary)
        with zipfile.ZipFile(deck_path) as archive:
            original_members = _extract_archive(archive, extracted)
            existing_manifest = _normalize_media_manifest(
                json.loads(archive.read("media")) if "media" in archive.namelist() else {}
            )
        database_path = extracted / "collection.anki2"
        connection = sqlite3.connect(database_path)
        original_count = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        rows = connection.execute("SELECT id, flds FROM notes").fetchall()
        for note_id, fields in rows:
            field_values = fields.split("\x1f")
            rewritten_values = []
            changed = False
            for field in field_values:
                rewritten, missing = _rewrite_field(field, asset_dir, localized, aliases)
                rewritten_values.append(rewritten)
                unresolved.extend(missing)
                changed |= rewritten != field
            rewritten_fields = "\x1f".join(rewritten_values)
            if changed:
                connection.execute(
                    "UPDATE notes SET flds = ?, csum = ? WHERE id = ?",
                    (rewritten_fields, field_checksum(rewritten_values[0]), note_id),
                )
            else:
                connection.execute(
                    "UPDATE notes SET csum = ? WHERE id = ?",
                    (field_checksum(rewritten_values[0]), note_id),
                )
        connection.commit()
        final_count = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        connection.close()

        media_manifest = dict(existing_manifest)
        member_by_name = {name: member for member, name in media_manifest.items()}
        next_media_id = max((int(member) for member in media_manifest), default=-1) + 1
        for name in sorted(localized):
            if name not in member_by_name:
                member = str(next_media_id)
                media_manifest[member] = name
                member_by_name[name] = member
                next_media_id += 1
            source_name = (aliases or {}).get(name, name)
            shutil.copy2(asset_dir / source_name, extracted / name)
        media_path = extracted / "media"
        media_path.write_text(json.dumps(media_manifest, separators=(",", ":")), encoding="utf-8")

        media_sources: dict[str, Path] = {}
        media_members: set[str] = set()
        for member, name in media_manifest.items():
            member_path = extracted / member
            name_path = extracted / name
            if name in localized:
                media_sources[member] = name_path
            elif member_path.exists():
                media_sources[member] = member_path
            elif name_path.exists():
                media_sources[member] = name_path
            else:
                raise ValueError(f"media member missing: {member} ({name})")
            media_members.update({name, member})

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(database_path, "collection.anki2")
            archive.write(media_path, "media")
            for member in original_members:
                if member not in {"collection.anki2", "media"} and member not in media_members:
                    archive.write(extracted / member, member)
            for member, source in sorted(media_sources.items()):
                archive.write(source, member)

    return LocalizationReport(
        deck_path=deck_path,
        localized_filenames=sorted(localized),
        unresolved_urls=unresolved,
        original_note_count=original_count,
        final_note_count=final_count,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deck", action="append", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--in-place", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.assets.is_dir():
        raise SystemExit(f"assets directory does not exist: {args.assets}")
    if any(not deck.is_file() for deck in args.deck):
        raise SystemExit("all --deck paths must be files")
    if args.in_place and args.output:
        raise SystemExit("--output and --in-place cannot be combined")
    if not args.in_place and args.output is None:
        raise SystemExit("--output is required unless --in-place is supplied")
    if not args.in_place and len(args.deck) != 1:
        raise SystemExit("--output is required for one deck; use --in-place for multiple decks")
    for deck in args.deck:
        output = deck if args.in_place else args.output
        if output == deck and not args.in_place:
            raise SystemExit("refusing to overwrite input deck without --in-place")
        deck_assets, deck_aliases = deck_asset_config(deck, args.assets)
        report = localize_deck(deck, deck_assets, output, aliases=deck_aliases)
        print(f"{deck}: localized {len(report.localized_filenames)} media files")
        for url in sorted(set(report.unresolved_urls)):
            print(f"unresolved: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
