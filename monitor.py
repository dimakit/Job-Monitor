"""
Job posting monitor -- cloud-routine version.

Fetches current postings from every company with a known-working platform
(see job_monitor_config.json), filters to PM-track + NY/Remote-eligible roles,
diffs against seen_pairs.json (this repo's running memory of every posting
already emailed), emails anything new via SMTP, and commits the updated
seen_pairs.json back to this repo so the next run doesn't re-report it.

Credentials come from environment variables (set by the routine before
invoking this script) -- nothing sensitive is stored in this repo:
  SMTP_HOST, SMTP_PORT, SMTP_SENDER_EMAIL, SMTP_SENDER_PASSWORD, SMTP_RECIPIENT_EMAIL

This script intentionally does NOT assign fit scores/rationale -- it only
detects and reports. A human (or a separate reviewed pass) decides what's
actually worth pursuing from the raw digest.
"""

import json
import re
import os
import subprocess
import smtplib
import time
import urllib.request
from datetime import date, datetime
from email.mime.text import MIMEText
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
CONFIG_PATH = REPO_DIR / "job_monitor_config.json"
SEEN_PATH = REPO_DIR / "seen_pairs.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

PM_REGEX = re.compile(
    r"Product Manager|Group Product Manager|Staff Product Manager|"
    r"Principal Product Manager|Director.{0,3}Product Manag|Director of Product\b|"
    r"VP.{0,3}Product\b|Head of Product\b|Research Product Manager|Product Lead",
    re.IGNORECASE,
)
EXCLUDE_REGEX = re.compile(
    r"Productivity|Security Engineer|Designer|Product Legal|Product Marketing|"
    r"Program Manager|Product Owner",
    re.IGNORECASE,
)
LOC_REGEX = re.compile(r"New York|NYC|Remote", re.IGNORECASE)


def fetch(url, method="GET", body=None, extra_headers=None, timeout=15):
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.status, resp.headers


def qualifies(title, location_text, location_exception=False):
    if EXCLUDE_REGEX.search(title):
        return False
    if not PM_REGEX.search(title):
        return False
    if location_exception:
        return True
    return bool(LOC_REGEX.search(location_text or ""))


# ---------- generic ATS handlers ----------

def handler_greenhouse(token, location_exception):
    body, status, _ = fetch(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false")
    data = json.loads(body)
    out = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        loc = (j.get("location") or {}).get("name", "")
        if qualifies(title, loc, location_exception):
            out.append({"title": title, "location": loc, "url": j.get("absolute_url", "")})
    return out


def handler_ashby(token, location_exception):
    body, status, _ = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    data = json.loads(body)
    out = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        loc = j.get("location", "") or ""
        secondary = j.get("secondaryLocations") or []
        loc_full = loc + " " + " ".join(sl.get("location", "") for sl in secondary if isinstance(sl, dict))
        if qualifies(title, loc_full, location_exception):
            out.append({"title": title, "location": loc, "url": j.get("jobUrl", "")})
    return out


def handler_lever(token, location_exception):
    body, status, _ = fetch(f"https://api.lever.co/v0/postings/{token}?mode=json")
    data = json.loads(body)
    out = []
    for j in data:
        title = j.get("text", "")
        loc = (j.get("categories") or {}).get("location", "")
        if qualifies(title, loc, location_exception):
            out.append({"title": title, "location": loc, "url": j.get("hostedUrl", "")})
    return out


# ---------- special handlers ----------

def handler_amazon(cfg):
    out = []
    offset = 0
    seen_ids = set()
    while True:
        url = f"https://www.amazon.jobs/en/search.json?base_query=Product+Manager&result_limit=100&offset={offset}"
        body, status, _ = fetch(url)
        data = json.loads(body)
        jobs = data.get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            jid = j.get("id_icims") or j.get("id")
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            title = j.get("title", "")
            loc = j.get("location", "") or ""
            if qualifies(title, loc, False):
                out.append({"title": title, "location": loc, "url": "https://www.amazon.jobs" + j.get("job_path", "")})
        offset += 100
        if offset > 1000:
            break
    return out


def handler_jpmorgan(cfg):
    out = []
    offset = 0
    while True:
        url = (
            "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber=CX_1,facetsList=LOCATIONS,"
            f"limit=25,offset={offset},keyword=Product%20Manager"
        )
        body, status, _ = fetch(url)
        data = json.loads(body)
        items = (data.get("items") or [{}])[0].get("requisitionList", [])
        if not items:
            break
        for j in items:
            title = j.get("Title", "")
            loc = j.get("PrimaryLocation", "") or ""
            if qualifies(title, loc, False):
                job_id = j.get("Id") or j.get("JobRequisitionId", "")
                out.append({"title": title, "location": loc, "url": f"https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisition/{job_id}"})
        offset += 25
        if offset > 300:
            break
    return out


def handler_palo_alto_networks(cfg):
    out = []
    offset = 0
    while True:
        url = "https://paloaltonetworks.wd5.myworkdayjobs.com/wday/cxs/paloaltonetworks/panwexternalcareers/jobs"
        body_req = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "Product Manager"}
        body, status, _ = fetch(url, method="POST", body=body_req)
        data = json.loads(body)
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for j in postings:
            title = j.get("title", "")
            loc = j.get("locationsText", "") or ""
            if qualifies(title, loc, False):
                path = j.get("externalPath", "")
                out.append({"title": title, "location": loc, "url": "https://paloaltonetworks.wd5.myworkdayjobs.com/panwexternalcareers" + path})
        offset += 20
        if offset > 200:
            break
    return out


