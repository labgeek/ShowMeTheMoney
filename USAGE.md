# Usage

## The window

```
python statisticallydoomed.py
```

Controls sit in two strips, divided by what they cost:

**Fetch** — the game checkboxes and **Check payouts** (or Ctrl+R). The only thing that
touches the network. A check takes a few seconds per game.

**View** — jurisdiction, `Lump sum` / `Annuity`, and `Split N ways`. None of these
refetch: one page load carries every jurisdiction, so switching is instant. The strip
stays disabled until there is something to view.

Each game gets a row: the jackpot and draw date it came from, the take-home figure, and
where that jurisdiction ranks against all the others. Splitting divides both the headline
figure and the per-year annuity figure. **Copy results** puts the visible figures on the
clipboard.

Jurisdiction and payout mode are remembered between launches, so the window opens where
you left it — with last check's figures already on screen and no network call.

### Reading the header and the marker

| Header | Marker | Means |
|---|---|---|
| `Checked at 4:31 PM` | green | Fetched just now |
| `Last checked Sep 12, 4:31 PM` | grey | Restored from the database, not refetched |
| `Could not refresh, last checked …` | grey | The refresh failed; these are the last good figures |
| `Nothing came back` | — | The check failed with nothing stored to fall back on |

A game that fails shows `Unavailable` in its own row without affecting the other. A
jurisdiction one game doesn't serve shows `Not sold here` — Puerto Rico and United
Kingdom are Powerball-only.

## Command line

```
python statisticallydoomed.py [--alert AMOUNT | --history [N]]
                              [--place PLACE] [--mode {Lump sum,Annuity}]
                              [--game {Powerball,Mega Millions}] [--dry-run]
```

With neither `--alert` nor `--history`, it opens the window.

| Flag | Default | Notes |
|---|---|---|
| `--alert AMOUNT` | — | Check, and email if take-home reaches this. Whole dollars, no commas. |
| `--history [N]` | 10 when bare | Print the last N recorded checks instead of fetching. |
| `--place PLACE` | `LOTTERY_PLACE`, else Virginia | Spelled as usamega.com spells it: `New York`, `Puerto Rico`, `United Kingdom`. |
| `--mode` | `Lump sum` | `Annuity` uses the 30-payment total. Quote it: `--mode "Lump sum"`. |
| `--game` | both | Repeat for more than one. Quote it: `--game "Mega Millions"`. |
| `--dry-run` | off | With `--alert`: print the subject and body, send nothing. |

### What an alert contains

The body is HTML with a plain-text alternative, so it stays
readable in a text-only client or an SMS gateway. Per game:

| Row | |
|---|---|
| Take-home | The figure that cleared your threshold, after federal and state tax |
| Draw | Date of the drawing it applies to |
| Jackpot | Advertised annuity and cash value |
| The other option | Annuity total and per-year figure, or the lump sum — whichever you didn't alert on |
| Rank | Where your jurisdiction sits among all of them |
| Best anywhere | Top jurisdiction and how much more it keeps; omitted when yours is top |
| Record | Whether this beats, ties, or trails the best you have ever recorded, with the date |

## Common tasks

**See what another state keeps.** No refetch needed in the window — just change the
dropdown. From the command line:

```
python statisticallydoomed.py --history 5 --place California
python statisticallydoomed.py --alert 1 --place "New York" --dry-run
```

**Preview an alert without sending.** `--alert 1` always clears, so it prints a full
message every time:

```
python statisticallydoomed.py --alert 1 --dry-run
```

**Schedule alerts.** Drawings are Powerball Mon/Wed/Sat and Mega Millions Tue/Fri, both
late evening, with ticket sales closing hours earlier. 6:00 PM on each game's draw days
leaves time to act:

```powershell
schtasks /create /tn "Lottery - Powerball" /sc weekly /d MON,WED,SAT /st 18:00 /f /tr '"C:\Program Files\Python313\pythonw.exe" "C:\data\projects\USAMega\statisticallydoomed.py" --alert 100000000 --game Powerball'

schtasks /create /tn "Lottery - Mega Millions" /sc weekly /d TUE,FRI /st 18:00 /f /tr '"C:\Program Files\Python313\pythonw.exe" "C:\data\projects\USAMega\statisticallydoomed.py" --alert 100000000 --game "Mega Millions"'
```

No "Start in" is required: `.env` and the database resolve from the script's own path.
`pythonw.exe` avoids a console window flashing. The task runs on every draw day; mail
only arrives when the threshold is met, and each run records to the database either way.

To have it run while you are logged out, open Task Scheduler and set *Run whether user is
logged on or not* — it will ask for your Windows password.

**Read the record.**

```
python statisticallydoomed.py --history 30
python -c "import sqlite3; [print(r) for r in sqlite3.connect('statisticallydoomed.db').execute('SELECT game, place, lump, fetched_at FROM fetch ORDER BY fetched_at DESC LIMIT 5')]"
```

**Start over.** Delete `statisticallydoomed.db` to forget every recorded figure; the next
check rebuilds it, and the window reverts to `LOTTERY_PLACE`. To reset the remembered
jurisdiction and mode on their own:

```powershell
Remove-Item HKCU:\Software\usamega-tools\lottery -Recurse
```

## What it stores, and where

`statisticallydoomed.db` sits beside the script — SQLite, one table:

```sql
CREATE TABLE fetch (
    game, draw_date, annuity, cash, place,
    lump, per_year, payments, total, fetched_at,
    PRIMARY KEY (game, draw_date, annuity, place)
);
```

Every check is recorded rather than overwritten. Re-checking an unchanged jackpot
replaces its own rows; a jackpot rise or a new draw starts a new snapshot. So the table
grows with real changes, not with button presses, and the window restores the newest
snapshot at launch.

Preferences live apart from the data, in
`HKEY_CURRENT_USER\Software\usamega-tools\lottery` — only the selected jurisdiction and
payout mode.

## Troubleshooting

**`Set LOTTERY_GMAIL, LOTTERY_GMAIL_APP_PASSWORD and LOTTERY_SMS_TO to send alerts…`**
One of the three is missing or blank. Blank values are ignored deliberately, so an empty
`LOTTERY_GMAIL_APP_PASSWORD=` counts as missing. Note `.env` is read from the script's
directory, not your current one.

**`SMTPAuthenticationError`** — the app password is wrong, expired, or revoked. Regenerate
it at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). If
you pasted it with the spaces Google displays, try it as 16 characters with no spaces.

**Mail says it sent, nothing arrives.** Check spam first, then confirm `LOTTERY_SMS_TO`
is the address you are watching.

**`Could not reach usamega.com (TimeoutError)`** — the site was slow or unreachable. The
window keeps the last good figures and says so; just check again.

**`usamega.com listed no payouts`, or every row says `Unavailable`** — the page structure
changed and parsing failed. This is deliberate: it reports nothing rather than a wrong
number. The parsing lives in `parse_draw()`.

**`Executable doesn't exist … playwright install`** — the browser was never downloaded.
Run `playwright install chromium`.

**`Not sold here`** — that game has no payout listed for the selected jurisdiction.
Powerball lists 49, Mega Millions 47.

**The window opens on the wrong state.** It remembers your last pick, which overrides
`LOTTERY_PLACE`. Choose the one you want once, or clear the registry key above.

**A scheduled task appears to do nothing.** It only mails when the threshold is met. To
see what it actually does, run the same command by hand with `python.exe` instead of
`pythonw.exe`.
