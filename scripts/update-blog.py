#!/usr/bin/env python3
"""
Fetch Vastlink's Medium RSS feed and regenerate:
  - blog/index.html          (listing page with internal links)
  - blog/{slug}/index.html   (full article pages)
  - sitemap.xml              (add article URLs)

Usage:
    python3 scripts/update-blog.py              # run from site root
    python3 scripts/update-blog.py --dry-run    # preview without writing files
    python3 scripts/update-blog.py --force      # regenerate all article pages
"""

import datetime
import hashlib
import html
import json
import os
import re
import ssl
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urlunparse, urlencode

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FEED_URL = "https://medium.com/feed/@vastlink"
SITE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOG_DIR = os.path.join(SITE_ROOT, "blog")
BLOG_HTML = os.path.join(BLOG_DIR, "index.html")
SITEMAP_XML = os.path.join(SITE_ROOT, "sitemap.xml")
BLOG_URL = "https://vastlink.xyz/blog/"
SITE_URL = "https://vastlink.xyz"
INDEXNOW_KEY = "ba150392df6bc02b73933b68989bdacd"

HEADER_PARTIAL = os.path.join(BLOG_DIR, "_header.html")
FOOTER_PARTIAL = os.path.join(BLOG_DIR, "_footer.html")
ARTICLE_TEMPLATE = os.path.join(BLOG_DIR, "_article.html")
FEED_CACHE = os.path.join(BLOG_DIR, "_feed_cache.xml")

DEFAULT_OG_IMAGE = "https://vastlink.xyz/assets/img/Vastlink_logo/Dark%20mode.png"

# Clean slug overrides for known articles
SLUG_OVERRIDES = {
    "Digital Asset Management Landscape Research 2025": "digital-asset-management-landscape-2025",
    "The fundamental problem with current crypto wallets (1)": "fundamental-problem-crypto-wallets",
    "Vastbase FAQ": "vastbase-faq",
    # Keys with em-dash and smart quotes — include both Unicode and ASCII variants
    "What can we learn from the biggest crypto hack in history \u2014 #Bybit\u2019s 1.4B hack": "bybit-hack-crypto-security",
    "What can we learn from the biggest crypto hack in history — #Bybit's 1.4B hack": "bybit-hack-crypto-security",
    "Decentralized MPC \u2014 The Future Infrastructure for Crypto Wallets": "decentralized-mpc-future-infrastructure",
    "Decentralized MPC — The Future Infrastructure for Crypto Wallets": "decentralized-mpc-future-infrastructure",
}

# ---------------------------------------------------------------------------
# RSS helpers
# ---------------------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    """Create an SSL context, falling back to unverified on macOS cert issues."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    try:
        urllib.request.urlopen(
            urllib.request.Request("https://medium.com", method="HEAD"),
            timeout=5, context=ctx,
        )
        return ctx
    except Exception:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


