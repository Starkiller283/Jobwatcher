import os
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import hashlib
import azure.functions as func
from azure.data.tables import TableClient
from azure.core.exceptions import ResourceNotFoundError
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

app = func.FunctionApp()

# ── CONFIG ─────────────────────────────────────────────────────────────────────
# 1) List of UK IT contract pages to scrape (generic HTML)
JOB_SITES = [
    "https://www.contractoruk.com/it_contract_jobs/",
    "https://www.cwjobs.co.uk/jobs/it-contract",
    "https://www.technojobs.co.uk/",
    "https://www.jobserve.com/gb/en/IT%2BTelecommunications-sector-jobs-in-United-Kingdom/",
    "https://www.jobserve.com/gb/en/",
    "https://www.reed.co.uk/jobs/contract-it-jobs",
    "https://www.cv-library.co.uk/it-jobs?contract=true",
    "https://www.totaljobs.com/jobs/contract/it",
    "https://www.contractorjobs.co.uk/",
    "https://www.adzuna.co.uk/jobs/it-contract",
    "https://uk.indeed.com/q-it-contractor-jobs.html",
    "https://uk.indeed.com",
    "https://www.linkedin.com/jobs",
    "https://www.monster.co.uk",
    "https://www.glassdoor.co.uk",
    "https://www.totaljobs.com",
    "https://www.reed.co.uk",
    "https://www.cv-library.co.uk",
    "https://www.jobsite.co.uk",
    "https://www.fish4.co.uk",
    "https://www.gumtree.com/jobs",
    "https://uk.jooble.org",
    "https://uk.welcometothejungle.com",
    "https://uk.talent.com",
    "https://itjobswatch.co.uk/Contract-IT-Job-Market",
    "https://outsideir35roles.com",
    "https://www.procontractjobs.com",
    "https://alltechishuman.org",
    "https://workintech.io",
    "https://www.womentech.net",
    "https://www.crunchboard.com",
    "https://techspark.co",
    "https://www.bubble-jobs.co.uk",
    "https://www.itjobboard.co.uk",
    "https://uk.welcometothejungle.com",
    "https://uk.jora.com",
    "https://uk.jobrapido.com",
    "https://www.careerbuilder.co.uk",
    "https://www.ziprecruiter.co.uk",
    "https://www.simplyhired.co.uk",
    "https://jobs.theguardian.com",
    "https://www.jobs.nhs.uk",
    "https://www.civilservicejobs.service.gov.uk",
    "https://www.charityjob.co.uk"
]

# 2) Keyword filters
ROLE_KEYWORDS = [
    "azure", "solutions", "devops", "engineer", "network",
    "application delivery", "network security", "loadbalancing architect",
    "network security architect"
]
SKILL_KEYWORDS = [
    "python", "django", "azure", "docker", "terraform", "kubernetes",
    "bash", "powershell", "firewalls", "vpn", "automation", "git"
]

# 3) Environment variables (configure in local.settings.json and in Azure)
TABLE_CONN       = os.getenv("TABLE_CONN")
TABLE_NAME       = os.getenv("TABLE_NAME")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
EMAIL_FROM       = os.getenv("EMAIL_FROM")
EMAIL_TO         = os.getenv("EMAIL_TO")

# ── AZURE TABLE CLIENT ─────────────────────────────────────────────────────────
table = None
if TABLE_CONN and TABLE_NAME:
    try:
        table = TableClient.from_connection_string(
            conn_str=TABLE_CONN,
            table_name=TABLE_NAME
        )
    except Exception as e:
        logging.error(f"Failed to create TableClient: {e}")

