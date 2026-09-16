import asyncio
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from sqlmodel import Session, select

from src.config import DEFAULT_CONCURRENCY, DEFAULT_MODEL_CONCURRENCY, DEFAULT_RPS
from src.database.session import get_session
from src.models import Channel, ModelActor, Source, SourceEntrypoint, Tag, Video, VideoStage
from src.scrapers.schemas import ParsedActorData, ParsedVideoData
from src.scrapers.throttle import Throttle
from src.scrapers.xvideos import XvideosScraper


def create_client(concurrency: int = DEFAULT_CONCURRENCY) -> httpx.AsyncClient:
    """One httpx client for the whole run — reuses TCP+TLS connections."""
    return httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=concurrency,
            max_keepalive_connections=concurrency,
        ),
        timeout=httpx.Timeout(20.0, connect=10.0),
    )


def load_known_video_urls() -> set[str]:
    """All already-saved video URLs — one query per run, shared across all models."""
    with get_session() as session:
        return set(session.exec(select(Video.url)).all())


def get_or_create_source(session: Session, url: str) -> Source:
    """Gets or creates a Source (domain) from a URL."""
    parsed = urlparse(url)
    domain = parsed.hostname or ""
    if not domain:
        raise ValueError(f"Could not extract domain from URL: {url}")

    code = domain.replace("www.", "").split(".")[0]

    statement = select(Source).where(Source.domain == domain)
    source = session.exec(statement).first()

    if not source:
        source = Source(domain=domain, code=code)
        session.add(source)
        session.commit()
        session.refresh(source)
        print(f"➕ Created new source in DB: {domain} (code: {code})")

    return source


def get_or_create_entrypoint(session: Session, source: Source, url: str) -> SourceEntrypoint:
    """Gets or creates a SourceEntrypoint for a domain."""
    parsed = urlparse(url)
    path = parsed.path
    if not path:
        path = "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    statement = select(SourceEntrypoint).where(
        SourceEntrypoint.source_id == source.id,
        SourceEntrypoint.path == path
    )
    entrypoint = session.exec(statement).first()

    if not entrypoint:
        entrypoint = SourceEntrypoint(
            source_id=source.id,
            path=path,
            last_parsed_page=1,
            is_completed=False
        )
        session.add(entrypoint)
        session.commit()
        session.refresh(entrypoint)
        print(f"➕ Created new entrypoint in DB: {path}")

    return entrypoint


def save_parsed_videos(
    session: Session,
    data_list: list[ParsedVideoData],
    source_id: int,
    entrypoint_id: int,
    check_existing: bool = True,
) -> tuple[list[Video], int]:
    """Bulk-saves videos and returns (new videos, duplicate count)."""
    unique_data = {data.url: data for data in data_list}
    if not unique_data:
        return [], 0

    existing_urls = (
        set(session.exec(select(Video.url).where(Video.url.in_(unique_data))).all())
        if check_existing
        else set()
    )
    pending = [data for url, data in unique_data.items() if url not in existing_urls]
    if not pending:
        return [], len(unique_data)

    channel_names = {data.channel for data in pending if data.channel}
    channels = {
        channel.name: channel
        for channel in session.exec(select(Channel).where(Channel.name.in_(channel_names))).all()
    }
    tag_names = {tag.name for data in pending for tag in data.tags}
    tags = {
        tag.name: tag
        for tag in session.exec(select(Tag).where(Tag.name.in_(tag_names))).all()
    }
    model_names = {model.name for data in pending for model in data.models}
    models = {
        model.name: model
        for model in session.exec(select(ModelActor).where(ModelActor.name.in_(model_names))).all()
    }

    for data in pending:
        if data.channel and data.channel not in channels:
            channels[data.channel] = Channel(name=data.channel, url=data.channel_url)
            session.add(channels[data.channel])
        elif data.channel and data.channel_url and not channels[data.channel].url:
            channels[data.channel].url = data.channel_url

        for tag_data in data.tags:
            if tag_data.name not in tags:
                tags[tag_data.name] = Tag(name=tag_data.name, url=tag_data.url)
                session.add(tags[tag_data.name])
            elif tag_data.url and not tags[tag_data.name].url:
                tags[tag_data.name].url = tag_data.url

        for model_data in data.models:
            if model_data.name not in models:
                models[model_data.name] = ModelActor(
                    name=model_data.name,
                    url=model_data.url,
                    thumb_url=model_data.thumb_url,
                    gender=model_data.gender,
                    age=model_data.age,
                    country=model_data.country,
                    subscribers=model_data.subscribers,
                    views=model_data.views,
                    description=model_data.description,
                )
                session.add(models[model_data.name])
            else:
                model = models[model_data.name]
                model.url = model_data.url or model.url
                model.thumb_url = model_data.thumb_url or model.thumb_url
                model.gender = model_data.gender or model.gender
                model.age = model_data.age or model.age
                model.country = model_data.country or model.country
                model.subscribers = model_data.subscribers or model.subscribers
                model.views = model_data.views or model.views
                model.description = model_data.description or model.description

    session.flush()
    videos = []
    for data in pending:
        videos.append(
            Video(
                url=data.url,
                thumb_url=data.thumb_url,
                download_url=data.download_url,
                alt=data.alt,
                desc=data.desc,
                duration=data.duration,
                views=data.views,
                source_id=source_id,
                source_entrypoint_id=entrypoint_id,
                channel=channels.get(data.channel) if data.channel else None,
                tags=[tags[tag.name] for tag in data.tags],
                models=[models[model.name] for model in data.models],
            )
        )
    session.add_all(videos)
    return videos, len(existing_urls)


