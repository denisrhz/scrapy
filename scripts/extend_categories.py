"""Extends the categories file with tags that matched no category.

Why a separate script instead of a manual edit: the importer picks a category
by an EXACT match of a video's tag against the `name` field (first column).
Checked against the rejection log: of 14,628 "no groups selected" rows, none
has a tag equal to any `name`. The `ts_keywords` column doesn't affect
category selection — it's search keywords for the site, so `masturbat`
doesn't catch the tag `gay-masturbation`, and `^anal$` doesn't catch `gay-anal`.

Hence the extension approach: every real tag from the data needs its own row,
and the human-readable name (second column) is made identical for synonyms so
they read as one category on the site. The file is already built this way —
`cock`/`cocks`, `student`/`students`, `spy`/`spying` are separate rows.

Usage:
    uv run python -m scripts.extend_categories \\
        --categories gay-categories-zh.txt \\
        --uncovered no-catrgories.txt \\
        --output gay-categories-zh.txt
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

# Categories: (display name, translation, tags from the data).
# Tags are listed exactly as they occur in the DB, including typos
# (`gay-hadcore`) and hyphen-less mashups (`gaysex`) — otherwise no match.
RUBRICS: list[tuple[str, str, tuple[str, ...]]] = [
    # --- generic niche tags: without them most of the videos go uncovered -------
    ("gay", "同志", ("gay", "gays", "gay-men", "hot-gay")),
    ("gay sex", "同志性爱", ("gaysex", "gay-sex", "sex", "gay-sex-porn")),
    ("gay porn", "同志色情", ("gayporn", "gay-porn", "porno-gay", "free-gay-porn-videos")),
    # --- body/character types -------------------------------------------------------------
    ("twink", "小鲜肉", ("twink", "twinks", "gay-twink", "gay-twinks")),
    ("twink studios", "小鲜肉工作室", ("twinkstudios",)),
    ("boy porn", "小男生", ("gay-boyporn", "gay-boysporn", "gay-boy-porn", "gay-boy")),
    ("boys", "男孩", ("gay-boys", "gay-young", "gay-boy18")),
    ("young men", "青年男", ("gay-youngmen", "gay-teen", "college-age")),
    ("dudes", "帅哥", ("gay-dudes", "gaydudes")),
    ("jocks", "运动猛男", ("gay-jocks",)),
    ("hunks", "猛男", ("gay-hunks",)),
    ("muscular", "肌肉男", ("gay-muscular", "gay-muscle")),
    ("studs", "壮男", ("gay-studs",)),
    ("daddies", "熟爸", ("gay-daddies",)),
    ("emo", "情绪少年", ("gay-emo", "emo-gay", "gayemo")),
    ("gothic", "哥特", ("gay-gothic",)),
    ("sissy", "娘娘腔", ("gay-sissy",)),
    ("vampire", "吸血鬼", ("gay-vampire",)),
    ("bottom", "受", ("gay-bottom",)),
    ("skinny", "骨感", ("gay-skinny",)),
    ("small", "娇小", ("gay-small",)),
    ("baby fat", "微胖", ("gay-babyfat",)),
    ("natural", "天然", ("gay-natural",)),
    # --- hair color and length -------------------------------------------------
    ("brown hair", "棕发男", ("gay-brownhair", "gay-brown-hair", "gay-dirtyblond")),
    ("blond hair", "金发男", ("gay-blondhair",)),
    ("black hair", "黑发男", ("gay-blackhair",)),
    ("red hair", "红发男", ("gay-redhair",)),
    ("long hair", "长发男", ("gay-longhair",)),
    ("short hair", "短发男", ("gay-shorthair", "gay-short-hair")),
    ("shaved head", "光头", ("gay-shavedhead",)),
    # --- body -------------------------------------------------------------
    ("hairy", "多毛", ("gay-hairy",)),
    ("shaved", "剃毛", ("gay-shaved",)),
    ("trimmed", "修毛", ("gay-trimmed",)),
    ("tattoos", "纹身", ("gay-tattoos",)),
    ("uncut", "未割包皮", ("uncut", "gay-uncut", "uncut-dick", "uncut-cock")),
    ("cut", "已割包皮", ("gay-cut",)),
    ("big dick", "大屌", ("big-dick", "gay-largedick", "gay-big-cock", "big-cock", "huge-cock")),
    ("average dick", "中等尺寸", ("gay-averagedick",)),
    ("small dick", "小屌", ("gay-smalldick",)),
    ("cock", "肉棒", ("gay-cock",)),
    # --- acts -----------------------------------------------------------
    ("anal", "肛交", ("gay-anal",)),
    ("bareback", "无套", ("bareback", "gay-bareback", "barebacking", "bareback-gay-porn")),
    ("fucking", "操", ("fucking", "gay-fucking", "gay-fuck", "gay-bang")),
    ("doggystyle", "后入", ("doggystyle", "gay-doggystyle", "doggy-boys", "doggyboys")),
    ("blowjob", "口交", ("gay-blowjob", "free-blow-job-porn")),
    ("oral sex", "口交", ("gay-oralsex", "gay-oral-sex")),
    ("cock sucking", "吮屌", ("cock-sucking",)),
    ("deepthroat", "深喉", ("gay-deepthroat", "gay-deep-throat")),
    ("pov blowjob", "第一视角口交", ("pov-blowjob",)),
    ("rimming", "舔肛", ("gay-rimming", "ass-rimming", "gay-licking")),
    ("ass play", "玩肛", ("gay-assplay",)),
    ("ass to mouth", "肛交后口交", ("gay-asstomouth",)),
    ("sixty nine", "69", ("gay-69",)),
    ("double penetration", "双洞齐插", ("gay-doublepenetration",)),
    ("handjob", "手交", ("gay-handjob",)),
    ("jacking off", "打手枪", ("jacking-off", "gay-cumjerkingoff")),
    ("masturbation", "自慰", ("gay-masturbation",)),
    ("solo", "独自", ("gay-solo", "soloboy", "boy-solo")),
    ("kissing", "接吻", ("gay-kissing",)),
    ("facial", "颜射", ("gay-facial",)),
    ("cumshot", "射精", ("gay-cumshot", "cum-shot", "cum-shots", "gay-cum")),
    ("cum eating", "吃精", ("gay-cumeating", "gay-cumswapping")),
    ("creampie", "内射", ("gay-cumgettingfucked",)),
    ("bukkake", "群交颜射", ("gay-bukkake",)),
    ("threesome", "3P", ("gay-3some",)),
    ("group sex", "群交", ("gay-group", "group-sex")),
    ("gangbang", "轮操", ("gay-gangbang",)),
    ("orgy", "群P", ("gay-orgy",)),
    ("party", "派对", ("gay-party",)),
    ("pissing", "圣水", ("gay-pissing", "gayasianpiss")),
    ("smoking", "吸烟", ("gay-smoking",)),
    ("tickling", "挠痒", ("gay-tickling",)),
    ("wrestling", "摔跤", ("gay-wrestling",)),
    ("spanking", "打屁股", ("gay-spank",)),
    ("bondage", "捆绑", ("gay-bondage", "gaybondage")),
    ("domination", "支配", ("gay-domination",)),
    ("fetish", "恋物", ("gay-fetish", "gayfetish", "kink")),
    ("foot fetish", "恋足", ("gay-footfetish",)),
    ("medical fetish", "医检", ("medical-fetish", "gay-medical", "gay-medic",
                                "gay-physicals", "gay-examination", "gay-clinic", "gay-doctor")),
    ("uniforms", "制服", ("gay-uniforms", "gay-clothed")),
    ("sex toys", "情趣玩具", ("gay-toys", "sex-toy", "gay-boytoys", "gay-lollipop")),
    ("hardcore", "硬核", ("gay-hardcore", "gay-hadcore")),
    ("bizarre", "奇葩", ("gay-bizarre",)),
    ("glamour", "魅力", ("gay-glamour",)),
    # --- scenarios and roles ------------------------------------------------------
    ("straight guys", "直男", ("gay-straight", "gay-straight-boys", "straightturnedgay")),
    ("broken boys", "被掰弯", ("gay-broken", "gay-brokenboys")),
    ("friend", "好友", ("gay-friend",)),
    ("amateur", "素人", ("gay-amateur",)),
    ("pornstar", "男优", ("gay-pornstar",)),
    ("reality", "真实", ("gay-reality",)),
    ("college", "大学生", ("gay-college",)),
    ("interracial", "跨种族", ("gay-interracial",)),
    ("pov", "第一视角", ("gay-pov",)),
    ("behind the scenes", "幕后", ("gay-behindthescenes",)),
    ("voyeur", "偷窥", ("gay-voyeur",)),
    ("cruising", "猎艳", ("gay-cruising",)),
    ("money", "金钱交易", ("gay-money", "gay-cash")),
    ("pawn shop", "典当行", ("gaypawn", "gay-pawn", "gay-pawnshop", "gay-baitbus", "gay-shop")),
    ("webcams", "网络直播", ("gay-webcams",)),
    ("ai generated", "AI生成", ("ai", "aivideo")),
    # --- ethnicity and country -----------------------------------------------------
    ("asian", "亚洲男", ("asian", "asiaboy", "asiaboyvideo")),
    ("latino", "拉丁男", ("gay-latino", "latino")),
    ("black", "黑人", ("gay-black",)),
    ("american", "美国", ("gay-american",)),
    ("european", "欧洲男", ("gay-european",)),
    # --- locations ------------------------------------------------------------
    ("outdoors", "户外", ("gay-outdoors", "gay-outdoor")),
    ("public", "公共场合", ("gay-public",)),
    ("bedroom", "卧室", ("gay-inthebedroom",)),
    ("bathroom", "浴室", ("gay-inthebathroom",)),
    ("sofa", "沙发", ("gay-onthesofa",)),
    ("bus", "公交车", ("gay-bus",)),
    ("other location", "其他场景", ("gay-otherlocation",)),
]


def build_keywords(tag: str, label: str) -> str:
    """Builds ts_keywords: a space-separated tag variant plus the category name.

    Field syntax (see filter-keywords-docs.txt): space = AND, `|` = OR,
    `()` = grouping, `-` = NEGATION, `"..."` = adjacent words, `^`/`$` =
    start/end of word. So a bare tag like `ass-rimming` can't be written as-is:
    the hyphen reads as negation and the parser returns "Can not parse your
    filter keywords". A hyphen is only allowed inside quotes — that's how the
    file already does it (`"glory-hole"`, `"strap-on"`), but here it's simpler
    to just replace it with a space.

    This column doesn't pick the category itself: a Tags Based import selects
    a group by an exact match of the tag against `name`. Keywords are searched
    in the description, so the space-separated variant is more useful than the
    hyphenated one.
    """
    candidates: list[str] = []
    for value in (tag.replace("-", " ").replace("_", " "), label):
        if value and value not in candidates:
            candidates.append(value)

    parts: list[str] = []
    for value in candidates:
        if " " in value:
            parts.append(f'"{value}"')
        elif len(value) <= 3:
            # A short word without anchors matches inside other words
            # (`ai` in "hair", `cut` in "cute"), so pin down the boundaries.
            parts.append(f"^{value}$")
        else:
            parts.append(value)
    return "|".join(parts)


def keyword_problems(expr: str) -> list[str]:
    """Checks that the importer will actually be able to parse ts_keywords.

    Catches exactly the errors the CMS answers with "Can not parse your
    filter keywords": unmatched quotes and parens, empty alternatives around
    `|`, and above all a hyphen in the middle of a token (`ass-rimming`),
    which reads as a misplaced negation operator. A legal negation is a
    separate token with a space before it, like `shower -golden`.
    """
    problems: list[str] = []
    if expr.count('"') % 2:
        problems.append("unmatched quotes")

    depth = 0
    for char in expr:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                problems.append("extra closing parenthesis")
                break
    if depth > 0:
        problems.append("unclosed parenthesis")

    inside_quotes = False
    for index, char in enumerate(expr):
        if char == '"':
            inside_quotes = not inside_quotes
        elif char == "-" and not inside_quotes:
            previous = expr[index - 1] if index else " "
            following = expr[index + 1] if index + 1 < len(expr) else ""
            if previous not in " |(" or following in {"", " ", "|", ")"}:
                problems.append(f"hyphen in the middle of a token: {expr[max(0, index - 10):index + 10]}")

    if not expr.strip():
        problems.append("empty field")
    elif re.search(r"\|\s*\||^\s*\||\|\s*$", expr):
        problems.append("empty alternative around |")
    return problems


def parse_categories(path: Path) -> tuple[list[str], list[tuple[str, str]], str]:
    """Returns (header, rows as (name, rest), the original line separator)."""
    header: list[str] = []
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            header.append(line)
            continue
        name, _, rest = line.partition(";")
        rows.append((name.strip(), rest))
    return header, rows, "\n"


def parse_uncovered(path: Path) -> tuple[list[list[str]], Counter[str]]:
    """Tags from log rows with the "no groups selected" reason."""
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.search(r"\|\s*\(no groups selected\)\s*$", line)
        if not match:
            continue
        fields = line[: match.start()].split("|")
        if len(fields) < 6:
            continue
        rows.append([tag.strip().lower() for tag in fields[4].split(",") if tag.strip()])
    return rows, Counter(tag for row in rows for tag in row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--categories", type=Path, default=Path("gay-categories-zh.txt"))
    parser.add_argument("--uncovered", type=Path, default=Path("no-catrgories.txt"))
    parser.add_argument("--output", type=Path, default=Path("gay-categories-zh.txt"))
    parser.add_argument(
        "--report-threshold",
        type=int,
        default=5,
        help="Show uncovered tags that occur at least N times",
    )
    args = parser.parse_args()

    header, rows, newline = parse_categories(args.categories)
    existing = {name for name, _ in rows}

    added: list[tuple[str, str]] = []
    for label, translation, tags in RUBRICS:
        for tag in tags:
            if tag in existing:
                continue
            existing.add(tag)
            added.append((tag, f"{label} | {translation};{build_keywords(tag, label)}"))

    broken = [
        (name, rest.split(";", 1)[1], problems)
        for name, rest in added
        if (problems := keyword_problems(rest.split(";", 1)[1]))
    ]
    if broken:
        for name, expr, problems in broken:
            print(f"❌ {name}: {expr} — {'; '.join(problems)}")
        raise SystemExit("generated keywords the importer won't be able to parse")

    merged = sorted(rows + added, key=lambda row: row[0].lower())
    lines = header + [f"{name};{rest}" for name, rest in merged]
    args.output.write_text(newline.join(lines) + newline, encoding="utf-8")

    print(f"categories before: {len(rows)}, added: {len(added)}, after: {len(merged)}")
    print(f"file: {args.output}")

    suspect = [
        (name, rest.split(";", 1)[1])
        for name, rest in merged
        if ";" in rest and keyword_problems(rest.split(";", 1)[1])
    ]
    if suspect:
        print(f"\n⚠️ keywords in question ({len(suspect)}):")
        for name, expr in suspect:
            print(f"  {name}: {expr}")

    if not args.uncovered.exists():
        return

    log_rows, tag_counts = parse_uncovered(args.uncovered)
    covered = sum(1 for row in log_rows if any(tag in existing for tag in row))
    print(
        f"\nlog rows: {len(log_rows)}; will land in a category after this fix: "
        f"{covered} ({covered / len(log_rows) * 100:.1f}%)"
    )
    rest = [
        (tag, count)
        for tag, count in tag_counts.most_common()
        if tag not in existing and count >= args.report_threshold
    ]
    if rest:
        print(f"\nfrequent tags still without a category (>= {args.report_threshold}):")
        for tag, count in rest:
            print(f"  {count:6d}  {tag}")


if __name__ == "__main__":
    main()