def fetch_feed(url: str) -> bytes:
    """Download the RSS feed XML."""
    ctx = _ssl_context()
    req = urllib.request.Request(url, headers={"User-Agent": "VastlinkBlogUpdater/1.0"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return resp.read()


def strip_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_link(url: str) -> str:
    """Remove ?source= and other tracking query params from Medium URLs."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query=""))


def parse_date(date_str: str) -> datetime.date:
    """Parse RSS pubDate like 'Sat, 22 Feb 2025 07:01:23 GMT'."""
    parts = date_str.strip().split()
    if len(parts) >= 5:
        day = int(parts[1])
        month_str = parts[2]
        year = int(parts[3])
        months = {
            "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
            "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
        }
        month = months.get(month_str, 1)
        return datetime.date(year, month, day)
    return datetime.date.fromisoformat(date_str[:10])


def truncate(text: str, length: int = 200) -> str:
    """Truncate text to ~length chars on a word boundary."""
    if len(text) <= length:
        return text
    truncated = text[:length].rsplit(" ", 1)[0]
    return truncated.rstrip(".,;:!? ") + "..."


# ---------------------------------------------------------------------------
# New helpers: slug, read time, HTML cleaning, templates
# ---------------------------------------------------------------------------

def _normalize_title(title: str) -> str:
    """Normalize Unicode chars for slug lookup (hair spaces, smart quotes, etc.)."""
    t = title
    t = t.replace("\u200a", " ")   # hair space → regular space
    t = t.replace("\u200b", "")    # strip zero-width space
    t = t.replace("\u00a0", " ")   # non-breaking space → regular space
    t = t.replace("\u2019", "'")   # right single quote → apostrophe
    t = t.replace("\u2018", "'")   # left single quote → apostrophe
    t = t.replace("\u201c", '"')   # left double quote
    t = t.replace("\u201d", '"')   # right double quote
    # Collapse multiple spaces
    t = re.sub(r"  +", " ", t)
    return t


def slugify(title: str) -> str:
    """Convert title to URL slug. Uses SLUG_OVERRIDES first, then mechanical."""
    if title in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[title]
    # Try normalized version for Unicode-variant matches
    normalized = _normalize_title(title)
    if normalized in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[normalized]
    # Also check normalized override keys against normalized title
    for key, slug_val in SLUG_OVERRIDES.items():
        if _normalize_title(key) == normalized:
            return slug_val
    slug = title.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)       # strip special chars
    slug = re.sub(r"[\s_]+", "-", slug)         # spaces/underscores to hyphens
    slug = re.sub(r"-+", "-", slug)             # collapse hyphens
    slug = slug.strip("-")
    return slug[:60]


def estimate_read_time(html_content: str) -> int:
    """Estimate read time in minutes from HTML content."""
    text = strip_html(html_content)
    word_count = len(text.split())
    return max(1, round(word_count / 200))


def extract_first_image(html_content: str) -> str:
    """Extract the first <img src="..."> URL from HTML. Falls back to site logo."""
    match = re.search(r'<img[^>]+src="([^"]+)"', html_content)
    if match:
        return match.group(1)
    return DEFAULT_OG_IMAGE


def clean_medium_html(html_content: str) -> str:
    """Strip Medium tracking pixels, empty paragraphs, and other cruft."""
    content = html_content
    # Remove tracking pixel images (1x1, hidden, etc.)
    content = re.sub(
        r'<img[^>]*(?:width="1"|height="1"|class="[^"]*tracking[^"]*")[^>]*/?>',
        "", content, flags=re.IGNORECASE
    )
    # Remove Medium tracking pixels (typically 1x1 images at the end)
    content = re.sub(
        r'<img[^>]+src="https://medium\.com/_/stat[^"]*"[^>]*/?>',
        "", content, flags=re.IGNORECASE
    )
    # Remove empty paragraphs
    content = re.sub(r"<p>\s*</p>", "", content)
    # Remove empty divs
    content = re.sub(r"<div>\s*</div>", "", content)
    # Remove Medium's "Continue reading" links
    content = re.sub(
        r'<a[^>]*href="[^"]*\?source=rss[^"]*"[^>]*>Continue reading[^<]*</a>',
        "", content, flags=re.IGNORECASE
    )
    # Replace Medium Embedly iframes with direct YouTube embeds
    def _replace_embedly_iframe(match):
        src = match.group(0)
        yt_match = re.search(r'youtube\.com%2Fembed%2F([a-zA-Z0-9_-]+)', src)
        if yt_match:
            video_id = yt_match.group(1)
            return f'<iframe width="640" height="480" src="https://www.youtube.com/embed/{video_id}" frameborder="0" allowfullscreen></iframe>'
        return ""  # Remove non-YouTube Embedly iframes
    content = re.sub(
        r'<iframe[^>]*src="https://cdn\.embedly\.com/[^"]*"[^>]*>.*?</iframe>',
        _replace_embedly_iframe, content, flags=re.IGNORECASE
    )
    return content.strip()


def load_template(path: str) -> str:
    """Read a template file."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def render_template(template: str, variables: dict) -> str:
    """Replace {{key}} placeholders with values. Simple str.replace approach."""
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", str(value))
    return result


def load_faq_schema(slug: str) -> str:
    """Read optional blog/{slug}/meta.yaml for FAQ schema. Returns empty string if none."""
    meta_path = os.path.join(BLOG_DIR, slug, "meta.yaml")
    if not os.path.exists(meta_path):
        return ""
    try:
        import yaml
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = yaml.safe_load(f)
        faqs = meta.get("faq", [])
        if not faqs:
            return ""
        entities = []
        for faq in faqs:
            entities.append({
                "@type": "Question",
                "name": faq["q"],
                "acceptedAnswer": {"@type": "Answer", "text": faq["a"]},
            })
        schema = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": entities,
        }
        return (
            '  <script type="application/ld+json">\n'
            f"  {json.dumps(schema, indent=2, ensure_ascii=False)}\n"
            "  </script>"
        )
    except ImportError:
        # PyYAML not installed — skip gracefully
        return ""
    except Exception:
        return ""


def content_hash(content: str) -> str:
    """MD5 hash of content for change detection."""
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def read_existing_hash(slug: str) -> str:
    """Read the content hash from a previously generated article page."""
    page_path = os.path.join(BLOG_DIR, slug, "index.html")
    if not os.path.exists(page_path):
        return ""
    try:
        with open(page_path, "r", encoding="utf-8") as f:
            first_lines = f.read(500)
        match = re.search(r"<!-- content-hash: ([a-f0-9]{32}) -->", first_lines)
        if match:
            return match.group(1)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# RSS parsing (extended)
# ---------------------------------------------------------------------------

def parse_feed(xml_bytes: bytes) -> list:
    """Parse RSS XML and return list of article dicts sorted newest-first."""
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pub_date_el = item.find("pubDate")
        desc_el = item.find("description")
        content_el = item.find("{http://purl.org/rss/1.0/modules/content/}encoded")

        title = title_el.text.strip() if title_el is not None and title_el.text else "Untitled"
        link = clean_link(link_el.text.strip()) if link_el is not None and link_el.text else ""
        pub_date = parse_date(pub_date_el.text) if pub_date_el is not None and pub_date_el.text else datetime.date.today()

        # Full HTML content from content:encoded
        content_html = ""
        if content_el is not None and content_el.text:
            content_html = content_el.text

        # Short description for cards
        raw_desc = content_html or (desc_el.text if desc_el is not None and desc_el.text else "")
        description = truncate(strip_html(raw_desc), 200)

        slug = slugify(title)
        read_time = estimate_read_time(content_html) if content_html else 1

        items.append({
            "title": title,
            "link": link,
            "date": pub_date,
            "description": description,
            "content_html": content_html,
            "slug": slug,
            "read_time": read_time,
        })

    items.sort(key=lambda x: x["date"], reverse=True)
    return items


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def format_date_display(d: datetime.date) -> str:
    """Format as 'Dec 2, 2025'."""
    return d.strftime("%b %-d, %Y") if sys.platform != "win32" else d.strftime("%b %d, %Y").replace(" 0", " ")


def make_article_card_internal(article: dict, delay: int = 0) -> str:
    """Generate one article card HTML linking to internal /blog/{slug}/ page."""
    delay_attr = f' data-delay="{delay}"' if delay else ""
    date_display = format_date_display(article["date"])
    date_iso = article["date"].isoformat()
    title_escaped = html.escape(article["title"])
    desc_escaped = html.escape(article["description"])
    slug = article["slug"]

    return (
        f'      <a class="p-6 rounded-2xl border border-slate-800 bg-slate-900/50 hover:border-green-500/40 transition-colors reveal tile-float flex flex-col"{delay_attr}\n'
        f'         href="/blog/{slug}/">\n'
        f'        <div class="flex items-start gap-3">\n'
        f'          <img src="/assets/img/icon_article.svg" width="22" height="22" alt="" aria-hidden="true" class="border-green-500 pt-1" />\n'
        f'          <h2 class="font-semibold">{title_escaped}</h2>\n'
        f'        </div>\n'
        f'        <p class="text-sm mt-2 text-slate-400 flex-1">{desc_escaped}</p>\n'
        f'        <div class="mt-4 flex items-center justify-between text-xs text-slate-500">\n'
        f'          <time datetime="{date_iso}">{date_display}</time>\n'
        f'          <span class="text-green-400 font-medium">Read article &rarr;</span>\n'
        f'        </div>\n'
        f'      </a>'
    )


def make_json_ld_posts(articles: list) -> str:
    """Generate the blogPost JSON-LD array entries with internal URLs."""
    posts = []
    for a in articles:
        posts.append({
            "@type": "BlogPosting",
            "headline": a["title"],
            "url": f"https://vastlink.xyz/blog/{a['slug']}/",
            "datePublished": a["date"].isoformat(),
            "author": {"@type": "Organization", "name": "Vastlink"},
        })
    return json.dumps(posts, indent=6, ensure_ascii=False)


def build_related_articles(current_slug: str, all_articles: list) -> str:
    """Pick up to 3 other articles and render as internal cards."""
    others = [a for a in all_articles if a["slug"] != current_slug][:3]
    cards = []
    for i, article in enumerate(others):
        delay = i * 100
        cards.append(make_article_card_internal(article, delay))
    return "\n\n".join(cards)


def build_article_page(article: dict, all_articles: list) -> str:
    """Load template + partials and render a full article page."""
    template = load_template(ARTICLE_TEMPLATE)
    header_html = load_template(HEADER_PARTIAL)
    footer_html = load_template(FOOTER_PARTIAL)

    cleaned_content = clean_medium_html(article["content_html"])
    og_image = extract_first_image(article["content_html"])
    faq_schema = load_faq_schema(article["slug"])
    related = build_related_articles(article["slug"], all_articles)
    meta_desc = html.escape(article["description"])

    variables = {
        "title": html.escape(article["title"]),
        "meta_description": meta_desc,
        "slug": article["slug"],
        "date_iso": article["date"].isoformat(),
        "date_display": format_date_display(article["date"]),
        "read_time": str(article["read_time"]),
        "og_image": og_image,
        "content": cleaned_content,
        "header": header_html,
        "footer": footer_html,
        "faq_schema": faq_schema,
        "related_articles": related,
    }

    rendered = render_template(template, variables)

    # Inject content hash as HTML comment after <!DOCTYPE html>
    c_hash = content_hash(article["content_html"])
    rendered = rendered.replace(
        "<!DOCTYPE html>",
        f"<!DOCTYPE html>\n<!-- content-hash: {c_hash} -->",
        1,
    )

    return rendered


def build_blog_html(articles: list) -> str:
    """Build the complete blog/index.html content with internal links."""
    # Build article cards with staggered delay pattern (per row of 3)
    cards = []
    for i, article in enumerate(articles):
        delay = (i % 3) * 100
        cards.append(make_article_card_internal(article, delay))

    article_cards = "\n\n".join(cards)
    json_ld_posts = make_json_ld_posts(articles)

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blog — Vastlink</title>
  <meta name="description" content="Articles on MPC wallets, digital asset management, crypto security, and decentralized infrastructure from the Vastlink team." />
  <link rel="icon" href="../assets/img/favicon.png" />
  <link rel="canonical" href="https://vastlink.xyz/blog/" />
  <meta name="robots" content="index,follow" />

  <!-- Open Graph / Twitter -->
  <meta property="og:type" content="website" />
  <meta property="og:title" content="Blog — Vastlink" />
  <meta property="og:description" content="Articles on MPC wallets, digital asset management, crypto security, and decentralized infrastructure from the Vastlink team." />
  <meta property="og:url" content="https://vastlink.xyz/blog/" />
  <meta property="og:image" content="https://vastlink.xyz/assets/img/Vastlink_logo/Dark%20mode.png" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:site" content="@thevastlink" />
  <meta name="twitter:title" content="Blog — Vastlink" />
  <meta name="twitter:description" content="Articles on MPC wallets, digital asset management, crypto security, and decentralized infrastructure from the Vastlink team." />
  <meta name="twitter:image" content="https://vastlink.xyz/assets/img/Vastlink_logo/Dark%20mode.png" />

  <!-- Font Awesome icons (free version)-->
  <script src="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.2.1/js/all.min.js"
  crossorigin="anonymous"></script>

  <!-- Tailwind CSS (CDN) -->
  <script src="https://cdn.tailwindcss.com"></script>

  <!-- Schema.org JSON-LD -->
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "Blog",
    "name": "Vastlink Blog",
    "url": "https://vastlink.xyz/blog/",
    "description": "Articles on MPC wallets, digital asset management, crypto security, and decentralized infrastructure from the Vastlink team.",
    "publisher": {{
      "@type": "Organization",
      "name": "Vastlink",
      "url": "https://vastlink.xyz/",
      "logo": "https://vastlink.xyz/assets/img/Vastlink_logo/Dark%20mode.png"
    }},
    "blogPost": {json_ld_posts}
  }}
  </script>

  <!-- Simple styles for background gradient -->
  <style>
    :root {{ --brand-start: #52C41A; --brand-end: #3AA312; }}
    .glow {{ box-shadow: 0 0 80px rgba(82,196,26,0.25); }}
    .brand-gradient {{ background-image: linear-gradient(90deg, var(--brand-start), var(--brand-end)); }}
    .brand-text {{ background: linear-gradient(90deg, var(--brand-start), var(--brand-end)); -webkit-background-clip: text; background-clip: text; color: transparent; }}
    .reveal {{ opacity: 0; transform: translateY(16px); transition: opacity .6s ease, transform .6s ease; }}
    .reveal.in-view {{ opacity: 1; transform: none; }}
    .reveal[data-delay="100"] {{ transition-delay: .1s; }}
    .reveal[data-delay="200"] {{ transition-delay: .2s; }}
    .reveal[data-delay="300"] {{ transition-delay: .3s; }}
    .section-underline::after {{ content: ""; display: block; width: 72px; height: 4px; border-radius: 9999px; margin-top: 12px; background-image: linear-gradient(90deg, var(--brand-start), var(--brand-end)); }}
    .link-brand{{position:relative;transition:color .2s ease;}}
    .link-brand:hover{{color:#fff}}
    .link-brand::after{{content:"";position:absolute;left:0;bottom:-4px;height:2px;width:0;background-image:linear-gradient(90deg,var(--brand-start),var(--brand-end));transition:width .25s ease}}
    .link-brand:hover::after{{width:100%}}
    .link-cta{{background-image:linear-gradient(90deg,var(--brand-start),var(--brand-end));color:#fff}}
    .link-cta:hover{{opacity:.92}}
    .tile-float{{will-change:transform,box-shadow;transition:transform .4s cubic-bezier(.2,.8,.2,1), box-shadow .4s ease}}
    .tile-float.in-view{{transform:translateY(0)}}
    .tile-float:hover{{transform:translateY(-3px)}}
  </style>

  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-Y830MJMX1L"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{dataLayer.push(arguments);}}
    gtag('js', new Date());
    gtag('config', 'G-Y830MJMX1L');
  </script>
</head>
<body class="bg-slate-950 text-slate-100 antialiased">
  <!-- Header -->
  <header class="sticky top-0 z-50 bg-slate-950/80 backdrop-blur border-b border-slate-800">
    <div class="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
      <a href="/" class="flex items-center gap-2"><img src="../assets/img/Vastlink_logo/Dark%20mode.png" alt="Vastlink" class="h-8 w-auto rounded"/><span class="sr-only">Vastlink</span></a>
      <nav class="hidden md:flex gap-6 text-slate-300">
        <a href="https://vastlink.xyz/#features" class="link-brand">Features</a>
        <a href="https://vastlink.xyz/#use-cases" class="link-brand">Use cases</a>
        <a href="https://vastlink.xyz/blog/" class="link-brand">Blog</a>
        <a href="https://vastlink.xyz/#faq" class="link-brand">FAQ</a>
      </nav>
      <div class="flex gap-3">
        <a href="https://vastlink.xyz/#waitlist" class="px-4 py-2 rounded-xl brand-gradient text-white font-medium hover:opacity-90 shadow-md hover:shadow-lg focus:outline-none focus:ring-2 focus:ring-green-500/60">Join now</a>
      </div>
    </div>
  </header>

  <!-- Blog Hero -->
  <section class="relative overflow-hidden">
    <div class="absolute inset-0 bg-gradient-to-b from-green-500/10 via-transparent to-transparent"></div>
    <div class="max-w-7xl mx-auto px-6 pt-20 pb-12">
      <h1 class="text-3xl md:text-4xl font-semibold brand-text reveal">Blog</h1>
      <p class="mt-3 text-slate-300 text-lg reveal" data-delay="100">Insights on MPC wallets, digital asset management, and crypto security from the Vastlink team.</p>
    </div>
  </section>

  <!-- Articles Grid -->
  <section class="max-w-7xl mx-auto px-6 pb-24">
    <div class="grid md:grid-cols-2 lg:grid-cols-3 gap-6">

{article_cards}

    </div>

  </section>

  <!-- Footer -->
  <footer class="border-t border-slate-800">
    <div class="max-w-7xl mx-auto px-4 py-10 grid md:grid-cols-3 gap-6 text-slate-400">
      <div>
        <div class="text-white font-semibold">Vastlink</div>
        <p class="mt-2">A next generation Wallet-as-a-Protocol for mass adoption for humans and AI.</p>
      </div>
      <div>
        <div class="font-semibold text-slate-200">Company</div>
        <ul class="mt-2 space-y-2">
          <li><a href="https://vastlink.xyz/terms_and_conditions.html" class="link-brand" target="_blank">Terms and conditions</a></li>
          <li><a href="https://vastlink.xyz/privacy.html" class="link-brand" target="_blank">Privacy</a></li>
          <li><a href="https://vastlink.xyz/blog/" class="link-brand">Blog</a></li>
          <li><a href="https://vastlink.xyz/#faq" class="link-brand">FAQ</a></li>
        </ul>
      </div>
      <div>
        <div class="font-semibold text-slate-200">Follow</div>
        <ul class="mt-2 space-y-2">
          <li><a class="link-brand" href="https://x.com/thevastlink" target="_blank"><i class="fab fa-fw fa-twitter"></i> Twitter</a></li>
          <li><a class="link-brand" href="https://www.linkedin.com/company/104843132" target="_blank"><i class="fab fa-fw fa-linkedin"></i> LinkedIn</a></li>
          <li><a class="link-brand" href="https://github.com/vastlink-xyz" target="_blank"><i class="fab fa-fw fa-github"></i> GitHub</a></li>
          <li><a class="link-brand" href="https://vastlink.medium.com/" target="_blank"><i class="fab fa-fw fa-medium"></i> Medium</a></li>
          <li><a class="link-brand" href="https://t.me/VastlinkCommunity" target="_blank"><i class="fab fa-fw fa-telegram"></i> Telegram</a></li>
          <li><a class="link-brand" href="https://www.youtube.com/@vastlink" target="_blank"><i class="fab fa-fw fa-youtube"></i> YouTube</a></li>
          <li><a class="link-brand" href="https://vastlink.gitbook.io/vastlink-whitepaper" target="_blank">&nbsp;<i class="fa fa-book"></i> Whitepaper</a></li>
        </ul>
      </div>
    </div>
    <div class="text-center text-slate-500 pb-8">Dubai &middot; Hong Kong &middot; Singapore &middot; Sydney &middot; &copy; Vastlink 2024 ~ 2026</div>
  </footer>

  <script>
    // Add shadow to header on scroll
    const header = document.querySelector('header');
    const onScrollHeader = () => {{
      if (window.scrollY > 10) header.classList.add('shadow-lg','border-slate-700');
      else header.classList.remove('shadow-lg','border-slate-700');
    }};
    document.addEventListener('scroll', onScrollHeader, {{ passive: true }});
    onScrollHeader();

    // IntersectionObserver to reveal elements
    const io = new IntersectionObserver((entries) => {{
      entries.forEach((e) => {{ if (e.isIntersecting) e.target.classList.add('in-view'); }});
    }}, {{ threshold: 0.12 }});
    document.querySelectorAll('.reveal').forEach((el) => io.observe(el));
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Sitemap updater (extended for article URLs)
# ---------------------------------------------------------------------------

def update_sitemap(sitemap_path: str, articles: list, today: str) -> bool:
    """Update sitemap.xml: update /blog/ lastmod and add article URLs. Returns True if changed."""
    with open(sitemap_path, "r", encoding="utf-8") as f:
        content = f.read()

    original = content

    # Update /blog/ lastmod
    pattern = r"(<url>\s*<loc>https://vastlink\.xyz/blog/</loc>\s*<lastmod>)(\d{4}-\d{2}-\d{2})(</lastmod>)"
    content = re.sub(pattern, rf"\g<1>{today}\3", content)

    # Update homepage lastmod too
    pattern_home = r"(<url>\s*<loc>https://vastlink\.xyz/</loc>\s*<lastmod>)(\d{4}-\d{2}-\d{2})(</lastmod>)"
    content = re.sub(pattern_home, rf"\g<1>{today}\3", content)

    # Add article URLs if not already present
    for article in articles:
        article_url = f"https://vastlink.xyz/blog/{article['slug']}/"
        if article_url not in content:
            article_entry = (
                f"  <url>\n"
                f"    <loc>{article_url}</loc>\n"
                f"    <lastmod>{article['date'].isoformat()}</lastmod>\n"
                f"    <changefreq>monthly</changefreq>\n"
                f"    <priority>0.7</priority>\n"
                f"  </url>\n"
            )
            # Insert before closing </urlset>
            content = content.replace("</urlset>", article_entry + "</urlset>")

    if content == original:
        return False

    with open(sitemap_path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


# ---------------------------------------------------------------------------
# IndexNow ping
# ---------------------------------------------------------------------------

def ping_indexnow(urls: list, key: str | None = None):
    """Ping IndexNow with the given URLs. Best-effort, errors are logged."""
    if not key:
        print("  [skip] IndexNow: no API key configured")
        return
    for url in urls:
        try:
            ctx = _ssl_context()
            params = urlencode({"url": url, "key": key})
            ping_url = f"https://api.indexnow.org/indexnow?{params}"
            req = urllib.request.Request(ping_url, headers={"User-Agent": "VastlinkBlogUpdater/1.0"})
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                print(f"  IndexNow [{url}]: {resp.status}")
        except Exception as e:
            print(f"  [warn] IndexNow ping failed for {url}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv

    print("=" * 60)
    print("Vastlink Blog Updater")
    print("=" * 60)
    today = datetime.date.today().isoformat()
    print(f"Date: {today}")
    print(f"Site root: {SITE_ROOT}")
    print(f"Dry run: {dry_run}")
    print(f"Force regenerate: {force}")
    print()

    # 0. Verify templates exist
    for tpl_path, tpl_name in [
        (HEADER_PARTIAL, "_header.html"),
        (FOOTER_PARTIAL, "_footer.html"),
        (ARTICLE_TEMPLATE, "_article.html"),
    ]:
        if not os.path.exists(tpl_path):
            print(f"  [error] Template not found: blog/{tpl_name}")
            sys.exit(1)
    print("Templates verified.")
    print()

    # 1. Fetch RSS feed (with caching)
    print("Fetching RSS feed...")
    try:
        xml_bytes = fetch_feed(FEED_URL)
        # Cache the feed
        if not dry_run:
            with open(FEED_CACHE, "wb") as f:
                f.write(xml_bytes)
    except urllib.error.URLError as e:
        print(f"  [warn] Failed to fetch feed: {e}")
        # Try cached feed
        if os.path.exists(FEED_CACHE):
            print("  Using cached feed...")
            with open(FEED_CACHE, "rb") as f:
                xml_bytes = f.read()
        else:
            print("  [error] No cached feed available.")
            sys.exit(1)
    print(f"  Feed fetched ({len(xml_bytes)} bytes)")

    # 2. Parse articles
    articles = parse_feed(xml_bytes)
    print(f"  Found {len(articles)} articles")
    for a in articles:
        print(f"    - [{a['date']}] {a['title']}")
        print(f"      slug: {a['slug']} | read_time: {a['read_time']}min | content: {len(a['content_html'])} chars")
    print()

    # 3. Generate article pages
    print("Generating article pages...")
    new_article_urls = []
    for article in articles:
        slug = article["slug"]
        if not article["content_html"]:
            print(f"  [skip] {slug}: no content_html in feed")
            continue

        existing_hash = read_existing_hash(slug)
        current_hash = content_hash(article["content_html"])

        if not force and existing_hash == current_hash:
            print(f"  [skip] {slug}: unchanged (hash match)")
            continue

        print(f"  Generating: blog/{slug}/index.html")
        page_html = build_article_page(article, articles)

        if not dry_run:
            article_dir = os.path.join(BLOG_DIR, slug)
            os.makedirs(article_dir, exist_ok=True)
            page_path = os.path.join(article_dir, "index.html")
            with open(page_path, "w", encoding="utf-8") as f:
                f.write(page_html)
            print(f"    Written ({len(page_html)} bytes)")
        else:
            print(f"    [dry-run] Would write {len(page_html)} bytes")

        article_url = f"{SITE_URL}/blog/{slug}/"
        new_article_urls.append(article_url)
    print()

    # 4. Generate blog listing HTML
    print("Generating blog/index.html...")
    blog_html = build_blog_html(articles)
    if not dry_run:
        os.makedirs(os.path.dirname(BLOG_HTML), exist_ok=True)
        with open(BLOG_HTML, "w", encoding="utf-8") as f:
            f.write(blog_html)
        print(f"  Written ({len(blog_html)} bytes)")
    else:
        print(f"  [dry-run] Would write {len(blog_html)} bytes")

    # 5. Update sitemap.xml
    print("Updating sitemap.xml...")
    if not dry_run:
        changed = update_sitemap(SITEMAP_XML, articles, today)
        if changed:
            print(f"  Updated (lastmod={today}, added article URLs)")
        else:
            print(f"  Already up to date")
    else:
        print(f"  [dry-run] Would update sitemap")

    # 6. Ping IndexNow for new/changed articles
    print("Pinging IndexNow...")
    if not dry_run and new_article_urls:
        all_urls = [BLOG_URL] + new_article_urls
        ping_indexnow(all_urls, INDEXNOW_KEY)
    elif not new_article_urls:
        print("  No new/changed articles to ping")
    else:
        print("  [dry-run] Would ping IndexNow")

    # Summary
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  Total articles:       {len(articles)}")
    print(f"  Articles generated:   {len(new_article_urls)}")
    print(f"  blog/index.html:      {'updated' if not dry_run else 'dry-run'}")
    print(f"  sitemap.xml:          {'updated' if not dry_run else 'dry-run'}")
    for a in articles:
        print(f"  /blog/{a['slug']}/")
    print("Done.")


if __name__ == "__main__":
    main()