def save_parsed_video(
    session: Session,
    data: ParsedVideoData,
    source_id: int,
    entrypoint_id: int,
) -> Video:
    """Single-video compatibility wrapper around the bulk save."""
    videos, _ = save_parsed_videos(session, [data], source_id, entrypoint_id)
    session.commit()
    if videos:
        session.refresh(videos[0])
        return videos[0]
    return session.exec(select(Video).where(Video.url == data.url)).one()


def update_parsed_video(session: Session, video: Video, data: ParsedVideoData) -> None:
    """Updates an existing video and its relations after a re-parse."""
    video.thumb_url = data.thumb_url
    video.download_url = data.download_url
    video.alt = data.alt
    video.desc = data.desc
    video.duration = data.duration
    video.views = data.views

    if data.channel:
        channel = session.exec(select(Channel).where(Channel.name == data.channel)).first()
        if not channel:
            channel = Channel(name=data.channel, url=data.channel_url)
            session.add(channel)
            session.flush()
        elif data.channel_url and not channel.url:
            channel.url = data.channel_url
        video.channel = channel

    tag_names = {tag.name for tag in data.tags}
    tags = {
        tag.name: tag
        for tag in session.exec(select(Tag).where(Tag.name.in_(tag_names))).all()
    }
    for tag_data in data.tags:
        if tag_data.name not in tags:
            tags[tag_data.name] = Tag(name=tag_data.name, url=tag_data.url)
            session.add(tags[tag_data.name])
        elif tag_data.url and not tags[tag_data.name].url:
            tags[tag_data.name].url = tag_data.url
    session.flush()
    video.tags = [tags[tag.name] for tag in data.tags]

    model_names = {model.name for model in data.models}
    models = {
        model.name: model
        for model in session.exec(select(ModelActor).where(ModelActor.name.in_(model_names))).all()
    }
    for model_data in data.models:
        if model_data.name not in models:
            models[model_data.name] = ModelActor(
                name=model_data.name,
                url=model_data.url,
                thumb_url=model_data.thumb_url,
            )
            session.add(models[model_data.name])
        else:
            model = models[model_data.name]
            model.url = model_data.url or model.url
            model.thumb_url = model_data.thumb_url or model.thumb_url
    session.flush()
    video.models = [models[model.name] for model in data.models]
    video.status = (int(video.status) | int(VideoStage.PARSED)) & ~int(VideoStage.REPARSE)
    session.add(video)