def handler_workable_huggingface(cfg):
    body, status, _ = fetch("https://apply.workable.com/api/v1/widget/accounts/huggingface")
    data = json.loads(body)
    out = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        loc_obj = j.get("location") or {}
        loc = f"{loc_obj.get('city','')}, {loc_obj.get('country','')}"
        is_remote = loc_obj.get("telecommuting")
        if qualifies(title, "Remote" if is_remote else loc, False):
            out.append({"title": title, "location": loc, "url": j.get("shortlink", "") or j.get("url", "")})
    return out


def handler_netflix(cfg):
    out = []
    start = 0
    seen_ids = set()
    while True:
        url = f"https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com&query=Product+Manager&start={start}&num=25"
        body, status, _ = fetch(url)
        data = json.loads(body)
        positions = data.get("positions", [])
        if not positions:
            break
        for j in positions:
            jid = j.get("id")
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            title = j.get("name", "")
            locations = j.get("locations") or [j.get("location", "")]
            loc_full = " / ".join(locations)
            if qualifies(title, loc_full, False):
                out.append({"title": title, "location": loc_full, "url": f"https://explore.jobs.netflix.net/careers/job/{jid}"})
        start += 25
        if start > 300:
            break
    return out


def handler_shopify(cfg):
    body, status, _ = fetch("https://www.shopify.com/careers")
    html = body.decode("utf-8", errors="ignore")
    hrefs = set(re.findall(r'href="(/careers/[a-z0-9-]+_[0-9a-f-]{36})"', html))
    out = []
    pm_slug_re = re.compile(r"product-manager|product-partner|product-lead|product-partnerships", re.IGNORECASE)
    for href in hrefs:
        if not pm_slug_re.search(href):
            continue
        job_url = "https://www.shopify.com" + href
        try:
            jbody, jstatus, _ = fetch(job_url)
        except Exception:
            continue
        jhtml = jbody.decode("utf-8", errors="ignore")
        m = re.search(r"<title>([^<]*)</title>", jhtml)
        title_full = m.group(1) if m else href
        title = title_full.split(" - ")[0].strip() if " - " in title_full else title_full
        if qualifies(title, title_full, True):
            out.append({"title": title, "location": "(see posting)", "url": job_url})
    return out


