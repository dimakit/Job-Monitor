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
import html as html_module
import os
import subprocess
import smtplib
import time
import urllib.request
from datetime import date, datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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
NY_MENTION_REGEX = re.compile(r"New York|NYC", re.IGNORECASE)
# "Remote" alone is not enough -- many postings pair it with a non-US country/city
# (e.g. "Remote - India", "Canada - Remote", "Israel - Remote"). Dimitry's constraint
# is NY-or-US-remote, not remote-from-anywhere. If a foreign marker appears anywhere
# near "Remote" and there's no separate explicit NY mention, reject it.
FOREIGN_REMOTE_MARKER_REGEX = re.compile(
    r"India|Brazil|Mexico|Israel|Germany|Turkey|Colombia|France|Ireland|Spain|"
    r"United Kingdom|\bUK\b|Poland|Portugal|Singapore|\bUAE\b|Emirates|Austria|"
    r"\bCanada\b|Philippines|Argentina|Chile|Japan|China|Australia|Netherlands|"
    r"Italy|Switzerland|Czech|Hungary|Greece|Nigeria|Egypt|Pakistan|Indonesia|"
    r"Vietnam|Thailand|Malaysia|Korea|Taiwan|Hong Kong|New Zealand|Romania|Sweden|"
    r"Lithuania|Bangalore|Krakow|Dublin|London|Berlin|Madrid|Cyprus|Vilnius|"
    r"S(?:a|ã)o Paulo",
    re.IGNORECASE,
)


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
    loc = location_text or ""
    if not LOC_REGEX.search(loc):
        return False
    if NY_MENTION_REGEX.search(loc):
        return True
    # matched only via "Remote" -- reject if it's paired with a non-US location marker
    return not FOREIGN_REMOTE_MARKER_REGEX.search(loc)


# ---------- scoring ----------
# Two layers:
#  1. Deterministic bonuses (tier, exact-NY location) -- exact by definition,
#     computed in code so they're perfectly consistent every run.
#  2. LLM-judged "role fit" (domain/pricing relevance, technical-vs-strategic
#     orientation, seniority-band fit) -- these need real judgment about
#     intent, not keyword matching, so one batched Claude API call per run
#     scores everything found that day against Dimitry's actual background.
#     Falls back to a cruder keyword heuristic if no API key is configured
#     (e.g. local testing) or the call fails, so the pipeline never breaks.

TOP_PICK_THRESHOLD = 8

NY_EXACT_REGEX = re.compile(r"New York|NYC", re.IGNORECASE)

DIMITRY_CONTEXT = """\
Dimitry Kitaigorodsky is a Director of Product Management (IC) at Visa, Embedded Finance, NYC. \
~15 years total career in product/payments/finance, but genuine hands-on "core PM" tenure \
(product-team PM work as usually defined) is only about 4 years -- the rest of his career \
includes adjacent strategy/consulting/analytics roles that touch PM-like work without being a \
formal PM seat. Do not over-credit seniority just because his current title is "Director" or his \
total career is long.

TARGET SENIORITY BAND for role requirements: roughly 8 years of PM experience, 0-2 years directly \
managing other PMs. Roles pitched well above that (10+ years PM experience, 3+ years managing a \
team of PMs) are a poor seniority match despite his Director title. Roles pitched well below that \
band (entry-level IC tasks like "write PRDs," "support senior PMs," 2-3 years experience) are \
ALSO a poor match -- he does not want a step down into basic execution work. The sweet spot is the \
middle band, not either extreme.

STRENGTHS: product strategy, pricing & monetization, GTM/commercialization strategy, market \
sizing, storytelling, cross-functional influence, payments/fintech domain knowledge (non-technical). \
Pricing/monetization work is his single clearest, most defensible positioning spike.

EXPLICIT WEAKNESSES (documented, self-acknowledged): deep technical/system-design ownership, \
engineering execution, data science, AI/ML technical depth, platform architecture, growth-loop \
mechanics, heavy experimentation/A-B-testing roles. Roles that read as deeply technical/engineering- \
heavy PM work are a bad fit even if the title says "Product Manager."

Target comp: $450K+ total comp minimum bar for a next move.
"""

