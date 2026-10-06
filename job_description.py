"""Best-effort extraction of a short, scannable highlight from a job
posting's full description -- a "Requirements"/"Qualifications"-style
bullet list when the posting has one, or a short blurb otherwise.

Runs on the HTML (or RSS item text, which is also HTML) that each scraper
source already downloads, so this adds no extra network requests. It's a
heuristic over arbitrary third-party markup, not a real HTML parser.
Ported from the iOS app's JobDescriptionParser, so both apps pick out the
same highlights for the same posting.
"""
import html as html_lib
import re
from dataclasses import dataclass

# Heading text (case-insensitive substring match) that introduces a
# requirements-style section.
_HEADING_KEYWORDS = [
    "requirement", "qualification", "what you'll need", "what you will need",
    "what you bring", "what we're looking for", "what we are looking for",
    "who you are", "about you", "you have", "you'll have", "you will have",
    "must have", "skills", "experience",
]

_BULLET_PREFIXES = ("•", "-", "*", "·")
# Tags that end the current line of text.
_LINE_BREAK_TAGS = {
    "li", "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr",
}
# Tags whose text marks a line as a heading.
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"}
# A tag's name runs from just after "<" (or "</") up to whitespace, "/" or ">".
_TAG_RE = re.compile(r"<(/?)([^\s/>]*)[^>]*>")

MAX_REQUIREMENTS = 5
REQUIREMENT_LENGTH = 120
SUMMARY_LENGTH = 280


@dataclass
class _Line:
    text: str
    is_heading: bool = False
    is_bullet: bool = False


def highlights(
    html: "str | None", excerpt: "str | None" = None
) -> "tuple[list[str] | None, str | None]":
    """Returns (requirements, summary) -- at most one of the two is set.

    `html` is the posting's full description, as HTML (most sources) or
    plain text (a few RSS feeds strip their own markup already). `excerpt`
    is a short plain-text summary the source provides on its own, used as
    the fallback instead of guessing one from `html`.
    """
    if not html:
        return None, _fallback_summary(excerpt, [])
    lines = _lines(html)
    requirements = _requirement_bullets(lines)
    if requirements:
        return requirements, None
    return None, _fallback_summary(excerpt, lines)


# ---------- finding the requirements section ----------

def _requirement_bullets(lines: list[_Line]) -> "list[str] | None":
    """The first heading-like line matching _HEADING_KEYWORDS, followed by
    up to 5 bullets. Stops at the next heading, or at the first non-bullet
    line once at least one bullet has been collected."""
    heading_index = next(
        (i for i, line in enumerate(lines) if _is_requirements_heading(line)), None
    )
    if heading_index is None:
        return None
    bullets: list[str] = []
    for line in lines[heading_index + 1:]:
        if line.is_heading:
            break
        if line.is_bullet:
            bullets.append(line.text)
            if len(bullets) == MAX_REQUIREMENTS:
                break
        elif bullets:
            break  # the list has ended
        # else: an intervening non-bullet, non-heading line (e.g. a one-line
        # blurb) before the list actually starts -- skip it.
    if not bullets:
        return None
    return [_truncated(bullet, REQUIREMENT_LENGTH) for bullet in bullets]


def _is_requirements_heading(line: _Line) -> bool:
    lowered = line.text.lower()
    if not (line.is_heading or (len(lowered) <= 60 and lowered.endswith(":"))):
        return False
    return any(keyword in lowered for keyword in _HEADING_KEYWORDS)


# ---------- fallback summary ----------

def _fallback_summary(excerpt: "str | None", lines: list[_Line]) -> "str | None":
    if excerpt:
        trimmed = html_lib.unescape(excerpt).strip()
        if trimmed:
            return _truncated(trimmed, SUMMARY_LENGTH)
    joined = ""
    for line in lines:
        if line.is_heading or line.is_bullet:
            continue
        # Skip short, nav-like fragments (e.g. a lone "Remote" tag line).
        if len(line.text) < 20:
            continue
        joined += (" " if joined else "") + line.text
        if len(joined) >= SUMMARY_LENGTH:
            break
    return _truncated(joined, SUMMARY_LENGTH) if joined else None


# ---------- HTML/text to lines ----------

def _lines(html: str) -> list[_Line]:
    """Flattens `html` into text lines, tagging which ones are headings
    (wrapped in <h1-6>, <strong>, or <b>) or bullets (<li>, or a line
    starting with a bullet character in already-plain-text input)."""
    if "<" not in html:
        # A few RSS feeds hand us already-stripped plain text -- though it
        # can still carry character references like "&#8217;" (Jobspresso).
        result = []
        for raw_line in html_lib.unescape(html).splitlines():
            trimmed = raw_line.strip()
            if not trimmed:
                continue
            if trimmed.startswith(_BULLET_PREFIXES):
                result.append(_Line(trimmed[1:].strip(), is_bullet=True))
            else:
                result.append(_Line(trimmed))
        return result

    result: list[_Line] = []
    buffer = ""
    heading_depth = 0
    is_current_bullet = False
    saw_heading_text = False

    def flush():
        nonlocal buffer, is_current_bullet, saw_heading_text
        collapsed = " ".join(buffer.split())
        buffer = ""
        if collapsed:
            result.append(
                _Line(collapsed, is_heading=saw_heading_text, is_bullet=is_current_bullet)
            )
        saw_heading_text = False
        is_current_bullet = False

    position = 0
    for match in _TAG_RE.finditer(html):
        text_part = html[position:match.start()]
        position = match.end()
        if text_part:
            buffer += html_lib.unescape(text_part)
            if heading_depth > 0:
                saw_heading_text = True

        is_closing = match.group(1) == "/"
        name = match.group(2).lower()
        if not name:
            continue
        if name in _LINE_BREAK_TAGS:
            flush()
            if name == "li":
                is_current_bullet = not is_closing
        if name in _HEADING_TAGS:
            heading_depth = max(0, heading_depth - 1) if is_closing else heading_depth + 1

    # Anything after the last tag -- including an unterminated "<" -- is text.
    buffer += html_lib.unescape(html[position:])
    flush()
    return result


def _truncated(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    last_space = head.rfind(" ")
    return (head[:last_space] if last_space != -1 else head) + "…"