def handler_klarna(cfg):
    body, status, _ = fetch(
        "https://jobs.deel.com/klarna",
        extra_headers={"RSC": "1", "Accept": "text/x-component"},
    )
    text = body.decode("utf-8", errors="ignore")
    out = []
    for m in re.finditer(r'"title":"([^"]{3,120})"[^}]{0,400}?"location":"([^"]{0,120})"', text):
        title, loc = m.group(1), m.group(2)
        if qualifies(title, loc, False):
            out.append({"title": title, "location": loc, "url": "https://jobs.deel.com/klarna"})
    return out


def handler_intuit(cfg):
    url = "https://jobs.intuit.com/employment/new-york-product-management-jobs/27595/68351/6252001-5128638-5128581/4"
    body, status, _ = fetch(url)
    html = body.decode("utf-8", errors="ignore")
    out = []
    for m in re.finditer(r'<a[^>]*href="(/job/[^"]+)"[^>]*>([^<]{3,150})</a>', html):
        href, title = m.group(1), m.group(2).strip()
        if qualifies(title, "New York", False):
            out.append({"title": title, "location": "New York, NY (category page)", "url": "https://jobs.intuit.com" + href})
    return out


def handler_snowflake(cfg):
    sm_body, _, _ = fetch("https://careers.snowflake.com/sitemap.xml")
    sm_text = sm_body.decode("utf-8", errors="ignore")
    urls = re.findall(r"<loc>([^<]+)</loc>", sm_text)
    job_urls = [u for u in urls if "/job/" in u]
    out = []
    for ju in job_urls:
        if "product-manager" not in ju.lower() and "product manager" not in ju.lower():
            continue
        try:
            jbody, _, _ = fetch(ju)
        except Exception:
            continue
        jhtml = jbody.decode("utf-8", errors="ignore")
        m = re.search(r'"jobLocation".*?\](?=[,}])', jhtml, re.DOTALL)
        loc_blob = m.group(0) if m else ""
        cities = re.findall(r'"addressLocality":"([^"]+)"', loc_blob)
        loc_full = ", ".join(cities)
        mt = re.search(r"<title>([^<]*)</title>", jhtml)
        title = mt.group(1).split("|")[0].strip() if mt else ju
        if qualifies(title, loc_full, False):
            out.append({"title": title, "location": loc_full, "url": ju})
    return out


def handler_docusign(cfg):
    sm_body, _, _ = fetch("https://careers.docusign.com/sitemap.xml")
    sm_text = sm_body.decode("utf-8", errors="ignore")
    inner_sitemaps = re.findall(r"<loc>([^<]+)</loc>", sm_text)
    job_urls = []
    for sm in inner_sitemaps:
        if "sitemap1" in sm or "/jobs/" in sm:
            try:
                b, _, _ = fetch(sm)
            except Exception:
                continue
            t = b.decode("utf-8", errors="ignore")
            job_urls.extend(re.findall(r"<loc>([^<]+)</loc>", t))
    job_urls = [u for u in job_urls if "/jobs/" in u]
    out = []
    for ju in job_urls:
        try:
            body, status, _ = fetch(ju + ("?lang=en-us" if "?" not in ju else ""))
        except Exception:
            continue
        html = body.decode("utf-8", errors="ignore")
        m = re.search(r"<title>([^<]*)</title>", html)
        if not m:
            continue
        title_full = m.group(1)
        title = title_full.split(" in ")[0].replace("careers-home", "").strip(" |-")
        if not qualifies(title, title_full, False):
            continue
        if "Multiple Locations" in title_full:
            jm = re.search(r'"addressLocality":"([^"]+)"', html)
            loc = jm.group(1) if jm else "Multiple Locations"
        else:
            loc = title_full.split(" in ")[-1].split("|")[0].strip() if " in " in title_full else ""
        if qualifies(title, loc, False) or qualifies(title, title_full, False):
            out.append({"title": title, "location": loc, "url": ju})
    return out


