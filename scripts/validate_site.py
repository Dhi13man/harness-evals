#!/usr/bin/env python3
"""Validate the deterministic GitHub Pages artifact without network access."""

from __future__ import annotations

import json
import struct
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse


ORIGIN = "https://dhi13man.github.io/skivolve/"
LEGACY_MARKERS = (
    "harness" + "-evals",
    "harness" + "_evals",
    "Harness" + " Evals",
)


class SiteValidationError(ValueError):
    """Raised when the static site violates its deploy contract."""


@dataclass
class Page:
    path: Path
    title: str = ""
    metas: dict[str, str] = field(default_factory=dict)
    canonicals: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    hrefs: list[str] = field(default_factory=list)
    images: list[dict[str, str]] = field(default_factory=list)
    stylesheets: list[str] = field(default_factory=list)
    heading_levels: list[int] = field(default_factory=list)
    json_ld: list[object] = field(default_factory=list)
    html_lang: str | None = None
    main_count: int = 0
    main_focusable: bool = False
    nav_count: int = 0
    inline_style_count: int = 0
    disallowed_scripts: int = 0


class _PageParser(HTMLParser):
    def __init__(self, path: Path) -> None:
        super().__init__(convert_charrefs=True)
        self.page = Page(path=path)
        self._title = False
        self._title_parts: list[str] = []
        self._json_ld = False
        self._json_ld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if "style" in values:
            self.page.inline_style_count += 1
        if identifier := values.get("id"):
            self.page.ids.append(identifier)
        if tag == "html":
            self.page.html_lang = values.get("lang")
        elif tag == "title":
            self._title = True
        elif tag == "meta":
            key = values.get("name") or values.get("property")
            if key:
                self.page.metas[key] = values.get("content", "")
        elif tag == "link":
            relations = set(values.get("rel", "").split())
            if "canonical" in relations:
                self.page.canonicals.append(values.get("href", ""))
            if "stylesheet" in relations:
                self.page.stylesheets.append(values.get("href", ""))
        elif tag == "a":
            if "href" in values:
                self.page.hrefs.append(values["href"])
        elif tag == "img":
            self.page.images.append(values)
        elif tag == "main":
            self.page.main_count += 1
            self.page.main_focusable = values.get("tabindex") == "-1"
        elif tag == "nav":
            self.page.nav_count += 1
        elif len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self.page.heading_levels.append(int(tag[1]))
        elif tag == "script":
            if values.get("type") == "application/ld+json":
                self._json_ld = True
                self._json_ld_parts = []
            else:
                self.page.disallowed_scripts += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._title = False
            self.page.title = "".join(self._title_parts).strip()
        elif tag == "script" and self._json_ld:
            raw = "".join(self._json_ld_parts).strip()
            try:
                self.page.json_ld.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise SiteValidationError(
                    f"{self.page.path}: invalid JSON-LD: {exc}"
                ) from exc
            self._json_ld = False
            self._json_ld_parts = []

    def handle_data(self, data: str) -> None:
        if self._title:
            self._title_parts.append(data)
        if self._json_ld:
            self._json_ld_parts.append(data)


def _fail(path: Path, message: str) -> None:
    raise SiteValidationError(f"{path}: {message}")


