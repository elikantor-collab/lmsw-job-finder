"""
LMSW Remote NY Job Finder — v2
------------------------------
Same purpose as v1: search the web daily for remote LMSW (Licensed Master
Social Worker) postings in NY, email the new ones, dedupe with SQLite.

What changed in v2 (May 2026):
  * Broader search queries — catches "Social Worker", "Therapist", and
    "Behavioral Health" roles whose descriptions require LMSW.
  * Strict LMSW post-filter preserved — so the inbox stays clean.
  * SerpAPI Google Jobs paginated (2 pages per query) for more coverage.
  * SerpAPI location filter relaxed from "New York, US" to "United States"
    so national remote roles that accept NY-licensed workers come through.
  * Indeed RSS removed (deprecated by Indeed in early 2026); replaced by
    a direct HTML scraper of indeed.com/jobs search pages.
  * Same .env, same DB, same launchd plist — drop-in replacement.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import smtplib
import sqlite3
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone as dt_timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass


# ----------------------------- Configuration ------------------------------- #

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "sent_jobs.db")
LOG_PATH = os.path.join(SCRIPT_DIR, "job_finder.log")

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", GMAIL_USER).strip()
SEND_EMPTY_DIGEST = os.getenv("SEND_EMPTY_DIGEST", "false").lower() == "true"

# Only run at the scheduled local hour. GitHub Actions cron only understands
# UTC, so the workflow fires two cron triggers a day (one for each side of
# US Daylight Saving Time) and this gate makes sure only the "correct" one
# for the current time of year actually does the work — the other exits
# immediately without hitting SerpAPI/LinkedIn/Indeed. Manual "Run workflow"
# clicks set SKIP_TIME_GATE=true so they always run immediately.
TARGET_HOUR_LOCAL = int(os.getenv("TARGET_HOUR_LOCAL", "21"))  # 9pm
LOCAL_TZ = os.getenv("TIMEZONE", "America/New_York")
SKIP_TIME_GATE = os.getenv("SKIP_TIME_GATE", "false").lower() == "true"

# Broader queries — these catch roles titled "Social Worker", "Therapist",
# "Behavioral Health Clinician", etc. whose descriptions require LMSW.
# The strict post-filter (KW_LMSW) keeps non-LMSW roles out of the digest.
SEARCH_QUERIES = [
    "LMSW remote New York",
    "Licensed Master Social Worker remote New York",
    "LMSW telehealth",
    "remote social worker New York",
    "remote therapist LMSW",
    "telehealth therapist New York MSW",
    "remote behavioral health clinician New York LMSW",
]

KW_LMSW = [
    "lmsw",
    "licensed master social worker",
    "master of social work",
    "msw, lmsw",
    "msw/lmsw",
]
KW_REMOTE = ["remote", "work from home", "wfh", "telehealth", "virtual", "telework"]
KW_NY = ["new york", " ny", "nyc", "new-york"]


# ------------------------------ Logging ------------------------------------ #

def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ------------------------------- Database ---------------------------------- #

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_jobs (
            job_id     TEXT PRIMARY KEY,
            title      TEXT,
            company    TEXT,
            link       TEXT,
            source     TEXT,
            sent_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn


def make_job_id(title: str, company: str, link: str) -> str:
    clean_link = (link or "").split("?")[0].rstrip("/")
    # Indeed jobs share the same posting under many tracking URLs but
    # the `jk=...` parameter is stable. Use it as the canonical key.
    jk = re.search(r"[?&]jk=([a-f0-9]{8,32})", link or "")
    if jk:
        clean_link = f"indeed:{jk.group(1)}"
    key = f"{(title or '').lower().strip()}|{(company or '').lower().strip()}|{clean_link.lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def already_sent(conn: sqlite3.Connection, job_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM sent_jobs WHERE job_id = ?", (job_id,))
    return cur.fetchone() is not None


def mark_sent(conn: sqlite3.Connection, job: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sent_jobs (job_id, title, company, link, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (job["job_id"], job["title"], job["company"], job["link"], job["source"]),
    )
    conn.commit()


# -------------------------------- Sources ---------------------------------- #

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.5 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def search_serpapi_google_jobs(query: str, max_pages: int = 2) -> list[dict]:
    """Google Jobs via SerpAPI. Aggregates Indeed, LinkedIn, ZipRecruiter,
    idealist, niche boards. Now paginates up to `max_pages` for coverage."""
    if not SERPAPI_KEY:
        return []

    jobs: list[dict] = []
    next_page_token = None
    for page_index in range(max_pages):
        params = {
            "engine": "google_jobs",
            "q": query,
            # Relaxed: country-wide instead of New York-only.
            # National remote roles that accept NY licensure now surface.
            "location": "United States",
            "api_key": SERPAPI_KEY,
            "ltype": "1",  # remote-only
            "hl": "en",
        }
        if next_page_token:
            params["next_page_token"] = next_page_token

        try:
            r = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
        except requests.RequestException as e:
            log(f"  SerpAPI request error (page {page_index + 1}): {e}")
            break

        if r.status_code != 200:
            log(f"  SerpAPI HTTP {r.status_code} (page {page_index + 1}): {r.text[:200]}")
            break

        try:
            data = r.json()
        except ValueError as e:
            log(f"  SerpAPI JSON error: {e}")
            break

        for j in data.get("jobs_results", []) or []:
            ext = j.get("detected_extensions", {}) or {}
            apply_options = j.get("apply_options", []) or []
            link = ""
            source_name = "Google Jobs"
            if apply_options:
                link = apply_options[0].get("link", "") or ""
                source_name = apply_options[0].get("title", "Google Jobs") or "Google Jobs"
            link = link or j.get("share_link", "") or j.get("link", "")
            jobs.append({
                "title": j.get("title", "") or "",
                "company": j.get("company_name", "") or "",
                "location": j.get("location", "") or "",
                "link": link,
                "source": source_name,
                "description": (j.get("description", "") or "")[:1200],
                "remote_flag": bool(ext.get("work_from_home")),
            })

        # Pagination token. SerpAPI may surface this under different keys
        # depending on response shape; try common variants.
        pagination = data.get("serpapi_pagination") or data.get("pagination") or {}
        next_page_token = (
            pagination.get("next_page_token")
            or data.get("next_page_token")
        )
        if not next_page_token:
            break

    return jobs


def search_indeed_html(query: str) -> list[dict]:
    """Direct scrape of Indeed's HTML search page. Replaces v1's RSS source
    (Indeed deprecated public RSS in early 2026)."""
    jobs: list[dict] = []
    encoded_q = quote_plus(query)
    # Two passes per query: one in NY explicitly, one Remote-only.
    urls = [
        f"https://www.indeed.com/jobs?q={encoded_q}&l=Remote&sort=date&fromage=14",
        f"https://www.indeed.com/jobs?q={encoded_q}&l=New+York%2C+NY&sort=date&fromage=14",
    ]

    for url in urls:
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        except requests.RequestException as e:
            log(f"  Indeed HTML error: {e}")
            continue

        if r.status_code != 200:
            log(f"  Indeed HTML HTTP {r.status_code}")
            continue

        page = r.text

        # Indeed renders job cards inside <a class="...tapItem..."> elements
        # that carry data-jk (job key) and contain a <span title="..."> for
        # the job title plus a <span class="companyName"> for the employer.
        # Their markup churns; we extract whatever we can match.
        # First: collect all unique jk values present in the HTML.
        jk_values = list(dict.fromkeys(re.findall(r'data-jk="([a-f0-9]{12,32})"', page)))
        if not jk_values:
            # Fallback: jk also appears in href URLs.
            jk_values = list(dict.fromkeys(re.findall(r'/rc/clk\?jk=([a-f0-9]{12,32})', page)))

        for jk in jk_values:
            # Extract the title (best-effort regex around the data-jk).
            title = ""
            company = ""
            location = ""
            # Find the chunk of HTML following the data-jk attribute.
            window = re.search(
                rf'data-jk="{re.escape(jk)}"(.{{0,4000}}?)</a>',
                page, flags=re.DOTALL,
            )
            chunk = window.group(1) if window else ""

            t = re.search(r'<span[^>]*title="([^"]+)"', chunk)
            if t:
                title = html.unescape(t.group(1)).strip()
            else:
                # Sometimes title is in an h2 with the role text.
                t = re.search(r'<h2[^>]*>[^<]*<a[^>]*>([^<]+)</a>', chunk)
                if t:
                    title = html.unescape(t.group(1)).strip()

            c = re.search(r'companyName[^>]*>([^<]+)<', chunk)
            if c:
                company = html.unescape(c.group(1)).strip()

            l = re.search(r'companyLocation[^>]*>([^<]+)<', chunk)
            if l:
                location = html.unescape(l.group(1)).strip()

            jobs.append({
                "title": title or f"Indeed posting {jk[:8]}",
                "company": company,
                "location": location or ("Remote" if "Remote" in url else "New York, NY"),
                "link": f"https://www.indeed.com/viewjob?jk={jk}",
                "source": "Indeed",
                "description": "",
                "remote_flag": "Remote" in url or "remote" in (location + title).lower(),
            })

    return jobs


def search_linkedin(query: str) -> list[dict]:
    """LinkedIn public guest job search. Anti-bot may rate-limit; fail soft."""
    jobs: list[dict] = []
    url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={quote_plus(query)}"
        "&location=New%20York%2C%20United%20States"
        "&f_WT=2&start=0"
    )
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    except requests.RequestException as e:
        log(f"  LinkedIn error: {e}")
        return []
    if r.status_code != 200:
        log(f"  LinkedIn HTTP {r.status_code}")
        return []

    blocks = re.findall(r"<li[^>]*>(.*?)</li>", r.text, flags=re.DOTALL)
    for block in blocks:
        title_m = re.search(
            r'class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)<',
            block, flags=re.DOTALL,
        )
        company_m = re.search(
            r'class="[^"]*base-search-card__subtitle[^"]*".*?>([^<]+)<',
            block, flags=re.DOTALL,
        )
        location_m = re.search(
            r'class="[^"]*job-search-card__location[^"]*">([^<]+)<',
            block, flags=re.DOTALL,
        )
        link_m = re.search(
            r'href="(https://www\.linkedin\.com/jobs/view/[^"?]+)',
            block,
        )
        if not (title_m and link_m):
            continue
        jobs.append({
            "title": html.unescape(title_m.group(1)).strip(),
            "company": html.unescape(company_m.group(1)).strip() if company_m else "",
            "location": html.unescape(location_m.group(1)).strip() if location_m else "Remote",
            "link": link_m.group(1).strip(),
            "source": "LinkedIn",
            "description": "",
            "remote_flag": True,
        })
    return jobs


def search_idealist(query: str) -> list[dict]:
    """Idealist.org — nonprofit-focused, often has LMSW roles."""
    jobs: list[dict] = []
    url = (
        "https://www.idealist.org/api/v1/search/listings"
        f"?q={quote_plus(query)}&type=JOB&locationType=REMOTE&sort=recent&perPage=25"
    )
    try:
        r = requests.get(
            url,
            headers={**BROWSER_HEADERS, "Accept": "application/json"},
            timeout=30,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        for hit in data.get("hits", []) or []:
            slug = hit.get("url") or hit.get("slug") or ""
            link = slug if slug.startswith("http") else f"https://www.idealist.org{slug}"
            jobs.append({
                "title": hit.get("name", "") or hit.get("title", ""),
                "company": (hit.get("orgName") or hit.get("organizationName") or ""),
                "location": "Remote",
                "link": link,
                "source": "Idealist",
                "description": (hit.get("description") or "")[:600],
                "remote_flag": True,
            })
    except (requests.RequestException, ValueError):
        pass
    return jobs


# ------------------------------- Filtering --------------------------------- #

def matches_criteria(job: dict) -> bool:
    haystack = " ".join([
        (job.get("title") or "").lower(),
        (job.get("company") or "").lower(),
        (job.get("location") or "").lower(),
        (job.get("description") or "").lower(),
    ])

    has_lmsw = any(k in haystack for k in KW_LMSW)
    if not has_lmsw:
        return False

    is_remote = job.get("remote_flag") or any(k in haystack for k in KW_REMOTE)
    if not is_remote:
        return False

    is_ny = any(k in haystack for k in KW_NY)
    if not (is_ny or "remote" in (job.get("location") or "").lower()):
        return False

    return True


# --------------------------------- Email ----------------------------------- #

def build_email_html(jobs: list[dict]) -> str:
    if not jobs:
        return (
            "<p style='font-family:system-ui,sans-serif;color:#444'>"
            "No new LMSW remote NY job postings today.</p>"
        )

    rows = []
    for j in jobs:
        title = html.escape(j["title"] or "Untitled role")
        company = html.escape(j["company"] or "")
        location = html.escape(j["location"] or "")
        source = html.escape(j["source"] or "")
        link = j["link"]
        rows.append(
            "<li style='margin:0 0 14px 0;padding:0;'>"
            f"<a href='{link}' style='font-size:15px;font-weight:600;color:#1559cf;text-decoration:none'>{title}</a>"
            f"{(' &mdash; ' + company) if company else ''}"
            f"<div style='font-size:12px;color:#666;margin-top:2px'>"
            f"{location}{' · ' if location and source else ''}{source}"
            "</div></li>"
        )

    return f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:640px">
  <h2 style="margin:0 0 4px 0;font-size:18px">
    {len(jobs)} new LMSW remote NY job posting{'s' if len(jobs) != 1 else ''}
  </h2>
  <p style="margin:0 0 16px 0;color:#666;font-size:13px">
    {datetime.now().strftime('%A, %B %d, %Y')}
  </p>
  <ul style="list-style:none;padding:0;margin:0">{''.join(rows)}</ul>
  <p style="margin-top:24px;color:#999;font-size:11px">
    Sent automatically by your LMSW Job Finder. Already-emailed listings are
    suppressed; only new postings appear here.
  </p>
</div>
""".strip()


