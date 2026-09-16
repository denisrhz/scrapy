from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

import httpx

from src.scrapers.schemas import ParsedVideoData
from src.scrapers.throttle import Throttle


class BaseScraper(ABC):
    """Abstract base class for all site scrapers."""

    def __init__(self, client: httpx.AsyncClient, throttle: Throttle | None = None) -> None:
        self.client = client
        # Throttle is shared for the whole run and sets the pace for all requests.
        # Separate scraper instances (one per model/listing) share the same one.
        self.throttle = throttle if throttle is not None else Throttle(rps=8.0, concurrency=1)
        # Listing page the scraper is working on right now.
        # The orchestrator uses this to save progress (last_parsed_page).
        self.current_page = 1
        self.total_pages: int | None = None

    @abstractmethod
    async def fetch_page_links(
        self, url: str, max_pages: int = 0, start_page: int = 1
    ) -> AsyncGenerator[str, None]:
        """Collects video links from the given listing page.

        Must be a generator so links are yielded as they're found.
        Must update self.current_page whenever it moves to a new page.
        """
        yield ""  # placeholder for the abstract generator method

    @abstractmethod
    async def parse_video(self, url: str) -> ParsedVideoData | None:
        """Parses a single video page and returns a ParsedVideoData DTO."""

    def parse_iso_duration(self, iso_str: str | None) -> int:
        """Converts an ISO 8601 duration (e.g. PT1H2M10S or PT45S) to seconds."""
        if not iso_str:
            return 0
        
        pattern = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
        match = pattern.match(iso_str)
        if not match:
            return 0
            
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        
        return hours * 3600 + minutes * 60 + seconds
