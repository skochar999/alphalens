#!/usr/bin/env python3
"""Sort mixed monthly-portfolio files from mf_data/_phase1_drop/_inbox/ into
the correct per-house drop folder, by reading each file's own header content
(AMC / scheme names) — filenames are ignored, they vary too much.

Usage:  python3 _phase1_sort_inbox.py            # sort, then print a report
        python3 _phase1_sort_inbox.py --dry-run  # classify only, move nothing
"""
import re
import shutil
import sys
from pathlib import Path

import ingest_phase1 as p

BASE = Path(__file__).parent / "mf_data" / "_phase1_drop"
INBOX = BASE / "_inbox"

# (house_key, signature regex) — tested against sheet names + the first 4 rows
# of the first sheets, where only AMC/scheme titles appear (never holdings, so
# e.g. a "Shriram Finance Ltd" bond in another house's portfolio can't confuse
# it). Order matters: most distinctive first.
SIGS = [
    ("icici",    r"icici\s+prudential"),
    ("absl",     r"aditya\s+birla"),
    ("mahindra", r"mahindra\s+manulife"),
    ("boi",      r"bank\s+of\s+india"),
    ("bajaj",    r"bajaj\s+finserv"),
    ("360one",   r"360\s*one"),
    ("groww",    r"\bgroww\b"),
    ("iti",      r"\biti\b"),
    ("shriram",  r"\bshriram\b"),
    ("nj",       r"\bnj\b"),
]


def classify(path: Path) -> str | None:
    try:
        sheets = p.load_sheets(path.read_bytes())
    except Exception:
        return None
    if not sheets:
        return None
    text = []
    for name, rows in sheets[:6]:
        text.append(str(name))
        for row in rows[:4]:
            text += [v for v in row if isinstance(v, str)]
    blob = " ".join(text).lower()
    for key, sig in SIGS:
        if re.search(sig, blob):
            return key
    return None


def main() -> int:
    dry = "--dry-run" in sys.argv
    INBOX.mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in INBOX.iterdir()
                   if f.is_file() and ".xls" in f.suffix.lower())
    if not files:
        print(f"inbox empty: {INBOX}")
        return 0
    moved, dups, unknown = {}, [], []
    for f in files:
        key = classify(f)
        if key is None:
            unknown.append(f.name)
            continue
        dest = BASE / key / f.name
        if dest.exists() and dest.stat().st_size == f.stat().st_size:
            if not dry:
                f.unlink()                      # exact duplicate, drop it
            dups.append(f"{f.name} (already in {key}/)")
            continue
        if not dry:
            (BASE / key).mkdir(exist_ok=True)
            shutil.move(str(f), str(dest))
        moved.setdefault(key, []).append(f.name)

    tag = " (DRY-RUN, nothing moved)" if dry else ""
    print(f"sorted {sum(len(v) for v in moved.values())} files{tag}:")
    for key in sorted(moved):
        print(f"  {key:9s} <- {len(moved[key])} file(s)")
        for n in moved[key]:
            print(f"              {n}")
    for d in dups:
        print(f"  duplicate: {d}")
    for u in unknown:
        print(f"  UNRECOGNIZED (left in _inbox): {u}")
    if moved and not dry:
        print("\nnext: python3 _phase1_run.py " +
              " && python3 _phase1_run.py ".join(sorted(moved)) +
              " && python3 run_monthly_update.py --from-step 2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
