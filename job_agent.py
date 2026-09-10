import os
import re
import time
import traceback
import requests
import pandas as pd
from jobspy import scrape_jobs
from rapidfuzz import fuzz

# ---------- config ----------
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DB_ID"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEARCH_TERMS = [
    "associate product manager",
    "founder's office",
    "executive assistant founder's office",
    "product associate",
    "product analyst",
]

SITES = ["linkedin", "indeed", "naukri"]

HOURS_OLD = 6
RESULTS_WANTED = 40

# cities you'd actually take, including spelling variants
CITY_KEYWORDS = [
    "pune", "bangalore", "bengaluru", "hyderabad",
    "mumbai", "navi mumbai", "thane",
    "delhi", "new delhi", "noida", "gurgaon", "gurugram", "ncr",
    "ahmedabad", "chennai", "kochi", "cochin",
    "remote", "anywhere", "work from home",
]

TITLE_ALLOW = [
    r"associate\s+product\s+manager",
    r"\bapm\b",
    r"(junior|jr\.?|trainee|graduate|entry.level)\s+product\s+manager",
    r"product\s+associate",
    r"product\s+analyst",
    r"product\s+owner",
    r"founder'?s?\s*office",
    r"founding\s+(associate|member|team)",
    r"chief\s+of\s+staff",
    r"(executive|personal)\s+assistant",
    r"\bea\s+to\b",
]

TITLE_BLOCK = [
    r"\bsenior\b", r"\bsr\.?\b", r"\blead\b", r"\bstaff\b", r"\bprincipal\b",
    r"\bdirector\b", r"\bhead\s+of\b", r"\bvp\b", r"vice\s+president",
    r"\bmanager\s+(ii|iii|iv)\b", r"\bowner\s+(ii|iii|iv)\b",
    r"\bassoc\.?\s*dir", r"\bagm\b", r"\bgpm\b",
    r"\bintern\b", r"internship",
    r"[3-9]\s*[-+]\s*\d*\s*\+?\s*(yrs|years)",
    r"[3-9]\+\s*(yrs|years)",
]

# an EA role only counts if it smells like founder's office
EA_REQUIRED_SIGNALS = ["founder", "ceo", "chief of staff", "founding"]

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


# ---------- telegram ----------
def tg(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "disable_web_page_preview": True},
            timeout=20,
        )
    except Exception as e:
        print("telegram failed:", e)


# ---------- notion ----------
def existing_jobs():
    jobs, cursor = [], None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
            headers=NOTION_HEADERS, json=payload, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        for row in data["results"]:
            props = row["properties"]
            url = props.get("Job URL", {}).get("url")
            company = "".join(t["plain_text"] for t in props.get("Company", {}).get("title", []))
            role = "".join(t["plain_text"] for t in props.get("Role", {}).get("rich_text", []))
            jobs.append({
                "url": (url or "").strip(),
                "key": normalize(f"{company} {role}"),
            })
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return jobs


def is_duplicate(company, title, seen_jobs, seen_keys_this_run):
    key = normalize(f"{company} {title}")
    for existing in seen_jobs:
        if fuzz.ratio(key, existing["key"]) >= 87:
            return True
    for k in seen_keys_this_run:
        if fuzz.ratio(key, k) >= 87:
            return True
    return False


def add_to_notion(job):
    props = {
        "Company": {"title": [{"text": {"content": job["company"][:200]}}]},
        "Role": {"rich_text": [{"text": {"content": job["title"][:200]}}]},
        "Location": {"rich_text": [{"text": {"content": job["location"][:200]}}]},
        "Job URL": {"url": job["job_url"]},
        "Source": {"select": {"name": job["source"]}},
        "Status": {"select": {"name": "Discovered"}},
    }
    if job.get("apply_url"):
        props["Apply URL"] = {"url": job["apply_url"]}
    if job.get("date_posted"):
        props["Date Posted"] = {"date": {"start": job["date_posted"]}}

    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={"parent": {"database_id": NOTION_DB_ID}, "properties": props},
        timeout=30,
    )
    if r.status_code >= 300:
        print("notion write failed:", r.text[:300])
        return False
    return True


# ---------- filters ----------
def clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def normalize(text):
    text = text.lower()
    text = re.sub(r"\b(pvt\.?|private|ltd\.?|limited|inc\.?|llp)\b", "", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def keep(title, location):
    t, loc = title.lower(), location.lower()

    if not any(re.search(p, t) for p in TITLE_ALLOW):
        return False

    if any(re.search(p, t) for p in TITLE_BLOCK):
        return False

    if "executive assistant" in t or "personal assistant" in t or "ea to" in t:
        if not any(s in t for s in EA_REQUIRED_SIGNALS):
            return False

    if not loc:
        return False
    return any(c in loc for c in CITY_KEYWORDS)


SOURCE_MAP = {"linkedin": "LinkedIn", "indeed": "Indeed",
              "naukri": "Naukri", "glassdoor": "Glassdoor",
              "google": "Google"}


# ---------- main ----------
def run():
    seen_jobs = existing_jobs()
    seen_urls = {j["url"] for j in seen_jobs if j["url"]}
    seen_keys_this_run = set()
    print(f"{len(seen_jobs)} jobs already in Notion")

    found, added = {}, 0

    for term in SEARCH_TERMS:
        for remote in (False, True):
            try:
                df = scrape_jobs(
                    site_name=SITES,
                    search_term=term,
                    location="India",
                    country_indeed="India",
                    is_remote=remote,
                    results_wanted=RESULTS_WANTED,
                    hours_old=HOURS_OLD,
                )
            except Exception as e:
                print(f"scrape failed [{term}, remote={remote}]: {e}")
                continue

            if df is None or df.empty:
                continue

            for _, row in df.iterrows():
                url = clean(row.get("job_url"))
                title = clean(row.get("title"))
                company = clean(row.get("company"))
                location = clean(row.get("location"))

                if not url or not title or url in seen_urls or url in found:
                    continue
                if not keep(title, location):
                    continue
                if is_duplicate(company, title, seen_jobs, seen_keys_this_run):
                    continue

                found[url] = {
                    "job_url": url,
                    "apply_url": clean(row.get("job_url_direct")) or None,
                    "title": title,
                    "company": company or "Unknown",
                    "location": location,
                    "date_posted": clean(row.get("date_posted")) or None,
                    "source": SOURCE_MAP.get(clean(row.get("site")).lower(), "Manual"),
                }
                seen_keys_this_run.add(normalize(f"{company} {title}"))

            time.sleep(4)

    lines = []
    for job in found.values():
        if add_to_notion(job):
            added += 1
            lines.append(f"• {job['title']} — {job['company']} ({job['location']})")
        time.sleep(0.4)

    if added:
        tg(f"{added} new role(s)\n\n" + "\n".join(lines[:15]))
    else:
        tg("Ran fine. 0 new roles.")
    print(f"added {added}")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        err = traceback.format_exc()[-800:]
        tg(f"JOB AGENT FAILED\n\n{err}")
        raise
