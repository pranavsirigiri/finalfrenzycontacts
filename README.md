# Family-Owned Restaurant Lead Finder

Finds small, **independent / family-owned restaurants** in Northern Virginia (or
any cities you pass) and extracts their public contact info — name, phone,
email, website, address — into CSV + JSON.

It is built specifically for restaurants and actively filters **out** chains and
franchises. **No API key required** by default. It never fabricates data: a
field is blank when no source provides it.

---

## 🚀 Quick start — run the app (no coding needed)

These steps are written for macOS and assume the one-time setup below has
already been done (dependencies installed into a `.venv` folder).

**1. Open the Terminal app**
Press **⌘ (Command) + Spacebar**, type **`Terminal`**, press **Return**.

**2. Copy this ONE line, paste it into Terminal, press Return:**

```bash
cd /Users/pranavsirigiri/frenzycontacttool && ./.venv/bin/streamlit run app.py
```

*(To paste in Terminal: click the window, press **⌘ + V**, then **Return**.)*

**3. Your browser opens the dashboard automatically** at
`http://localhost:8501`. If it doesn't, open a browser and go to
**`localhost:8501`**.

**4. Use the three tabs:**

| Tab | What you do |
|-----|-------------|
| **1 · Find leads** | Pick a **Source**, enter a few cities, click **Run search**. Start with source `demo (sample data)` to try it with no internet. Use `osm` for real free searches (do 1–3 cities at a time), or `google` for the most reliable results (needs a free API key — see below). |
| **2 · Review & send** | See the restaurants in a table. Click **⬇️ Download these leads as a spreadsheet (CSV)** to open them in Excel/Numbers. Edit the email template, **Dry-run preview** to see the exact emails (nothing is sent), then send for real only after configuring a provider + ticking the confirm box. |
| **3 · Sent & suppression** | A log of every email, a do-not-contact list, and **Data backup & restore** — download your whole database as one file, or restore a previous backup. |

**5. To stop the app:** go back to Terminal and press **Control + C**.

**6. Next time:** just repeat steps 1–3. Same one line every time.

> **Getting the latest updates:** if the code has changed, run
> `cd /Users/pranavsirigiri/frenzycontacttool && git pull` first, then
> restart the app. If the running app shows **"Source file changed → Rerun"**
> in the top-right, click **Rerun**.

### First-time setup (only needed once, on a new computer)

```bash
cd /Users/pranavsirigiri/frenzycontacttool
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Then use the Quick start steps above.

### Optional: reliable searches with a Google Places key

The free OpenStreetMap servers occasionally rate-limit (HTTP 429/504). For
rock-solid results, get a free Google Places API key
([console.cloud.google.com](https://console.cloud.google.com/): create a
project → enable **Places API** → **Credentials → Create API key**), then in
the **Find leads** tab set **Source** to `google` and paste the key. A generous
monthly free tier means most small users pay nothing.

---

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

## Dashboard (find → review → email)

A Streamlit dashboard wraps the finder with a persistent database and an email
sender, so you can discover leads, review them, and send outreach from one UI.

```bash
streamlit run app.py
```

Three tabs:

1. **Find leads** — run discovery (OSM / Google / demo) into a local SQLite
   database (`outreach.db`). Re-running merges results and de-dupes; it never
   double-counts a restaurant or overwrites contact info you already have.
2. **Review & send** — filter leads, edit the email template (placeholders:
   `{name}`, `{city}`, `{family_signal}`, `{website}`, `{phone}`, `{address}`),
   preview against a real lead, tick the leads to contact, then **dry-run** to
   see the exact rendered emails. Sending for real requires configuring a
   provider in the sidebar *and* ticking a confirmation box.
3. **Sent & suppression** — an audit log of every email (dry-run and real) plus
   a do-not-contact list. Suppressed addresses are never emailed again.

### Sending safely

- **Dry-run is the default.** Nothing is sent until you pick the SMTP provider
  in the sidebar and confirm. Always dry-run first and read the output.
- Every email gets a **CAN-SPAM footer** (physical address + an unsubscribe
  line) built from the sidebar "Sender identity" fields — fill these in.
- A **daily cap** and per-send **throttle** (sidebar) protect deliverability.
  For real cold outreach use a dedicated sending domain with SPF/DKIM/DMARC —
  not a personal Gmail. SMTP works for testing; a provider like Resend or SES
  is the next step (add one alongside `SMTPProvider` in `sender.py`).
- The suppression list and sent-log persist in `outreach.db`, so "already
  contacted" and unsubscribes are honored across runs.

New files: `db.py` (SQLite persistence), `sender.py` (templating + sending),
`app.py` (the dashboard). The original CLI below still works unchanged.

## CLI usage

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
