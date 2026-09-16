"""Merges category rows that share a display name into one.

Problem: several rows with different `name` but the same `custom_name::main`
show up on the site as several categories with the identical title
(`asiaboy`, `asiaboyvideo`, `asian` — all "asian | 亚洲男").

What the script does: groups rows by the English part of the display name
(whatever is left of `|`), keeps one row per group, and merges ts_keywords
with `|`.

Important consequence the script counts and prints: a Tags Based import picks
a group by an EXACT tag match against `name`, so only one tag out of the
group survives, and videos with the other tags lose their category unless
they have some other matching tag. The report shows exactly how much is lost.

Usage:
    uv run python -m scripts.dedupe_categories \\
        --categories gay-categories-zh.txt --output gay-categories-zh.txt \\
        --uncovered no-catrgories.txt --db gay_all_database
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path


def split_alternatives(expr: str) -> list[str]:
    """Splits an expression on top-level `|`, without touching quotes or parens."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_quotes = False
    for char in expr:
        if char == '"':
            in_quotes = not in_quotes
        elif not in_quotes and char == "(":
            depth += 1
        elif not in_quotes and char == ")":
            depth -= 1
        if char == "|" and not in_quotes and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    parts.append("".join(current).strip())
    return [part for part in parts if part]


def protect(alternative: str) -> str:
    """Wraps an alternative with a top-level space in parentheses.

    A space in this syntax means AND. Joining `amazing butt` with something
    else via `|` makes operator precedence ambiguous, and `shower -golden`
    changes meaning entirely once joined. Parens make the grouping explicit.
    """
    depth = 0
    in_quotes = False
    has_top_level_space = False
    for char in alternative:
        if char == '"':
            in_quotes = not in_quotes
        elif not in_quotes and char == "(":
            depth += 1
        elif not in_quotes and char == ")":
            depth -= 1
        elif char == " " and not in_quotes and depth == 0:
            has_top_level_space = True
    if not has_top_level_space:
        return alternative
    if alternative.startswith("(") and alternative.endswith(")"):
        return alternative
    return f"({alternative})"


def merge_keywords(expressions: list[str]) -> str:
    merged: list[str] = []
    for expr in expressions:
        for alternative in split_alternatives(expr):
            protected = protect(alternative)
            if protected not in merged:
                merged.append(protected)
    return "|".join(merged)


def tag_counts(db: str | None) -> Counter[str]:
    """How many videos each tag has — this count picks the surviving `name`."""
    if not db:
        return Counter()
    from sqlmodel import func, select

    from src.database.session import get_session, set_db_path
    from src.models import Tag, VideoTagLink

    set_db_path(db)
    with get_session() as session:
        rows = session.exec(
            select(Tag.name, func.count(VideoTagLink.video_id))
            .join(VideoTagLink, VideoTagLink.tag_id == Tag.id)
            .group_by(Tag.id)
        ).all()
    return Counter({name: count for name, count in rows})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--categories", type=Path, default=Path("gay-categories-zh.txt"))
    parser.add_argument("--output", type=Path, default=Path("gay-categories-zh.txt"))
    parser.add_argument("--uncovered", type=Path, default=None)
    parser.add_argument("--db", default=None, help="Database for counting tag frequency")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report only, don't overwrite the file"
    )
    args = parser.parse_args()

    header: list[str] = []
    rows: list[tuple[str, str, str]] = []
    for line in args.categories.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            header.append(line)
            continue
        fields = line.split(";")
        name, display, keywords = fields[0], fields[1], ";".join(fields[2:])
        rows.append((name.strip(), display.strip(), keywords.strip()))

    groups: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for row in rows:
        # Key is the English part of the name: "emo | 情绪摇滚" and "emo | 情绪少年"
        # both show up as "emo" on the site, so these need merging too.
        groups[row[1].split("|")[0].strip().lower()].append(row)

    counts = tag_counts(args.db)
    merged_rows: list[tuple[str, str, str]] = []
    dropped: dict[str, list[str]] = {}
    for key, group in groups.items():
        names = [name for name, _, _ in group]
        if len(group) == 1:
            merged_rows.append(group[0])
            continue
        # The survivor is whichever `name` actually exists as a tag in the DB
        # and catches the most videos. Matching the display name is NOT a
        # priority: in the source file such names are written with spaces
        # (`ass to mouth`, `foot fetish`, `pov`), while xvideos tags are
        # always hyphenated — so such a name matches no tag and the category
        # ends up empty.
        real = [name for name in names if counts.get(name, 0) > 0]
        if real:
            survivor = max(real, key=lambda name: (counts[name], -len(name)))
        elif key in {name.lower() for name in names}:
            survivor = next(name for name in names if name.lower() == key)
        else:
            survivor = min(names, key=len)
        # The label and the surviving `name` are chosen independently. Name by
        # tag frequency (see above), label from whichever row is named after
        # the category itself (`emo;emo | 情绪摇滚`, `pov;pov | POV`): such rows
        # were written by hand, translations taken from live sites, and are
        # more accurate than generated ones named after a tag (`gayemo`, `gay-pov`).
        curated = [display for name, display, _ in group if name.lower() == key]
        display = curated[0] if curated else next(
            display for name, display, _ in group if name == survivor
        )
        merged_rows.append(
            (survivor, display, merge_keywords([keywords for _, _, keywords in group]))
        )
        dropped[survivor] = [name for name in names if name != survivor]

    merged_rows.sort(key=lambda row: row[0].lower())
    text = "\n".join(header + [f"{name};{display};{keywords}" for name, display, keywords in merged_rows])
    if not args.dry_run:
        args.output.write_text(text + "\n", encoding="utf-8")

    print(f"rows before: {len(rows)}, after: {len(merged_rows)}, merged: {len(rows) - len(merged_rows)}")
    print(f"groups merged: {len(dropped)}")

    if args.uncovered and args.uncovered.exists():
        log_rows = []
        for line in args.uncovered.read_text(encoding="utf-8").splitlines():
            match = re.search(r"\|\s*\(no groups selected\)\s*$", line)
            if not match:
                continue
            fields = line[: match.start()].split("|")
            if len(fields) >= 6:
                log_rows.append([t.strip().lower() for t in fields[4].split(",") if t.strip()])
        before = {name.lower() for name, _, _ in rows}
        after = {name.lower() for name, _, _ in merged_rows}
        covered_before = sum(1 for row in log_rows if any(tag in before for tag in row))
        covered_after = sum(1 for row in log_rows if any(tag in after for tag in row))
        print(
            f"\nlog coverage ({len(log_rows)} rows): before {covered_before}, "
            f"after {covered_after}, lost {covered_before - covered_after}"
        )
        lost_tags = Counter()
        for row in log_rows:
            if any(tag in before for tag in row) and not any(tag in after for tag in row):
                for tag in row:
                    if tag in before:
                        lost_tags[tag] += 1
        if lost_tags:
            print("\ntags causing lost videos:")
            for tag, count in lost_tags.most_common(20):
                print(f"  {count:6d}  {tag}")

    if dropped:
        print("\nwhat merged into what (first 25 groups):")
        for survivor, names in list(dropped.items())[:25]:
            print(f"  {survivor:22s} <- {', '.join(names)}")


if __name__ == "__main__":
    main()
