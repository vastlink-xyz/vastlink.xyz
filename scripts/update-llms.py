#!/usr/bin/env python3
"""
Regenerate the AI-facing files from the blog pages on disk:
  - llms.txt       (rewrites the "## Blog articles" section, keeps everything else)
  - llms-full.txt  (llms.txt intro + the full text of every article as Markdown)

Articles are discovered from blog/{slug}/index.html, so both Medium-generated
and hand-written articles are included.

Usage:
    python3 scripts/update-llms.py              # run from anywhere
    python3 scripts/update-llms.py --dry-run    # preview without writing files
"""

import glob
import html
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SITE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOG_DIR = os.path.join(SITE_ROOT, "blog")
LLMS_TXT = os.path.join(SITE_ROOT, "llms.txt")
LLMS_FULL_TXT = os.path.join(SITE_ROOT, "llms-full.txt")
SITE_URL = "https://vastlink.xyz"

ARTICLES_HEADING = "## Blog articles"

# Better one-line summaries for articles whose meta description is a
# truncated copy of the Medium body text.
SUMMARY_OVERRIDES = {
    "bybit-hack-crypto-security": "lessons from the Safe{Wallet} and Bybit $1.4B hack, and the risk of concentrating assets in a single wallet.",
    "decentralized-mpc-future-infrastructure": "explains MPC, threshold signature schemes (TSS) and decentralized MPC networks, and why they are the future of wallet infrastructure.",
    "digital-asset-management-landscape-2025": "research on the convergence of TradFi and DeFi in digital asset management.",
    "fundamental-problem-crypto-wallets": "why key management is hard, and how MPC, TSS and account abstraction let users keep control without managing keys.",
    "vastbase-faq": "what Vastbase is, the problems it solves, why it is built with Lit Protocol and how it works.",
}

# ---------------------------------------------------------------------------
# HTML -> Markdown
# ---------------------------------------------------------------------------

class MarkdownConverter(HTMLParser):
    """Minimal HTML-to-Markdown converter for the article prose markup."""

    BLOCKS = {"p", "div", "figure", "figcaption", "section", "blockquote", "pre", "table"}

    def __init__(self, base_url: str, heading_offset: int = 0):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.heading_offset = heading_offset
        self.out = []
        self.lists = []          # stack of ["ul"|"ol", counter]
        self.link_stack = []
        self.in_pre = False
        self.quote_starts = []   # indexes into self.out where each <blockquote> began
        self.table = None        # list of rows, each a list of cells
        self.cell = None

    # -- helpers -----------------------------------------------------------
    def _write(self, text: str):
        if self.cell is not None:
            self.cell.append(text)
        else:
            self.out.append(text)

    def _block_break(self):
        self.out.append("\n\n")

    def _close_emphasis(self, marker: str):
        # "**text **" is not valid Markdown emphasis: move trailing spaces outside
        target = self.cell if self.cell is not None else self.out
        trailing = ""
        while target and target[-1].endswith(" ") and target[-1].strip(" ") != "":
            stripped = target[-1].rstrip(" ")
            trailing = target[-1][len(stripped):]
            target[-1] = stripped
            break
        target.append(marker + trailing)

    # -- parser callbacks --------------------------------------------------
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if re.fullmatch(r"h[1-6]", tag):
            level = min(6, int(tag[1]) + self.heading_offset)
            self._block_break()
            self._write("#" * level + " ")
        elif tag in self.BLOCKS:
            self._block_break()
            if tag == "blockquote":
                self.quote_starts.append(len(self.out))
            if tag == "pre":
                self.in_pre = True
                self._write("```\n")
            if tag == "table":
                self.table = []
        elif tag == "br":
            self._write("  \n")
        elif tag in ("strong", "b"):
            self._write("**")
        elif tag in ("em", "i"):
            self._write("*")
        elif tag == "code" and not self.in_pre:
            self._write("`")
        elif tag == "a":
            href = attrs.get("href", "")
            self.link_stack.append(urljoin(self.base_url, href) if href else "")
            if href:
                self._write("[")
        elif tag == "img":
            alt = (attrs.get("alt") or "").strip()
            src = attrs.get("src", "")
            if src:
                self._write(f"![{alt}]({urljoin(self.base_url, src)})")
        elif tag in ("ul", "ol"):
            self._block_break()
            self.lists.append([tag, 0])
        elif tag == "li":
            indent = "  " * (len(self.lists) - 1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                marker = f"{self.lists[-1][1]}."
            else:
                marker = "-"
            self.out.append(f"\n{indent}{marker} ")
        elif tag == "tr" and self.table is not None:
            self.table.append([])
        elif tag in ("td", "th") and self.table is not None:
            self.cell = []
        elif tag == "hr":
            self._block_break()
            self._write("---")

    def handle_endtag(self, tag):
        if re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCKS:
            if tag == "pre":
                self.in_pre = False
                self._write("\n```")
            if tag == "blockquote" and self.quote_starts:
                start = self.quote_starts.pop()
                quoted = "".join(self.out[start:]).strip()
                quoted = "\n".join("> " + line if line.strip() else ">" for line in quoted.split("\n"))
                self.out[start:] = ["\n\n", quoted]
            if tag == "table" and self.table is not None:
                self.out.append(self._render_table(self.table))
                self.table = None
            self._block_break()
        elif tag in ("strong", "b"):
            self._close_emphasis("**")
        elif tag in ("em", "i"):
            self._close_emphasis("*")
        elif tag == "code" and not self.in_pre:
            self._write("`")
        elif tag == "a" and self.link_stack:
            href = self.link_stack.pop()
            if href:
                self._write(f"]({href})")
        elif tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self._block_break()
        elif tag in ("td", "th") and self.cell is not None and self.table:
            self.table[-1].append(re.sub(r"\s+", " ", "".join(self.cell)).strip().replace("|", "\\|"))
            self.cell = None

    def handle_data(self, data):
        if self.table is not None and self.cell is None:
            return  # whitespace between table tags
        if not self.in_pre:
            data = re.sub(r"\s+", " ", data)
        self._write(data)

    @staticmethod
    def _render_table(rows: list) -> str:
        rows = [r for r in rows if r]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines)

    def markdown(self) -> str:
        blocks = []
        for block in re.split(r"\n\s*\n", "".join(self.out)):
            block = block.strip("\n")
            if block.strip():
                # Keep list indentation, drop stray leading spaces elsewhere
                blocks.append(block if re.match(r"\s*(-|\d+\.) ", block) else block.strip(" "))
        md = "\n\n".join(blocks)
        return re.sub(r"\*\*\*\*", "", md).strip() + "\n"


