import re
import time
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from job_description import highlights

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

REMOTE_WORDS = ["remote", "work from home", "wfh", "fully remote"]
HYBRID_WORDS = ["hybrid"]
ONSITE_WORDS = ["on-site", "onsite", "on site", "in office", "in-office"]

# Class/id name fragments that commonly wrap a single job posting (fallback strategy)
JOB_HINTS = ["job", "listing", "posting", "position", "vacancy", "career"]

# URL path patterns that usually point at a single job's detail page
JOB_LINK_RE = re.compile(
    r"/(job|jobs|career|careers|position|positions|opening|openings|"
    r"vacancy|vacancies)/[^/]+/?(?:$|\?)",
    re.IGNORECASE,
)

# Company profile / employer page link patterns
COMPANY_LINK_RE = re.compile(r"/(company|companies|employer|employers)/[^/]+/?", re.I)

# Text that marks the "act on this posting" control inside a job card
CTA_MARKERS = ["view job", "apply now", "apply", "read more", "see details", "view details"]

# Text to ignore when guessing at a location line
IGNORE_LINE_WORDS = {"save", "share", "view job", "apply", "apply now", "read more"}


def _repair_mojibake(text: str) -> str:
    """Some sources' feeds have already mangled their own UTF-8 text by the
    time it reaches us -- misreading it as Latin-1 upstream and re-encoding
    that, which turns e.g. "á" into "Ã¡". Reversing the same misreading
    (treat each character as a Latin-1 byte, then decode those bytes as
    UTF-8) restores the original. Correctly-encoded text almost never
    survives that round-trip as valid UTF-8, so it's left unchanged rather
    than corrupted."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return text


@dataclass
class JobPosting:
    title: str
    company: str
    location: str
    work_type: str  # "Remote" | "Hybrid" | "On-site" | "Unknown"
    url: str
    # Full-time/part-time/contract, when the source lists it.
    employment_type: "str | None" = None
    # Free-text pay range, when the source lists it.
    salary_range: "str | None" = None
    # Up to 5 bullet points pulled from under a "Requirements"/"Qualifications"-
    # style heading in the full listing, when one could be found (see
    # job_description.highlights).
    requirements: "list[str] | None" = None
    # Short plain-text blurb about the role, shown in place of `requirements`
    # when no such heading was found.
    summary: "str | None" = None

    def __post_init__(self):
        # Repair before truncating, so a cut can't land partway through a
        # garbled multi-byte sequence and leave it unrepairable.
        self.title = _repair_mojibake(self.title or "")[:140]
        self.company = _repair_mojibake(self.company or "")[:80]
        self.location = _repair_mojibake(self.location or "")[:80]
        self.employment_type = _optional_text(self.employment_type, 40)
        self.salary_range = _optional_text(self.salary_range, 60)
        self.summary = _optional_text(self.summary, 280)
        if self.requirements:
            repaired = [_optional_text(r, 120) for r in self.requirements]
            self.requirements = [r for r in repaired if r][:5] or None
        else:
            self.requirements = None


def _optional_text(text: "str | None", limit: int) -> "str | None":
    """Repaired and truncated `text`, or None when there's nothing to show."""
    if not text or not str(text).strip():
        return None
    return _repair_mojibake(str(text).strip())[:limit]


def _classify_work_type(text: str) -> str:
    t = text.lower()
    if any(w in t for w in REMOTE_WORDS):
        return "Remote"
    if any(w in t for w in HYBRID_WORDS):
        return "Hybrid"
    if any(w in t for w in ONSITE_WORDS):
        return "On-site"
    return "Unknown"


def _normalize(href: str, base_url: str) -> str:
    return urljoin(base_url, href)


def _job_hrefs_within(tag) -> set:
    return {
        _normalize(a["href"], "")
        for a in tag.find_all("a", href=True)
        if JOB_LINK_RE.search(a["href"])
    }