def _expected_canonical(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    if relative == Path("index.html"):
        return ORIGIN
    return f"{ORIGIN}{relative.parent.as_posix()}/"


def _resolve_local(root: Path, page_path: Path, target: str) -> tuple[Path | None, str]:
    parsed = urlparse(target)
    fragment = unquote(parsed.fragment)
    if parsed.scheme or parsed.netloc:
        if target.startswith(ORIGIN):
            local = unquote(target.removeprefix(ORIGIN).split("#", 1)[0])
            candidate = root / local
        else:
            return None, fragment
    elif parsed.path.startswith("/skivolve/"):
        candidate = root / unquote(parsed.path.removeprefix("/skivolve/"))
    elif parsed.path.startswith("/"):
        return None, fragment
    else:
        candidate = page_path.parent / unquote(parsed.path)

    if parsed.path in {"", "."}:
        candidate = page_path
    if candidate.is_dir() or target.endswith("/"):
        candidate /= "index.html"
    return candidate.resolve(strict=False), fragment


def _parse_page(path: Path) -> Page:
    parser = _PageParser(path)
    try:
        parser.feed(path.read_text(encoding="utf-8"))
        parser.close()
    except UnicodeDecodeError as exc:
        _fail(path, f"HTML is not UTF-8: {exc}")
    return parser.page


def _validate_page(root: Path, page: Page, pages: dict[Path, Page]) -> None:
    path = page.path
    relative = path.relative_to(root)
    is_error_page = relative == Path("404.html")
    if page.html_lang != "en":
        _fail(path, "html lang must be en")
    if not page.title or "Skivolve" not in page.title:
        _fail(path, "title must be descriptive and include Skivolve")
    description = page.metas.get("description", "")
    if not 50 <= len(description) <= 180:
        _fail(path, "meta description must contain 50 to 180 characters")
    if page.main_count != 1 or not page.main_focusable:
        _fail(path, "exactly one focusable main landmark is required")
    if page.nav_count < 1:
        _fail(path, "at least one navigation landmark is required")
    if page.inline_style_count:
        _fail(path, "inline style attributes are forbidden")
    if page.disallowed_scripts:
        _fail(path, "runtime scripts are forbidden; only JSON-LD is allowed")
    if len(page.ids) != len(set(page.ids)):
        _fail(path, "duplicate element id")
    if page.hrefs[:1] != ["#main-content"]:
        _fail(path, "skip link must be the first link")
    if page.heading_levels.count(1) != 1 or page.heading_levels[:1] != [1]:
        _fail(path, "exactly one h1 must be the first heading")
    for previous, current in zip(page.heading_levels, page.heading_levels[1:]):
        if current > previous + 1:
            _fail(path, f"heading level jumps from h{previous} to h{current}")
    for image in page.images:
        if "alt" not in image:
            _fail(path, f"image lacks alt text: {image.get('src', '<unknown>')}")
        source, _ = _resolve_local(root, path, image.get("src", ""))
        if source is not None and not source.is_file():
            _fail(path, f"missing image: {image.get('src', '')}")
    for stylesheet in page.stylesheets:
        source, _ = _resolve_local(root, path, stylesheet)
        if not source.is_file():
            _fail(path, f"missing stylesheet: {stylesheet}")

    if is_error_page:
        if page.metas.get("robots") != "noindex, follow":
            _fail(path, "404 page must be noindex, follow")
    else:
        expected = _expected_canonical(root, path)
        if page.canonicals != [expected]:
            _fail(path, f"canonical must be exactly {expected}")
        required_meta = {
            "og:title",
            "og:description",
            "og:url",
            "og:image",
            "og:image:alt",
            "twitter:card",
            "twitter:title",
            "twitter:description",
            "twitter:image",
            "twitter:image:alt",
        }
        missing = sorted(required_meta - page.metas.keys())
        if missing:
            _fail(path, f"missing social metadata: {', '.join(missing)}")
        if page.metas["og:url"] != expected:
            _fail(path, "Open Graph URL differs from canonical")
        if page.metas["og:image"] != f"{ORIGIN}assets/og-image.png":
            _fail(path, "Open Graph image differs from canonical social card")
        if not page.json_ld:
            _fail(path, "at least one JSON-LD object is required")

    root_resolved = root.resolve()
    for href in page.hrefs:
        target, fragment = _resolve_local(root, path, href)
        if target is None:
            continue
        if not target.is_relative_to(root_resolved):
            _fail(path, f"local link escapes site root: {href}")
        if not target.is_file():
            _fail(path, f"broken local link: {href}")
        if fragment:
            target_page = pages.get(target)
            if target_page is None or fragment not in target_page.ids:
                _fail(path, f"broken fragment link: {href}")


def _validate_png(path: Path) -> None:
    raw = path.read_bytes()
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        _fail(path, "social image is not a PNG")
    width, height = struct.unpack(">II", raw[16:24])
    if (width, height) != (1200, 630):
        _fail(path, f"social image must be 1200x630, got {width}x{height}")
    if len(raw) >= 500_000:
        _fail(path, "social image must be smaller than 500 KB")


def _validate_discovery(root: Path, pages: dict[Path, Page]) -> None:
    robots = (root / "robots.txt").read_text(encoding="utf-8")
    if "User-agent: *" not in robots or f"Sitemap: {ORIGIN}sitemap.xml" not in robots:
        _fail(root / "robots.txt", "robots policy or sitemap pointer is missing")
    sitemap_path = root / "sitemap.xml"
    try:
        sitemap = ET.parse(sitemap_path)
    except ET.ParseError as exc:
        _fail(sitemap_path, f"invalid XML: {exc}")
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = {element.text for element in sitemap.findall("s:url/s:loc", namespace)}
    expected = {
        _expected_canonical(root, path)
        for path in pages
        if path.relative_to(root) != Path("404.html")
    }
    if locations != expected:
        _fail(sitemap_path, "sitemap URLs differ from canonical HTML pages")
    manifest = json.loads((root / "site.webmanifest").read_text(encoding="utf-8"))
    if manifest.get("start_url") != "/skivolve/" or manifest.get("name") != "Skivolve":
        _fail(root / "site.webmanifest", "manifest identity or start URL is invalid")
    _validate_png(root / "assets" / "og-image.png")


def validate_site(root: Path) -> None:
    root = root.resolve()
    if not root.is_dir():
        raise SiteValidationError(f"site root does not exist: {root}")
    html_paths = sorted(root.rglob("*.html"))
    if len(html_paths) < 6:
        _fail(root, "expected a homepage, 404 page, and at least four crawlable guides")
    pages = {path.resolve(): _parse_page(path.resolve()) for path in html_paths}
    titles: set[str] = set()
    for page in pages.values():
        if page.title in titles:
            _fail(page.path, f"duplicate page title: {page.title}")
        titles.add(page.title)
        _validate_page(root, page, pages)
    _validate_discovery(root, pages)
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".html", ".css", ".xml", ".txt", ".json"}:
            text = path.read_text(encoding="utf-8")
            marker = next((item for item in LEGACY_MARKERS if item in text), None)
            if marker:
                _fail(path, f"legacy identity remains in published site: {marker}")


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("site")
    try:
        validate_site(root)
    except (OSError, SiteValidationError, json.JSONDecodeError) as exc:
        print(f"site validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"site validation passed: {root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
