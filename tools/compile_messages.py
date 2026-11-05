"""Build standard GNU gettext MO catalogues from reviewable UTF-8 JSON mappings."""
import argparse
import json
from pathlib import Path
import struct


def build_mo(messages):
    if not isinstance(messages, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in messages.items()):
        raise ValueError("Translation sources must map text keys to text values.")
    if "charset=UTF-8" not in messages.get("", ""):
        raise ValueError("A UTF-8 gettext header is required.")
    pairs = sorted((key.encode("utf-8"), value.encode("utf-8"))
                   for key, value in messages.items() if value)
    count = len(pairs)
    originals = bytearray()
    translations = bytearray()
    original_table, translation_table = [], []
    original_start = 28 + 16 * count
    for source, _ in pairs:
        original_table.append((len(source), original_start + len(originals)))
        originals.extend(source + b"\0")
    translated_start = original_start + len(originals)
    for _, target in pairs:
        translation_table.append((len(target), translated_start + len(translations)))
        translations.extend(target + b"\0")
    header = struct.pack("<7I", 0x950412DE, 0, count, 28, 28 + 8 * count, 0, 0)
    tables = b"".join(struct.pack("<2I", *entry) for entry in original_table + translation_table)
    return header + tables + originals + translations


def compile_catalogues(root, check=False):
    stale = []
    for source in sorted(root.glob("*/LC_MESSAGES/django.json")):
        compiled = build_mo(json.loads(source.read_text(encoding="utf-8")))
        target = source.with_suffix(".mo")
        if check:
            if not target.exists() or target.read_bytes() != compiled:
                stale.append(target)
        else:
            target.write_bytes(compiled)
    return stale


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail on stale compiled files without modifying them.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent / "backend" / "core" / "locale"
    stale = compile_catalogues(root, check=args.check)
    if stale:
        parser.exit(1, "Stale translations:\n" + "\n".join(map(str, stale)) + "\n")


if __name__ == "__main__":
    main()
