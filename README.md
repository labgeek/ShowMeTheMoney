# statisticallydoomed.py

What a Powerball or Mega Millions jackpot would actually leave you after federal and
state tax — for your state, not the number on the billboard. Opens as a desktop window,
and runs windowless for scheduled email alerts.

- Every jurisdiction usamega.com lists, from one fetch: 49 for Powerball, 47 for Mega
  Millions. Switching between them is instant.
- Lump sum or annuity, split any number of ways, ranked against every other jurisdiction.
- Emails you when a jackpot clears a threshold you set, with enough detail to decide from
  the notification alone.
- Records every check to SQLite, so it can tell you whether this jackpot is actually big
  or just another Tuesday.

Figures come from the [usamega.com](https://www.usamega.com) jackpot analysis pages,
which ask to be credited when their numbers are reused.

## Snapshot of the application
<img width="1001" height="512" alt="image" src="https://github.com/user-attachments/assets/bed34044-cae1-48d8-8356-bb353d4db71e" />


## Install

Python 3.11+, two packages, plus the browser Playwright drives:

```
pip install playwright PySide6
playwright install chromium
```

`playwright install chromium` is not optional — the site returns 403 to a plain HTTP
request, so a real browser is required. It runs headless; nothing appears on screen while
it fetches.

## Quick start

```
python statisticallydoomed.py
```

Press **Check payouts**. A few seconds later you have both games' take-home for Virginia,
and a dropdown holding every other jurisdiction.

For an alert instead of a window — mails you if Virginia's lump sum clears $100M:

```
python statisticallydoomed.py --alert 100000000
```

That one needs the credentials below. Everything else the app does is in
[USAGE.md](USAGE.md).

## Configuration

Optional, all read from a `.env` file beside the script. Anything already set in the real
environment wins over the file.

| Variable | Default | What it does |
|---|---|---|
| `LOTTERY_PLACE` | `Florida` | Jurisdiction used when `--place` is omitted, and by a window with nothing recorded yet. |
| `LOTTERY_GMAIL` | — | Gmail account that sends alerts. |
| `LOTTERY_GMAIL_APP_PASSWORD` | — | A Gmail [app password](https://myaccount.google.com/apppasswords), 16 characters. Requires 2-Step Verification; your normal password will not work. |
| `LOTTERY_SMS_TO` | — | Where alerts go: an email address, or a carrier gateway like `5551234567@vtext.com` for a text. |

The three alert variables are only needed for `--alert`. The window and `--history` work
without them.

`.env` holds a live credential. It is in `.gitignore`, and belongs out of any repo,
backup, or screen share.

## Files

| Path | What it is |
|---|---|
| `statisticallydoomed.py` | The whole app: window, scraper, storage, CLI |
| `test_statisticallydoomed.py` | Offline self-check — `python test_statisticallydoomed.py` |
| `statisticallydoomed.db` | Recorded checks, created on first use |
| `.env` | Your configuration and credentials |

## Limits

- Every figure assumes a single winner filing as Single. The site offers other filing
  statuses; this reads the default.
- Figures depend on usamega.com's page wording. If that changes, the app reports
  `Unavailable` rather than showing a wrong number.
- Windows-oriented: preferences live in the registry, and the scheduling notes in
  [USAGE.md](USAGE.md) assume Task Scheduler. The rest is portable.

## License

MIT License

Copyright (c) 2026 JD Durick

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