def _find_card(anchor, own_href: str):
    """Climb from a title anchor to the smallest ancestor that looks like
    a single job card (contains a CTA marker, and doesn't also contain a
    *different* job's detail link)."""
    block = anchor
    best = anchor.parent or anchor
    for _ in range(8):
        parent = block.parent
        if parent is None:
            break
        block = parent
        hrefs_inside = _job_hrefs_within(block)
        if len(hrefs_inside) > 1:
            break  # this level already spans more than one job card
        best = block
        if any(m in block.get_text(" ", strip=True).lower() for m in CTA_MARKERS):
            break
    return best


def _extract_from_card(card, title: str, href: str) -> JobPosting:
    company = ""
    for company_link in card.find_all("a", href=COMPANY_LINK_RE):
        text = company_link.get_text(strip=True)
        if text:
            company = text
            break

    location = ""
    lines = [ln.strip() for ln in card.get_text("\n", strip=True).split("\n") if ln.strip()]
    for i, line in enumerate(lines):
        if title[: min(20, len(title))] in line and i + 1 < len(lines):
            for candidate in lines[i + 1 : i + 4]:
                low = candidate.lower()
                if low in IGNORE_LINE_WORDS or low == company.lower():
                    continue
                if candidate.startswith("$"):
                    continue
                if len(candidate) <= 60:
                    location = candidate
                break
            break

    work_type = _classify_work_type(card.get_text(" ", strip=True))
    return JobPosting(
        title=title, company=company, location=location,
        work_type=work_type, url=href,
    )


# Paginated HTML boards (VibeCode Careers) rate-limit page requests to
# about 30 a minute, answering anything past that with a 429 (and no
# Retry-After header) until the minute rolls over. Pacing requests evenly
# would be slower than fetching ~30 pages at full speed and then waiting
# out the block, so the crawl does the latter: on a 429 it re-checks every
# _RATE_LIMIT_BACKOFF seconds and resumes from the same page, rather than
# treating the block as the end of the list.
_RATE_LIMIT_BACKOFF = 15
_MAX_RATE_LIMIT_RETRIES = 8  # ~2 minutes of waiting before giving up on a page
# Safety cap so a site with a pagination loop can't keep us fetching forever.
_MAX_HTML_PAGES = 500


def _get_page(session: requests.Session, url: str, timeout: int) -> str:
    """GET `url`, waiting out and retrying 429 responses."""
    for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
        resp = session.get(url, timeout=timeout)
        if resp.status_code != 429 or attempt == _MAX_RATE_LIMIT_RETRIES:
            break
        retry_after = resp.headers.get("Retry-After", "")
        wait = float(retry_after) if retry_after.isdigit() else _RATE_LIMIT_BACKOFF
        time.sleep(min(max(wait, 1), 60))
    resp.raise_for_status()
    return resp.text


