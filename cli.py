import asyncio

import typer
from dotenv import load_dotenv

from src.config import (
    DEFAULT_CONCURRENCY,
    DEFAULT_DB_NAME,
    DEFAULT_DB_PATH,
    DEFAULT_EXPORT_DIR,
    DEFAULT_EXPORT_FILE,
    DEFAULT_LONGTAILS_FILE,
    DEFAULT_MODEL_CONCURRENCY,
    DEFAULT_MODELS_EXPORT_FILE,
    DEFAULT_RPS,
    display_path,
    resolve_config_path,
    resolve_db_path,
    resolve_output_path,
)
from src.database.session import init_db

load_dotenv()

# SQLite caps bind params at ~32766 per query, and export easily produces
# tens of thousands of ids — any IN (...) over them goes in chunks of this size.
SQLITE_ID_CHUNK = 500

app = typer.Typer(help="CLI tool for scraping video content")


@app.callback()
def global_options(
    ctx: typer.Context,
    db: str = typer.Option(
        DEFAULT_DB_NAME,
        "--db",
        help="DB file name (looked up in db/) or a path with a slash",
    ),
) -> None:
    """Global CLI options."""
    from src.database.session import set_db_path

    ctx.meta["db"] = resolve_db_path(db)
    set_db_path(db)


@app.command(name="init-db")
def init(
    ctx: typer.Context,
):
    """Initializes the database and creates tables."""
    # Get the DB path passed via the callback context
    db_path = ctx.meta.get("db", DEFAULT_DB_PATH)
    typer.echo("Initializing database...")
    init_db()
    typer.echo(f"Database created successfully ({display_path(db_path)})!")


