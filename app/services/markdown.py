"""Server-side Markdown rendering for course content.

Course module bodies are bilingual markdown blobs authored by compliance
staff in the admin panel. We render them server-side because:
  - the trainee's CSP is `script-src 'self'` (no client-side renderer libs),
  - and we want a tight allow-list of HTML tags / attributes regardless
    of who authored the source.

`render(text)` returns a `markupsafe.Markup` so the Jinja filter call
`{{ body | markdown_safe }}` doesn't also need `| safe`.
"""

from __future__ import annotations

import bleach
import markdown as md
from markupsafe import Markup

# Tight allow-list — covers headings, lists, tables, code, links, images.
# Intentionally excludes <script>, <iframe>, <style>, raw <a target>, etc.
_ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "em",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "code",
    "pre",
    "a",
    "img",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}
_ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "img": ["src", "alt", "title"],
}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def render(text: str | None) -> Markup:
    if not text:
        return Markup("")
    raw_html = md.markdown(
        text,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html",
    )
    cleaned = bleach.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return Markup(cleaned)
