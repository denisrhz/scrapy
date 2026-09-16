
from pydantic import BaseModel


class ParsedTagData(BaseModel):
    """Tag data schema."""
    name: str
    url: str | None = None


class ParsedActorData(BaseModel):
    """Detailed actor/model data schema."""
    name: str
    url: str | None = None
    thumb_url: str | None = None
    gender: str | None = None
    age: int | None = None
    country: str | None = None
    subscribers: int | None = None
    views: int | None = None
    description: str | None = None


class ParsedVideoData(BaseModel):
    """DTO for a parsed video."""
    url: str
    thumb_url: str
    download_url: str | None = None
    alt: str | None = None
    desc: str | None = None
    duration: int = 0
    views: int = 0
    tags: list[ParsedTagData] = []
    models: list[ParsedActorData] = []
    channel: str | None = None
    channel_url: str | None = None
