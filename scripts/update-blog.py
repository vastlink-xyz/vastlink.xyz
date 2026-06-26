#!/usr/bin/env python3
"""
Fetch Vastlink's Medium RSS feed and regenerate blog/index.html + update sitemap.xml.

Usage:
    python3 scripts/update-blog.py          # run from site root
    python3 scripts/update-blog.py --dry-run  # preview without writing files
"""

import datetime
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
BLOG_HTML = os.path.join(SITE_ROOT, "blog", "index.html")
SITEMAP_XML = os.path.join(SITE_ROOT, "sitemap.xml")
BLOG_URL = "https://vastlink.xyz/blog/"
INDEXNOW_KEY = "ba150392df6bc02b73933b68989bdacd"

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
        # macOS Python often lacks system certs; fall back to unverified
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
    # collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_link(url: str) -> str:
    """Remove ?source= and other tracking query params from Medium URLs."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query=""))


def parse_date(date_str: str) -> datetime.date:
    """Parse RSS pubDate like 'Sat, 22 Feb 2025 07:01:23 GMT'."""
    # Remove day-of-week prefix and timezone suffix for simpler parsing
    parts = date_str.strip().split()
    # Expected: ['Sat,', '22', 'Feb', '2025', '07:01:23', 'GMT']
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
    # fallback: try ISO
    return datetime.date.fromisoformat(date_str[:10])


def truncate(text: str, length: int = 200) -> str:
    """Truncate text to ~length chars on a word boundary."""
    if len(text) <= length:
        return text
    truncated = text[:length].rsplit(" ", 1)[0]
    return truncated.rstrip(".,;:!? ") + "..."


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """Parse RSS XML and return list of article dicts sorted newest-first."""
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pub_date_el = item.find("pubDate")
        # Medium puts HTML content in content:encoded; description has a short version
        desc_el = item.find("description")
        # content:encoded namespace
        content_el = item.find("{http://purl.org/rss/1.0/modules/content/}encoded")

        title = title_el.text.strip() if title_el is not None and title_el.text else "Untitled"
        link = clean_link(link_el.text.strip()) if link_el is not None and link_el.text else ""
        pub_date = parse_date(pub_date_el.text) if pub_date_el is not None and pub_date_el.text else datetime.date.today()

        # Extract description: prefer content:encoded (richer), fall back to description
        raw_desc = ""
        if content_el is not None and content_el.text:
            raw_desc = content_el.text
        elif desc_el is not None and desc_el.text:
            raw_desc = desc_el.text
        description = truncate(strip_html(raw_desc), 200)

        items.append({
            "title": title,
            "link": link,
            "date": pub_date,
            "description": description,
        })

    # Sort newest first
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def format_date_display(d: datetime.date) -> str:
    """Format as 'Dec 2, 2025'."""
    return d.strftime("%b %-d, %Y") if sys.platform != "win32" else d.strftime("%b %d, %Y").replace(" 0", " ")


def make_article_card(article: dict, delay: int = 0) -> str:
    """Generate one article card HTML block."""
    delay_attr = f' data-delay="{delay}"' if delay else ""
    date_display = format_date_display(article["date"])
    date_iso = article["date"].isoformat()
    title_escaped = html.escape(article["title"])
    desc_escaped = html.escape(article["description"])
    link_escaped = html.escape(article["link"])

    # 6-space indent to sit inside the grid container
    return (
        f'      <a class="p-6 rounded-2xl border border-slate-800 bg-slate-900/50 hover:border-green-500/40 transition-colors reveal tile-float flex flex-col"{delay_attr}\n'
        f'         href="{link_escaped}"\n'
        f'         target="_blank" rel="noopener">\n'
        f'        <div class="flex items-start gap-3">\n'
        f'          <img src="../assets/img/icon_article.svg" width="22" height="22" alt="" aria-hidden="true" class="border-green-500 pt-1" />\n'
        f'          <h2 class="font-semibold">{title_escaped}</h2>\n'
        f'        </div>\n'
        f'        <p class="text-sm mt-2 text-slate-400 flex-1">{desc_escaped}</p>\n'
        f'        <div class="mt-4 flex items-center justify-between text-xs text-slate-500">\n'
        f'          <time datetime="{date_iso}">{date_display}</time>\n'
        f'          <span class="text-green-400 font-medium">Read on Medium &rarr;</span>\n'
        f'        </div>\n'
        f'      </a>'
    )


def make_json_ld_posts(articles: list[dict]) -> str:
    """Generate the blogPost JSON-LD array entries."""
    posts = []
    for a in articles:
        posts.append({
            "@type": "BlogPosting",
            "headline": a["title"],
            "url": a["link"],
            "datePublished": a["date"].isoformat(),
            "author": {"@type": "Organization", "name": "Vastlink"},
        })
    return json.dumps(posts, indent=6, ensure_ascii=False)


def build_blog_html(articles: list[dict]) -> str:
    """Build the complete blog/index.html content."""
    # Build article cards with staggered delay pattern (per row of 3)
    cards = []
    for i, article in enumerate(articles):
        delay = (i % 3) * 100  # 0, 100, 200, 0, 100, 200, ...
        cards.append(make_article_card(article, delay))

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
# Sitemap updater
# ---------------------------------------------------------------------------

def update_sitemap(sitemap_path: str, today: str) -> bool:
    """Update the lastmod for /blog/ in sitemap.xml. Returns True if changed."""
    with open(sitemap_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Match the <url> block containing /blog/
    pattern = r"(<url>\s*<loc>https://vastlink\.xyz/blog/</loc>\s*<lastmod>)(\d{4}-\d{2}-\d{2})(</lastmod>)"
    match = re.search(pattern, content)
    if not match:
        print("  [warn] Could not find /blog/ entry in sitemap.xml")
        return False

    old_date = match.group(2)
    if old_date == today:
        return False

    new_content = re.sub(pattern, rf"\g<1>{today}\3", content)
    with open(sitemap_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True


# ---------------------------------------------------------------------------
# IndexNow ping
# ---------------------------------------------------------------------------

def ping_indexnow(url: str, key: str | None = None):
    """Ping IndexNow with the given URL. Best-effort, errors are logged."""
    if not key:
        print("  [skip] IndexNow: no API key configured")
        return
    try:
        ctx = _ssl_context()
        params = urlencode({"url": url, "key": key})
        ping_url = f"https://api.indexnow.org/indexnow?{params}"
        req = urllib.request.Request(ping_url, headers={"User-Agent": "VastlinkBlogUpdater/1.0"})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            print(f"  IndexNow response: {resp.status}")
    except Exception as e:
        print(f"  [warn] IndexNow ping failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv

    print("=" * 60)
    print("Vastlink Blog Updater")
    print("=" * 60)
    today = datetime.date.today().isoformat()
    print(f"Date: {today}")
    print(f"Site root: {SITE_ROOT}")
    print(f"Dry run: {dry_run}")
    print()

    # 1. Read existing article links for diff detection
    existing_links = set()
    if os.path.exists(BLOG_HTML):
        with open(BLOG_HTML, "r", encoding="utf-8") as f:
            for m in re.finditer(r'href="(https://vastlink\.medium\.com/[^"]+)"', f.read()):
                existing_links.add(clean_link(m.group(1)))

    # 2. Fetch RSS feed
    print("Fetching RSS feed...")
    try:
        xml_bytes = fetch_feed(FEED_URL)
    except urllib.error.URLError as e:
        print(f"  [error] Failed to fetch feed: {e}")
        sys.exit(1)
    print(f"  Feed fetched ({len(xml_bytes)} bytes)")

    # 3. Parse articles
    articles = parse_feed(xml_bytes)
    print(f"  Found {len(articles)} articles")
    for a in articles:
        print(f"    - [{a['date']}] {a['title']}")
    print()

    # 4. Detect new articles
    new_links = set(a["link"] for a in articles)
    added = new_links - existing_links
    removed = existing_links - new_links
    if added:
        print(f"New articles detected ({len(added)}):")
        for link in sorted(added):
            print(f"  + {link}")
    if removed:
        print(f"Articles no longer in feed ({len(removed)}):")
        for link in sorted(removed):
            print(f"  - {link}")
    if not added and not removed:
        print("No new articles detected (feed matches existing page).")
    print()

    # 5. Generate blog HTML
    print("Generating blog/index.html...")
    blog_html = build_blog_html(articles)
    if not dry_run:
        os.makedirs(os.path.dirname(BLOG_HTML), exist_ok=True)
        with open(BLOG_HTML, "w", encoding="utf-8") as f:
            f.write(blog_html)
        print(f"  Written ({len(blog_html)} bytes)")
    else:
        print(f"  [dry-run] Would write {len(blog_html)} bytes")

    # 6. Update sitemap.xml
    print("Updating sitemap.xml...")
    if not dry_run:
        changed = update_sitemap(SITEMAP_XML, today)
        if changed:
            print(f"  Updated lastmod to {today}")
        else:
            print(f"  Already up to date ({today})")
    else:
        print(f"  [dry-run] Would update lastmod to {today}")

    # 7. Ping IndexNow
    print("Pinging IndexNow...")
    if not dry_run:
        ping_indexnow(BLOG_URL, INDEXNOW_KEY)
    else:
        print("  [dry-run] Would ping IndexNow")

    # Summary
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  Total articles: {len(articles)}")
    print(f"  New articles:   {len(added)}")
    print(f"  Removed:        {len(removed)}")
    print(f"  blog/index.html: {'updated' if not dry_run else 'dry-run'}")
    print(f"  sitemap.xml:     {'updated' if not dry_run else 'dry-run'}")
    print("Done.")


if __name__ == "__main__":
    main()