def _parse_page_jobs(html: str, url: str) -> tuple[list[JobPosting], BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")

    # --- primary strategy: job-detail-link based ---
    best_anchor_for_href: dict[str, object] = {}
    for a in soup.find_all("a", href=True):
        if not JOB_LINK_RE.search(a["href"]):
            continue
        href = _normalize(a["href"], url)
        text = a.get_text(strip=True)
        if not text:
            continue
        prev = best_anchor_for_href.get(href)
        if prev is None or len(text) > len(prev.get_text(strip=True)):
            best_anchor_for_href[href] = a

    postings: list[JobPosting] = []
    for href, anchor in best_anchor_for_href.items():
        title = anchor.get_text(strip=True)
        if len(title) < 3 or len(title) > 140:
            continue
        card = _find_card(anchor, href)
        postings.append(_extract_from_card(card, title, href))

    if postings:
        return postings, soup

    # --- fallback strategy: class/id name heuristics ---
    candidates = soup.find_all(["li", "div", "article"], class_=True)
    candidates += soup.find_all(["li", "div", "article"], id=True)
    seen_text = set()
    for tag in candidates:
        attrs = " ".join(
            [tag.get("class") and " ".join(tag.get("class")) or "", tag.get("id") or ""]
        ).lower()
        if not any(hint in attrs for hint in JOB_HINTS):
            continue
        link = tag.find("a", href=True)
        if not link:
            continue
        title = link.get_text(strip=True)
        if not title or len(title) < 3 or len(title) > 140:
            continue
        full_text = tag.get_text(" ", strip=True)
        if full_text in seen_text:
            continue
        seen_text.add(full_text)
        href = _normalize(link["href"], url)
        postings.append(
            JobPosting(
                title=title, company="", location="",
                work_type=_classify_work_type(full_text), url=href,
            )
        )

    return postings, soup


def _highlight_fields(description: "str | None", excerpt: "str | None" = None) -> dict:
    """JobPosting keyword args for a posting's requirements/summary, pulled
    from its full description (see job_description.highlights)."""
    requirements, summary = highlights(description, excerpt)
    return {"requirements": requirements, "summary": summary}


def _fetch_remoteok_jobs(url: str, timeout: int) -> list[JobPosting]:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    postings = []
    for entry in resp.json():
        title = entry.get("position", "")
        job_url = entry.get("url") or entry.get("apply_url") or ""
        if not title or not job_url:
            continue  # skips the leading API-terms notice entry
        location = entry.get("location") or "Remote"
        work_type = _classify_work_type(f"{location} {' '.join(entry.get('tags', []))}")
        postings.append(
            JobPosting(
                title=title,
                company=entry.get("company", ""),
                location=location,
                work_type="Remote" if work_type == "Unknown" else work_type,
                url=job_url,
                **_highlight_fields(entry.get("description")),
            )
        )
    return postings


def _fetch_remotive_jobs(url: str, timeout: int) -> list[JobPosting]:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    postings = []
    for entry in resp.json().get("jobs", []):
        title = entry.get("title", "")
        job_url = entry.get("url", "")
        if not title or not job_url:
            continue
        postings.append(
            JobPosting(
                title=title,
                company=entry.get("company_name", ""),
                location=(entry.get("candidate_required_location") or "Remote"),
                work_type="Remote",
                url=job_url,
                # e.g. "full_time" -> "Full-Time"
                employment_type=(entry.get("job_type") or "").replace("_", "-").title(),
                salary_range=entry.get("salary"),
                **_highlight_fields(entry.get("description")),
            )
        )
    return postings


def _fetch_wwr_rss_jobs(url: str, timeout: int) -> list[JobPosting]:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.content)
    postings = []
    for item in root.findall(".//item"):
        raw_title = (item.findtext("title") or "").strip()
        company, sep, job_title = raw_title.partition(":")
        company, job_title = (company.strip(), job_title.strip()) if sep else ("", raw_title)
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        region = (item.findtext("region") or "").strip()
        if not job_title or not link:
            continue
        work_type = _classify_work_type(region)
        postings.append(
            JobPosting(
                title=job_title,
                company=company,
                location=region,
                work_type="Remote" if work_type == "Unknown" else work_type,
                url=link,
                **_highlight_fields(item.findtext("description")),
            )
        )
    return postings


_JOBSPRESSO_NS = "{https://jobspresso.co}"


