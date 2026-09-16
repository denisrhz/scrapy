from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from selectolax.parser import HTMLParser

from src.scrapers.base import BaseScraper
from src.scrapers.schemas import ParsedActorData, ParsedTagData, ParsedVideoData
from src.scrapers.throttle import Throttle


class XvideosScraper(BaseScraper):
    """Scraper for the XVideos site."""

    def __init__(self, client: httpx.AsyncClient, throttle: Throttle | None = None) -> None:
        super().__init__(client, throttle)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:144.0) Gecko/20100101 Firefox/144.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Connection": "keep-alive",
            # noqa: E501 — session cookie is a long opaque token
            "Cookie": (
                "session_token=24381ea72fbd10e8Zzl5l74cAQpbJ2YIH8zZnhNrz9cHSlYTuJAVuezcdxmsQEscRaeabYTNFNrahljtYucuj08uefbaJyhrAoxMXv0r5f22_6oYGNk1__dP5VVohy86OB5uGFc4fmlNFz9u-VeGwUh37_4X9b6FkFAVwDxgEedf8qUaQT_wEVCrm3VH49nhEBLQI5fhwravdKNArXJwP3mm34qKd2QlVo5W8XpruChKfW4DU84hZwTZ_tAB_3xlIJSN2fHspOd2QlUWW6Ym70Dyk-Fa1oNx7YHfZQsLhoiRUZ3VVe3THLoF7ylWF8nXNYOi4c15o_W3yOi6jRVGIx8NogOnsB-9HBlDecQFHuSkGJxuyQJIpaICN04%3D"
            ),
        }

    async def _get(self, url: str, **kwargs) -> httpx.Response | None:
        """GET with retries. The single request choke point — throttling lives here too."""
        retry_statuses = {408, 425, 429, 500, 502, 503, 504}
        attempts = 4
        for attempt in range(attempts):
            try:
                async with self.throttle:
                    response = await self.client.get(url, **kwargs)
                if response.status_code not in retry_statuses:
                    return response
                self.throttle.penalize(response.status_code)
                if attempt < attempts - 1:
                    delay = 2 ** attempt
                    print(
                        f"⚠️ HTTP {response.status_code} for {url}; retrying in {delay}s "
                        f"({attempt + 1}/{attempts - 1})"
                    )
                    await asyncio.sleep(delay)
            except httpx.RequestError as error:
                if attempt >= attempts - 1:
                    print(f"❌ Network error after {attempts} attempts: {url}: {error}")
                    return None
                delay = 2 ** attempt
                print(f"⚠️ Network error for {url}; retrying in {delay}s")
                await asyncio.sleep(delay)
        return response

    def _parse_int(self, text: str | None) -> int | None:
        """Strips commas/spaces from a string and turns it into an int."""
        if not text:
            return None
        cleaned = re.sub(r"[^\d]", "", text)
        return int(cleaned) if cleaned else None

    def _parse_k_number(self, text: str | None) -> int | None:
        """Converts strings like '252k' or '1.5M' into integers."""
        if not text:
            return None
        text = text.lower().strip()
        if "k" in text:
            val = float(text.replace("k", ""))
            return int(val * 1000)
        if "m" in text:
            val = float(text.replace("m", ""))
            return int(val * 1000000)
        return self._parse_int(text)

    # Sections where videos live behind a JSON endpoint instead of HTML.
    _PROFILE_PREFIXES = ("profiles", "channels", "models", "pornstars")

    def _profile_api_base(self, url: str) -> str | None:
        """Base JSON API URL, if this is a channel/profile/model listing.

        This page's HTML only returns a sidebar, videos come from a separate
        JSON endpoint. A bare name like `/javhd#_tabVideos` lives under `/channels/`.
        """
        parsed_url = urlsplit(url)
        if parsed_url.query:
            return None
        segments = [segment for segment in parsed_url.path.split("/") if segment]

        if len(segments) == 2 and segments[0] in self._PROFILE_PREFIXES:
            path = f"/{segments[0]}/{segments[1]}"
        elif len(segments) == 1 and parsed_url.fragment.startswith("_tab"):
            path = f"/channels/{segments[0]}"
        else:
            return None

        return urlunsplit((parsed_url.scheme, parsed_url.netloc, path, "", ""))

    def _build_listing_page_url(self, url: str, page_num: int) -> str:
        """Builds the starting URL for a regular, tag, or search listing."""
        parsed_url = urlsplit(url)
        path = parsed_url.path.rstrip("/") or "/"
        query = dict(parse_qsl(parsed_url.query, keep_blank_values=True))

        if "p" in query:
            query["p"] = str(page_num)
        elif "/tags/" in path and re.search(r"/\d+$", path):
            path = re.sub(r"/\d+$", f"/{page_num}", path)
        elif page_num > 1:
            path = f"{path}/{page_num - 1}"

        return urlunsplit(
            (parsed_url.scheme, parsed_url.netloc, path, urlencode(query), parsed_url.fragment)
        )

    async def fetch_page_links(self, url: str, max_pages: int = 0, start_page: int = 1) -> AsyncGenerator[str, None]:
        """Parses listing pages and extracts video links. max_pages=0 means no limit."""
        api_base = self._profile_api_base(url)
        if api_base is not None:
            print(f"↪️ Channel/profile listing — going through the JSON API: {api_base}")
            async for link in self.fetch_model_video_links(
                api_base, start_page=start_page, max_pages=max_pages
            ):
                yield link
            return

        parsed_url = urlsplit(url)
        fragment_base = re.sub(r",page-\d+$", "", parsed_url.fragment)
        is_model_videos_pagination = fragment_base.startswith("_tabVideos,rating")
        page_num = start_page

        while max_pages == 0 or page_num < start_page + max_pages:
            # Page 1 is the base URL, page 2 is URL/1, page 3 is URL/2, etc.
            if is_model_videos_pagination:
                page_url = urlunsplit(
                    (parsed_url.scheme, parsed_url.netloc, parsed_url.path.rstrip("/"), parsed_url.query, "")
                )
                current_url = f"{page_url}#{fragment_base},page-{page_num}"
            else:
                current_url = url if page_num > start_page else self._build_listing_page_url(url, start_page)
            self.current_page = page_num
            print(f"\n📄 [Page {page_num}] Fetching: {current_url}")
            try:
                response = await self._get(current_url, headers=self.headers, follow_redirects=True)
                if response is None:
                    break
                if response.status_code != 200:
                    print(f"⚠️ Error loading list page: {current_url} Status: {response.status_code}")
                    break

                html = response.text
                parser = HTMLParser(html)

                location = parser.css_first(".your-country, #site-localisation .txt")
                location_text = location.text().strip() if location else "Not Found"
                print(f"📍 [Location: {location_text}]")

                thumbs = parser.css(".video-listing .thumb, .mozaique .thumb-block, .thumb-block, .thumb")
                if not thumbs:
                    print(f"⚠️ No .thumb elements found on {current_url}")
                    break

                next_page = parser.css_first("a.next-page, a.dir.next")
                next_href = next_page.attributes.get("href") if next_page else None
                page_numbers = [
                    int(link.text().strip())
                    for link in parser.css(".pagination a")
                    if link.text().strip().isdigit()
                ]
                if page_numbers:
                    self.total_pages = max(page_numbers)
                has_next_page = bool(next_page and next_href)
                if self.total_pages is not None:
                    has_next_page = has_next_page and page_num < self.total_pages
                next_url = urljoin(str(response.url), next_href) if has_next_page else None

                found_links = 0
                for thumb in thumbs:
                    a_tag = thumb.css_first("a")
                    if a_tag:
                        href = a_tag.attributes.get("href")
                        if href:
                            absolute_url = urljoin(str(response.url), href)
                            found_links += 1
                            yield absolute_url

                print(f"🔍 Found {found_links} video link(s) on page {page_num}")

                if not has_next_page:
                    if self.total_pages is None:
                        self.total_pages = page_num
                    print(f"🛑 Last page: Next is missing or unavailable (page {page_num})")
                    break

                page_num += 1
                if next_url:
                    url = next_url

            except Exception as e:
                print(f"❌ Error while fetching listing page: {e}")
                break

    async def fetch_model_video_links(
        self, model_url: str, start_page: int = 1, max_pages: int = 0
    ) -> AsyncGenerator[str, None]:
        """Model video links via the JSON API. The API pages from 0, we expose 1-based."""
        parsed_url = urlsplit(model_url)
        model_path = parsed_url.path.rstrip("/")
        page_num = max(start_page, 1)
        # Last page that actually returned videos. When the API says there
        # are no more pages, the position needs to roll back to it: otherwise
        # last_parsed_page ends up pointing at a page that doesn't exist and
        # the next run immediately gets a 404.
        last_filled_page: int | None = None

        def rewind() -> None:
            self.current_page = last_filled_page or 1

        while max_pages == 0 or page_num < start_page + max_pages:
            api_url = (
                f"{parsed_url.scheme}://{parsed_url.netloc}{model_path}"
                f"/videos/best/{page_num - 1}"
            )
            self.current_page = page_num
            print(f"\n📄 [Model page {page_num}] Fetching: {api_url}")

            try:
                response = await self._get(api_url, headers=self.headers, follow_redirects=True)
                if response is None:
                    break
                if response.status_code == 404:
                    print(f"🛑 Last model page: API returned 404 (page {page_num})")
                    rewind()
                    break
                if response.status_code != 200:
                    print(f"⚠️ Error loading model videos: {response.status_code}")
                    break

                data = response.json()
                if data.get("code") == 404 or not data.get("result"):
                    print(f"🛑 Last model page: code={data.get('code')}")
                    rewind()
                    break

                videos = data.get("videos", [])
                if not videos:
                    print(f"🛑 Empty model page: page {page_num}")
                    rewind()
                    break

                total_videos = data.get("nb_videos")
                per_page = data.get("nb_per_page")
                if total_videos and per_page:
                    self.total_pages = -(-int(total_videos) // int(per_page))

                print(f"🔍 Found {len(videos)} video link(s) on model page {page_num}")
                last_filled_page = page_num
                for video in videos:
                    video_id = video.get("eid")
                    video_path = video.get("u", "")
                    video_slug = video_path.rstrip("/").split("/")[-1]
                    if video_id and video_slug:
                        yield f"{parsed_url.scheme}://{parsed_url.netloc}/video.{video_id}/{video_slug}"

                page_num += 1

            except Exception as e:
                print(f"❌ Error while fetching model videos: {e}")
                break

    async def parse_actor(self, profile_url: str, name_placeholder: str) -> ParsedActorData | None:
        """Parses a model (actor) profile page in detail."""
        target_url = profile_url if "#" in profile_url else f"{profile_url}#_tabAboutMe"
        print(f"   👤 Parsing model in detail: {profile_url}")
        
        try:
            response = await self._get(target_url, headers=self.headers, follow_redirects=True)
            if response is None:
                return ParsedActorData(name=name_placeholder, url=profile_url)
            if response.status_code != 200:
                print(f"   ⚠️ Failed to load model page: {profile_url} ({response.status_code})")
                return ParsedActorData(name=name_placeholder, url=profile_url)

            html = response.text
            parser = HTMLParser(html)

            # Profile name
            title_node = parser.css_first("h2.profile-title, h1, h2")
            name = ""
            if title_node:
                name = title_node.text().split("\n")[0].strip()
            if not name:
                name = name_placeholder
            name = re.sub(r"\s+Official\s+profile.*", "", name, flags=re.IGNORECASE).strip()
            name = re.sub(r"\s+Pornstar.*", "", name, flags=re.IGNORECASE).strip()

            gender = None
            age = None
            country = None
            subscribers = None
            views = None

            gender_node = parser.css_first("#pinfo-sex span")
            if gender_node:
                gender = gender_node.text().strip()

            age_node = parser.css_first("#pinfo-age span")
            if age_node:
                age_text = age_node.text().strip()
                age_match = re.search(r"\d+", age_text)
                if age_match:
                    age = int(age_match.group(0))

            country_node = parser.css_first("#pinfo-country span")
            if country_node:
                country = country_node.text().strip()

            subscribers_node = parser.css_first("#pinfo-subscribers span")
            if subscribers_node:
                subscribers = self._parse_int(subscribers_node.text())

            views_node = parser.css_first("#pinfo-videos-views span")
            if views_node:
                views = self._parse_int(views_node.text())

            # Model avatar
            thumb_url = None
            pic_node = parser.css_first(".profile-infos .profile-pic img")
            if pic_node:
                thumb_url = pic_node.attributes.get("src")

            return ParsedActorData(
                name=name,
                url=profile_url,
                thumb_url=thumb_url,
                gender=gender,
                age=age,
                country=country,
                subscribers=subscribers,
                views=views,
                description=""
            )

        except Exception as e:
            print(f"   ❌ Error parsing model {profile_url}: {e}")
            return ParsedActorData(name=name_placeholder, url=profile_url)

    async def parse_video(self, url: str) -> ParsedVideoData | None:
        """Parses the video page itself in detail."""
        try:
            response = await self._get(url, headers=self.headers, follow_redirects=True)
            if response is None:
                return None
            if response.status_code != 200:
                print(f"⚠️ Error loading video page: {url} Status: {response.status_code}")
                return None

            html = response.text
            parser = HTMLParser(html)

            # 1. og:title -> alt
            alt_meta = parser.css_first('meta[property="og:title"]')
            alt = alt_meta.attributes.get("content", "").strip() if alt_meta else ""

            # 2. Look for setThumbUrl169 in scripts
            thumb_url = ""
            for script in parser.css("script"):
                script_text = script.text()
                if "setThumbUrl169" in script_text:
                    match = re.search(r"setThumbUrl169\s*\(\s*['\"](.*?)['\"]\s*\)", script_text)
                    if match:
                        thumb_url = match.group(1)
                        break

            # 3. Collect tags (name + URL)
            tags = []
            seen_tag_names = set()
            for tag_a in parser.css('.video-tags-list a[href*="/tags/"]'):
                tag_text = tag_a.text().strip()
                href = tag_a.attributes.get("href")
                
                if tag_text and tag_text not in seen_tag_names:
                    seen_tag_names.add(tag_text)
                    abs_tag_url = urljoin(str(response.url), href) if href else None
                    tags.append(ParsedTagData(name=tag_text, url=abs_tag_url))

            # 4. Collect actors/models (name + link)
            # Structure: <li class="model"><a href="/models/xxx or /pornstars/xxx" class="... profile ...">
            #            <span class="name">Name</span>...</a></li>
            actor_infos = []
            for model_li in parser.css("li.model"):
                a_tag = model_li.css_first("a")
                if not a_tag:
                    continue
                href = a_tag.attributes.get("href")
                if not href:
                    continue
                abs_url = urljoin(str(response.url), href)
                name_span = a_tag.css_first("span.name")
                model_name = name_span.text().strip() if name_span else a_tag.text().strip()
                model_name = re.sub(r"\s+Official\s+profile.*", "", model_name, flags=re.IGNORECASE).strip()
                model_name = re.sub(r"\s+Pornstar.*", "", model_name, flags=re.IGNORECASE).strip()
                if model_name and abs_url not in [x[1] for x in actor_infos]:
                    actor_infos.append((model_name, abs_url))

            # Fallback for a different markup
            if not actor_infos:
                for model_a in parser.css('a[href*="/models/"], a[href*="/pornstars/"]'):
                    href = model_a.attributes.get("href")
                    if href:
                        abs_url = urljoin(str(response.url), href)
                        name_span = model_a.css_first("span.name")
                        model_name = name_span.text().strip() if name_span else model_a.text().strip()
                        if model_name and abs_url not in [x[1] for x in actor_infos]:
                            actor_infos.append((model_name, abs_url))

            # Models are stored as (name, URL) only — detailed profile
            # parsing runs as a separate `parse-models` command
            models = [
                ParsedActorData(name=model_name, url=actor_url)
                for model_name, actor_url in actor_infos
            ]

            # 5. Collect the channel (uploader)
            channel = None
            channel_url = None
            uploader_tag = parser.css_first('.video-metadata .main-uploader a.uploader-tag')
            if uploader_tag:
                href = uploader_tag.attributes.get("href", "")
                channel_url = urljoin(str(response.url), href) if href else None
                channel_tag = uploader_tag.css_first("span.name")
                if channel_tag:
                    channel = channel_tag.text().strip()

            # 6. JSON-LD metadata
            download_url = None
            duration = 0
            views = 0
            ld_json_tag = parser.css_first('script[type="application/ld+json"]')
            if ld_json_tag:
                try:
                    ld_data = json.loads(ld_json_tag.text())
                    if isinstance(ld_data, list):
                        ld_data = next(
                            (item for item in ld_data if isinstance(item, dict)), {}
                        )
                    if isinstance(ld_data, dict):
                        download_url = ld_data.get("contentUrl")
                        duration_str = ld_data.get("duration")
                        if duration_str:
                            duration = self.parse_iso_duration(duration_str)
                        statistic = ld_data.get("interactionStatistic", {})
                        if isinstance(statistic, list):
                            statistic = next(
                                (item for item in statistic if isinstance(item, dict)), {}
                            )
                        raw_views = statistic.get("userInteractionCount", 0)
                        views = int(raw_views) if str(raw_views).isdigit() else 0
                except Exception as e:
                    print(f"⚠️ JSON-LD parsing exception: {e}")

            if not thumb_url:
                print(f"⚠️ Missing thumb_url for {url}")
                return None

            return ParsedVideoData(
                url=url,
                thumb_url=thumb_url,
                download_url=download_url,
                alt=alt,
                desc="",
                tags=tags,
                models=models,
                channel=channel,
                channel_url=channel_url,
                duration=duration,
                views=views,
            )

        except Exception as e:
            print(f"❌ Error parsing video {url}: {e}")
            return None
