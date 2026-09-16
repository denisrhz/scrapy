from datetime import UTC, datetime
from enum import IntFlag
from typing import Optional

from sqlalchemy import Column, Integer, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class VideoStage(IntFlag):
    NONE = 0
    PARSED = 1
    REWRITTEN = 2
    TRANSLATED = 4
    DOWNLOADED = 8
    EXPORTED = 16
    FAILED = 32
    REPARSE = 64


# ---------------- Link tables (Many-to-Many) ----------------

class VideoTagLink(SQLModel, table=True):
    """Many-to-many link table between Video and Tag."""
    video_id: int | None = Field(default=None, foreign_key="video.id", primary_key=True)
    tag_id: int | None = Field(default=None, foreign_key="tag.id", primary_key=True)


class VideoModelActorLink(SQLModel, table=True):
    """Many-to-many link table between Video and ModelActor."""
    video_id: int | None = Field(default=None, foreign_key="video.id", primary_key=True)
    model_actor_id: int | None = Field(default=None, foreign_key="modelactor.id", primary_key=True)


# ---------------- Metadata (Tags, Models, Channels) ----------------

class Tag(SQLModel, table=True):
    """Tag model."""
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    url: str | None = None
    total_pages: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    videos: list["Video"] = Relationship(back_populates="tags", link_model=VideoTagLink)


class ModelActor(SQLModel, table=True):
    """Actor/model model."""
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    url: str | None = None
    thumb_url: str | None = None
    gender: str | None = None
    age: int | None = None
    country: str | None = None
    subscribers: int | None = None
    views: int | None = None
    description: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    videos: list["Video"] = Relationship(back_populates="models", link_model=VideoModelActorLink)


class Channel(SQLModel, table=True):
    """Channel / studio / uploader model."""
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    url: str | None = None
    subscribers: int | None = None
    views: int | None = None
    description: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    videos: list["Video"] = Relationship(back_populates="channel")


# ---------------- Sources ----------------

class Source(SQLModel, table=True):
    """Source domain (e.g. xvideos.com)."""
    id: int | None = Field(default=None, primary_key=True)
    domain: str = Field(unique=True)
    code: str = Field(unique=True)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    entrypoints: list["SourceEntrypoint"] = Relationship(back_populates="source")
    videos: list["Video"] = Relationship(back_populates="source")


class SourceEntrypoint(SQLModel, table=True):
    """A scraping entry point for a source (section, tag, category)."""
    id: int | None = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="source.id", index=True)
    path: str = Field(index=True)
    is_active: bool = Field(default=True)
    last_parsed_page: int = Field(default=1)
    total_pages: int | None = Field(default=None)
    is_completed: bool = Field(default=False, index=True)
    last_parsed_at: datetime | None = None

    source: Optional["Source"] = Relationship(back_populates="entrypoints")
    videos: list["Video"] = Relationship(back_populates="source_entrypoint")


# ---------------- Main Video model ----------------

class Video(SQLModel, table=True):
    """Video model."""
    id: int | None = Field(default=None, primary_key=True)

    source_id: int | None = Field(default=None, foreign_key="source.id", index=True)
    source_entrypoint_id: int | None = Field(default=None, foreign_key="sourceentrypoint.id", index=True)
    channel_id: int | None = Field(default=None, foreign_key="channel.id", index=True)

    url: str = Field(unique=True)
    thumb_url: str
    download_url: str | None = None
    alt: str | None = None
    desc: str | None = None
    duration: int = Field(default=0)
    views: int = Field(default=0)

    status: int = Field(
        default=int(VideoStage.PARSED),
        sa_column=Column(Integer, nullable=False, default=int(VideoStage.PARSED)),
    )
    error_message: str | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"onupdate": lambda: datetime.now(UTC)},
    )

    # One-to-many relationships
    source: Optional["Source"] = Relationship(back_populates="videos")
    source_entrypoint: Optional["SourceEntrypoint"] = Relationship(back_populates="videos")
    channel: Optional["Channel"] = Relationship(back_populates="videos")

    # Many-to-many relationships
    tags: list[Tag] = Relationship(back_populates="videos", link_model=VideoTagLink)
    models: list[ModelActor] = Relationship(back_populates="videos", link_model=VideoModelActorLink)
    translations: list["VideoTranslation"] = Relationship(back_populates="video")


class VideoTranslation(SQLModel, table=True):
    """Video title/description translation for a specific language."""
    __table_args__ = (
        UniqueConstraint("video_id", "language"),
        UniqueConstraint("language", "slug"),
    )

    id: int | None = Field(default=None, primary_key=True)
    video_id: int = Field(foreign_key="video.id", index=True)
    language: str = Field(index=True)
    title: str
    description: str = ""
    slug: str = Field(index=True)
    provider: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    video: "Video" = Relationship(back_populates="translations")