SCORING_PROMPT_TEMPLATE = """\
{context}

Score each job posting below on a ROLE FIT scale from 1 to 6 (integers only), based ONLY on the \
qualitative factors described above -- ignore company tier and exact location, those are scored \
separately. Consider:
- Domain relevance: is this genuinely fintech/payments and/or pricing/monetization work? (both can \
  apply and should be weighted positively if so; a fintech pricing role is his best-case scenario)
- Technical vs. strategic orientation: does this read as deep technical/engineering-heavy PM work \
  (score down) or strategic/pricing/GTM-oriented work (score up)?
- Seniority-band fit: does the implied experience level match the ~8-years-PM / 0-2-years-managing \
  sweet spot? IMPORTANT -- the penalty is NOT symmetric. A role that reads as somewhat MORE senior \
  than the sweet spot (e.g. wants 10 years of PM experience) is a comfortable stretch for him and \
  should only be penalized mildly, if at all -- he has a strong track record and can credibly reach \
  up. A role that reads as MORE JUNIOR than the sweet spot (e.g. ~2 years PM experience, entry-level \
  IC/PRD-writing tasks, "support senior PMs") is a much worse mismatch and should be penalized more \
  heavily -- he would be overqualified, bored, and it signals a real level mismatch, not a stretch.

1-2 = poor fit (wrong domain, clearly too junior, and/or heavily technical).
3-4 = plausible/decent fit (roughly right band or a comfortable senior stretch, domain adjacent or \
unclear, mixed orientation).
5-6 = strong fit (right seniority band or a reasonable senior stretch, clearly strategic/pricing/GTM- \
oriented, touches fintech/payments and/or pricing/monetization).

Postings (JSON array, each with an "id"):
{postings_json}

Respond with ONLY a JSON array, no other text, in this exact shape:
[{{"id": 0, "role_fit_score": 4, "rationale": "one short sentence"}}, ...]
One entry per posting, in any order, matching every "id" given.
"""


def _deterministic_bonus(tier, location_text):
    try:
        tier_num = float(tier)
    except (TypeError, ValueError):
        tier_num = 2
    if tier_num <= 1:
        tier_bonus = 3
    elif tier_num <= 2:
        tier_bonus = 1
    else:
        tier_bonus = 0
    location_bonus = 0.5 if NY_EXACT_REGEX.search(location_text or "") else 0
    return tier_bonus + location_bonus


def score_posting_heuristic(title, location_text, tier):
    """Crude keyword-based fallback if the LLM call isn't available."""
    score = 4
    if re.search(r"Director|VP\b|Vice President|Head of|Chief Product", title, re.IGNORECASE):
        score += 1
    if re.search(r"payment|fintech|pricing|monetization|bank|wallet|lending|credit", title, re.IGNORECASE):
        score += 1
    score += _deterministic_bonus(tier, location_text)
    return max(2, min(9.5, score))


