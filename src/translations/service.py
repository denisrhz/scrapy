from pypinyin import lazy_pinyin
from slugify import slugify
from sqlmodel import select

from src.database.session import get_session
from src.models import Video, VideoStage, VideoTranslation
from src.translations.providers import TranslationProvider, TranslationResult


def make_slug(title: str, language: str, video_id: int) -> str:
    value = " ".join(lazy_pinyin(title)) if language.startswith("zh") else title
    return f"{slugify(value) or 'video'}-{video_id}"


async def translate_videos(
    provider: TranslationProvider,
    languages: list[str],
    limit: int = 0,
    batch_size: int = 10,
    dry_run: bool = False,
) -> int:
    if batch_size < 1:
        raise ValueError("batch_size must be greater than zero")

    translated_count = 0

    with get_session() as session:
        videos = session.exec(select(Video).order_by(Video.id)).all()

    for language in languages:
        with get_session() as session:
            translated_ids = set(
                session.exec(
                    select(VideoTranslation.video_id).where(VideoTranslation.language == language)
                ).all()
            )

        pending = [video for video in videos if video.id not in translated_ids]
        if limit > 0:
            pending = pending[:limit]

        results: dict[int, TranslationResult] = {}
        batch_translator = getattr(provider, "translate_batch", None)
        if provider.name == "ai" and batch_translator:
            for offset in range(0, len(pending), batch_size):
                batch = [video for video in pending[offset : offset + batch_size] if video.id is not None]
                items = [(video.id, video.alt or "", video.desc or "") for video in batch]
                try:
                    print(f"[{language}] AI batch: {len(items)} videos")
                    results.update(await batch_translator(items, language))
                except Exception as error:
                    print(f"⚠️ Batch failed ({len(items)} videos): {error}. Falling back to single requests.")
                    for video_id, title, description in items:
                        results[video_id] = await provider.translate(title, description, language)
        else:
            for video in pending:
                if video.id is not None:
                    results[video.id] = await provider.translate(
                        video.alt or "", video.desc or "", language
                    )

        pending_translations: list[VideoTranslation] = []
        for video in pending:
            if video.id is None:
                continue
            result = results[video.id]
            slug = make_slug(result.title, language, video.id)
            print(f"[{language}] {video.id}: {result.title} -> {slug}")

            if dry_run:
                continue

            pending_translations.append(
                VideoTranslation(
                    video_id=video.id,
                    language=language,
                    title=result.title,
                    description=result.description,
                    slug=slug,
                    provider=provider.name,
                )
            )

        if pending_translations:
            with get_session() as session:
                session.add_all(pending_translations)
                video_ids = [translation.video_id for translation in pending_translations]
                translated_videos = session.exec(
                    select(Video).where(Video.id.in_(video_ids))
                ).all()
                for video in translated_videos:
                    video.status = int(video.status) | int(VideoStage.TRANSLATED)
                    session.add(video)
                session.commit()
            translated_count += len(pending_translations)

    return translated_count
