"""Build a compact category export from the tag table.

This is intentionally a one-off/reporting script. It never changes the database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer
from slugify import slugify
from sqlmodel import func, select

from src.config import (
    DEFAULT_AI_API_URL,
    DEFAULT_AI_BATCH_SIZE,
    DEFAULT_AI_MODEL,
    DEFAULT_CATEGORIES_DIR,
    DEFAULT_CATEGORIES_FILE,
    DEFAULT_CONFIDENCE,
    DEFAULT_DB_NAME,
    DEFAULT_MIN_VIDEO_COUNT,
    DEFAULT_NICHE,
    DEFAULT_SIMILARITY,
    ENV_FILE,
    display_path,
    resolve_categories_path,
    resolve_config_path,
    resolve_db_path,
)
from src.database.session import get_session, set_db_path
from src.export_presets import get_preset, preset_video_condition
from src.models import Tag, Video, VideoTagLink

load_dotenv(ENV_FILE)


@dataclass
class TagGroup:
    normalized: str
    aliases: set[str] = field(default_factory=set)
    video_count: int = 0


def normalize_tag(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("-", " ").replace("_", " ").replace("+", " ")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value)).strip()


def keyword_term(value: str) -> str:
    value = value.strip()
    return f'"{value}"' if " " in value else value


def clean_field(value: str) -> str:
    return value.replace(";", ",").replace("\n", " ").replace("\r", "").strip()


def load_tag_groups(min_video_count: int, video_condition=None) -> list[TagGroup]:
    """Counts tags and how many videos each has.

    video_condition restricts the selection to a subset of videos (e.g. an
    export preset): ALL tags of those videos are taken, and counts are
    computed within the subset, not across the whole database.
    """
    with get_session() as session:
        statement = (
            select(Tag.name, func.count(func.distinct(VideoTagLink.video_id)))
            .join(VideoTagLink, VideoTagLink.tag_id == Tag.id)
        )
        if video_condition is not None:
            statement = statement.where(
                VideoTagLink.video_id.in_(select(Video.id).where(video_condition))
            )
        rows = session.exec(
            statement
            .group_by(Tag.id, Tag.name)
            .having(func.count(func.distinct(VideoTagLink.video_id)) >= min_video_count)
        ).all()

    groups: dict[str, TagGroup] = {}
    for name, video_count in rows:
        normalized = normalize_tag(name)
        if not normalized:
            continue
        group = groups.setdefault(normalized, TagGroup(normalized=normalized))
        group.aliases.add(name)
        group.video_count += video_count
    return list(groups.values())


def cluster_groups(groups: list[TagGroup], threshold: float) -> list[list[int]]:
    if not groups:
        return []
    if len(groups) == 1:
        return [[0]]

    vectorizer = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 5),
        min_df=1,
    )
    embeddings = vectorizer.fit_transform(group.normalized for group in groups).toarray()
    try:
        clustering = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=1 - threshold,
            metric="cosine",
            linkage="average",
        )
    except TypeError:  # compatibility with older scikit-learn
        clustering = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=1 - threshold,
            affinity="cosine",
            linkage="average",
        )
    labels = clustering.fit_predict(embeddings)
    result: dict[int, list[int]] = defaultdict(list)
    for index, label in enumerate(labels):
        result[int(label)].append(index)
    return list(result.values())


async def ask_ai(
    client: httpx.AsyncClient,
    clusters: list[list[TagGroup]],
    model: str,
    batch_size: int,
    niche: str = DEFAULT_NICHE,
) -> dict[int, dict[str, object]]:
    api_key = __import__("os").environ.get("TRANSLATION_AI_API_KEY")
    api_url = __import__("os").environ.get("TRANSLATION_AI_API_URL", DEFAULT_AI_API_URL)
    if not api_key:
        return {}

    prompt = (
        f"You are creating a compact taxonomy for {niche}. "
        "For each cluster, choose one useful English category name and slug. "
        "Do not merge unrelated meanings. Return one compact JSON object per line with keys "
        "cluster_id, name, slug, confidence, reason."
    )
    labels: dict[int, dict[str, object]] = {}
    for offset in range(0, len(clusters), batch_size):
        payload = [
            {
                "cluster_id": index,
                "tags": sorted(group.normalized for group in cluster),
                "video_count": sum(group.video_count for group in cluster),
            }
            for index, cluster in enumerate(clusters[offset : offset + batch_size], offset)
        ]
        response = await client.post(
            api_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            batch_labels = json.loads(content)
            if isinstance(batch_labels, dict):
                batch_labels = batch_labels.get("items", [batch_labels])
        except json.JSONDecodeError:
            batch_labels = []
            for line in content.splitlines():
                try:
                    item = json.loads(line.strip())
                    if isinstance(item, dict):
                        batch_labels.append(item)
                except json.JSONDecodeError:
                    continue
            if not batch_labels:
                print(f"⚠️ AI returned no valid JSON objects for clusters {offset}-{offset + len(payload) - 1}")
        labels.update({int(item["cluster_id"]): item for item in batch_labels})
    return labels


async def load_suggestions(
    client: httpx.AsyncClient,
    names: list[str],
    cache_path: Path,
) -> dict[str, list[str]]:
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    for name in names:
        if name in cache:
            continue
        response = await client.get(
            f"https://www.xvideos.com/search-suggest/gay/{quote(name)}",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        if response.status_code != 200:
            cache[name] = []
            continue
        cache[name] = [item["N"] for item in response.json().get("data", {}).get("keywords", [])]
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    return cache


@dataclass
class SeedCategory:
    """A category from a ready-made third-party category file."""

    name: str
    slug: str
    terms: list[str]
    matched_tags: dict[str, int] = field(default_factory=dict)
    video_count: int = 0


def load_seed_categories(path: Path) -> list[SeedCategory]:
    """Reads a file shaped like `name;slug;keywords;;;…`.

    Different sites write keywords differently: parenthesized alternatives
    (`big ass|(large butt)`) and regex anchors (`^18$`, `^adorabl`). Both get
    reduced to a plain term — anchors and parens are stripped.
    """
    categories: list[SeedCategory] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        parts = line.split(";")
        if len(parts) < 3 or not parts[0].strip():
            continue
        name, slug = parts[0].strip(), parts[1].strip()
        terms: list[str] = []
        for raw in parts[2].split("|"):
            term = normalize_tag(raw.strip().strip("()").strip().lstrip("^").rstrip("$"))
            if term:
                terms.append(term)
        terms = list(dict.fromkeys(terms)) or [normalize_tag(name)]
        categories.append(SeedCategory(name=name, slug=slug or slugify(name), terms=terms))
    return categories


def load_tag_videos(min_video_count: int, video_condition=None) -> dict[str, set[int]]:
    """Returns {normalized tag: set of video ids} over a subset."""
    with get_session() as session:
        statement = select(Tag.name, VideoTagLink.video_id).join(
            VideoTagLink, VideoTagLink.tag_id == Tag.id
        )
        if video_condition is not None:
            statement = statement.where(
                VideoTagLink.video_id.in_(select(Video.id).where(video_condition))
            )
        rows = session.exec(statement).all()

    tag_videos: dict[str, set[int]] = defaultdict(set)
    for name, video_id in rows:
        normalized = normalize_tag(name)
        if normalized:
            tag_videos[normalized].add(video_id)
    return {tag: videos for tag, videos in tag_videos.items() if len(videos) >= min_video_count}


def match_tags_to_categories(
    categories: list[SeedCategory], tag_videos: dict[str, set[int]]
) -> set[str]:
    """Sorts our tags into categories; returns tags that matched nothing.

    A term is matched from the start of a word: `japan` will catch
    `japanese`, but `asian` won't catch `caucasian`.
    """
    patterns = [
        (category, [re.compile(rf"\b{re.escape(term)}") for term in category.terms])
        for category in categories
    ]
    uncovered = set(tag_videos)
    for category, compiled in patterns:
        videos: set[int] = set()
        for tag, tag_video_ids in tag_videos.items():
            if any(pattern.search(tag) for pattern in compiled):
                category.matched_tags[tag] = len(tag_video_ids)
                videos |= tag_video_ids
                uncovered.discard(tag)
        category.video_count = len(videos)
    return uncovered


def write_seed_categories(
    categories: list[SeedCategory], uncovered: dict[str, int], output: Path
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for category in categories:
        keywords = list(dict.fromkeys(category.terms + sorted(category.matched_tags)))
        lines.append(
            f"{category.slug};{clean_field(category.name)};"
            + "|".join(keyword_term(keyword) for keyword in keywords)
        )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")

    output.with_name("category-stats.json").write_text(
        json.dumps(
            [
                {
                    "slug": category.slug,
                    "name": category.name,
                    "video_count": category.video_count,
                    "matched_tags": len(category.matched_tags),
                }
                for category in categories
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    empty = [category for category in categories if not category.video_count]
    output.with_name("categories-empty.csv").write_text(
        "category;slug;terms\n"
        + "\n".join(f"{c.name};{c.slug};{'|'.join(c.terms)}" for c in empty)
        + "\n",
        encoding="utf-8",
    )
    output.with_name("tags-uncovered.csv").write_text(
        "tag;video_count\n"
        + "\n".join(
            f"{tag};{count}"
            for tag, count in sorted(uncovered.items(), key=lambda item: -item[1])
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Exported {len(categories)} categories to {display_path(output)}")
    print(f"  with no videos in our DB: {len(empty)} (categories-empty.csv)")
    print(f"  our tags outside any category: {len(uncovered)} (tags-uncovered.csv)")


async def main(args: argparse.Namespace) -> None:
    # Resolve paths once: the rest of the code uses ready Path objects.
    args.output = resolve_categories_path(args.output)
    if args.seed_categories:
        args.seed_categories = resolve_config_path(args.seed_categories)
    if args.extra_categories:
        args.extra_categories = resolve_config_path(args.extra_categories)

    args.db = resolve_db_path(args.db)
    set_db_path(args.db)

    video_condition = None
    if args.preset:
        preset = get_preset(args.preset)
        video_condition = preset_video_condition(preset, tags_only=args.preset_tags_only)
        print(f"Preset '{preset.name}': {preset.description}")

    if args.seed_categories:
        # The taxonomy comes from a ready-made file — no clustering or AI
        # needed, just match categories against our tags and count videos.
        categories = load_seed_categories(args.seed_categories)
        if args.extra_categories:
            known_slugs = {category.slug for category in categories}
            extra = [
                category
                for category in load_seed_categories(args.extra_categories)
                if category.slug not in known_slugs
            ]
            categories += extra
            print(f"Extra: +{len(extra)} categories from {display_path(args.extra_categories)}")
        tag_videos = load_tag_videos(args.min_video_count, video_condition)
        print(f"Seed: {len(categories)} categories from {display_path(args.seed_categories)}, "
              f"our tags: {len(tag_videos)}")
        uncovered_tags = match_tags_to_categories(categories, tag_videos)
        write_seed_categories(
            categories,
            {tag: len(tag_videos[tag]) for tag in uncovered_tags},
            args.output,
        )
        return

    groups = load_tag_groups(args.min_video_count, video_condition)
    print(f"Loaded {len(groups)} normalized tag groups from {display_path(args.db)}")
    clusters_indexes = cluster_groups(groups, args.similarity)
    clusters = [[groups[index] for index in cluster] for cluster in clusters_indexes]
    print(f"Built {len(clusters)} clusters")

    ai_labels: dict[int, dict[str, object]] = {}
    if not args.no_ai:
        async with httpx.AsyncClient(timeout=120) as client:
            ai_labels = await ask_ai(client, clusters, args.model, args.ai_batch_size, args.niche)

    suggestions: dict[str, list[str]] = {}
    if args.suggestions:
        cache_path = args.output.parent / "search-suggestions-cache.json"
        async with httpx.AsyncClient(timeout=30) as client:
            suggestions = await load_suggestions(
                client,
                [str(ai_labels.get(i, {}).get("name", "")) for i in range(len(clusters))],
                cache_path,
            )

    normalized_names = {group.normalized for group in groups}
    categories = []
    mapping = []
    review = []
    used_slugs: set[str] = set()
    for index, cluster in enumerate(clusters):
        label = ai_labels.get(index, {})
        fallback = max(cluster, key=lambda group: group.video_count)
        name = str(label.get("name") or fallback.normalized)
        category_slug = slugify(str(label.get("slug") or name)) or f"category-{index + 1}"
        if category_slug in used_slugs:
            category_slug = f"{category_slug}-{index + 1}"
        used_slugs.add(category_slug)

        aliases = {normalize_tag(alias) for group in cluster for alias in group.aliases}
        aliases.update(normalize_tag(alias) for alias in suggestions.get(name, []))
        aliases = {alias for alias in aliases if alias in normalized_names}
        keywords = "|".join(keyword_term(alias) for alias in sorted(aliases))
        confidence = float(label.get("confidence", 0.75 if len(cluster) == 1 else 0.0))
        categories.append((category_slug, name, keywords, confidence, sum(g.video_count for g in cluster)))
        if confidence < args.confidence:
            review.append((name, confidence, str(label.get("reason", "low confidence"))))
        for group in cluster:
            for alias in group.aliases:
                mapping.append((alias, group.video_count, name, confidence, "normalized+embedding"))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(f"{slug};{clean_field(name)};{keywords}" for slug, name, keywords, _, _ in categories)
        + "\n",
        encoding="utf-8",
    )
    args.output.with_name("tag-category-map.csv").write_text(
        "source_tag;video_count;category;confidence;method\n"
        + "\n".join(";".join(map(str, row)) for row in mapping)
        + "\n",
        encoding="utf-8",
    )
    args.output.with_name("categories-review.csv").write_text(
        "category;confidence;reason\n"
        + "\n".join(";".join(map(str, row)) for row in review)
        + "\n",
        encoding="utf-8",
    )
    args.output.with_name("category-stats.json").write_text(
        json.dumps(
            [
                {"slug": slug, "name": name, "confidence": confidence, "video_count": count}
                for slug, name, _, confidence, count in categories
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Exported {len(categories)} categories to {display_path(args.output)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=DEFAULT_CATEGORIES_FILE,
        help=f"File name (goes into {DEFAULT_CATEGORIES_DIR}/) or a path with a slash; "
             "tag-category-map.csv, categories-review.csv, category-stats.json land next to it",
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB_NAME, help="DB file name (looked up in db/) or a path with a slash"
    )
    parser.add_argument(
        "--seed-categories",
        default=None,
        help="Ready-made categories file (name;slug;keywords): use its categories "
             "as the taxonomy and extend their keywords with tags from the DB",
    )
    parser.add_argument(
        "--preset", default=None, help="Restrict videos to an export preset (e.g. asian)"
    )
    parser.add_argument(
        "--preset-tags-only", action="store_true", help="Only consider tags in the preset"
    )
    parser.add_argument("--niche", default=DEFAULT_NICHE, help="Topic for the AI tagging prompt")
    parser.add_argument(
        "--extra-categories",
        default=None,
        help="File with extra categories in the same format; appended to "
             "--seed-categories, duplicate slugs are skipped",
    )
    parser.add_argument(
        "--min-video-count",
        type=int,
        default=DEFAULT_MIN_VIDEO_COUNT,
        help="Threshold on a tag's video count. Raise it for large DBs: "
             "13k+ groups expand into a matrix several GB in size",
    )
    parser.add_argument("--similarity", type=float, default=DEFAULT_SIMILARITY)
    parser.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE)
    parser.add_argument("--model", default=DEFAULT_AI_MODEL)
    parser.add_argument("--ai-batch-size", type=int, default=DEFAULT_AI_BATCH_SIZE)
    parser.add_argument("--suggestions", action="store_true")
    parser.add_argument("--no-ai", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