def score_postings_llm(findings):
    """Batched LLM scoring. Returns dict {index_in_findings: (score, rationale)}; empty dict on failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not findings:
        return {}

    postings = [
        {"id": i, "company": f["company"], "title": f["title"], "location": f["location"]}
        for i, f in enumerate(findings)
    ]
    prompt = SCORING_PROMPT_TEMPLATE.format(
        context=DIMITRY_CONTEXT,
        postings_json=json.dumps(postings, indent=2),
    )

    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp_body, status, _ = fetch(
            "https://api.anthropic.com/v1/messages",
            method="POST",
            body=body,
            extra_headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        data = json.loads(resp_body)
        text = data["content"][0]["text"].strip()
        # strip markdown code fences if present
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        parsed = json.loads(text)
        results = {}
        for item in parsed:
            idx = item["id"]
            role_fit = max(1, min(6, float(item["role_fit_score"])))
            results[idx] = (role_fit, item.get("rationale", ""))
        return results
    except Exception as e:
        print(f"LLM scoring failed, falling back to heuristic: {type(e).__name__}: {e}")
        return {}


def score_all_findings(findings):
    """Attaches 'score' and 'rationale' to each finding dict in place."""
    llm_results = score_postings_llm(findings)
    for i, f in enumerate(findings):
        bonus = _deterministic_bonus(f["tier"], f["location"])
        if i in llm_results:
            role_fit, rationale = llm_results[i]
            f["score"] = round(max(2, min(9.5, role_fit + bonus)) * 2) / 2  # round to nearest 0.5
            f["rationale"] = rationale
        else:
            f["score"] = score_posting_heuristic(f["title"], f["location"], f["tier"])
            f["rationale"] = "(heuristic fallback -- LLM scoring unavailable this run)"


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
        title_full = html_module.unescape(m.group(1)) if m else href
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
        title_full = html_module.unescape(m.group(1))
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


def _slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-+", "-", slug)


def handler_revolut(cfg):
    body, status, _ = fetch("https://www.revolut.com/careers/")
    html = body.decode("utf-8", errors="ignore")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    data = json.loads(m.group(1))
    positions = data["props"]["pageProps"]["positions"]
    out = []
    for p in positions:
        title = p.get("text", "")
        locs = p.get("locations", []) or []
        loc_names = [l.get("name", "") for l in locs if isinstance(l, dict)]
        loc_full = ", ".join(loc_names)
        if qualifies(title, loc_full, False):
            pid = p.get("id", "")
            url = f"https://www.revolut.com/careers/position/{_slugify(title)}-{pid}/"
            out.append({"title": title, "location": loc_full, "url": url})
    return out


def handler_uber(cfg):
    sm_body, _, _ = fetch("https://jobs.uber.com/en/jobs/sitemap.xml")
    sm_text = sm_body.decode("utf-8", errors="ignore")
    job_urls = re.findall(r"<loc>([^<]+)</loc>", sm_text)
    out = []
    for ju in job_urls:
        try:
            body, status, _ = fetch(ju, extra_headers={"Range": "bytes=0-5000"})
        except Exception:
            continue
        html = body.decode("utf-8", errors="ignore")
        m = re.search(r"<title>([^<]*)</title>", html)
        if not m:
            continue
        title_full = html_module.unescape(m.group(1))
        if qualifies(title_full, title_full, False):
            parts = title_full.split(", ")
            title = ", ".join(parts[:-2]) if len(parts) > 2 else parts[0]
            loc = ", ".join(parts[-2:]) if len(parts) > 2 else (parts[1] if len(parts) > 1 else "")
            out.append({"title": title, "location": loc, "url": ju})
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
    "revolut": handler_revolut,
    "uber": handler_uber,
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


def _html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def build_plain_text(new_findings, errors, today):
    top_picks = sorted([nf for nf in new_findings if nf["score"] >= TOP_PICK_THRESHOLD],
                        key=lambda x: -x["score"])
    lines = [f"Job monitor run for {today}.", "", f"{len(new_findings)} new qualifying posting(s):", ""]
    if top_picks:
        lines.append(f"TOP PICKS (score {TOP_PICK_THRESHOLD}+):")
        for it in top_picks:
            lines.append(f"  [{it['score']}] {it['company']} — {it['title']}")
            lines.append(f"    {it['location']}")
            if it.get("rationale"):
                lines.append(f"    {it['rationale']}")
            lines.append(f"    {it['url']}")
        lines.append("")
    by_company = {}
    for nf in new_findings:
        by_company.setdefault(nf["company"], []).append(nf)
    for company, items in sorted(by_company.items()):
        lines.append(f"=== {company} (Tier {items[0]['tier']}) ===")
        for it in sorted(items, key=lambda x: -x["score"]):
            lines.append(f"  [{it['score']}] {it['title']}")
            lines.append(f"    {it['location']}")
            if it.get("rationale"):
                lines.append(f"    {it['rationale']}")
            lines.append(f"    {it['url']}")
        lines.append("")
    if errors:
        lines.append(f"Errors during this run ({len(errors)}):")
        for e in errors:
            lines.append(f"  - {e}")
        lines.append("")
    lines.append("-- Automated Job Monitor (auto-scores are AI-assisted, not full manual review)")
    return "\n".join(lines)


def _score_color(score):
    if score >= TOP_PICK_THRESHOLD:
        return "#0d652d", "#e6f4ea"
    if score >= 6:
        return "#8a6d00", "#fef7e0"
    return "#5f6368", "#f1f3f4"


def _render_card(it, show_company=False):
    text_color, bg_color = _score_color(it["score"])
    company_line = f"<div style=\"font-size:13px;color:#5f6368;margin-top:2px;\">{_html_escape(it['company'])} &middot; Tier {_html_escape(it['tier'])}</div>" if show_company else ""
    rationale_line = ""
    if it.get("rationale"):
        rationale_line = f"<div style=\"font-size:12px;color:#3c4043;margin-top:6px;font-style:italic;\">{_html_escape(it['rationale'])}</div>"
    return f"""
            <tr>
              <td style="padding:14px 16px;border:1px solid #e0e0e0;border-radius:8px;display:block;margin-bottom:10px;">
                <span style="font-size:11px;font-weight:700;color:{text_color};background:{bg_color};border-radius:4px;padding:2px 7px;">SCORE {it['score']}</span>
                <div style="margin-top:8px;">
                  <a href="{_html_escape(it['url'])}" style="font-size:15px;font-weight:600;color:#1a1a1a;text-decoration:none;">{_html_escape(it['title'])}</a>
                </div>
                {company_line}
                <div style="font-size:13px;color:#5f6368;margin-top:4px;">{_html_escape(it['location'])}</div>
                {rationale_line}
                <a href="{_html_escape(it['url'])}" style="font-size:12px;color:#1a73e8;text-decoration:none;display:inline-block;margin-top:8px;">View posting &rarr;</a>
              </td>
            </tr>"""


def build_html(new_findings, errors, today):
    top_picks = sorted([nf for nf in new_findings if nf["score"] >= TOP_PICK_THRESHOLD],
                        key=lambda x: -x["score"])

    top_picks_html = ""
    if top_picks:
        cards = "".join(_render_card(it, show_company=True) for it in top_picks)
        top_picks_html = f"""
        <div style="margin-bottom:28px;">
          <div style="font-size:16px;font-weight:700;color:#0d652d;margin-bottom:8px;">
            &#128293; Top Picks (score {TOP_PICK_THRESHOLD}+)
          </div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate;border-spacing:0 8px;">
            {cards}
          </table>
        </div>
        <hr style="border:none;border-top:1px solid #e0e0e0;margin:0 0 24px 0;">"""

    by_company = {}
    for nf in new_findings:
        by_company.setdefault(nf["company"], []).append(nf)

    TIER_COLORS = {1: "#1a73e8", 2: "#7c3aed", "2.5": "#7c3aed"}

    company_blocks = []
    for company, items in sorted(by_company.items(), key=lambda kv: (kv[1][0].get("tier", 99), kv[0])):
        tier = items[0].get("tier", "")
        tier_color = TIER_COLORS.get(tier, "#5f6368")
        cards = "".join(_render_card(it) for it in sorted(items, key=lambda x: -x["score"]))
        company_blocks.append(f"""
        <div style="margin-bottom:24px;">
          <div style="font-size:16px;font-weight:700;color:#1a1a1a;margin-bottom:8px;">
            {_html_escape(company)}
            <span style="font-size:11px;font-weight:600;color:#ffffff;background:{tier_color};border-radius:4px;padding:2px 8px;margin-left:8px;vertical-align:middle;">TIER {tier}</span>
          </div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate;border-spacing:0 8px;">
            {cards}
          </table>
        </div>""")

    errors_html = ""
    if errors:
        error_items = "".join(f"<li style='margin-bottom:4px;'>{_html_escape(e)}</li>" for e in errors)
        errors_html = f"""
        <div style="margin-top:24px;padding:12px 16px;background:#fef7e0;border-radius:8px;font-size:12px;color:#5f6368;">
          <strong>{len(errors)} company check(s) had errors this run:</strong>
          <ul style="margin:8px 0 0 0;padding-left:20px;">{error_items}</ul>
        </div>"""

    return f"""\
