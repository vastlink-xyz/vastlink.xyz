# vastlink.xyz

Static website for [Vastlink](https://vastlink.xyz), hosted on GitHub Pages.

## Blog updater

`scripts/update-blog.py` fetches the [Medium RSS feed](https://medium.com/feed/@vastlink) and regenerates the blog pages and sitemap.

### Prerequisites

- Python 3.10+
- No third-party packages required (uses stdlib only)
- Optional: `PyYAML` for FAQ schema support via `blog/{slug}/meta.yaml`
- Optional: `certifi` for SSL on macOS

### Usage

Run from the repository root:

```bash
# Fetch feed and regenerate all blog pages + sitemap
python3 scripts/update-blog.py

# Preview what would change without writing files
python3 scripts/update-blog.py --dry-run

# Force regenerate all article pages (ignore content hash cache)
python3 scripts/update-blog.py --force
```

### What it does

1. Fetches the Medium RSS feed (caches to `blog/_feed_cache.xml` as fallback)
2. Generates individual article pages at `blog/{slug}/index.html` using templates
3. Regenerates the blog listing page at `blog/index.html`
4. Updates `sitemap.xml` with article URLs and today's date
5. Pings IndexNow to notify search engines of new/changed pages

### Templates

Article pages are built from partial templates in `blog/`:

| File | Purpose |
|---|---|
| `blog/_header.html` | Shared site header (nav, logo) |
| `blog/_footer.html` | Shared site footer (links, socials) |
| `blog/_article.html` | Article page layout with `{{placeholder}}` variables |

### Slug overrides

Article URL slugs are auto-generated from titles. To use a custom slug, add an entry to the `SLUG_OVERRIDES` dict in the script.

### Change detection

Each generated article page embeds a `<!-- content-hash: ... -->` comment. On subsequent runs, the script skips articles whose content hasn't changed. Use `--force` to bypass this.