def html_to_markdown(fragment: str, base_url: str, heading_offset: int = 0) -> str:
    parser = MarkdownConverter(base_url, heading_offset)
    parser.feed(fragment)
    parser.close()
    return parser.markdown()


# ---------------------------------------------------------------------------
# Article discovery
# ---------------------------------------------------------------------------

def _meta(pattern: str, source: str) -> str:
    m = re.search(pattern, source, re.S)
    return html.unescape(m.group(1)).strip() if m else ""


def strip_leading_title(markdown: str, title: str) -> str:
    """Medium posts often repeat the title as their first heading; drop it."""
    first, _, rest = markdown.partition("\n\n")
    if first.startswith("#") and first.lstrip("# ").strip() == title:
        return rest
    return markdown


def load_articles() -> list:
    """Read every blog/{slug}/index.html and return article dicts, newest first."""
    articles = []
    for page in sorted(glob.glob(os.path.join(BLOG_DIR, "*", "index.html"))):
        slug = os.path.basename(os.path.dirname(page))
        with open(page, "r", encoding="utf-8") as f:
            source = f.read()

        title = re.sub(r"\s+", " ", _meta(r"<h1[^>]*>(.*?)</h1>", source))
        title = re.sub(r"<[^>]+>", "", title)
        date = _meta(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})', source)
        description = _meta(r'<meta name="description" content="([^"]*)"', source)
        body = re.search(
            r'<article[^>]*>\s*<div class="prose[^"]*">(.*?)</div>\s*</article>', source, re.S
        )
        if not title or not body:
            print(f"  [skip] blog/{slug}/: no title or article body found")
            continue

        url = f"{SITE_URL}/blog/{slug}/"
        articles.append({
            "slug": slug,
            "title": title,
            "date": date,
            "url": url,
            "summary": SUMMARY_OVERRIDES.get(slug) or description,
            "markdown": strip_leading_title(html_to_markdown(body.group(1), url, heading_offset=1), title),
        })

    articles.sort(key=lambda a: a["date"], reverse=True)
    return articles


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_articles_section(articles: list) -> str:
    lines = [ARTICLES_HEADING, ""]
    for a in articles:
        summary = a["summary"].rstrip()
        date = f" ({a['date']})" if a["date"] else ""
        lines.append(f"- [{a['title']}]({a['url']}): {summary}{date}")
    return "\n".join(lines) + "\n"


def update_llms_txt(content: str, articles: list) -> str:
    """Replace the "## Blog articles" section (up to the next H2) with a fresh list."""
    section = build_articles_section(articles)
    pattern = re.compile(rf"^{re.escape(ARTICLES_HEADING)}\n.*?(?=^## |\Z)", re.S | re.M)
    if pattern.search(content):
        return pattern.sub(lambda _: section + "\n", content, count=1)
    # No section yet: insert it before "## Optional", or append
    if "\n## Optional" in content:
        return content.replace("\n## Optional", "\n" + section + "\n## Optional", 1)
    return content.rstrip() + "\n\n" + section


def build_llms_full(llms_txt: str, articles: list) -> str:
    """llms.txt intro (everything before the first H2) followed by every article."""
    intro = llms_txt.split("\n## ", 1)[0].rstrip()
    parts = [
        intro,
        "",
        f"This file contains the full text of every article on the Vastlink blog. "
        f"The index of the site is at {SITE_URL}/llms.txt.",
    ]
    for a in articles:
        parts += [
            "",
            "---",
            "",
            f"## {a['title']}",
            "",
            f"Source: {a['url']}  ",
            f"Published: {a['date']}",
            "",
            a["markdown"].rstrip(),
        ]
    return "\n".join(parts) + "\n"


def write_if_changed(path: str, content: str, dry_run: bool):
    name = os.path.relpath(path, SITE_ROOT)
    old = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            old = f.read()
    if old == content:
        print(f"  {name}: already up to date")
    elif dry_run:
        print(f"  [dry-run] Would write {name} ({len(content)} bytes)")
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  {name}: written ({len(content)} bytes)")


def main():
    dry_run = "--dry-run" in sys.argv

    if not os.path.exists(LLMS_TXT):
        print("  [error] llms.txt not found at the site root")
        sys.exit(1)

    articles = load_articles()
    print(f"Found {len(articles)} articles")

    with open(LLMS_TXT, "r", encoding="utf-8") as f:
        llms_txt = update_llms_txt(f.read(), articles)

    write_if_changed(LLMS_TXT, llms_txt, dry_run)
    write_if_changed(LLMS_FULL_TXT, build_llms_full(llms_txt, articles), dry_run)


if __name__ == "__main__":
    main()
