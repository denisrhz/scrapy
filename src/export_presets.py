"""Ready-made filter sets for the export command.

A preset is a curated list of SQL LIKE patterns, not a single substring: tags
on xvideos are set by uploaders, so "asian" is scattered across dozens of
names (`asian`, `japanese`, `jav`, `thai`, `chinese`…), and a naive `%asian%`
also pulls in `caucasian`. Hence separate include/exclude lists.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExportPreset:
    """Filters videos by tags and (optionally) by title."""

    name: str
    description: str
    tag_include: tuple[str, ...]
    tag_exclude: tuple[str, ...] = ()
    # Patterns for alt: pick up videos the uploader just didn't tag.
    title_include: tuple[str, ...] = ()
    title_exclude: tuple[str, ...] = ()


ASIAN = ExportPreset(
    name="asian",
    description="East and Southeast Asia by tags and title",
    tag_include=(
        "%asia%",        # asian, asians, asiatica, littleasians, gayasian…
        "%japan%",       # japanese, japan, japanese-hentai-game
        "jav%",          # jav, javhub, javz, jav-guru
        "%-jav",         # best-jav, uncensored-jav
        "china%",
        "%chinese%",
        "%-china",
        "%korea%",
        "%thai%",
        "%pinoy%",
        "%filipin%",
        "%philippin%",
        "%vietnam%",
        "%indonesi%",
        "%malaysia%",
        "%taiwan%",
        "%hongkong%",
        "%hong-kong%",
        "%singapor%",
        "%mongolian%",
        "%cambodia%",
    ),
    tag_exclude=(
        "%caucasian%",   # contains "asian"
        "%anastasia%",   # contains "asia"
        "fantasia",
        "kasia",
        "%javi%",        # magic-javi — a name, not JAV
        "%shorthair%",   # contains "thai"
    ),
    # This list mirrors tag_include: it used to be noticeably narrower
    # (`%asian%` instead of `%asia%`, `%korean%` instead of `%korea%`, no
    # China or Taiwan), so videos without an Asian tag but a title like
    # "China boy home made" or "LATINO SE ENAMORA DE UN ASIATICO" got missed.
    title_include=(
        "%asia%",
        "%japan%",
        "%thai%",
        "%chinese%",
        "%china%",
        "%korea%",
        "%pinoy%",
        "%pinay%",
        "%filipin%",
        "%philippin%",
        "%vietnam%",
        "%indonesi%",
        "%malaysia%",
        "%taiwan%",
        "%hongkong%",
        "%hong kong%",
        "%singapor%",
        "%mongolian%",
        "%cambodia%",
        "%myanmar%",
        "%burmese%",
        "%khmer%",
    ),
    title_exclude=(
        "%caucasian%",   # contains "asia"
        "%anastasia%",
        "%fantasia%",
        "%shorthair%",   # contains "thai"
        "%short hair%",
    ),
)

PRESETS: dict[str, ExportPreset] = {ASIAN.name: ASIAN}


def get_preset(name: str) -> ExportPreset:
    """Returns a preset by name, or raises ValueError listing what's available."""
    try:
        return PRESETS[name]
    except KeyError:
        available = ", ".join(sorted(PRESETS)) or "none"
        raise ValueError(f"Unknown preset: {name}. Available: {available}") from None


def preset_video_condition(preset: ExportPreset, tags_only: bool = False):
    """Condition for Video: does it match the preset.

    Tags are checked via a subquery, not a join: a join would duplicate rows
    for videos with several matching tags. Title (alt) is a second OR branch
    that picks up videos the uploader didn't tag.
    """
    from sqlmodel import and_, func, or_, select

    from src.models import Tag, Video, VideoTagLink

    tag_match = select(VideoTagLink.video_id).join(Tag, Tag.id == VideoTagLink.tag_id).where(
        or_(*(Tag.name.like(pattern) for pattern in preset.tag_include))
    )
    for pattern in preset.tag_exclude:
        tag_match = tag_match.where(Tag.name.not_like(pattern))

    conditions = [Video.id.in_(tag_match)]
    if not tags_only and preset.title_include:
        title = func.lower(Video.alt)
        title_conditions = [or_(*(title.like(pattern) for pattern in preset.title_include))]
        title_conditions += [title.not_like(pattern) for pattern in preset.title_exclude]
        conditions.append(and_(*title_conditions))
    return or_(*conditions)
