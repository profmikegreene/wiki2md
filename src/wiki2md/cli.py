#!/usr/bin/env python3
"""
wiki2md – Fetch a MediaWiki page via API, convert to Markdown, and save.

Usage:
  wiki2md --title "Python (programming language)"
  wiki2md --url "https://en.wikipedia.org/wiki/Python_(programming_language)"
  wiki2md --title "Home" --api "https://your.wiki.example.org/w/api.php" -o home.md
"""

import argparse
import html
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests

# Optional imports (graceful fallbacks)
try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:
    BeautifulSoup = None  # type: ignore

try:
    from markdownify import markdownify as md_convert  # type: ignore
except Exception:
    md_convert = None

try:
    import html2text  # type: ignore
except Exception:
    html2text = None


def derive_api_and_title_from_url(page_url: str):
    """
    Return a list of candidate API endpoints and the page title for a MediaWiki URL.
    Tries both `/w/api.php` (Wikipedia, many MW installs) and `/api.php` (Fandom & others).
    """
    parsed = urlparse(page_url)

    # Extract title
    title = None
    if "/wiki/" in parsed.path:
        title = parsed.path.split("/wiki/", 1)[1]
    else:
        m = re.search(r"(?:^|&)title=([^&]+)", parsed.query or "")
        if m:
            title = m.group(1)
    if not title:
        raise ValueError("Could not infer page title from URL. Pass --title explicitly.")

    title = unquote(title).replace("_", " ")

    base = f"{parsed.scheme}://{parsed.netloc}"
    api_candidates = [
        f"{base}/w/api.php",  # Wikipedia & many MW installs
        f"{base}/api.php",    # Fandom & others
    ]
    return api_candidates, title


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "page"


def ensure_md_suffix(name: str) -> str:
    # If user provided any extension, keep it; otherwise add .md
    p = Path(name)
    return name if p.suffix else f"{name}.md"


def fetch_page_html(api_endpoint: str, title: str, timeout: int = 30, lang: str | None = None) -> dict:
    """Fetch HTML via MediaWiki API action=parse."""
    params = {
        "action": "parse",
        "prop": "text|displaytitle",
        "format": "json",
        "redirects": 1,
        "page": title,
    }
    if lang:
        params["uselang"] = lang

    r = requests.get(api_endpoint, params=params, timeout=timeout)
    # Sanity check: ensure JSON
    ct = r.headers.get("Content-Type", "")
    if "json" not in ct.lower():
        raise RuntimeError(
            f"Unexpected content type from API ({ct}); endpoint may be wrong: {api_endpoint}"
        )
    r.raise_for_status()
    data = r.json()

    if "error" in data:
        raise RuntimeError(f"MediaWiki API error: {data['error'].get('info', data['error'])}")

    parse = data.get("parse", {})
    display_title = html.unescape(parse.get("displaytitle", title))
    text_blob = parse.get("text", {})
    html_content = text_blob.get("*", "") if isinstance(text_blob, dict) else text_blob
    if not html_content:
        raise RuntimeError("No HTML content returned by the API.")
    return {"html": html_content, "title": display_title}


def clean_html(html_content: str) -> str:
    """Remove edit links, reference anchors, and TOC where possible."""
    if not BeautifulSoup:
        return html_content
    soup = BeautifulSoup(html_content, "html.parser")

    # Remove common cruft
    for sel in (".mw-editsection", "sup.reference", "span.mw-cite-backlink", "#toc"):
        for node in soup.select(sel):
            node.decompose()

    for sup in soup.find_all("sup"):
        if not sup.get_text(strip=True):
            sup.decompose()

    return str(soup)


def html_to_markdown(html_content: str) -> str:
    """Convert HTML to Markdown using markdownify (preferred) or html2text (fallback)."""
    # Prefer markdownify; avoid passing both strip & convert (version differences)
    if md_convert:
        try:
            return md_convert(
                html_content,
                heading_style="ATX",
                strip=["script", "style"],
            ).strip()
        except Exception:
            # Fallback: minimal call for maximum compatibility
            try:
                return md_convert(html_content).strip()
            except Exception:
                pass  # fall through to html2text/plain

    # Fallback to html2text if available
    if html2text:
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = False
        h.body_width = 0
        return h.handle(html_content).strip()

    # Very basic fallback
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", html_content, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def postprocess_markdown(md: str, fix_fandom_images: bool) -> str:
    """
    Optionally trim Fandom/Wikia image URLs to the base filename and drop
    any trailing resize paths, query strings, and titles.
    """
    if not fix_fandom_images:
        return md

    pattern = re.compile(
        r'(\!\[[^\]]*\]\('
        r'https?://static\.wikia\.nocookie\.net/'
        r'[^\s)"]+?\.(?:png|jpe?g|gif|webp))'  # up to real file extension
        r'[^)]*\)'                               # everything after that until ')'
    )
    return pattern.sub(r'\1)', md)


