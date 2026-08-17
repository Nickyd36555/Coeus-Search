# fbmarket — Facebook Marketplace car monitor

Watches Facebook Marketplace searches and pings your phone the moment a car
matching **exactly** what you asked for gets listed.

- **Precise matching.** Marketplace's own filters are loose and it happily
  shows you results that ignore them. Every listing is re-checked locally
  against your price, year, mileage and keyword rules — including things
  Marketplace has no filter for at all, like excluding salvage titles.
- **Pings once, never twice.** Seen listings are recorded in SQLite, keyed per
  search. You get one alert per car.
- **Your choice of alert.** ntfy (phone push), Telegram, Discord, email,
  generic webhook, or desktop notification. Enable as many as you want.
- **No first-run spam.** The first cycle quietly records what's already listed,
  then alerts only on what appears *after* that.
- **Price-drop alerts** when a car you've already been shown gets cheaper.

---

## Setup

```bash
git clone https://github.com/nickyd36555/coeus-search.git
cd coeus-search

python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium        # one-time browser download

cp config.example.yaml config.yaml
```

### Then either use the web UI…

```bash
pip install -e ".[web]"
fbmarket web            # opens on http://127.0.0.1:8765
```

Fill in a form to say what you're hunting — car, location, price range, years,
mileage, words to reject. **Test** runs the search live and shows you every
listing on the page with a verdict on each: *would alert you*, or *skipped —
price 26,900 > 25,000*. That makes tuning filters a ten-second loop instead of
guesswork. **Start monitoring** runs the watcher in the background.

### …or edit the config directly

