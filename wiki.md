# Website Monitor — Wiki

Full documentation. For a quick overview see [README.md](README.md).

- [Installation](#installation)
- [First target, start to finish](#first-target-start-to-finish)
- [The element picker](#the-element-picker)
- [Stopping false alarms](#stopping-false-alarms)
- [Notifications](#notifications)
- [Configuration reference](#configuration-reference)
- [How it stays cheap](#how-it-stays-cheap)
- [Architecture](#architecture)
- [Extending it](#extending-it)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

---

## Installation

Requires **Python 3.11–3.14**. Built and tested primarily on 3.14 / Windows 11;
CI also covers 3.11–3.14 on Linux.

```bash
git clone https://github.com/Prooxie/WebsiteMonitor.git
cd WebsiteMonitor
```

With [uv](https://github.com/astral-sh/uv) — recommended, much faster because
PySide6 is a ~150 MB download:

```bash
uv sync --all-extras
uv run python -m webmonitor
```

With stock tooling:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m webmonitor
```

### Where state lives

| What | Windows | Linux | macOS |
|---|---|---|---|
| Settings, database, logs | `%LOCALAPPDATA%\WebsiteMonitor` | `$XDG_DATA_HOME/WebsiteMonitor` | `~/Library/Application Support/WebsiteMonitor` |

Set `WM_DATA_DIR` to relocate all of it — useful for a portable install or for
keeping test runs away from real data.

Credentials do **not** live there; see [Notifications](#notifications).

---

## First target, start to finish

1. **Inspector** tab → type a URL → <kbd>Enter</kbd>.
2. Click **Pick element**. The cursor becomes a crosshair.
3. Hover. The element under the cursor is outlined in blue with a badge showing
   its tag and pixel size.
4. Click it. If you grabbed a child when you wanted its container, press
   <kbd>↑</kbd> to walk up the tree, <kbd>↓</kbd> to go back down, <kbd>Enter</kbd>
   to confirm, <kbd>Esc</kbd> to cancel.
5. The **New target** dialog opens with the URL and selector filled in. Set the
   interval and save.
6. Click **Start monitoring**.

The first check establishes a **baseline** and deliberately does *not* alert —
otherwise every target you add would immediately email you. The second differing
check is the one that fires.

Closing the window minimises to the tray. Right-click the tray icon → **Quit** to
exit properly.

### Target statuses in the sidebar

| Dot | Meaning |
|---|---|
| Grey, "no baseline yet" | Added but not checked once yet |
| Green, "watching" | Baseline captured, nothing has changed |
| Green, "changed 4m ago" | A change was detected recently |
| Grey, "paused" | `enabled` is off; the scheduler skips it |
| Red, "N failure(s)" | Consecutive errors; retry interval is backing off |

---

## The element picker

### Why not just record the DOM path

A browser's "Copy selector" typically gives you something like
`body > div:nth-child(2) > div > div:nth-child(3) > ul > li`. That breaks the
first time anyone inserts a wrapper div. The picker aims for the shortest
selector that is both **unique** and **unlikely to churn**.

### Strategy, in order

1. **A hand-written `id`** — `#main-content`
2. **A deliberate attribute** — `[data-testid="cart-total"]`, also `data-test`,
   `data-qa`, `data-id`, `itemprop`, `name`, `role`
3. **A short path of `tag.class` segments**, extended toward the root only until
   unique, and stopped early if an ancestor has a usable `id` — so you get
   `#main-content > ul.items` rather than a path down from `<body>`.

`:nth-of-type(n)` is added only when siblings would otherwise be ambiguous.

### What it refuses to use

Class names that look generated are rejected as selector ingredients:

| Pattern | Example | Why |
|---|---|---|
| CSS-module hashes | `_a1b2c3d4` | Regenerated on every build |
| styled-components | `sc-a1b2c3` | Regenerated on every build |
| emotion | `css-9xyz88` | Regenerated on every build |
| State classes | `is-active`, `has-error`, `selected` | Change as you interact |
| Framework hooks | `js-toggle`, `ng-scope`, `v-abc` | Behavioural, not structural |
| Long digit runs | `item-84726` | Usually a record id |

Generated `id`s are refused too: `:r3:` (React `useId`), `ember42`, anything
with a long hex run.

### Verifying a selector later

Right-click a target → **Open in Inspector**. The page loads and the saved
selector is highlighted, with a match count next to it. `1 match` is what you
want; `no match` means the page layout moved and the target needs re-picking.

---

## Stopping false alarms

This is where a change monitor lives or dies. A real page changes on almost every
request — rotating adverts, CSRF tokens, "1,247 views", a footer timestamp.

Options live in the target dialog under **Ignore noise**:

| Option | Default | What it does |
|---|---|---|
| **Compare visible text only** | on | Discards markup entirely. Immune to class-name churn. |
| **Strip scripts, styles and comments** | on | Removes `<script>`, `<style>`, `<noscript>`, `<template>`, `<svg>` and HTML comments. |
| **Collapse whitespace** | on | Every run of whitespace becomes one space. Reindentation stops mattering. |
| **Ignore numbers** | off | Digit runs become `#`. Kills counters, prices, timestamps. |
| **Ignore HTML attributes** | off | Strips attributes. Only applies when text-only is **off**. |
| **Ignore patterns** | — | Your own regexes, separated by `;;`. |

### Worked example

A product page whose stock count you *don't* care about but whose product list
you do:

- Selector: `ul.items`
- Compare visible text only: **on**
- Ignore numbers: **on** — so "3 left" → "# left" and restocking does not alert

Conversely, if the stock count is *exactly* what you want, leave **Ignore
numbers** off and point the selector at `[data-testid="seats-remaining"]`.

### Ignore patterns

Regexes are applied before hashing. Separate several with `;;`:

```
session-[a-f0-9]+;;Updated \d{2}:\d{2};;\bnonce-\w+\b
```

An invalid regex is logged and skipped rather than breaking the check.

> **Editing a target resets its baseline.** Changing the URL, selector or
> normalization rules clears the stored hash, so the next check re-baselines
> instead of reporting a spurious "everything changed".

---

## Notifications

All three are configured under **Settings**, and each has a **Test** button that
sends immediately using the values currently in the form — no need to save first.

Leaving every channel unticked on a target means "use every configured channel".

### Desktop

No setup. Native toast via the tray icon — Action Center on Windows,
Notification Center on macOS, libnotify on Linux.

### Email

| Field | Example |
|---|---|
| Server | `smtp.gmail.com` |
| Port | `587` (STARTTLS) or `465` with **Implicit TLS** ticked |
| Username | usually the same as *From* |
| From | `you@gmail.com` |
| To | `me@example.com, someone@example.com` |

Sends a colour-coded HTML diff plus a plain-text alternative.

> **Gmail and Outlook reject account passwords outright.** You must create an
> app-specific password:
> - Gmail: Google Account → Security → 2-Step Verification → App passwords
> - Outlook: Security → Advanced security options → App passwords

### Telegram

Free, instant, and it reaches your phone — which is what most people actually
want when they say "SMS".

1. Message [@BotFather](https://t.me/BotFather) and run `/newbot`. Copy the token.
2. Send your new bot any message.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read
   `result[0].message.chat.id`.

Paste the token and chat ID into Settings → Telegram.

### Where credentials are stored

In the **OS credential store** via `keyring` — Windows Credential Manager, macOS
Keychain, or SecretService on Linux. `settings.json` never contains a secret and
is safe to sync or commit.

For headless, container or CI use, environment variables take precedence over the
keyring:

```bash
WM_SMTP_PASSWORD=...
WM_TELEGRAM_TOKEN=...
```

If no keyring backend exists, the settings dialog says so and tells you to use
the environment variables instead — it will not silently drop your password.

> **Historical note.** Credentials used to be run through `bcrypt`. That could
> never have worked: bcrypt is a deliberately one-way hash, so the stored value
> could not be turned back into the plaintext `SMTP.login` requires. Hashing is
> for *verifying* a password someone types at you; *storing* one you must later
> present to a third party needs reversible storage guarded by the OS.

### Alert suppression

A page that flaps between two states would otherwise produce an unbounded stream
of alerts. A **60-second per-target cooldown** collapses repeats. It is per
target, so one noisy page never mutes a different one, and **Test** always
bypasses it.

---

## Configuration reference

Every setting is overridable by environment variable, prefix `WM_`, `__` for
nesting. **Environment variables beat `settings.json`**, so a deliberate override
is never shadowed by a stale config file.

### Network

| Setting | Env | Default | Notes |
|---|---|---|---|
| `network.timeout` | `WM_NETWORK__TIMEOUT` | `30.0` | Per-request seconds |
| `network.max_retries` | `WM_NETWORK__MAX_RETRIES` | `3` | Exponential backoff + jitter |
| `network.proxy` | `WM_NETWORK__PROXY` | `""` | Global; targets may override |
| `network.user_agent` | `WM_NETWORK__USER_AGENT` | Chrome-like | Some sites vary markup by agent |
| `network.verify_tls` | `WM_NETWORK__VERIFY_TLS` | `true` | Disable only for known self-signed hosts |
| `network.http2` | `WM_NETWORK__HTTP2` | `true` | Multiplexes checks of one host |
| `network.max_connections` | `WM_NETWORK__MAX_CONNECTIONS` | `20` | Shared pool ceiling |
| `network.max_response_bytes` | `WM_NETWORK__MAX_RESPONSE_BYTES` | `10485760` | Hard cap on retained body |

### Scheduler

| Setting | Env | Default | Notes |
|---|---|---|---|
| `scheduler.max_concurrent_checks` | `WM_SCHEDULER__MAX_CONCURRENT_CHECKS` | `8` | In-flight checks |
| `scheduler.adaptive_intervals` | `WM_SCHEDULER__ADAPTIVE_INTERVALS` | `true` | See below |
| `scheduler.backoff_factor` | `WM_SCHEDULER__BACKOFF_FACTOR` | `1.5` | Interval growth per quiet check |
| `scheduler.error_backoff_base` | `WM_SCHEDULER__ERROR_BACKOFF_BASE` | `60.0` | First retry delay after failure |
| `scheduler.jitter_ratio` | `WM_SCHEDULER__JITTER_RATIO` | `0.1` | De-synchronises targets |
| `scheduler.snapshot_history` | `WM_SCHEDULER__SNAPSHOT_HISTORY` | `20` | Snapshots kept per target |
| `scheduler.check_on_start` | `WM_SCHEDULER__CHECK_ON_START` | `true` | Poll everything on start |

### Application

| Setting | Env | Default |
|---|---|---|
| `theme` | `WM_THEME` | `dark` (`light`, `system`) |
| `minimize_to_tray` | `WM_MINIMIZE_TO_TRAY` | `true` |
| `start_monitoring_on_launch` | `WM_START_MONITORING_ON_LAUNCH` | `false` |
| `log_level` | `WM_LOG_LEVEL` | `INFO` |
| — | `WM_DATA_DIR` | platform default |

A corrupt or unreadable `settings.json` is logged and ignored rather than
preventing startup.

---

## How it stays cheap

### Don't download anything

Every poll replays the stored `ETag` / `Last-Modified` as `If-None-Match` /
`If-Modified-Since`. A server that supports them answers **`304 Not Modified`
with an empty body**, so an unchanged page costs one round trip and a few hundred
header bytes instead of the full document. The status bar reports the cumulative
saving.

The one subtlety: validators are only sent when a matching snapshot is actually
stored. A `304` with nothing to compare against would leave the monitor holding
no content at all.

### Adaptive intervals

A target's interval grows by `backoff_factor` after each quiet check, up to
`interval_max`, and snaps straight back to `interval_min` the moment something
changes. A page that changes twice a year gets polled hourly; one that just
changed gets watched closely again. Jitter is folded into each new interval so a
dozen targets added in the same minute never stampede together.

### Static first, browser last

| Mode | Cost | When |
|---|---|---|
| `static` | ~20 ms | Server-rendered HTML |
| `browser` | 1–3 s | Content built by client-side JavaScript |
| `auto` (default) | ~20 ms, sometimes 1–3 s | Tries static; falls back to the browser only if the selector matches nothing |

When an `auto` target does need the browser, it is **promoted to `browser`
permanently**, so the doomed static fetch happens once rather than on every cycle.

### The rest

- **Lexbor parsing** via `selectolax` — a C HTML5 parser, roughly an order of
  magnitude faster than BeautifulSoup with `html.parser`.
- **Hash, don't diff.** Detection is a BLAKE2b comparison of 32 bytes. Diffs are
  computed only *after* a change is known to exist, purely to describe it.
- **Pooled HTTP/2 connections**, one client per proxy, reused process-wide, so
  TCP and TLS handshakes amortise across checks.
- **Streamed, capped bodies**, so a watched URL that starts returning a gigabyte
  cannot exhaust memory.
- **zlib-compressed snapshots** in SQLite (WAL mode), pruned to a rolling history.

---

## Architecture

```
webmonitor/
├─ core/          fetch → extract → diff → schedule   (no Qt, headless-testable)
│  ├─ fetcher.py     pooled httpx, conditional GETs, retries
│  ├─ extractor.py   Lexbor CSS selection + normalization
│  ├─ differ.py      BLAKE2b hashing, text and HTML diffs
│  └─ engine.py      the polling loop and adaptive scheduling
├─ notifiers/     pluggable channels behind one interface
├─ gui/           PySide6 + QtWebEngine
├─ models.py      plain dataclasses shared by every layer
├─ storage.py     SQLite (WAL, compressed snapshots)
└─ secrets_store.py   OS keyring access
```

The dependency arrow runs one way: **`gui → notifiers → core`**. Nothing in
`core` imports Qt, which is what lets the engine run on a worker thread, be
tested without a display, and later back a console-only build.

### Threading

The engine owns an **asyncio loop on a plain background thread**. It reaches the
GUI by emitting Qt signals: emitting from a non-owning thread to a receiver that
lives on the GUI thread produces a queued connection, so slots run on the GUI
thread with no locking of our own.

(The pre-0.2 design called its blocking `while` loop directly from a button
handler, which froze the window the moment you clicked Start.)

### Browser rendering across the boundary

`core` defines a `Renderer` protocol — one `async def render(url) -> str`. The
GUI satisfies it with an offscreen `QWebEnginePage`. QtWebEngine is strictly
single-threaded and lives on the GUI thread, so the two are bridged with a queued
signal outbound and `loop.call_soon_threadsafe` inbound. Renders are serialised
with an `asyncio.Lock`, since a second concurrent Chromium page would multiply
memory for no throughput gain on what is a network-bound job.

The inspector and the renderer **share the default web profile** deliberately: a
site you log into in the Inspector stays logged in when the engine renders it,
which is what makes watching pages behind a login work at all.

---

## Extending it

### Adding a notification channel

Implement `Notifier` and register it. That is the entire contract:

```python
from webmonitor.notifiers.base import Notifier, NotifierError


class DiscordNotifier(Notifier):
    """Posts a change summary to a Discord webhook."""

    name = "discord"
    label = "Discord"

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    @property
    def is_configured(self) -> bool:
        return bool(self._url)

    async def send(self, event) -> None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(self._url, json={"content": event.summary})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NotifierError(str(exc)) from exc
```

Register it in `build_dispatcher()` in [app.py](src/webmonitor/app.py). It then
appears automatically in Settings and in each target's channel list.

`SecretRef` already reserves `TWILIO_AUTH_TOKEN` if you want real SMS.

### Running the tests

```bash
uv run pytest -q
uvx ruff check src tests
uvx ruff format --check src tests
```

113 tests, none of which need a display. `tests/test_integration.py` stands up a
real HTTP server on a real socket and drives the actual scheduler loop — mocked
transports cannot catch a broken `ETag` round trip or a scheduler that never
wakes up.

---

## Troubleshooting

**"Selector matched nothing"**
The page layout changed, or the content is rendered by JavaScript. Open the
target in the Inspector to see. If the element is visibly there but the static
fetch misses it, set **Fetch mode** to `Browser`.

**Email fails with an authentication error**
Almost always an app-specific password issue — see [Email](#email). The error
message from the server is surfaced verbatim in the settings dialog.

**Telegram says "chat not found"**
You must send your bot a message *first*; a bot cannot open a conversation. Then
re-read the chat ID from `getUpdates`.

**"No usable keyring backend"**
Common on headless Linux. Use `WM_SMTP_PASSWORD` / `WM_TELEGRAM_TOKEN` instead.

**Too many alerts**
Turn on **Ignore numbers**, confirm **Compare visible text only** is on, and
tighten the selector to the smallest element that contains what you care about.

**No alerts when you expected one**
Check the target actually has a baseline (sidebar shows "watching", not "no
baseline yet"), that monitoring is started, and that at least one channel reports
**Ready** in Settings. The Activity tab log shows every check.

**Logs**
`<data dir>/logs/webmonitor.log`, rotating at 5 MB × 5 files. Raise the level to
`DEBUG` in Settings → Advanced.

---

## FAQ

**Can it watch pages behind a login?**
Yes — log in through the Inspector first. The monitoring renderer shares the
browser profile, so the session carries over. It still expires on the site's own
schedule.

**Does it respect `robots.txt`?**
No. It is a user-driven tool checking pages you already visit, at intervals you
set. Be a reasonable neighbour: the default 5-minute floor exists for a reason,
and adaptive intervals will widen it further on their own.

**Why no SMS?**
Telegram covers the "reaches my phone" case for free and without a carrier
account. The `Notifier` interface and a reserved `SecretRef.TWILIO_AUTH_TOKEN`
are there if you want to add it.

**Can it run headless / as a service?**
Not yet — the GUI is the only front end. The `core` package is deliberately
Qt-free so a console entry point is a small addition, minus browser rendering.

**How much disk does it use?**
Snapshots are zlib-compressed and capped at `snapshot_history` per target
(default 20). A typical target costs a few hundred KB.
