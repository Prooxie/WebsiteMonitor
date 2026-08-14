# Website Monitor

Watch a *specific element* on a web page — not the whole page — and get told the
moment it changes.

Point the built-in browser at a page, click **Pick element**, hover until the
thing you care about lights up, click it. The app works out a CSS selector,
stores a baseline, and from then on checks only that fragment. When it changes
you get an email, a desktop toast, a Telegram message, or all three.

[![CI](https://github.com/Prooxie/WebsiteMonitor/actions/workflows/ci.yml/badge.svg)](https://github.com/Prooxie/WebsiteMonitor/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%E2%80%933.14-blue)](https://www.python.org/)
[![Qt](https://img.shields.io/badge/GUI-PySide6%20%2B%20QtWebEngine-41cd52)](https://doc.qt.io/qtforpython-6/)
[![License](https://img.shields.io/badge/license-GPL--3.0-orange)](LICENSE)

📖 **[Full documentation in the wiki](wiki.md)** — picker internals, configuration
reference, extending the notifier interface, troubleshooting.

---

## Why element-level watching

Whole-page monitors cry wolf. A page has rotating adverts, CSRF tokens, "1,247
views" counters and a timestamp in the footer; diff the raw HTML and it changes
every single poll. This watches one subtree, compares its *rendered text* by
default, and lets you strip out the rest:

| Noise source | What handles it |
|---|---|
| CSS class churn from a framework | **Compare text only** (default) |
| View counters, prices, timestamps | **Ignore numbers** |
| Analytics and inline styles | **Strip scripts/styles/comments** (default) |
| Reindented markup | **Collapse whitespace** (default) |
| Anything site-specific | **Ignore patterns** — your own regexes |

---

## Install

Requires Python 3.11–3.14 (3.14 is what this was built and tested on).

```bash
git clone https://github.com/Prooxie/WebsiteMonitor.git && cd WebsiteMonitor
```

With [uv](https://github.com/astral-sh/uv) — recommended, since PySide6 is a
~150 MB download:

```bash
uv sync --all-extras && uv run python -m webmonitor
```

Or with stock tooling:

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m webmonitor
```

The first launch creates `settings.json` and `monitor.db` under
`%LOCALAPPDATA%\WebsiteMonitor` (or the XDG/Application Support equivalent). Set
`WM_DATA_DIR` to put them somewhere else.

---

## Using it

1. **Inspector tab** → type a URL → Enter.
2. **Pick element** → hover, then click. `Arrow Up` selects the parent element,
   `Enter` confirms, `Escape` cancels.
3. The target dialog opens pre-filled. Set an interval and save.
4. **Start monitoring**.

Closing the window keeps it running in the tray. Right-click the tray icon to
quit for real.

### The generated selector

The picker does not emit the brittle 12-level `div > div > div` path browsers
often give you. It tries, in order:

1. A hand-written `id` — `#main-content`
2. A deliberate attribute — `[data-testid="cart-total"]`
3. A short path of tag + *stable* class segments, extended toward the root only
   until it is unique, and anchored on an `id` ancestor if one is available.

Classes that look generated are refused as selector ingredients: CSS-module
hashes, `styled-components`/`emotion` names (`sc-a1b2c3`, `css-9xyz88`), state
classes (`is-active`, `has-error`), and anything with a long digit run. Same for
React-style generated ids (`:r3:`, `ember42`).

---

## Notifications

Configured under **Settings**, each with a **Test** button.

| Channel | Setup | Notes |
|---|---|---|
| **Desktop** | none | Native toast via the tray icon. |
| **Email** | SMTP host, port, from/to, password | Sends a colour-coded HTML diff. |
| **Telegram** | Bot token + chat ID | Free, instant, reaches your phone. |

**Email with Gmail or Outlook needs an app-specific password**, not your account
password — both providers reject the latter outright.

**Telegram setup:** message [@BotFather](https://t.me/BotFather), run `/newbot`,
copy the token. Then send your new bot any message and open
`https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat ID.

### Credentials are not in the config file

Passwords and tokens go to the OS credential store (Windows Credential Manager,
macOS Keychain, SecretService) via `keyring`. `settings.json` never contains a
secret and is safe to sync or commit.

For headless or CI use, environment variables take precedence over the keyring:

```bash
WM_SMTP_PASSWORD=...  WM_TELEGRAM_TOKEN=...
```

> **Note on the previous version.** Credentials used to be run through `bcrypt`.
> That could not work: bcrypt is a deliberately one-way hash, so the stored value
> could never be turned back into the plaintext `SMTP.login` needs. Hashing is
> for *verifying* a password someone types at you; *storing* a password you must
> later present to a third party needs reversible storage guarded by the OS.

### Adding a channel

Implement `Notifier` (`name`, `label`, `is_configured`, `send`) and register it
in `build_dispatcher()`. That is the whole contract — SMS via Twilio, a Discord
or Slack webhook, or ntfy are each about thirty lines. `SecretRef` already
reserves a slot for a Twilio token.

---

## How it stays cheap

The single biggest saving is **not downloading anything**. Every poll replays the
stored `ETag`/`Last-Modified`; a server that supports them answers `304 Not
Modified` with an empty body, so an unchanged page costs one round trip and a few
hundred header bytes. The status bar shows how much this has saved you.

On top of that:

- **Adaptive intervals** — a target's interval grows while the page is quiet and
  snaps back to the minimum the instant something changes. A page that changes
  twice a year gets polled hourly; one that just changed gets watched closely.
- **Static first, browser last** — plain HTTP costs ~20 ms. Rendering in Chromium
  costs 1–3 s. `Auto` mode tries HTTP, and only falls back to the browser if the
  selector matches nothing (the signature of a client-rendered page). It then
  *promotes* the target permanently, so the doomed static fetch happens once.
- **Lexbor parsing** via `selectolax` — a C HTML5 parser, roughly an order of
  magnitude faster than BeautifulSoup with `html.parser`.
- **Hash, don't diff** — detection is a BLAKE2b comparison of 32 bytes. Diffs are
  computed only *after* a change is known to exist, purely to describe it.
- **Pooled HTTP/2 connections**, one client per proxy, reused process-wide.
- **Jittered scheduling** so many targets never stampede together.
- **Compressed snapshots** in SQLite (WAL mode), pruned to a rolling history.

---

## Architecture

```
webmonitor/
├─ core/          fetch → extract → diff → schedule   (no Qt, headless-testable)
├─ notifiers/     pluggable channels behind one interface
├─ gui/           PySide6 + QtWebEngine
├─ models.py      plain dataclasses shared by every layer
└─ storage.py     SQLite (WAL, compressed snapshots)
```

The dependency arrow runs one way: `gui → notifiers → core`. Nothing in `core`
imports Qt, which is what lets the engine run on a worker thread, be tested
without a display, and later back a console-only build.

The engine owns an asyncio loop on a background thread and reaches the GUI
through queued Qt signals. (The previous version called its blocking `while` loop
directly from a button handler, which froze the window on click.)

Browser rendering crosses that boundary through a `Renderer` protocol, satisfied
by a QtWebEngine page in the GUI layer — so `core` asks for a render without ever
knowing Qt exists.

---

## Development

```bash
uv run pytest -q                        # 113 tests, no display needed
uvx ruff check src tests
uvx ruff format --check src tests
```

The suite includes end-to-end tests that stand up a real HTTP server on a real
socket and drive the actual scheduler loop, so a broken `ETag` round trip or a
scheduler that never wakes up gets caught.

CI runs lint plus the full suite on Python 3.11–3.14 (Linux) and 3.14 (Windows),
then builds and metadata-checks the distributions. Pushing a `vX.Y.Z` tag runs
the same gates and publishes a GitHub release with the built wheel and sdist —
the release workflow refuses to run if the tag does not match the version in
`pyproject.toml`.

### Configuration

Every setting is overridable by environment variable with a `WM_` prefix and `__`
for nesting — these beat `settings.json`:

```bash
WM_SMTP__HOST=smtp.example.com
WM_NETWORK__TIMEOUT=60
WM_SCHEDULER__MAX_CONCURRENT_CHECKS=16
WM_DATA_DIR=D:\monitor-data
```

---

## Limitations

- Login-protected pages work by logging in through the Inspector first — the
  monitoring renderer shares the browser profile, so the session carries over.
  Sessions still expire on the site's own schedule.
- Only the first match is highlighted when previewing a multi-match selector,
  though all matches are concatenated when comparing.
- `Ignore HTML attributes` is regex-based and applies only when you have turned
  *off* text-only comparison.
- SMS is not implemented; Telegram covers the "reaches my phone" case for free.
  The interface is there if you want to add Twilio.
- No headless/service mode yet. `core` is deliberately Qt-free so a console entry
  point is a small addition, minus browser rendering.

## Licence

GPL-3.0 — see [LICENSE](LICENSE).