def _fetch_jobspresso_rss_jobs(url: str, timeout: int) -> list[JobPosting]:
    """Jobspresso's feed carries company/location as their own custom
    `job_listing:*` tags, rather than a combined "Company: Title" line the
    way We Work Remotely's does."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.content)
    postings = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        if not title or not link:
            continue
        location = (item.findtext(f"{_JOBSPRESSO_NS}location") or "").strip()
        work_type = _classify_work_type(location)
        postings.append(
            JobPosting(
                title=title,
                company=(item.findtext(f"{_JOBSPRESSO_NS}company") or "").strip(),
                location=location,
                # Jobspresso only lists remote roles, so an unrecognized location never means on-site.
                work_type="Remote" if work_type == "Unknown" else work_type,
                url=link,
                # Despite the name, the feed's job_category tag holds the
                # employment type ("Full Time"); job_type holds the role area.
                employment_type=item.findtext(f"{_JOBSPRESSO_NS}job_category"),
                **_highlight_fields(item.findtext("description")),
            )
        )
    return postings


def _fetch_himalayas_jobs(url: str, timeout: int) -> list[JobPosting]:
    """Himalayas caps this at 20 results per request regardless of the limit
    requested; a single page is plenty anyway, since the full feed runs into
    the tens of thousands of listings."""
    resp = requests.get(url, headers=HEADERS, params={"limit": 100}, timeout=timeout)
    resp.raise_for_status()
    now = time.time()
    postings = []
    for entry in resp.json().get("jobs", []):
        # `guid` is the listing's own page on himalayas.app; `applicationLink`
        # often points off-site instead, so guid is the more stable link.
        title = entry.get("title") or ""
        job_url = entry.get("guid") or ""
        if not title or not job_url:
            continue
        # Himalayas is the one source that says when a listing expires, so
        # skip anything already past it that the feed just hasn't pruned yet.
        expiry = entry.get("expiryDate")
        if isinstance(expiry, (int, float)) and expiry < now:
            continue
        location = next((loc for loc in entry.get("locationRestrictions") or [] if loc), "Remote")
        work_type = _classify_work_type(location)
        postings.append(
            JobPosting(
                title=title,
                company=(entry.get("companyName") or ""),
                location=location,
                work_type="Remote" if work_type == "Unknown" else work_type,
                url=job_url,
                **_highlight_fields(entry.get("description"), entry.get("excerpt")),
            )
        )
    return postings


def _jobicy_salary(entry: dict) -> "str | None":
    """e.g. "90000-120000 USD", when Jobicy lists a pay range."""
    try:
        low, high = float(entry["salaryMin"]), float(entry["salaryMax"])
    except (KeyError, TypeError, ValueError):
        return None
    if high <= 0:
        return None
    return f"{int(low)}-{int(high)} {entry.get('salaryCurrency') or ''}".strip()


def _fetch_jobicy_jobs(url: str, timeout: int) -> list[JobPosting]:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    postings = []
    for entry in resp.json().get("jobs", []):
        title = entry.get("jobTitle") or ""
        job_url = entry.get("url") or ""
        if not title or not job_url:
            continue
        location = entry.get("jobGeo") or "Remote"
        work_type = _classify_work_type(location)
        postings.append(
            JobPosting(
                title=title,
                company=(entry.get("companyName") or ""),
                location=location,
                # Jobicy only lists remote roles, so an unrecognized location never means on-site.
                work_type="Remote" if work_type == "Unknown" else work_type,
                url=job_url,
                employment_type=next(iter(entry.get("jobType") or []), None),
                salary_range=_jobicy_salary(entry),
                **_highlight_fields(entry.get("jobDescription"), entry.get("jobExcerpt")),
            )
        )
    return postings


def _fetch_working_nomads_jobs(url: str, timeout: int) -> list[JobPosting]:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    postings = []
    for entry in resp.json():
        title = entry.get("title") or ""
        job_url = entry.get("url") or ""
        if not title or not job_url:
            continue
        location = entry.get("location") or "Remote"
        work_type = _classify_work_type(f"{location} {entry.get('tags') or ''}")
        postings.append(
            JobPosting(
                title=title,
                company=(entry.get("company_name") or ""),
                location=location,
                work_type="Remote" if work_type == "Unknown" else work_type,
                url=job_url,
                **_highlight_fields(entry.get("description")),
            )
        )
    return postings


def _fetch_arbeitnow_jobs(url: str, timeout: int) -> list[JobPosting]:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    postings = []
    for entry in resp.json().get("data", []):
        title = entry.get("title") or ""
        job_url = entry.get("url") or ""
        if not title or not job_url:
            continue
        location = entry.get("location") or "Remote"
        postings.append(
            JobPosting(
                title=title,
                company=(entry.get("company_name") or ""),
                location=location,
                work_type="Remote" if entry.get("remote") else _classify_work_type(location),
                url=job_url,
                employment_type=next(iter(entry.get("job_types") or []), None),
                **_highlight_fields(entry.get("description")),
            )
        )
    return postings


def _fetch_the_muse_jobs(url: str, timeout: int) -> list[JobPosting]:
    """Unlike the other JSON sources, The Muse is a general job board
    (on-site roles included, not remote-only), so an unrecognized location
    isn't nudged towards "Remote". Only the first page is fetched -- the
    catalog runs into the hundreds of thousands of listings."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    postings = []
    for entry in resp.json().get("results", []):
        title = entry.get("name") or ""
        job_url = (entry.get("refs") or {}).get("landing_page") or ""
        if not title or not job_url:
            continue
        locations = entry.get("locations") or []
        location = (locations[0].get("name") if locations else "") or "Remote"
        postings.append(
            JobPosting(
                title=title,
                company=((entry.get("company") or {}).get("name") or ""),
                location=location,
                work_type=_classify_work_type(location),
                url=job_url,
                **_highlight_fields(entry.get("contents")),
            )
        )
    return postings


