#!/usr/bin/env python3
"""
find_family_restaurants.py

Lead-generation tool that finds small, independent, family-owned restaurants in
Northern Virginia (or any set of cities) and extracts their public contact info.

Pipeline:
  1. Discover restaurants per city via the Google Places API (Text Search).
  2. Pull details (phone, website, address, open/closed status) per place.
  3. Drop closed listings and known/auto-detected chains.
  4. Fetch each restaurant's website and score "family-owned" signals; extract
     any publicly listed email address.
  5. Rank by confidence and write results to CSV + JSON.

It never fabricates contact data: a field is left blank when no source provides
it.

Usage:
    export GOOGLE_PLACES_API_KEY="your_key"
    python3 find_family_restaurants.py --max-per-city 20 --out leads.csv

    # Only keep places with a positive family-owned signal on their website:
    python3 find_family_restaurants.py --require-family-signal

    # Faster run that skips website scraping (no email / family score):
    python3 find_family_restaurants.py --no-website-check

Requires: requests  (pip install -r requirements.txt)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Set

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency. Run: pip install -r requirements.txt")

log = logging.getLogger("family_restaurants")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Default Northern Virginia coverage. Override with --cities.
DEFAULT_CITIES: List[str] = [
    "Arlington, VA",
    "Alexandria, VA",
    "Fairfax, VA",
    "Falls Church, VA",
    "Vienna, VA",
    "McLean, VA",
    "Herndon, VA",
    "Reston, VA",
    "Springfield, VA",
    "Burke, VA",
    "Annandale, VA",
    "Leesburg, VA",
    "Ashburn, VA",
    "Sterling, VA",
    "Manassas, VA",
    "Woodbridge, VA",
]

# Known restaurant chains / franchises to exclude (matched case-insensitively as
# substrings of the business name). Not exhaustive — the cross-city duplicate
# detector below catches the rest.
CHAIN_BLOCKLIST: Set[str] = {
    "mcdonald", "subway", "chick-fil-a", "chickfila", "starbucks", "chipotle",
    "panera", "dunkin", "wendy", "burger king", "taco bell", "kfc", "popeye",
    "domino", "pizza hut", "papa john", "five guys", "shake shack", "cava",
    "sweetgreen", "olive garden", "applebee", "ihop", "denny", "panda express",
    "jersey mike", "firehouse sub", "qdoba", "wingstop", "raising cane",
    "chili's", "outback", "red lobster", "buffalo wild wings", "sonic drive",
    "arby", "hardee", "bojangles", "dairy queen", "baskin", "auntie anne",
    "cinnabon", "jimmy john", "moe's southwest", "potbelly", "noodles & company",
    "chopt", "&pizza", "blaze pizza", "mod pizza", "wawa", "sheetz", "7-eleven",
    "cheesecake factory", "p.f. chang", "pf chang", "texas roadhouse",
    "longhorn steakhouse", "red robin", "the melting pot", "tgi friday",
    "ruby tuesday", "carrabba", "bonefish grill", "first watch", "silver diner",
    "ledo pizza", "&pizza", "nando", "true food kitchen",
}

# Positive "family-owned / independent" signals (weighted). Searched in lowercased
# website text.
FAMILY_SIGNALS: Dict[str, int] = {
    "family owned": 5,
    "family-owned": 5,
    "family run": 4,
    "family-run": 4,
    "owned and operated": 4,
    "owner operated": 3,
    "our family": 3,
    "family business": 4,
    "family recipe": 3,
    "generation": 3,            # "second/third generation", "generations"
    "husband and wife": 4,
    "husband & wife": 4,
    "mom and pop": 5,
    "mom-and-pop": 5,
    "locally owned": 4,
    "independently owned": 4,
    "since 19": 3,
    "since 20": 1,
    "established 19": 3,
    "est. 19": 3,
    "est 19": 2,
    "proudly serving": 1,
    "neighborhood": 1,
}

# Negative signals that suggest a chain / franchise.
CHAIN_SIGNALS: Dict[str, int] = {
    "franchise": 4,
    "franchising": 5,
    "our locations": 3,
    "all locations": 3,
    "find a location": 3,
    "nationwide": 3,
    "corporate office": 4,
    "headquarters": 2,
    "gift cards available at all": 3,
}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Emails we never want (image/asset names, example placeholders, trackers).
EMAIL_BLOCKLIST_SUBSTR = (
    "example.com", "sentry", "wix", "squarespace", "@2x", ".png", ".jpg",
    ".jpeg", ".gif", ".svg", ".webp", "godaddy", "domain.com", "email.com",
    "yourdomain", "sentry.io",
)

PLACES_TEXTSEARCH = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACES_DETAILS = "https://maps.googleapis.com/maps/api/place/details/json"

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Lead:
    name: str
    category: str = "restaurant"
    city: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    source: str = ""               # Google Maps place URL
    address: str = ""
    family_score: int = 0
    family_signals: List[str] = field(default_factory=list)
    rating: Optional[float] = None
    review_count: Optional[int] = None
    place_id: str = ""


# --------------------------------------------------------------------------- #
# Google Places client
# --------------------------------------------------------------------------- #

class PlacesClient:
    def __init__(self, api_key: str, session: requests.Session):
        self.api_key = api_key
        self.session = session

    def text_search(self, query: str, max_results: int) -> List[dict]:
        """Run a Text Search, following next_page_token up to max_results."""
        results: List[dict] = []
        params = {"query": query, "key": self.api_key, "type": "restaurant"}
        while True:
            data = self._get(PLACES_TEXTSEARCH, params)
            if not data:
                break
            results.extend(data.get("results", []))
            token = data.get("next_page_token")
            if not token or len(results) >= max_results:
                break
            # next_page_token needs a short delay before it becomes valid.
            time.sleep(2)
            params = {"pagetoken": token, "key": self.api_key}
        return results[:max_results]

    def details(self, place_id: str) -> Optional[dict]:
        fields = (
            "name,formatted_phone_number,international_phone_number,website,"
            "formatted_address,business_status,url,rating,user_ratings_total,"
            "types,address_components"
        )
        params = {"place_id": place_id, "fields": fields, "key": self.api_key}
        data = self._get(PLACES_DETAILS, params)
        return data.get("result") if data else None

    def _get(self, url: str, params: dict) -> Optional[dict]:
        try:
            r = self.session.get(url, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Places request failed: %s", exc)
            return None
        status = data.get("status")
        if status in ("OK", "ZERO_RESULTS"):
            return data
        if status == "INVALID_REQUEST" and "pagetoken" in params:
            # token not ready yet — caller will stop paginating
            return data
        log.warning("Places API status=%s error=%s",
                    status, data.get("error_message", ""))
        if status in ("REQUEST_DENIED", "OVER_QUERY_LIMIT"):
            raise SystemExit(f"Google Places API error: {status} — "
                             f"{data.get('error_message', '')}")
        return data


# --------------------------------------------------------------------------- #
# Filtering / scoring helpers
# --------------------------------------------------------------------------- #

def is_blocklisted_chain(name: str) -> bool:
    low = name.lower()
    return any(chain in low for chain in CHAIN_BLOCKLIST)


def normalize_name(name: str) -> str:
    """Normalize a business name for cross-location duplicate detection."""
    low = name.lower()
    # strip a trailing location qualifier like " - clarendon" or " (tysons)"
    low = re.split(r"[-–(]", low)[0]
    low = re.sub(r"[^a-z0-9 ]", "", low)
    return " ".join(low.split())


def city_from_components(components: List[dict], fallback: str) -> str:
    for comp in components or []:
        if "locality" in comp.get("types", []):
            return comp.get("long_name", fallback)
    return fallback


def score_family(text: str) -> (int, List[str]):
    """Return (score, matched_signal_phrases) for website text."""
    low = text.lower()
    score = 0
    hits: List[str] = []
    for phrase, weight in FAMILY_SIGNALS.items():
        if phrase in low:
            score += weight
            hits.append(phrase)
    for phrase, weight in CHAIN_SIGNALS.items():
        if phrase in low:
            score -= weight
    return score, hits


def extract_emails(text: str) -> List[str]:
    found = []
    for raw in EMAIL_RE.findall(text):
        email = raw.strip(".").lower()
        if any(bad in email for bad in EMAIL_BLOCKLIST_SUBSTR):
            continue
        if email not in found:
            found.append(email)
    return found


# --------------------------------------------------------------------------- #
# Website inspection
# --------------------------------------------------------------------------- #

CONTACT_PATHS = ("", "contact", "contact-us", "about", "about-us", "our-story")


def inspect_website(url: str, session: requests.Session) -> (int, List[str], str):
    """Fetch a few pages of a site; return (family_score, signals, best_email)."""
    base = url.rstrip("/")
    best_score, best_signals = 0, []
    emails: List[str] = []
    seen = set()
    for path in CONTACT_PATHS:
        target = base if path == "" else f"{base}/{path}"
        if target in seen:
            continue
        seen.add(target)
        try:
            r = session.get(target, headers=HTTP_HEADERS, timeout=12,
                            allow_redirects=True)
            if r.status_code != 200 or "text/html" not in \
                    r.headers.get("Content-Type", ""):
                continue
            html = r.text
        except requests.RequestException:
            continue
        score, signals = score_family(html)
        if score > best_score:
            best_score, best_signals = score, signals
        for e in extract_emails(html):
            if e not in emails:
                emails.append(e)
        time.sleep(0.4)  # politeness
    # Prefer an email on the business's own domain.
    domain = re.sub(r"^https?://(www\.)?", "", base).split("/")[0]
    emails.sort(key=lambda e: (domain not in e, e))
    return best_score, best_signals, (emails[0] if emails else "")


# --------------------------------------------------------------------------- #
# Main routine
# --------------------------------------------------------------------------- #

def gather(args) -> List[Lead]:
    session = requests.Session()
    client = PlacesClient(args.api_key, session)

    raw_by_place: Dict[str, dict] = {}
    place_city: Dict[str, str] = {}

    for city in args.cities:
        query = f"family owned restaurant in {city}"
        log.info("Searching: %s", query)
        try:
            hits = client.text_search(query, args.max_per_city)
        except SystemExit:
            raise
        for h in hits:
            pid = h.get("place_id")
            if pid and pid not in raw_by_place:
                raw_by_place[pid] = h
                place_city[pid] = city.split(",")[0]

    log.info("Discovered %d unique places across %d cities",
             len(raw_by_place), len(args.cities))

    # Count normalized names across all places to flag likely chains.
    name_counts: Dict[str, int] = {}
    for h in raw_by_place.values():
        name_counts[normalize_name(h.get("name", ""))] = \
            name_counts.get(normalize_name(h.get("name", "")), 0) + 1

    leads: List[Lead] = []
    for i, (pid, hit) in enumerate(raw_by_place.items(), 1):
        name = hit.get("name", "")
        if is_blocklisted_chain(name):
            log.debug("skip (blocklist chain): %s", name)
            continue
        if name_counts.get(normalize_name(name), 0) >= args.chain_threshold:
            log.debug("skip (multi-location/chain): %s", name)
            continue

        log.info("[%d/%d] details: %s", i, len(raw_by_place), name)
        det = client.details(pid)
        if not det:
            continue
        if det.get("business_status") not in (None, "OPERATIONAL"):
            log.debug("skip (not operational): %s", name)
            continue

        lead = Lead(
            name=det.get("name", name),
            city=city_from_components(det.get("address_components", []),
                                      place_city.get(pid, "")),
            phone=det.get("formatted_phone_number", ""),
            website=det.get("website", ""),
            source=det.get("url", ""),
            address=det.get("formatted_address", ""),
            rating=det.get("rating"),
            review_count=det.get("user_ratings_total"),
            place_id=pid,
        )

        if lead.website and not args.no_website_check:
            score, signals, email = inspect_website(lead.website, session)
            lead.family_score = score
            lead.family_signals = signals
            lead.email = email

        leads.append(lead)

    if args.require_family_signal:
        leads = [l for l in leads if l.family_score > 0]

    # Rank: strongest family signal first, then most-reviewed.
    leads.sort(key=lambda l: (l.family_score, l.review_count or 0), reverse=True)
    return leads


def write_csv(leads: List[Lead], path: str) -> None:
    cols = ["name", "category", "city", "phone", "email", "website", "source",
            "address", "family_score", "family_signals", "rating",
            "review_count"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for l in leads:
            w.writerow([
                l.name, l.category, l.city, l.phone, l.email, l.website,
                l.source, l.address, l.family_score,
                "; ".join(l.family_signals), l.rating or "", l.review_count or "",
            ])


def write_json(leads: List[Lead], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(l) for l in leads], f, indent=2)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Find small, family-owned restaurants and their public "
                    "contact info.")
    p.add_argument("--api-key", default=os.getenv("GOOGLE_PLACES_API_KEY"),
                   help="Google Places API key (or set GOOGLE_PLACES_API_KEY).")
    p.add_argument("--cities", nargs="+", default=DEFAULT_CITIES,
                   help="Cities to search (default: Northern Virginia set).")
    p.add_argument("--max-per-city", type=int, default=20,
                   help="Max places to pull per city (default 20, max ~60).")
    p.add_argument("--chain-threshold", type=int, default=3,
                   help="Flag a name as a chain if it appears in this many "
                        "locations (default 3).")
    p.add_argument("--require-family-signal", action="store_true",
                   help="Keep only places with a positive family-owned signal.")
    p.add_argument("--no-website-check", action="store_true",
                   help="Skip website scraping (no email/family score).")
    p.add_argument("--out", default="family_restaurants.csv",
                   help="Output CSV path (a .json sibling is also written).")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s")

    if not args.api_key:
        sys.exit(
            "No Google Places API key. Set GOOGLE_PLACES_API_KEY or pass "
            "--api-key.\nGet one at "
            "https://developers.google.com/maps/documentation/places/web-service/get-api-key")

    leads = gather(args)
    write_csv(leads, args.out)
    json_path = re.sub(r"\.csv$", ".json", args.out) or args.out + ".json"
    if json_path == args.out:
        json_path = args.out + ".json"
    write_json(leads, json_path)

    with_phone = sum(1 for l in leads if l.phone)
    with_email = sum(1 for l in leads if l.email)
    with_signal = sum(1 for l in leads if l.family_score > 0)
    log.info("Done. %d leads -> %s / %s", len(leads), args.out, json_path)
    log.info("  with phone: %d | with email: %d | family signal: %d",
             with_phone, with_email, with_signal)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
