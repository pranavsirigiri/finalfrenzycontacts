# Family-Owned Restaurant Lead Finder

Finds small, **independent / family-owned restaurants** in Northern Virginia (or
any cities you pass) and extracts their public contact info — name, phone,
email, website, address — into CSV + JSON.

It is built specifically for restaurants and actively filters **out** chains and
franchises. **No API key required** by default. It never fabricates data: a
field is blank when no source provides it.

## How it works

1. **Discover** — for each city, geocodes a bounding box via OpenStreetMap
   **Nominatim**, then pulls every `amenity=restaurant` in that area from the
   **Overpass API**. Both are free and need no key. (Optionally use the Google
   Places API instead — see below.)
2. **Filter chains** — drops any OSM entry tagged with a `brand` (a chain
   marker), names on a curated blocklist (McDonald's, Chipotle, Cava, …), and
   any name appearing in 3+ cities (an automatic chain signal). Closed/disused
   listings are skipped.
3. **Score "family-owned"** — fetches each restaurant's website (home +
   contact/about pages) and scores family language ("family owned", "owned and
   operated", "since 1979", "three generations", …). Chain language
   ("franchise", "all locations") subtracts.
4. **Email** — uses any email OSM already has, otherwise extracts real emails
   from the website, preferring the business's own domain. Junk (asset
   filenames, `noreply@example.com`) is rejected.
5. **Rank & export** — sorts by family-owned confidence, writes CSV + JSON.

## Setup

```bash
pip install -r requirements.txt
```

That's it — no key needed.

## Usage

```bash
# Preview the output format with bundled sample data (no network):
python3 find_family_restaurants.py --demo --out preview.csv

# Default keyless run: full Northern Virginia city list via OpenStreetMap
python3 find_family_restaurants.py --out leads.csv

# Only keep places with a positive family-owned website signal
python3 find_family_restaurants.py --require-family-signal --out leads.csv

# Specific cities
python3 find_family_restaurants.py \
    --cities "Arlington, VA" "Alexandria, VA" "Vienna, VA" \
    --max-per-city 40

# Fast pass without website scraping (no email / family score)
python3 find_family_restaurants.py --no-website-check
```

### Optional: Google Places source

If you have a Google Places API key and prefer its data (includes ratings /
review counts), use `--source google`:

```bash
export GOOGLE_PLACES_API_KEY="your_key"
python3 find_family_restaurants.py --source google --out leads.csv
```

Get a key: https://developers.google.com/maps/documentation/places/web-service/get-api-key

### Key options

| Flag | Purpose |
|------|---------|
| `--source` | `osm` (default, no key) or `google` (needs key) |
| `--cities` | Cities to search (default: NoVA set in `DEFAULT_CITIES`) |
| `--max-per-city` | Results per city |
| `--require-family-signal` | Keep only sites with a positive family score |
| `--chain-threshold` | # of locations before a name is treated as a chain (default 3) |
| `--no-website-check` | Skip scraping for speed |
| `--demo` | Preview output with sample data; no network |
| `--out` | CSV path (a `.json` sibling is also written) |

## Output columns

`name, category, city, phone, email, website, source, address, family_score,
family_signals, rating, review_count`

`source` is the OpenStreetMap listing URL (or Google Maps URL with
`--source google`). `family_signals` lists the matched phrases so you can see
*why* a place scored as family-owned. `rating` / `review_count` are populated
only with the Google source.

## Tuning

Edit the lists at the top of `find_family_restaurants.py`:
- `CHAIN_BLOCKLIST` — add chains to exclude.
- `FAMILY_SIGNALS` / `CHAIN_SIGNALS` — adjust the scoring phrases/weights.
- `DEFAULT_CITIES` — change the default geography.

## Notes / good citizenship

- OpenStreetMap data is © OpenStreetMap contributors, under the
  [ODbL](https://www.openstreetmap.org/copyright). Nominatim has a usage policy
  (≤ 1 request/second, descriptive User-Agent) — the tool already throttles to
  comply. For very heavy use, run your own Nominatim/Overpass instance.
- Website scraping is light (a handful of pages per site, with delays). Keep it
  that way.
- This collects **public business** contact info for B2B outreach. Honor
  CAN-SPAM and any do-not-contact requests when you reach out.