async def reparse_marked_videos(
    max_videos: int = 0,
    batch_size: int = 24,
    concurrency: int = DEFAULT_CONCURRENCY,
    rps: float = DEFAULT_RPS,
) -> tuple[int, int]:
    """Re-parses videos flagged with VideoStage.REPARSE."""
    if batch_size < 1 or concurrency < 1:
        raise ValueError("batch_size and concurrency must be greater than zero")

    with get_session() as session:
        videos = session.exec(
            select(Video).where(
                Video.status.op("&")(int(VideoStage.REPARSE)) != 0
            ).order_by(Video.id)
        ).all()
    if max_videos > 0:
        videos = videos[:max_videos]

    if not videos:
        print("No videos flagged for re-parsing.")
        return 0, 0

    print(f"Found videos to re-parse: {len(videos)}")
    failed = 0
    updated = 0

    async with create_client(concurrency) as client:
        scraper = XvideosScraper(client, Throttle(rps=rps, concurrency=concurrency))

        async def parse_one(video: Video) -> tuple[int, ParsedVideoData | None]:
            print(f"📥 Reparse: {video.url}")
            return video.id, await scraper.parse_video(video.url)

        for offset in range(0, len(videos), batch_size):
            batch = videos[offset : offset + batch_size]
            print(f"\nBatch {offset // batch_size + 1}: {len(batch)} videos")
            parsed_results = await asyncio.gather(*(parse_one(video) for video in batch))
            failed += sum(data is None for _, data in parsed_results)

            with get_session() as session:
                batch_updated = 0
                for video_id, data in parsed_results:
                    if data is None:
                        continue
                    video = session.get(Video, video_id)
                    if video:
                        update_parsed_video(session, video, data)
                        batch_updated += 1
                        print(f"   💾 Updated: {video.url} (views={data.views})")
                session.commit()
                updated += batch_updated
            print(f"   ✅ Batch saved to DB: {batch_updated}")

    print(f"Re-processed: {updated}, failed: {failed}")
    return updated, failed


def update_entrypoint_progress(entrypoint_id: int, page: int, total_pages: int | None = None) -> None:
    """Saves last_parsed_page for an entrypoint (called on every new-page transition)."""
    with get_session() as session:
        db_entrypoint = session.get(SourceEntrypoint, entrypoint_id)
        if db_entrypoint:
            db_entrypoint.last_parsed_page = page
            if total_pages is not None:
                db_entrypoint.total_pages = total_pages
            db_entrypoint.last_parsed_at = datetime.now(UTC)
            session.add(db_entrypoint)
            session.commit()


async def parse_video_batch(
    scraper: XvideosScraper,
    video_urls: list[str],
    source_id: int,
    entrypoint_id: int,
    known_urls: set[str] | None = None,
    write_lock: asyncio.Lock | None = None,
) -> tuple[int, int, int]:
    """Parses URLs in parallel, then saves them in one commit. Pace is held by the scraper's throttle."""
    if not video_urls:
        return 0, 0, 0

    if known_urls is None:
        with get_session() as session:
            known_urls = set(
                session.exec(select(Video.url).where(Video.url.in_(video_urls))).all()
            )

    pending_urls = [url for url in dict.fromkeys(video_urls) if url not in known_urls]
    skipped_count = len(video_urls) - len(pending_urls)
    # Reserve the links right away: parallel tasks shouldn't pick them up too.
    known_urls.update(pending_urls)

    async def parse_one(video_url: str) -> tuple[str, ParsedVideoData | None]:
        return video_url, await scraper.parse_video(video_url)

    parsed_results = await asyncio.gather(*(parse_one(url) for url in pending_urls))
    parsed_data = [data for _, data in parsed_results if data is not None]
    failed_count = len(pending_urls) - len(parsed_data)
    # Put unparsed links back into circulation — maybe they'll work next time.
    for video_url, data in parsed_results:
        if data is None:
            known_urls.discard(video_url)

    if not parsed_data:
        return 0, skipped_count, failed_count

    lock = write_lock if write_lock is not None else asyncio.Lock()
    async with lock:
        with get_session() as session:
            try:
                # check_existing=True — one SELECT per batch before inserting: it
                # catches videos saved by a parallel process after known_urls
                # was already read.
                saved_videos, duplicate_count = save_parsed_videos(
                    session, parsed_data, source_id, entrypoint_id, check_existing=True
                )
                session.commit()
                for video in saved_videos:
                    title_preview = (video.alt or "")[:40]
                    print(f"   💾 Saved: {title_preview}...")
                skipped_count += duplicate_count
                return len(saved_videos), skipped_count, failed_count
            except Exception as error:
                session.rollback()
                print(f"   ❌ Bulk save error: {error}")
                return 0, skipped_count, failed_count + len(parsed_data)