def send_email(jobs: list[dict]) -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        log("Skipping email: GMAIL_USER or GMAIL_APP_PASSWORD not set.")
        return

    subject_count = f"{len(jobs)} new" if jobs else "no new"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"LMSW Job Digest — {subject_count} posting"
        f"{'s' if len(jobs) != 1 else ''} ({datetime.now().strftime('%b %d')})"
    )
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(build_email_html(jobs), "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)
    log(f"Email sent to {RECIPIENT_EMAIL} with {len(jobs)} jobs.")


# ---------------------------------- Main ----------------------------------- #

def main() -> int:
    log("=" * 60)
    log("LMSW Job Finder v2 run starting")

    now_local = datetime.now(ZoneInfo(LOCAL_TZ))
    if not SKIP_TIME_GATE and not (TARGET_HOUR_LOCAL <= now_local.hour <= TARGET_HOUR_LOCAL + 2):
        log(f"Local time is {now_local.strftime('%H:%M %Z')}, outside the target "
            f"window ({TARGET_HOUR_LOCAL}:00–{TARGET_HOUR_LOCAL + 2}:59) — skipping "
            f"this run (expected for one of the two daily cron triggers, or if "
            f"GitHub delayed the scheduled trigger).")
        return 0

    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        log("ERROR: GMAIL_USER and GMAIL_APP_PASSWORD must be set (as .env locally, "
            "or as repo secrets in GitHub Actions)")
        return 1

    conn = init_db()

    raw_jobs: list[dict] = []
    for q in SEARCH_QUERIES:
        log(f"Searching: {q!r}")
        before = len(raw_jobs)
        raw_jobs.extend(search_serpapi_google_jobs(q, max_pages=2))
        raw_jobs.extend(search_indeed_html(q))
        raw_jobs.extend(search_linkedin(q))
        raw_jobs.extend(search_idealist(q))
        log(f"  +{len(raw_jobs) - before} raw results")

    log(f"Total raw results: {len(raw_jobs)}")

    seen: set[str] = set()
    new_jobs: list[dict] = []
    for j in raw_jobs:
        if not j.get("link"):
            continue
        if not matches_criteria(j):
            continue
        jid = make_job_id(j["title"], j["company"], j["link"])
        if jid in seen:
            continue
        seen.add(jid)
        if already_sent(conn, jid):
            continue
        j["job_id"] = jid
        new_jobs.append(j)

    log(f"After filter + dedup: {len(new_jobs)} new postings")

    if new_jobs or SEND_EMPTY_DIGEST:
        try:
            send_email(new_jobs)
        except smtplib.SMTPException as e:
            log(f"ERROR sending email: {e}")
            conn.close()
            return 2
        for j in new_jobs:
            mark_sent(conn, j)
    else:
        log("No new jobs and SEND_EMPTY_DIGEST is false — skipping email.")

    conn.close()
    log("Run complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