<!doctype html>
<html>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;max-width:600px;width:100%;">
          <tr>
            <td style="background:#1a1a1a;padding:20px 24px;">
              <div style="color:#ffffff;font-size:18px;font-weight:700;">Job Monitor</div>
              <div style="color:#9aa0a6;font-size:13px;margin-top:2px;">{len(new_findings)} new posting(s) &middot; {_html_escape(today)}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:24px;">
              {top_picks_html}
              {''.join(company_blocks)}
              {errors_html}
            </td>
          </tr>
          <tr>
            <td style="padding:0 24px 20px 24px;font-size:11px;color:#9aa0a6;">
              Auto-scores combine an AI judgment call (domain/seniority/orientation fit) with deterministic tier and location bonuses — a fast triage aid, not the same rigor as full manual review.
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


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

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(build_plain_text(new_findings, errors, today), "plain"))
    msg.attach(MIMEText(build_html(new_findings, errors, today), "html"))

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
                tier = cfg.get("tier", "")
                new_findings.append({
                    "company": company,
                    "tier": tier,
                    "title": r["title"],
                    "location": r["location"],
                    "url": r["url"],
                })
                seen_pairs.add(pair)
        time.sleep(0.2)

    score_all_findings(new_findings)

    today = date.today().isoformat()
    print(f"New qualifying postings: {len(new_findings)}")
    for nf in new_findings:
        print(f"  [{nf['score']}] [{nf['company']}] {nf['title']} — {nf['location']}")
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