def handler_surgeai(cfg):
    body, _, _ = fetch("https://www.surgehq.ai/careers")
    html = body.decode("utf-8", errors="ignore")
    out = []
    for m in re.finditer(r'data-job="([^"]{3,150})"', html):
        title = m.group(1)
        if qualifies(title, "", True):
            out.append({"title": title, "location": "(see careers page)", "url": "https://www.surgehq.ai/careers"})
    return out


def handler_airbnb(cfg):
    page_body, _, _ = fetch("https://careers.airbnb.com/positions/")
    page_html = page_body.decode("utf-8", errors="ignore")
    m = re.search(r"FWP_JSON = ({.*?});", page_html, re.DOTALL)
    fwp = json.loads(m.group(1))
    nonce = fwp["nonce"]
    payload = {
        "action": "facetwp_refresh",
        "data": {
            "facets": {"departments": ["product"], "where_you_work": [], "workplace_type": [],
                       "search_input": "", "jobs_pager": "", "jobs_sort": ""},
            "frozen_facets": [],
            "http_params": {"get": {}, "uri": "positions", "url_vars": []},
            "template": "wp",
            "extras": {"jobs_pager": True, "jobs_pagination": True, "jobs_sort": True,
                       "jobs_total": True, "jobs_reset": True},
            "soft_refresh": 0, "is_bfcache": 0, "first_load": 1, "paged": 1,
        },
    }
    body, status, _ = fetch(
        "https://careers.airbnb.com/positions/",
        method="POST",
        body=payload,
        extra_headers={"X-WP-Nonce": nonce, "Referer": "https://careers.airbnb.com/positions/",
                       "Origin": "https://careers.airbnb.com"},
    )
    data = json.loads(body)
    tmpl = data.get("template", "")
    out = []
    for m in re.finditer(
        r'<a href="(https://careers\.airbnb\.com/positions/\d+/?)"[^>]*>([^<]+)</a>\s*</span>\s*</div>\s*'
        r'<div class="col-span-4 lg:col-span-3[^"]*">\s*<span[^>]*>\s*<span[^>]*>\s*([^<]+?)\s*</span>',
        tmpl,
    ):
        url, title, loc = m.group(1), m.group(2).strip(), m.group(3).strip()
        if qualifies(title, loc, False):
            out.append({"title": title, "location": loc, "url": url})
    return out


SPECIAL_HANDLERS = {
    "amazon": handler_amazon,
    "jpmorgan": handler_jpmorgan,
    "palo_alto_networks": handler_palo_alto_networks,
    "workable_huggingface": handler_workable_huggingface,
    "netflix": handler_netflix,
    "shopify": handler_shopify,
    "klarna": handler_klarna,
    "intuit": handler_intuit,
    "snowflake": handler_snowflake,
    "docusign": handler_docusign,
    "surgeai": handler_surgeai,
    "airbnb": handler_airbnb,
}


def load_seen_pairs():
    if not SEEN_PATH.exists():
        return set()
    with open(SEEN_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {(d["company"].strip(), d["title"].strip()) for d in data}


def save_seen_pairs(pairs):
    seen = sorted([{"company": c, "title": t} for c, t in pairs], key=lambda x: (x["company"], x["title"]))
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2)


def send_digest_email(new_findings, errors, today):
    sender = os.environ.get("SMTP_SENDER_EMAIL")
    password = os.environ.get("SMTP_SENDER_PASSWORD")
    recipient = os.environ.get("SMTP_RECIPIENT_EMAIL")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))

    if not (sender and password and recipient):
        print("Missing SMTP env vars -- skipping email send.")
        return

    if not new_findings:
        print("No new findings -- skipping email (nothing to report).")
        return

    subject = f"Job Monitor: {len(new_findings)} new posting(s) — {today}"
    lines = [f"Job monitor run for {today}.", "", f"{len(new_findings)} new qualifying posting(s):", ""]
    by_company = {}
    for nf in new_findings:
        by_company.setdefault(nf["company"], []).append(nf)
    for company, items in sorted(by_company.items()):
        lines.append(f"=== {company} (Tier {items[0]['tier']}) ===")
        for it in items:
            lines.append(f"  - {it['title']}")
            lines.append(f"    {it['location']}")
            lines.append(f"    {it['url']}")
        lines.append("")
    if errors:
        lines.append(f"Errors during this run ({len(errors)}):")
        for e in errors:
            lines.append(f"  - {e}")
        lines.append("")
    lines.append("-- Automated Job Monitor (cloud routine)")

    msg = MIMEText("\n".join(lines), "plain")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    with smtplib.SMTP_SSL(host, port) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())
    print(f"Digest email sent to {recipient}.")