@app.command(name="parse")
def parse(
    urls: list[str] = typer.Argument(default=None, help="Listing URL(s) to scrape"),
    urls_file: str = typer.Option(None, "--urls-file", "-f", help="File with a list of URLs (one per line)"),
    limit: int = typer.Option(0, "--limit", "-l", help="Max new videos per URL (0 = unlimited)"),
    max_pages: int = typer.Option(0, "--max-pages", "-p", help="Max listing pages per URL (0 = unlimited)"),
    batch_size: int = typer.Option(24, "--batch-size", help="Video save batch size"),
    concurrency: int = typer.Option(DEFAULT_CONCURRENCY, "--concurrency", help="Max requests in flight"),
    rps: float = typer.Option(DEFAULT_RPS, "--rps", help="Request pace to the site (requests per second)"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Dry run without saving to the DB"),
):
    """Scrapes listing(s) and saves videos to the DB.

    You can pass one or more URLs as arguments:
      parse URL1 URL2 URL3

    Or point to a file with a list of URLs (one per line):
      parse --urls-file urls.txt

    --dry-run scrapes the first page of the first URL, prints data for
    3 videos, and exits without writing to the DB.
    """
    import httpx

    from src.database.engine import crawl_source_url, create_client, load_known_video_urls
    from src.scrapers.throttle import Throttle
    from src.scrapers.xvideos import XvideosScraper

    # Build the final URL list from arguments and/or the file
    all_urls: list[str] = list(urls or [])

    if urls_file:
        try:
            with open(urls_file, encoding="utf-8") as f:
                file_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            all_urls.extend(file_urls)
        except FileNotFoundError:
            typer.echo(f"File not found: {urls_file}", err=True)
            raise typer.Exit(1)

    if not all_urls:
        typer.echo("Provide at least one URL or --urls-file. Use --help for reference.", err=True)
        raise typer.Exit(1)

    # Deduplicate while keeping order
    seen: set[str] = set()
    unique_urls = [u for u in all_urls if not (u in seen or seen.add(u))]  # type: ignore[func-returns-value]

    if dry_run:
        target = unique_urls[0]

        async def run_dry() -> None:
            typer.echo(f"[DRY-RUN] URL: {target}")
            async with httpx.AsyncClient() as client:
                scraper = XvideosScraper(client)
                links: list[str] = []
                typer.echo("Collecting links from the first page...")
                async for link in scraper.fetch_page_links(target, max_pages=1):
                    links.append(link)

                if not links:
                    typer.echo("No links found. Check the URL or the page structure.")
                    return

                typer.echo(f"Found {len(links)} links. Parsing the first 3:\n")
                for video_url in links[:3]:
                    typer.echo(f"  -> {video_url}")
                    video = await scraper.parse_video(video_url)
                    if video:
                        typer.echo(f"     alt     : {video.alt}")
                        typer.echo(f"     thumb   : {video.thumb_url}")
                        typer.echo(f"     duration: {video.duration}s")
                        typer.echo(f"     channel : {video.channel}")
                        typer.echo(f"     models  : {[m.name for m in video.models]}")
                        typer.echo(f"     tags    : {[t.name for t in video.tags]}")
                    else:
                        typer.echo("     [failed to parse]")
                    typer.echo("")

        asyncio.run(run_dry())
    else:
        limit_label = limit if limit > 0 else "unlimited"
        pages_label = max_pages if max_pages > 0 else "unlimited"
        typer.echo(f"Parsing {len(unique_urls)} URL(s): {limit_label} videos, {pages_label} pages/URL\n")

        async def run_crawls() -> None:
            # One client, one throttle, and one set of known URLs across all listings
            throttle = Throttle(rps=rps, concurrency=concurrency)
            known_urls = load_known_video_urls()
            async with create_client(concurrency) as client:
                for i, url in enumerate(unique_urls, 1):
                    typer.echo(f"[{i}/{len(unique_urls)}] {url}")
                    await crawl_source_url(
                        url,
                        max_videos=limit,
                        max_pages=max_pages,
                        batch_size=batch_size,
                        concurrency=concurrency,
                        client=client,
                        throttle=throttle,
                        known_urls=known_urls,
                    )

        asyncio.run(run_crawls())


@app.command(name="parse-models")
def parse_models(
    limit: int = typer.Option(0, "--limit", "-l", help="Max models per session (0 = unlimited)"),
    all_models: bool = typer.Option(False, "--all", "-a", help="Parse all models, including ones that already have details"),
    batch_size: int = typer.Option(24, "--batch-size", help="Model save batch size"),
    concurrency: int = typer.Option(DEFAULT_CONCURRENCY, "--concurrency", help="Max requests in flight"),
    rps: float = typer.Option(DEFAULT_RPS, "--rps", help="Request pace to the site (requests per second)"),
):
    """Parses model profiles from the DB in detail (gender, age, country, thumb_url).

    By default it only processes models without details.
    --all forces re-fetching every model.
    """
    from src.database.engine import parse_models_details

    asyncio.run(
        parse_models_details(
            max_models=limit,
            only_missing=not all_models,
            batch_size=batch_size,
            concurrency=concurrency,
            rps=rps,
        )
    )


@app.command(name="translate")
def translate(
    provider: str = typer.Option("ai", "--provider", "-p", help="ai, google, bing, or baidu"),
    languages: list[str] = typer.Option(None, "--language", "-l", help="Language code; can be given multiple times"),
    limit: int = typer.Option(0, "--limit", help="Video limit per language (0 = unlimited)"),
    batch_size: int = typer.Option(10, "--batch-size", help="AI batch size (ignored for non-AI providers)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the result without writing to the DB"),
):
    """Translates video title/description and generates localized slugs."""
    import httpx

    from src.translations.config import DEFAULT_LANGUAGES, LANGUAGES
    from src.translations.providers import create_provider
    from src.translations.service import translate_videos

    selected_languages = languages or list(DEFAULT_LANGUAGES)
    if batch_size < 1:
        typer.echo("--batch-size must be greater than zero", err=True)
        raise typer.Exit(1)
    unknown_languages = [language for language in selected_languages if language not in LANGUAGES]
    if unknown_languages:
        supported = ", ".join(LANGUAGES)
        typer.echo(f"Unknown languages: {', '.join(unknown_languages)}. Available: {supported}", err=True)
        raise typer.Exit(1)

    async def run() -> int:
        async with httpx.AsyncClient(timeout=120) as client:
            translation_provider = create_provider(provider, client)
            return await translate_videos(
                translation_provider,
                selected_languages,
                limit=limit,
                batch_size=batch_size,
                dry_run=dry_run,
            )

    count = asyncio.run(run())
    if not dry_run:
        typer.echo(f"Translations saved: {count}")


@app.command(name="parse-model-videos")
def parse_model_videos(
    model_urls: list[str] = typer.Argument(default=None, help="Model page URL(s); without a URL, all models from the DB"),
    limit: int = typer.Option(0, "--limit", "-l", help="Max new videos per model (0 = unlimited)"),
    batch_size: int = typer.Option(24, "--batch-size", help="Video save batch size"),
    concurrency: int = typer.Option(DEFAULT_CONCURRENCY, "--concurrency", help="Max requests in flight"),
    model_concurrency: int = typer.Option(
        DEFAULT_MODEL_CONCURRENCY, "--model-concurrency", help="How many models to crawl in parallel"
    ),
    rps: float = typer.Option(DEFAULT_RPS, "--rps", help="Request pace to the site (requests per second)"),
):
    """Scrapes videos from the Videos tab of model pages.

    Example:
      parse-model-videos https://www.xvideos.com/models/lewis-romeo --limit 20

    If no URLs are given, processes every model in the modelactor table
    that has a url set.
    """
    from sqlmodel import select

    from src.database.engine import crawl_model_videos
    from src.database.session import get_session
    from src.models import ModelActor

    if model_urls:
        urls = model_urls
    else:
        with get_session() as session:
            urls = [
                model.url
                for model in session.exec(select(ModelActor).order_by(ModelActor.name)).all()
                if model.url
            ]

    unique_urls = list(dict.fromkeys(urls))
    if not unique_urls:
        typer.echo("No models with a URL in the database. Run parse first.")
        raise typer.Exit(1)

    limit_label = limit if limit > 0 else "unlimited"
    typer.echo(f"Parsing videos for {len(unique_urls)} models, limit: {limit_label} videos/model\n")

    asyncio.run(
        crawl_model_videos(
            unique_urls,
            max_videos=limit,
            batch_size=batch_size,
            concurrency=concurrency,
            model_concurrency=model_concurrency,
            rps=rps,
        )
    )


@app.command(name="mark-reparse")
def mark_reparse(
    urls: list[str] = typer.Argument(..., help="Video URL(s) to re-parse"),
):
    """Flags existing videos for re-parsing."""
    from sqlmodel import select

    from src.database.session import get_session
    from src.models import Video, VideoStage

    with get_session() as session:
        videos = session.exec(select(Video).where(Video.url.in_(urls))).all()
        found_urls = {video.url for video in videos}
        for video in videos:
            video.status = int(video.status) | int(VideoStage.REPARSE)
            session.add(video)
        session.commit()

    missing = [url for url in urls if url not in found_urls]
    typer.echo(f"Videos flagged: {len(videos)}")
    if missing:
        typer.echo(f"URLs not found: {len(missing)}", err=True)


@app.command(name="reparse")
def reparse(
    limit: int = typer.Option(0, "--limit", "-l", help="Video limit (0 = unlimited)"),
    batch_size: int = typer.Option(24, "--batch-size", help="How many videos to save per transaction"),
    concurrency: int = typer.Option(DEFAULT_CONCURRENCY, "--concurrency", help="Max requests in flight"),
    rps: float = typer.Option(DEFAULT_RPS, "--rps", help="Request pace to the site (requests per second)"),
):
    """Re-parses videos flagged by mark-reparse."""
    from src.database.engine import reparse_marked_videos

    asyncio.run(
        reparse_marked_videos(
            max_videos=limit,
            batch_size=batch_size,
            concurrency=concurrency,
            rps=rps,
        )
    )


@app.command(name="show-db")
def show_db(
    as_json: bool = typer.Option(False, "--json", "-j", help="Output data as JSON"),
):
    """Shows saved videos from the database."""
    import json

    from sqlmodel import select

    from src.database.session import get_session
    from src.models import Video, VideoStage

    with get_session() as session:
        videos = session.exec(select(Video)).all()

        if not videos:
            typer.echo("The database is empty. Run parse first.")
            return

        if as_json:
            data = [
                {
                    "id": v.id,
                    "url": v.url,
                    "thumb_url": v.thumb_url,
                    "alt": v.alt,
                    "duration": v.duration,
                    "views": v.views,
                    "status": int(v.status),
                    "stages": [
                        stage.name
                        for stage in VideoStage
                        if stage != VideoStage.NONE and int(v.status) & int(stage)
                    ],
                    "channel": v.channel.name if v.channel else None,
                    "tags": [t.name for t in v.tags],
                    "models": [m.name for m in v.models],
                }
                for v in videos
            ]
            typer.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            typer.echo(f"Total videos: {len(videos)}\n")
            for idx, v in enumerate(videos, 1):
                typer.echo(f"{idx}. {v.alt}")
                typer.echo(f"   URL    : {v.url}")
                typer.echo(f"   Views  : {v.views}")
                typer.echo(f"   Status : {int(v.status)}")
                typer.echo(f"   Channel: {v.channel.name if v.channel else '-'}")
                typer.echo(f"   Tags   : {[t.name for t in v.tags]}")
                typer.echo(f"   Models : {[m.name for m in v.models]}")
                typer.echo("-" * 60)


@app.command(name="export")
def export_videos(
    output: str = typer.Option(
        DEFAULT_EXPORT_FILE,
        "--output",
        "-o",
        help=f"File name (goes into {DEFAULT_EXPORT_DIR}/) or a path with a slash",
    ),
    limit: int = typer.Option(0, "--limit", "-l", help="Video limit: first N by URL (0 = unlimited)"),
    random_count: int = typer.Option(
        0, "--random", "-r", help="Take N random videos from the selection (0 = off)"
    ),
    min_views: int = typer.Option(0, "--views", help="Minimum views (0 = unlimited)"),
    not_exported: bool = typer.Option(
        False, "--not-exported", help="Only videos without the EXPORTED status flag"
    ),
    with_models: bool = typer.Option(
        False, "--models", help="Also export related models into models-<file>"
    ),
    preset: str | None = typer.Option(
        None, "--preset", help="Ready-made filter set: asian"
    ),
    preset_tags_only: bool = typer.Option(
        False, "--preset-tags-only", help="Only consider tags in the preset, not the title"
    ),
    tag_like: str | None = typer.Option(None, "--tag-like", help="Tag filter by substring, e.g. gay"),
    longtails_file: str = typer.Option(
        DEFAULT_LONGTAILS_FILE,
        "--longtails",
        help=f"Longtail phrases file name (looked up in config/, default "
             f"{DEFAULT_LONGTAILS_FILE}; empty string means don't mix in)",
    ),
    delimiter: str = typer.Option(" ", "--delimiter", help="Separator between the title and longtail words"),
    longtail_insert: bool = typer.Option(False, "--longtail-insert", help="Shuffle longtail words into the title"),
    seed: int | None = typer.Option(None, "--seed", help="Seed for reproducible random selection"),
):
    """Exports videos to a pipe-delimited .txt file for import.

    Format: #url|thumb|alt::1|duration|tags::1|model_autocreate|
    """
    import random

    from sqlmodel import select, update

    from src.database.session import get_session
    from src.export_presets import get_preset, preset_video_condition
    from src.models import Tag, Video, VideoStage, VideoTagLink

    if limit > 0 and random_count > 0:
        typer.echo(
            "--limit and --random both set the export size differently: pick one.",
            err=True,
        )
        raise typer.Exit(1)

    selected_preset = None
    if preset:
        try:
            selected_preset = get_preset(preset)
        except ValueError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(1)

    DELIMITER = "|"

    longtails: list[str] = []
    if longtails_file:
        longtails_path = resolve_config_path(longtails_file)
        is_default = longtails_file == DEFAULT_LONGTAILS_FILE
        if not longtails_path.is_file():
            # A missing explicitly-given file is an error, a missing default
            # is just a reason to skip mixing in phrases.
            if not is_default:
                typer.echo(f"Longtails file not found: {display_path(longtails_path)}", err=True)
                raise typer.Exit(1)
            typer.echo(f"ℹ️ {display_path(longtails_path)} not found — exporting without longtail phrases")
        else:
            longtails = [
                line.strip()
                for line in longtails_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if not longtails:
                typer.echo(f"No phrases in the longtails file: {display_path(longtails_path)}", err=True)
                raise typer.Exit(1)
    if "|" in delimiter or "\n" in delimiter or "\r" in delimiter:
        typer.echo("--delimiter must not contain '|', newline, or carriage return", err=True)
        raise typer.Exit(1)

    rng = random.Random(seed)

    def mix_title(title: str) -> str:
        if not longtails or not title:
            return title
        phrase = rng.choice(longtails)
        if phrase.casefold() in title.casefold():
            return title
        if longtail_insert:
            title_words = title.split()
            phrase_words = phrase.split()
            rng.shuffle(phrase_words)
            for word in phrase_words:
                title_words.insert(rng.randrange(len(title_words) + 1), word)
            return delimiter.join(title_words)
        if rng.choice((True, False)):
            return delimiter.join((phrase, title))
        return delimiter.join((title, phrase))

    # fieldMap: key = our field name, value = target field name
    FIELD_MAP: dict[str, str] = {
        "url":      "url",
        "thumb_url": "thumb",
        "alt":      "alt::1",
        "duration": "duration",
        "tags":     "tags::1",
        "models":   "model_autocreate",
    }

    EXPORT_FIELDS = ["url", "thumb_url", "alt", "duration", "tags", "models"]
    SANITIZE_FIELDS = {"alt", "tags", "models"}
    models_by_name: dict[str, dict[str, str]] = {}
    exported_video_ids: list[int] = []

    with get_session() as session:
        statement = select(Video)
        if min_views > 0:
            statement = statement.where(Video.views >= min_views)
        if not_exported:
            # The EXPORTED bit is set at the end of this same command, so
            # the filter only picks what has never gone into a file before.
            statement = statement.where(
                Video.status.op("&")(int(VideoStage.EXPORTED)) == 0
            )
        if selected_preset:
            statement = statement.where(
                preset_video_condition(selected_preset, tags_only=preset_tags_only)
            )
        if tag_like:
            statement = (
                statement
                .join(VideoTagLink, VideoTagLink.video_id == Video.id)
                .join(Tag, Tag.id == VideoTagLink.tag_id)
                .where(Tag.name.like(f"%{tag_like}%"))
                .distinct()
            )
        if random_count > 0:
            # Random sampling is done by id: loading the whole filtered set into
            # the ORM (tens of thousands of objects with tags and models) for a
            # hundred rows is wasteful. Selection goes through rng, not SQL
            # RANDOM(), so --seed works: a different seed gives a different set.
            ids = list(session.exec(statement.with_only_columns(Video.id)).all())
            chosen = rng.sample(ids, min(random_count, len(ids)))
            by_id: dict[int, Video] = {}
            for offset in range(0, len(chosen), SQLITE_ID_CHUNK):
                chunk = chosen[offset : offset + SQLITE_ID_CHUNK]
                for video in session.exec(select(Video).where(Video.id.in_(chunk))).all():
                    by_id[video.id] = video
            videos = [by_id[video_id] for video_id in chosen if video_id in by_id]
        else:
            videos = session.exec(statement.order_by(Video.url)).all()

        if not videos:
            if selected_preset:
                typer.echo(f"No videos matched the '{selected_preset.name}' preset.")
            elif not_exported:
                typer.echo("No videos without the EXPORTED flag — everything is already exported.")
            else:
                typer.echo("The database is empty. Run parse first.")
            raise typer.Exit(1)

        if limit > 0:
            videos = videos[:limit]

        header_fields = [FIELD_MAP[f] for f in EXPORT_FIELDS]
        file_content = "#" + DELIMITER.join(header_fields) + DELIMITER + "\n"

        for video in videos:
            if video.id is not None:
                exported_video_ids.append(video.id)
            tag_names = ",".join(t.name for t in video.tags)
            model_names = ",".join(m.name for m in video.models)
            if with_models:
                for model in video.models:
                    models_by_name[model.name] = {
                        "name": model.name or "",
                        "description": model.description or "",
                        "thumb_url": model.thumb_url or "",
                    }

            raw: dict[str, str] = {
                "url":      video.url or "",
                "thumb_url": video.thumb_url or "",
                "alt":      mix_title((video.alt or "").strip()),
                "duration": str(video.duration or 0),
                "tags":     tag_names,
                "models":   model_names,
            }

            line_parts = []
            for field in EXPORT_FIELDS:
                value = raw[field]
                if field in SANITIZE_FIELDS:
                    value = value.replace('"', '')
                value = (
                    value
                    .replace(DELIMITER, "")
                    .replace('"', "")
                    .replace("\n", " ")
                    .replace("\r", "")
                )
                line_parts.append(value)

            file_content += DELIMITER.join(line_parts) + DELIMITER + "\n"

    output_path = resolve_output_path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(file_content, encoding="utf-8")

    # Models are a separate export behind --models: the file lands next to
    # the video file. Without the flag only videos are exported (models have
    # their own export-models command).
    model_output = None
    if with_models:
        model_output = output_path.with_name(f"models-{output_path.name}")
        model_output.parent.mkdir(parents=True, exist_ok=True)
        model_lines = ["#name|description|thumb_url|\n"]
        missing_thumbs = 0
        for model in sorted(models_by_name.values(), key=lambda item: item["name"].lower()):
            if not model["thumb_url"]:
                missing_thumbs += 1
            values = [model["name"], model["description"], model["thumb_url"]]
            values = [
                value.replace(DELIMITER, "").replace('"', "").replace("\n", " ").replace("\r", "")
                for value in values
            ]
            model_lines.append(DELIMITER.join(values) + DELIMITER + "\n")

        if missing_thumbs:
            typer.echo(
                f"⚠️ Warning: {missing_thumbs} of {len(models_by_name)} related models "
                "are missing thumb_url. Run parse-models.",
                err=True,
            )

        model_output.write_text("".join(model_lines), encoding="utf-8")

    # Set the flag in batches: one UPDATE per chunk instead of loading each row into the ORM.
    with get_session() as session:
        for offset in range(0, len(exported_video_ids), SQLITE_ID_CHUNK):
            chunk = exported_video_ids[offset : offset + SQLITE_ID_CHUNK]
            session.execute(
                update(Video)
                .where(Video.id.in_(chunk))
                .values(status=Video.status.op("|")(int(VideoStage.EXPORTED)))
            )
        session.commit()

    filters = []
    if selected_preset:
        scope = "tags" if preset_tags_only else "tags + title"
        filters.append(f"preset {selected_preset.name} ({scope})")
    if tag_like:
        filters.append(f"tag LIKE '%{tag_like}%'")
    if not_exported:
        filters.append("only not-yet-exported")
    if random_count > 0:
        filters.append(f"random sample, seed={seed if seed is not None else 'none'}")
    filter_label = f" ({', '.join(filters)})" if filters else ""
    typer.echo(
        f"Exported {len(videos)} videos{filter_label} -> {display_path(output_path)}"
    )
    if model_output is not None:
        typer.echo(
            f"Exported {len(models_by_name)} related models "
            f"-> {display_path(model_output)}"
        )


@app.command(name="export-models")
def export_models(
    output: str = typer.Option(
        DEFAULT_MODELS_EXPORT_FILE,
        "--output",
        "-o",
        help=f"File name (goes into {DEFAULT_EXPORT_DIR}/) or a path with a slash",
    ),
    limit: int = typer.Option(0, "--limit", "-l", help="Model limit (0 = unlimited)"),
):
    """Exports models to a pipe-delimited .txt file.

    Format: #name|description|thumb_url|
    """

    from sqlmodel import select

    from src.database.session import get_session
    from src.models import ModelActor

    delimiter = "|"

    with get_session() as session:
        models = session.exec(select(ModelActor).order_by(ModelActor.name)).all()

        if not models:
            typer.echo("No models in the database. Run parse first.")
            raise typer.Exit(1)

        if limit > 0:
            models = models[:limit]

        file_content = "#name|description|thumb_url|\n"

        for model in models:
            values = [model.name or "", model.description or "", model.thumb_url or ""]
            clean_values = [
                value.replace(delimiter, "").replace('"', "").replace("\n", " ").replace("\r", "")
                for value in values
            ]
            file_content += delimiter.join(clean_values) + delimiter + "\n"

    output_path = resolve_output_path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(file_content, encoding="utf-8")

    typer.echo(f"Exported {len(models)} models -> {display_path(output_path)}")


if __name__ == "__main__":
    app()