def resolve_output_path(
    out: str | None,
    title: str,
    outdir: str | None,
    filename: str | None = None
) -> Path:
    """
    Decide the output path based on args:
      - If --output is given, use it (can be a file or directory).
      - Else, if --filename is given, use that (append .md only if no extension).
      - Else, derive a sanitized name from the title (with .md).
    """
    if out:
        p = Path(out).expanduser().resolve()
        if p.is_dir():
            base = sanitize_filename(filename or title)
            return p / ensure_md_suffix(base)
        # out is an explicit file path; honor whatever extension the user gave
        return p

    if filename:
        base = filename  # accept whatever the user passed; don't sanitize
        root = Path(outdir).expanduser().resolve() if outdir else Path.cwd()
        return root / ensure_md_suffix(base)

    # Default: title-derived, sanitized, with .md
    base = sanitize_filename(title)
    root = Path(outdir).expanduser().resolve() if outdir else Path.cwd()
    return root / f"{base}.md"


def try_fetch_any(api_list, title, **kw):
    """Try each API endpoint until one succeeds; return (endpoint_used, page_dict)."""
    last_err = None
    for endpoint in api_list:
        try:
            page = fetch_page_html(endpoint, title, **kw)
            return endpoint, page
        except Exception as e:
            last_err = e
    # If all failed, raise the last error
    raise last_err


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(
        prog="wiki2md",
        description="Fetch a MediaWiki page via the API, convert to Markdown, and save to a file."
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--title", help="Page title, e.g., 'Python (programming language)'.")
    src.add_argument("--url", help="Full page URL, e.g., 'https://en.wikipedia.org/wiki/Python_(programming_language)'.")
    ap.add_argument("--api", help="MediaWiki API endpoint (overrides auto-detection).")
    ap.add_argument("-o", "--output", help="Output Markdown file path (or directory).")
    ap.add_argument("--outdir", help="If set, write output into this directory (filename derived from title).")
    ap.add_argument("-f", "--filename", help="Filename to use (may include an extension). If missing an extension, .md is appended.")
    ap.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds (default: 30).")
    ap.add_argument("--lang", help="Optional uselang passed to the API (e.g., 'en', 'es').")
    ap.add_argument("--no-clean", action="store_true", help="Skip HTML cleanup before conversion.")
    ap.add_argument("--fix-fandom-images", action="store_true", help="Trim Fandom/Wikia image URLs to the base filename and drop titles.")
    ap.add_argument("-q", "--quiet", action="store_true", help="Suppress non-error logs.")
    args = ap.parse_args(argv)

    try:
        # Determine endpoint(s) and title
        if args.url:
            api_candidates, title = derive_api_and_title_from_url(args.url)
        else:
            title = args.title
            api_candidates = [args.api or "https://en.wikipedia.org/w/api.php"]

        # If user supplied --api explicitly, use only that
        if args.api:
            api_candidates = [args.api]

        # Fetch using first working endpoint
        endpoint_used, page = try_fetch_any(
            api_candidates, title, timeout=args.timeout, lang=args.lang
        )

        html_raw = page["html"]
        html_clean = html_raw if args.no_clean else clean_html(html_raw)
        md = html_to_markdown(html_clean)

        # Optional post-processing (Fandom image URL trimming)
        md = postprocess_markdown(md, fix_fandom_images=args.fix_fandom_images)

        out_path = resolve_output_path(args.output, page["title"], args.outdir, args.filename)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            f.write(f"# {page['title']}\n\n")
            f.write(md.rstrip() + "\n")

        if not args.quiet:
            print(f"Saved: {out_path} (via {endpoint_used})")

    except KeyboardInterrupt:
        if not args.quiet:
            print("Aborted.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