async def crawl_source_url(
    url: str,
    max_videos: int = 0,
    max_pages: int = 0,
    model_videos: bool = False,
    batch_size: int = 24,
    concurrency: int = DEFAULT_CONCURRENCY,
    rps: float = DEFAULT_RPS,
    client: httpx.AsyncClient | None = None,
    throttle: Throttle | None = None,
    known_urls: set[str] | None = None,
    write_lock: asyncio.Lock | None = None,
):
    """Orchestrator: parses a URL and saves results to the DB. Creates its own resources if none are passed."""
    if batch_size < 1 or concurrency < 1:
        raise ValueError("batch_size and concurrency must be greater than zero")

    owns_client = client is None
    client = client if client is not None else create_client(concurrency)
    throttle = throttle if throttle is not None else Throttle(rps=rps, concurrency=concurrency)
    write_lock = write_lock if write_lock is not None else asyncio.Lock()
    known_urls = known_urls if known_urls is not None else load_known_video_urls()

    with get_session() as session:
        source = get_or_create_source(session, url)
        entrypoint = get_or_create_entrypoint(session, source, url)

        start_page = entrypoint.last_parsed_page
        saved_total_pages = entrypoint.total_pages
        print(f"\n🚀 Starting crawler for {source.domain}{entrypoint.path}")
        limit_label = str(max_videos) if max_videos > 0 else "unlimited"
        pages_label = str(max_pages) if max_pages > 0 else "unlimited"
        print(f"Limit: {limit_label} videos, {pages_label} pages")

    added_count = 0
    skipped_count = 0
    seen_urls: set[str] = set()

    # Base listing URL without a page suffix — the scraper builds page URLs itself
    base_url = url.rstrip("/")

    try:
        # A fresh scraper per crawl: it holds the current page number, which
        # parallel models would otherwise stomp on each other.
        scraper = XvideosScraper(client, throttle)

        last_saved_page = start_page

        # Start the crawl from the page saved in the DB
        if model_videos:
            links = scraper.fetch_model_video_links(url, start_page=start_page)
        else:
            links = scraper.fetch_page_links(base_url, max_pages=max_pages, start_page=start_page)

        pending_urls: list[str] = []

        async def flush_pending() -> None:
            nonlocal added_count, skipped_count, pending_urls
            if not pending_urls:
                return
            remaining = max_videos - added_count if max_videos > 0 else len(pending_urls)
            urls_to_process = pending_urls[:remaining]
            added, skipped, _ = await parse_video_batch(
                scraper,
                urls_to_process,
                source.id,
                entrypoint.id,
                known_urls=known_urls,
                write_lock=write_lock,
            )
            added_count += added
            skipped_count += skipped
            pending_urls = pending_urls[len(urls_to_process):]

        async for video_url in links:
            # Save progress on every transition to a new page
            page_changed = scraper.current_page != last_saved_page
            if page_changed:
                await flush_pending()
                last_saved_page = scraper.current_page
            if (
                page_changed
                or scraper.total_pages != saved_total_pages
            ):
                async with write_lock:
                    update_entrypoint_progress(entrypoint.id, last_saved_page, scraper.total_pages)
                saved_total_pages = scraper.total_pages
                print(f"📑 Progress saved: page {last_saved_page}")

            if max_videos > 0 and added_count >= max_videos:
                print(f"\n🛑 Session limit of {max_videos} added videos reached.")
                break

            if video_url not in seen_urls:
                seen_urls.add(video_url)
                pending_urls.append(video_url)
            if len(pending_urls) >= batch_size:
                await flush_pending()

        await flush_pending()

        # Save the final position — the page the crawl ended on
        async with write_lock:
            with get_session() as session:
                db_entrypoint = session.get(SourceEntrypoint, entrypoint.id)
                if db_entrypoint:
                    db_entrypoint.last_parsed_at = datetime.now(UTC)
                    db_entrypoint.last_parsed_page = scraper.current_page
                    if scraper.total_pages is not None:
                        db_entrypoint.total_pages = scraper.total_pages
                    session.add(db_entrypoint)
                    session.commit()
                    next_page = db_entrypoint.last_parsed_page
                    print(f"\n🔄 Progress saved. Next run will start from page: {next_page}")
    finally:
        if owns_client:
            await client.aclose()

    print("\n🎉 Session finished!")
    print(f"New videos added: {added_count}")
    print(f"Duplicates skipped: {skipped_count}")
    return added_count, skipped_count