def write_run_log(new_findings, errors, companies_checked, email_attempted, email_sent):
    log_path = REPO_DIR / "last_run.log"
    timestamp = datetime.utcnow().isoformat() + "Z"
    lines = [
        f"Run at: {timestamp}",
        f"Companies checked: {companies_checked}",
        f"New findings: {len(new_findings)}",
        f"Errors: {len(errors)}",
        f"SMTP env vars present: {email_attempted}",
        f"Email sent: {email_sent}",
    ]
    if errors:
        lines.append("Error details:")
        for e in errors:
            lines.append(f"  - {e}")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def git_commit_and_push():
    subprocess.run(["git", "config", "user.email", "job-monitor-bot@local"], cwd=REPO_DIR, check=True)
    subprocess.run(["git", "config", "user.name", "Job Monitor Bot"], cwd=REPO_DIR, check=True)
    subprocess.run(["git", "add", "seen_pairs.json", "last_run.log"], cwd=REPO_DIR, check=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_DIR, capture_output=True, text=True, check=True)
    if not status.stdout.strip():
        print("No changes -- nothing to commit.")
        return
    today = date.today().isoformat()
    subprocess.run(["git", "commit", "-m", f"Run log + seen postings update — {today}"], cwd=REPO_DIR, check=True)
    subprocess.run(["git", "push"], cwd=REPO_DIR, check=True)
    print("Committed and pushed run log + updated seen_pairs.json.")


def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)["companies"]

    seen_pairs = load_seen_pairs()
    new_findings = []
    errors = []

    for company, cfg in config.items():
        platform = cfg.get("platform")
        if platform in ("unresolved", "manual_only"):
            continue
        loc_exc = cfg.get("location_exception", False)
        try:
            if platform == "greenhouse":
                results = handler_greenhouse(cfg["token"], loc_exc)
            elif platform == "ashby":
                results = handler_ashby(cfg["token"], loc_exc)
            elif platform == "lever":
                results = handler_lever(cfg["token"], loc_exc)
            elif platform == "special":
                results = SPECIAL_HANDLERS[cfg["handler"]](cfg)
            else:
                continue
        except Exception as e:
            errors.append(f"{company}: {type(e).__name__}: {e}")
            continue

        for r in results:
            pair = (company, r["title"].strip())
            if pair not in seen_pairs:
                new_findings.append({
                    "company": company,
                    "tier": cfg.get("tier", ""),
                    "title": r["title"],
                    "location": r["location"],
                    "url": r["url"],
                })
                seen_pairs.add(pair)
        time.sleep(0.2)

    today = date.today().isoformat()
    print(f"New qualifying postings: {len(new_findings)}")
    for nf in new_findings:
        print(f"  [{nf['company']}] {nf['title']} — {nf['location']}")
    if errors:
        print(f"Errors ({len(errors)}):")
        for e in errors:
            print(f"  {e}")

    save_seen_pairs(seen_pairs)
    send_digest_email(new_findings, errors, today)

    email_attempted = all(os.environ.get(v) for v in
                           ("SMTP_SENDER_EMAIL", "SMTP_SENDER_PASSWORD", "SMTP_RECIPIENT_EMAIL"))
    email_sent = email_attempted and bool(new_findings)
    companies_checked = len([c for c in config.values() if c.get("platform") not in ("unresolved", "manual_only")])
    write_run_log(new_findings, errors, companies_checked, email_attempted, email_sent)

    git_commit_and_push()


if __name__ == "__main__":
    main()