# ── FETCH ──────────────────────────────────────────────────────────────────────
def fetch_listings():
    """
    Visit each URL in JOB_SITES, parse the HTML, and extract whatever
    job cards can be found with generic selectors ('.job-listing' or '.job-card').
    Returns a list of dicts: { "id", "title", "link", "description" }.
    """
    listings = []
    for site in JOB_SITES:
        try:
            resp = requests.get(site)
            resp.raise_for_status()
        except Exception as e:
            # If a site returns 403, 404, DNS error, etc., log a warning and move on.
            logging.warning(f"Failed to fetch {site}: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # Generic “card” parser – many sites wrap each listing in a container like .job-card or .job-listing
        for card in soup.select(".job-listing, .job-card"):
            title_el = card.select_one(".title, h2, a")
            link_el  = card.select_one("a")
            if not title_el or not link_el:
                continue

            title = title_el.get_text(strip=True)
            href  = link_el.get("href", "")
            link  = urljoin(site, href)
            parsed = urlparse(link)
            # Use a hash of the full URL to avoid collisions across sites
            job_id = hashlib.sha1(link.encode()).hexdigest()

            # Some cards include a short summary/description
            desc_el = card.select_one(".description, .summary")
            desc = desc_el.get_text(strip=True) if desc_el else ""

            listings.append({
                "id":          job_id,
                "title":       title,
                "link":        link,
                "description": desc
            })

    return listings

# ── FILTER & DEDUPE ─────────────────────────────────────────────────────────────
def filter_new(listings):
    """
    From the raw listings, keep only those that:
      - Contain at least one ROLE_KEYWORD in the title (case-insensitive)
      - Contain at least one SKILL_KEYWORD in title or description
      - Contain “outside ir35” (anywhere)
      - Contain “remote” (anywhere)
    Then dedupe by checking Azure Table Storage for existing job IDs.
    """
    new_jobs = []
    for job in listings:
        title = job["title"].lower()
        desc  = job["description"].lower()

        # 1) Broad-role match
        if not any(rk.lower() in title for rk in ROLE_KEYWORDS):
            continue

        # 2) Generic skill match
        if not any(kw.lower() in title or kw.lower() in desc for kw in SKILL_KEYWORDS):
            continue

        # 3) Required “outside ir35” AND “remote”
        if "outside ir35" not in title and "outside ir35" not in desc:
            continue
        if "remote" not in title and "remote" not in desc:
            continue

        # 4) Deduplicate via Azure Table Storage if configured
        if table:
            try:
                table.get_entity(partition_key="jobs", row_key=job["id"])
                # If it already exists, skip it
            except ResourceNotFoundError:
                new_jobs.append(job)
        else:
            new_jobs.append(job)

    return new_jobs

# ── EMAIL NOTIFICATION ─────────────────────────────────────────────────────────
def send_email(jobs):
    """
    Build a simple HTML email with each job as a link, and send via SendGrid.
    """
    if not jobs or not SENDGRID_API_KEY or not EMAIL_FROM or not EMAIL_TO:
        logging.warning("Email settings incomplete; skipping email notification")
        return

    html = "<h3>New IT Contract Roles:</h3><ul>"
    for job in jobs:
        html += f'<li><a href="{job["link"]}">{job["title"]}</a></li>'
    html += "</ul>"

    message = Mail(
        from_email=EMAIL_FROM,
        to_emails=EMAIL_TO,
        subject=f"{len(jobs)} New IT Contract Role(s)",
        html_content=html
    )
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        sg.send(message)
        logging.info("Email sent successfully.")
    except Exception as e:
        logging.error(f"Error sending email: {e}")

# ── MARK AS SEEN ────────────────────────────────────────────────────────────────
def mark_seen(jobs):
    """
    For each job in `jobs`, create an entity in Azure Table Storage
    so that we don’t email it again on the next run.
    """
    if not table:
        logging.warning("Table storage not configured; cannot mark jobs as seen")
        return

    for job in jobs:
        try:
            table.create_entity({
                "PartitionKey": "jobs",
                "RowKey":       job["id"]
            })
        except Exception as e:
            logging.error(f"Failed to mark {job['id']} as seen: {e}")

# ── SCHEDULED FUNCTION ─────────────────────────────────────────────────────────
@app.schedule(
    schedule="0 0 8 * * *",    # sec=0, min=0, hour=8, day=*, month=*, weekday=*
    arg_name="timer",
    run_on_startup=True       # ← keep True for immediate testing
)
def JobWatcher(timer: func.TimerRequest):
    logging.info("JobWatcher triggered")

    # 1) Fetch all job “cards” from each site
    listings = fetch_listings()

    # 2) Filter & dedupe
    new_jobs = filter_new(listings)

    if new_jobs:
        logging.info(f"Found {len(new_jobs)} new jobs, sending email…")
        send_email(new_jobs)
        mark_seen(new_jobs)
    else:
        logging.info("No new jobs found today")