async def crawl_model_videos(
    model_urls: list[str],
    max_videos: int = 0,
    batch_size: int = 24,
    concurrency: int = DEFAULT_CONCURRENCY,
    model_concurrency: int = DEFAULT_MODEL_CONCURRENCY,
    rps: float = DEFAULT_RPS,
) -> tuple[int, int]:
    """Crawls videos for several models in parallel; a shared throttle holds the pace."""
    if model_concurrency < 1:
        raise ValueError("model_concurrency must be greater than zero")
    if not model_urls:
        return 0, 0

    throttle = Throttle(rps=rps, concurrency=concurrency)
    write_lock = asyncio.Lock()
    known_urls = load_known_video_urls()
    model_semaphore = asyncio.Semaphore(model_concurrency)
    total = len(model_urls)
    done = 0
    added_total = 0
    skipped_total = 0

    print(
        f"Known videos in DB: {len(known_urls)} | "
        f"pace: {rps} req/s, in flight: {concurrency}, models in parallel: {model_concurrency}"
    )

    async with create_client(concurrency) as client:

        async def crawl_one(model_url: str) -> tuple[int, int]:
            nonlocal done, added_total, skipped_total
            async with model_semaphore:
                page_url = f"{model_url.rstrip('/')}#_tabVideos,rating"
                added, skipped = await crawl_source_url(
                    page_url,
                    max_videos=max_videos,
                    model_videos=True,
                    batch_size=batch_size,
                    client=client,
                    throttle=throttle,
                    known_urls=known_urls,
                    write_lock=write_lock,
                )
            done += 1
            added_total += added
            skipped_total += skipped
            requests, rate = throttle.stats()
            remaining = (total - done) * (requests / done) / rate if done and rate else 0
            print(
                f"\n📊 Models: {done}/{total} | added: {added_total} | "
                f"requests: {requests} ({rate:.1f} req/s) | ETA ≈ {remaining / 60:.0f} min"
            )
            return added, skipped

        results = await asyncio.gather(
            *(crawl_one(model_url) for model_url in model_urls), return_exceptions=True
        )

    for model_url, result in zip(model_urls, results, strict=True):
        if isinstance(result, BaseException):
            print(f"❌ Model skipped due to error: {model_url}: {result}")

    print(f"\n🏁 Done. Models: {total}, videos added: {added_total}, duplicates: {skipped_total}")
    return added_total, skipped_total


async def parse_models_details(
    max_models: int = 0,
    only_missing: bool = True,
    batch_size: int = 24,
    concurrency: int = DEFAULT_CONCURRENCY,
    rps: float = DEFAULT_RPS,
):
    """Detailed parsing of model profiles from the DB (gender, age, country, thumb_url, etc.)."""
    if batch_size < 1 or concurrency < 1:
        raise ValueError("batch_size and concurrency must be greater than zero")

    with get_session() as session:
        stmt = select(ModelActor).where(ModelActor.url.isnot(None))  # type: ignore[union-attr]
        if only_missing:
            stmt = stmt.where(ModelActor.gender.is_(None))  # type: ignore[union-attr]
        models = session.exec(stmt).all()

    if not models:
        print("No models to parse in detail.")
        return

    total = len(models)
    if max_models > 0:
        models = models[:max_models]
        total = len(models)
    print(f"\n👤 Detailed model parsing: {total} models (only_missing={only_missing})")

    updated_count = 0
    failed_count = 0

    async with create_client(concurrency) as client:
        scraper = XvideosScraper(client, Throttle(rps=rps, concurrency=concurrency))

        async def parse_one(model: ModelActor) -> tuple[ModelActor, ParsedActorData | None]:
            if not model.url:
                return model, None
            return model, await scraper.parse_actor(model.url, name_placeholder=model.name)

        for offset in range(0, total, batch_size):
            batch = models[offset : offset + batch_size]
            results = await asyncio.gather(*(parse_one(model) for model in batch))
            successful = [(model, parsed) for model, parsed in results if parsed]
            failed_count += len(batch) - len(successful)

            with get_session() as session:
                for model, parsed in successful:
                    db_model = session.get(ModelActor, model.id)
                    if not db_model:
                        continue
                    db_model.name = parsed.name or db_model.name
                    db_model.thumb_url = parsed.thumb_url or db_model.thumb_url
                    db_model.gender = parsed.gender or db_model.gender
                    db_model.age = parsed.age or db_model.age
                    db_model.country = parsed.country or db_model.country
                    db_model.subscribers = parsed.subscribers or db_model.subscribers
                    db_model.views = parsed.views or db_model.views
                    db_model.description = parsed.description or db_model.description
                    session.add(db_model)
                    print(f"   💾 Updated: {db_model.name}")
                session.commit()
            updated_count += len(successful)

    print("\n🎉 Model parsing finished!")
    print(f"Updated: {updated_count}, failed: {failed_count}")