def _find_next_page_url(soup: BeautifulSoup, current_url: str) -> str | None:
    link = soup.find("a", rel="next")
    if link and link.get("href"):
        return _normalize(link["href"], current_url)
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True).lower() == "next":
            return _normalize(a["href"], current_url)
    return None


# Sources with their own single-call API/feed instead of paginated HTML.
_SINGLE_CALL_FETCHERS = {
    "remoteok.com": _fetch_remoteok_jobs,
    "remotive.com": _fetch_remotive_jobs,
    "weworkremotely.com": _fetch_wwr_rss_jobs,
    "jobspresso.co": _fetch_jobspresso_rss_jobs,
    "himalayas.app": _fetch_himalayas_jobs,
    "jobicy.com": _fetch_jobicy_jobs,
    "workingnomads.com": _fetch_working_nomads_jobs,
    "arbeitnow.com": _fetch_arbeitnow_jobs,
    "themuse.com": _fetch_the_muse_jobs,
}


def _dedupe(postings: list[JobPosting]) -> list[JobPosting]:
    seen_urls: set = set()
    deduped = []
    for job in postings:
        if job.url not in seen_urls:
            seen_urls.add(job.url)
            deduped.append(job)
    return deduped


def fetch_jobs(
    url: str, timeout: int = 15, max_pages: "int | None" = 1
) -> list[JobPosting]:
    """
    Fetch `url` (and optionally follow "next page" links up to max_pages
    pages total) and return a best-effort, de-duplicated list of
    JobPosting objects. Pass max_pages=None to follow "next page" links
    until none remain (fetch every page). Sources with a dedicated
    single-call API/feed (see _SINGLE_CALL_FETCHERS) ignore max_pages,
    since they return their full result set in one request. Raises
    requests.RequestException on network/HTTP failure of the first page.
    """
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for domain, fetcher in _SINGLE_CALL_FETCHERS.items():
        if host == domain or host.endswith(f".{domain}"):
            return _dedupe(fetcher(url, timeout))

    all_postings: list[JobPosting] = []
    seen_urls: set = set()
    visited_pages: set = set()
    next_url = url
    pages_fetched = 0
    page_limit = _MAX_HTML_PAGES if max_pages is None else min(max_pages, _MAX_HTML_PAGES)
    # One session for the whole crawl, so every page reuses the same
    # connection instead of paying for a new TLS handshake each time.
    session = requests.Session()
    session.headers.update(HEADERS)

    while next_url and next_url not in visited_pages and pages_fetched < page_limit:
        visited_pages.add(next_url)
        try:
            html = _get_page(session, next_url, timeout)
        except requests.RequestException:
            if pages_fetched == 0:
                raise
            break
        postings, soup = _parse_page_jobs(html, next_url)

        for job in postings:
            if job.url not in seen_urls:
                seen_urls.add(job.url)
                all_postings.append(job)

        pages_fetched += 1
        next_url = _find_next_page_url(soup, next_url) if pages_fetched < page_limit else None

    return all_postings


if __name__ == "__main__":
    import sys

    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    for job in fetch_jobs(test_url):
        print(job)
