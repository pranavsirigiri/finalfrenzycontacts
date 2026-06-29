# Family-Owned Restaurant Lead Finder

Finds small, **independent / family-owned restaurants** in Northern Virginia (or
any cities you pass) and extracts their public contact info — name, phone,
email, website, address — into CSV + JSON.

It is built specifically for restaurants and actively filters **out** chains and
franchises. It never fabricates data: a field is blank when no source provides
it.

## How it works

1. **Discover** — queries the Google Places API (`Text Search`) for
   "family owned restaurant in <city>" across each target city.
2. **Detail** — pulls phone, website, address, and open/closed status per place.
3. **Filter** — drops closed listings, names on a chain blocklist (McDonald's,
   Chipotle, Cava, Silver Diner, …), and any name that appears in 3+ cities
   (an automatic chain signal).
4. **Score** — fetches each restaurant's website (home + contact/about pages)
   and scores "family-owned" language ("family owned", "owned and operated",
   "since 1979", "three generations", …). Chain language ("franchise",
   "all locations") subtracts.
5. **Email** — extracts real emails from the site, preferring the business's own
   domain.
6. **Rank & export** — sorts by family-owned confidence, writes CSV + JSON.

## Setup

```bash
pip install -r requirements.txt
export GOOGLE_PLACES_API_KEY="your_key_here"
```

Get a key: https://developers.google.com/maps/documentation/places/web-service/get-api-key
(Enable the **Places API**. Text Search + Place Details are billable; the free
monthly credit covers a few thousand lookups.)

## Usage

```bash
# Default: full Northern Virginia city list, 20 places/city
python3 find_family_restaurants.py --out leads.csv

# Only keep places with a positive family-owned website signal
python3 find_family_restaurants.py --require-family-signal --out leads.csv

# Specific cities, more results each
python3 find_family_restaurants.py \
    --cities "Arlington, VA" "Alexandria, VA" "Vienna, VA" \
    --max-per-city 40

# Fast pass without website scraping (no email / family score)
python3 find_family_restaurants.py --no-website-check
```

### Key options

| Flag | Purpose |
|------|---------|
| `--cities` | Cities to search (default: NoVA set in `DEFAULT_CITIES`) |
| `--max-per-city` | Results per city (max ~60 via pagination) |
| `--require-family-signal` | Keep only sites with a positive family score |
| `--chain-threshold` | # of locations before a name is treated as a chain (default 3) |
| `--no-website-check` | Skip scraping for speed |
| `--out` | CSV path (a `.json` sibling is also written) |

## Output columns

`name, category, city, phone, email, website, source, address, family_score,
family_signals, rating, review_count`

`source` is the Google Maps listing URL. `family_signals` lists the matched
phrases so you can see *why* a place scored as family-owned.

## Tuning

Edit the lists at the top of `find_family_restaurants.py`:
- `CHAIN_BLOCKLIST` — add chains to exclude.
- `FAMILY_SIGNALS` / `CHAIN_SIGNALS` — adjust the scoring phrases/weights.
- `DEFAULT_CITIES` — change the default geography.

## Notes / good citizenship

- Respect Google Places API [Terms of Service](https://cloud.google.com/maps-platform/terms);
  in particular, don't cache Place data beyond what the terms allow.
- Website scraping is light (a handful of pages per site, with delays). Keep it
  that way.
- This collects **public business** contact info for B2B outreach. Honor
  CAN-SPAM and any do-not-contact requests when you reach out.
