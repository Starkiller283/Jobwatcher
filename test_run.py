import logging
from function_app import fetch_listings, filter_new, send_email, mark_seen

logging.basicConfig(level=logging.INFO)

def test_jobwatcher():
    listings = fetch_listings()
    logging.info(f"Fetched {len(listings)} listings")

    new_jobs = filter_new(listings)
    logging.info(f"Filtered to {len(new_jobs)} new jobs")

    if new_jobs:
        send_email(new_jobs)
        mark_seen(new_jobs)
        logging.info("Sent email and marked jobs as seen")
    else:
        logging.info("No new jobs to process")

if __name__ == "__main__":
    test_jobwatcher()