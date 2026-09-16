# XVideos Scraper CLI

Async scraper for video listings and model profiles into SQLite. Exports to
pipe-delimited TXT and translates title/description into other languages.

## Setup

```bash
uv sync
uv run python cli.py --help
```

Needs Python 3.12+ and `uv`. All default paths (databases, configs, exports) live
in `src/config.py`, so commands work from any working directory.

For `--db`, `-o`, `--longtails`, `--seed-categories`, `--extra-categories`: a bare
name is looked up in the default directory (`--db foo.db` → `db/foo.db`), a path
with a slash or tilde is used as-is.

## Database

```bash
uv run python cli.py init-db      # CREATE TABLE IF NOT EXISTS, doesn't drop anything
uv run python cli.py show-db      # or --json
```

Back up before changing the schema: `cp db/database.db db/database.db.backup-$(date +%Y%m%d)`.

## Scraping

```bash
uv run python cli.py parse https://www.xvideos.com/best-of-gay/2015-01
uv run python cli.py parse --urls-file urls.txt --limit 100 --max-pages 3 --concurrency 4
uv run python cli.py parse <url> --dry-run   # no DB writes, first 3 videos
```

Works with regular, search, and tag listings. Pagination is sequential, video
scraping runs with limited concurrency, DB writes go in batches.

Model profiles:

```bash
uv run python cli.py parse-models --limit 100          # only models without gender
uv run python cli.py parse-models --all                # refresh everything
uv run python cli.py parse-model-videos <model-url> --limit 25
```

## Export

```bash
uv run python cli.py export -o export.txt
uv run python cli.py export --tag-like gay --random 500 --seed 42 -o gay.txt
uv run python cli.py export-models -o all-models.txt
```

Format: `#url|thumb|alt::1|duration|tags::1|models|`. `--random N` takes a random
sample from the filtered results (`--seed` for reproducibility), incompatible with
`--limit`. Every `export` also writes `models-<file>` with the related models.

Longtail phrases in the title (`--longtails phrases.txt`, `--longtail-insert`,
`--seed`, `--delimiter`) — see the example file `longtails_com.txt`.

## Translations

Language list lives in `src/translations/config.py`. Providers: `ai` (needs a
`.env` with `TRANSLATION_AI_API_KEY/URL/MODEL`), `google`, `bing`, `baidu` — keys
also go in `.env`.

```bash
uv run python cli.py translate --provider ai --language zh-CN --limit 100 --dry-run
```

## Categories from tags

One-off script, doesn't touch the DB:

```bash
uv run python -m scripts.build_categories --min-video-count 5 --output categories.txt
uv run python -m scripts.build_categories --min-video-count 5 --model deepseek-v4-flash --suggestions --output categories.txt
```

Clusters tags by n-grams, optionally names clusters via AI, and extends keywords
through the XVideos suggestions API. Output goes to `~/Exports/scrapy/categories/`.

## Lint

```bash
uv run ruff check
```
