import os
import time
import traceback
import requests

from job_agent import (
    clean, normalize, keep, tg, existing_jobs, is_duplicate, add_to_notion,
)

# (slug, ats_type) - confirmed via search. Add more over time.
WATCHLIST = [
    ("meesho", "lever"),
    ("fampay", "lever"),
    ("mindtickle", "lever"),
    ("stable-money1", "lever"),
    ("Sprinto", "lever"),
    ("paytmpayments", "lever"),
    ("prophecysimpledatalabs", "greenhouse"),
    ("startree", "greenhouse"),
]

ATS_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{}/postings",
}


def fetch_greenhouse(slug):
    r = requests.get(ATS_URLS["greenhouse"].format(slug), timeout=15)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "title": clean(j.get("title")),
            "location": clean((j.get("location") or {}).get("name")),
            "url": clean(j.get("absolute_url")),
        })
    return out


def fetch_lever(slug):
    r = requests.get(ATS_URLS["lever"].format(slug), timeout=15)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json():
        out.append({
            "title": clean(j.get("text")),
            "location": clean((j.get("categories") or {}).get("location")),
            "url": clean(j.get("hostedUrl")),
        })
    return out


def fetch_ashby(slug):
    r = requests.get(ATS_URLS["ashby"].format(slug), timeout=15)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "title": clean(j.get("title")),
            "location": clean(j.get("location")),
            "url": clean(j.get("jobUrl")),
        })
    return out


def fetch_smartrecruiters(slug):
    r = requests.get(ATS_URLS["smartrecruiters"].format(slug), timeout=15)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location") or {}
        loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
        out.append({
            "title": clean(j.get("name")),
            "location": clean(loc_str),
            "url": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
        })
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
}


def run():
    seen_jobs = existing_jobs()
    seen_urls = {j["url"] for j in seen_jobs if j["url"]}
    seen_keys_this_run = set()

    found, added = {}, 0

    for slug, ats_type in WATCHLIST:
        fetch = FETCHERS.get(ats_type)
        if not fetch:
            continue
        try:
            jobs = fetch(slug)
        except Exception as e:
            print(f"ATS fetch failed [{slug}, {ats_type}]: {e}")
            continue

        for j in jobs:
            url, title, location = j["url"], j["title"], j["location"]
            company = slug.replace("-", " ").replace("_", " ").title()

            if not url or not title or url in seen_urls or url in found:
                continue
            if not keep(title, location):
                continue
            if is_duplicate(company, title, seen_jobs, seen_keys_this_run):
                continue

            found[url] = {
                "job_url": url,
                "apply_url": None,
                "title": title,
                "company": company,
                "location": location,
                "date_posted": None,
                "source": "ATS",
            }
            seen_keys_this_run.add(normalize(f"{company} {title}"))

        time.sleep(1)

    lines = []
    for job in found.values():
        if add_to_notion(job):
            added += 1
            lines.append(f"• {job['title']} — {job['company']} ({job['location']})")
        time.sleep(0.4)

    if added:
        tg(f"[ATS] {added} new role(s)\n\n" + "\n".join(lines[:15]))
    else:
        tg("[ATS] Ran fine. 0 new roles.")
    print(f"ATS added {added}")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        err = traceback.format_exc()[-800:]
        tg(f"ATS AGENT FAILED\n\n{err}")
        raise