See [Configuring your searches](#configuring-your-searches).

```bash
fbmarket check          # validates config, prints the exact URLs it will watch
fbmarket test-notify    # sends a fake listing so you know alerts reach you
fbmarket once           # first run: records what's listed now, stays quiet
fbmarket watch          # runs forever, pings you on anything new
```

Both edit the same `config.yaml`, so you can mix and match. One caveat: saving
from the UI rewrites the file and **drops the explanatory comments** (a `.bak`
copy is kept beside it).

### Getting alerts on your phone (fastest path)

Install the [ntfy](https://ntfy.sh) app, pick a topic name nobody could guess,
subscribe to it in the app, and put the same name in `config.yaml`:

```yaml
notify:
  channels:
    - type: ntfy
      enabled: true
      topic: nick-cars-8f3kd92xq     # anyone who knows this can read your alerts
      priority: high
```

Then `fbmarket test-notify` should buzz your phone. Tapping the notification
opens the listing.

### If Facebook shows a login wall

Marketplace results are usually visible logged out, but Facebook sometimes
demands a login. Do this once:

```bash
fbmarket login
```

A browser opens; log in normally (2FA included), then press Enter. The session
is saved to `~/.config/fbmarket/state.json` and reused from then on. Nothing is
sent anywhere — it's a cookie file on your own machine.

---

## Configuring your searches

Each block under `searches:` is one car you're hunting.

```yaml
searches:
  - name: Tacoma under 25k          # must be unique — it keys the "already seen" DB
    location: saltlakecity
    query: Toyota Tacoma
    min_price: 8000
    max_price: 25000
    min_year: 2014
    max_mileage: 150000
    radius_km: 100
    transmission: automatic
    days_since_listed: 7

    filters:                        # re-checked locally on every listing
      min_price: 8000
      max_price: 25000
      min_year: 2014
      max_mileage: 150000
      include_any: [tacoma]
      exclude: [salvage, rebuilt, parts only, no title, wanted, lease]
```

**Finding your `location`:** open Marketplace in a browser and look at the
address bar. `facebook.com/marketplace/109470862423276/vehicles` → use
`109470862423276`. `facebook.com/marketplace/saltlakecity/...` → use
`saltlakecity`.

**Anything this tool doesn't model:** build the search in your browser, copy the
URL, and paste it. `url:` overrides the generated one.

```yaml
  - name: Miata hunt
    url: https://www.facebook.com/marketplace/slc/search?query=miata&minPrice=3000
    filters:
      max_price: 12000
      exclude: [salvage, parts]
```

### Filter reference

| Key | Effect |
|---|---|
| `min_price` / `max_price` | Price bounds (whole dollars) |
| `min_year` / `max_year` | Model year, read from the title |
| `min_mileage` / `max_mileage` | Mileage in miles (km listings are converted) |
| `include_any` | At least one of these words must appear |
| `include_all` | Every one of these words must appear |
| `exclude` | Reject if any of these words appear |
| `title_regex` | Title must match this regular expression |
| `skip_sold` | Drop sold/pending listings (default `true`) |
| `require_price` | Drop listings with no price (default `false`) |

Keyword matching is case-insensitive and covers the title, subtitles and
location. A listing missing a mileage value is **not** rejected by a mileage
filter — Marketplace often omits it from the results grid, and dropping those
would hide good cars.

### Search-level settings

| Key | Default | Effect |
|---|---|---|
| `poll.interval_seconds` | `600` | How often to check |
| `poll.jitter_seconds` | `120` | Randomizes timing so checks aren't metronomic |
| `notify_on_first_run` | `false` | Alert on everything already listed at startup |
| `notify_price_drops` | `true` | Alert when a seen listing gets cheaper |
| `max_notifications_per_cycle` | `15` | Flood guard |
| `browser.scrolls` | `3` | More scrolls = deeper results, slower cycle |
| `browser.headless` | `true` | Set `false` to watch it work |
| `browser.executable_path` | — | Use an existing Chrome instead of Playwright's |
| `browser.debug_dump_dir` | — | Save page HTML when a search finds nothing |

---

## Running it continuously

The monitor only alerts while it's running. Pick one:

**Linux (systemd)** — edit the paths, then:

```bash
sudo cp deploy/fbmarket.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now fbmarket
journalctl -u fbmarket -f
```

**macOS (launchd)**:

```bash
cp deploy/com.fbmarket.monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.fbmarket.monitor.plist
tail -f /tmp/fbmarket.log
```

**Or just leave `fbmarket watch` running in a terminal.**

---

## Commands

| Command | What it does |
|---|---|
| `fbmarket check` | Validate config, print the exact search URLs |
| `fbmarket test-notify` | Send a sample listing through every channel |
| `fbmarket once` | Run every search a single time |
| `fbmarket once --dry-run` | Print matches to the console instead of notifying |
| `fbmarket watch` | Poll forever |
| `fbmarket login` | Save a Facebook session |
| `fbmarket status` | How many listings each search has recorded |
| `fbmarket reset --search NAME` | Forget seen listings; next run re-alerts |
| `fbmarket web` | Browser UI for managing searches |
| `fbmarket parse page.html` | Run the extractor over a saved page (offline) |

### Running the UI on a server

It binds `127.0.0.1` by default. Exposing it to a network **requires** a token —
the UI can read your searches and drive the scraper, so it refuses to start on a
public interface without one:

```bash
fbmarket web --host 0.0.0.0 --port 8765 --token "$(openssl rand -hex 16)"
```

Better still, leave it on localhost and reach it over an SSH tunnel:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@your-server
```

---

## Secrets

Never put tokens in `config.yaml` directly — use `${VAR}` and keep the values in
`.env` (gitignored):

```yaml
bot_token: ${TELEGRAM_BOT_TOKEN}
```

```bash
# .env
TELEGRAM_BOT_TOKEN=123456:AA...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

`.env` is loaded automatically at startup (from beside `config.yaml` and from
the working directory). Anything already exported in your shell wins over the
file. Both `config.yaml` and `.env` are gitignored.

A Discord webhook URL **is** a credential — anyone holding it can post to that
channel. Keep it in `.env`, never in `config.yaml`, and delete/recreate the
webhook in Discord if it ever leaks. For Gmail, use an
[App Password](https://support.google.com/accounts/answer/185833), not your
account password.

---

## Troubleshooting

**"no listings found"** — set a dump directory and inspect what Facebook
actually returned:

```yaml
browser:
  debug_dump_dir: ./debug
  headless: false        # watch the browser to see what it hits
```

```bash
fbmarket once -v
fbmarket parse debug/tacomas-1234567890.html --search "Tacoma under 25k"
```

`parse` prints every listing found and, for each, whether it passed your filters
and exactly which rule rejected it. That tells you immediately whether the
problem is scraping or filtering.

**Alerts never arrive** — run `fbmarket test-notify`. If that works but real
cars don't, your filters are too strict; check with `fbmarket once --dry-run -v`,
which logs the reject reason for every listing.

**Too many alerts on day one** — that's `notify_on_first_run: true`. Set it to
`false` and `fbmarket reset`.

**Getting rate-limited / blocked** — increase `poll.interval_seconds`. Checking
every 10 minutes is plenty for cars; every 30 seconds will get you throttled.

---

## Notes

This drives a real browser against Marketplace's public search pages, at a
polite polling interval, for personal use. Automated access is against
Facebook's Terms of Service, and Facebook may throttle or block scraping
regardless of interval — treat the tool as best-effort. Because it reads the
page structure, a Facebook redesign can break extraction; the `parse` command
above exists to make that quick to diagnose.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The parsing, filtering, URL-building, dedupe and notification-cycle logic are
covered by tests that run without a browser or network (`tests/`).
