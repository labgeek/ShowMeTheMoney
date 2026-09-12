#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright", "PySide6"]
# ///
"""Lottery take-home after taxes, by jurisdiction, for Powerball and Mega Millions.

Run with no arguments for the window. Run with --alert for a scheduled check that
texts you and never opens one:

    python statisticallydoomed.py --alert 100000000 --place Virginia
"""

import argparse
import os
import re
import sqlite3
import smtplib
import ssl
import sys
from contextlib import closing
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import NamedTuple

from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout,
    QLabel, QPushButton, QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)
from playwright.sync_api import sync_playwright

GAMES = {
    "Powerball": "https://www.usamega.com/powerball/jackpot",
    "Mega Millions": "https://www.usamega.com/mega-millions/jackpot",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
DEFAULT_PLACE = "Virginia"  # built-in fallback; .env can override it
SETTINGS = ("usamega-tools", "lottery")  # patched by the self-check so it cannot touch real settings
DB_PATH = Path(__file__).resolve().with_suffix(".db")  # next to this script, not the cwd
ENV_PATH = Path(__file__).resolve().parent / ".env"
LUMP, ANNUITY = "Lump sum", "Annuity"

# The page prints a jackpot header, then one block per jurisdiction.
JACKPOT = re.compile(r"Jackpot for ([^\n]+)\n\$([\d,]+)\n\t?\$([\d,]+)")
PLACE = re.compile(r"\n([A-Z][A-Za-z.'\- ]{2,40}): [^\n]*?(?:state tax|tax rate)[^\n]*")
FIGURES = re.compile(r"Your average net per year: \$([\d,]+)\s*Your net payout: \$([\d,]+)"
                     r"\s*After (\d+) payments: \$([\d,]+)")

INK, PAGE, SURFACE, RULE, MUTED, ACCENT = (
    "#14181F", "#F2F4F7", "#FFFFFF", "#DDE1E7", "#5C6672", "#1F5F4A")

STYLE = f"""
QWidget {{ color: {INK}; font-family: "Segoe UI", sans-serif; font-size: 13px; }}
QLabel {{ background: transparent; }}
#app {{ background: {PAGE}; }}
#header {{ background: {SURFACE}; border-bottom: 1px solid {RULE}; }}
#title {{ font-size: 15px; font-weight: 600; }}
#status {{ color: {MUTED}; }}
#fetchbar {{ background: {SURFACE}; border-bottom: 1px solid {RULE}; }}
#viewbar {{ background: {PAGE}; border-bottom: 1px solid {RULE}; }}
#row {{ background: {SURFACE}; border-bottom: 1px solid {RULE}; }}
#game {{ color: {MUTED}; }}
#meta {{ color: {MUTED}; }}
#note {{ color: {MUTED}; }}
#amount {{ color: {INK}; font-size: 27px; font-weight: 600; }}
#amount[pending="true"] {{ color: {MUTED}; }}
#amount[failed="true"] {{ color: #9B2C2C; }}
#amount[missing="true"] {{ color: {MUTED}; font-size: 20px; }}
#marker {{ background: {RULE}; border: none; }}
#marker[fresh="true"] {{ background: {ACCENT}; }}
#place {{ background: {SURFACE}; border: 1px solid #A9B2BD; border-radius: 5px;
    padding: 5px 8px; min-width: 145px; }}
#place:focus {{ border-color: {ACCENT}; }}
QSpinBox {{ background: {SURFACE}; border: 1px solid #A9B2BD; border-radius: 5px;
    padding: 5px 4px; min-width: 95px; }}
QSpinBox:focus {{ border-color: {ACCENT}; }}
/* Styling a spinbox at all switches Qt to the stylesheet style, which stacks the
   arrows side by side over the text unless their geometry is spelled out. */
QSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
    width: 18px; height: 13px; margin: 1px 2px 0 0; }}
QSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
    width: 18px; height: 13px; margin: 0 2px 1px 0; }}
QCheckBox, QRadioButton {{ spacing: 7px; padding: 4px 2px; }}
QPushButton#run {{ background: {INK}; color: {SURFACE}; border: none; border-radius: 5px;
    padding: 8px 20px; font-weight: 600; }}
QPushButton#run:hover {{ background: #2A303A; }}
QPushButton#run:disabled {{ background: #9AA3AE; }}
QPushButton#copy {{ background: transparent; color: {MUTED}; border: none; padding: 4px 6px;
    text-decoration: underline; }}
QPushButton#copy:hover {{ color: {INK}; }}
QPushButton#copy:disabled {{ color: #A9B2BD; text-decoration: none; }}
QPushButton:focus, QCheckBox:focus, QRadioButton:focus {{ border: 1px solid {ACCENT}; }}
"""


class Payout(NamedTuple):
    """What one jurisdiction keeps, both ways of taking the prize."""

    lump: int
    per_year: int
    payments: int
    total: int

    def by_mode(self, mode: str) -> int:
        return self.lump if mode == LUMP else self.total


class Draw(NamedTuple):
    """One game's current jackpot and every jurisdiction's take-home from it."""

    date: str
    annuity: int
    cash: int
    places: dict[str, Payout]


# Every check is kept, not just the last one, so the app can answer "is this a big one?".
# One row per game/draw/jackpot/jurisdiction: re-checking an unchanged jackpot replaces its
# rows instead of piling up, while a jackpot rise or a new draw starts a new snapshot.
SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch (
    game       TEXT    NOT NULL,
    draw_date  TEXT    NOT NULL,
    annuity    INTEGER NOT NULL,
    cash       INTEGER NOT NULL,
    place      TEXT    NOT NULL,
    lump       INTEGER NOT NULL,
    per_year   INTEGER NOT NULL,
    payments   INTEGER NOT NULL,
    total      INTEGER NOT NULL,
    fetched_at TEXT    NOT NULL,
    PRIMARY KEY (game, draw_date, annuity, place)
);
CREATE INDEX IF NOT EXISTS fetch_recent ON fetch (game, fetched_at);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the history database, creating the file and schema on first use.

    Only ever called from the thread that owns it: the GUI thread in the window, the
    main thread in alert mode. No connection is shared with the scraper thread.
    """
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.executescript(SCHEMA)
    except Exception:
        db.close()  # a corrupt file fails here; without this the handle stays open
        raise
    return db


def save_draws(draws: dict[str, Draw], when: str, path: Path | None = None) -> None:
    """Record one check. Unchanged jackpots replace their own rows; changes add a snapshot."""
    rows = [(game, d.date, d.annuity, d.cash, place, p.lump, p.per_year, p.payments, p.total, when)
            for game, d in draws.items() for place, p in d.places.items()]
    if not rows:
        return
    with closing(connect(path)) as db, db:
        db.executemany("INSERT OR REPLACE INTO fetch VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def load_latest(path: Path | None = None) -> tuple[dict[str, Draw], str]:
    """The newest snapshot per game, and when it was taken. Returns ({}, "") on a bad database.

    A snapshot is one (game, draw_date, annuity) group. Picking it explicitly matters:
    selecting on MAX(fetched_at) alone blends two snapshots written in the same second.
    """
    draws: dict[str, Draw] = {}
    when = ""
    try:
        with closing(connect(path)) as db:
            for (game,) in db.execute("SELECT DISTINCT game FROM fetch").fetchall():
                head = db.execute(
                    "SELECT draw_date, annuity, cash, fetched_at FROM fetch WHERE game = ? "
                    "ORDER BY fetched_at DESC, rowid DESC LIMIT 1", (game,)).fetchone()
                draw_date, annuity, cash, fetched_at = head
                places = {
                    place: Payout(lump, per_year, payments, total)
                    for place, lump, per_year, payments, total in db.execute(
                        "SELECT place, lump, per_year, payments, total FROM fetch "
                        "WHERE game = ? AND draw_date = ? AND annuity = ?",
                        (game, draw_date, annuity))}
                draws[game] = Draw(draw_date, annuity, cash, places)
                when = max(when, fetched_at)
    except (sqlite3.Error, OSError):
        return {}, ""  # unreadable or corrupt; the next check writes a clean snapshot
    return draws, when


def history(game: str, place: str, limit: int = 10,
            path: Path | None = None) -> list[tuple[str, str, int, int, int]]:
    """Past snapshots for one game and jurisdiction, newest first."""
    with closing(connect(path)) as db:
        return db.execute(
            "SELECT fetched_at, draw_date, annuity, lump, total FROM fetch "
            "WHERE game = ? AND place = ? ORDER BY fetched_at DESC LIMIT ?",
            (game, place, limit)).fetchall()


def default_place() -> str:
    """The jurisdiction to use when nothing else says otherwise, from .env if it does."""
    return os.environ.get("LOTTERY_PLACE") or os.environ.get("DEFAULT_PLACE") or DEFAULT_PLACE


def money(n: int) -> str:
    return f"${n:,}"


def ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def parse_draw(text: str) -> Draw | None:
    """Pull the jackpot header and every jurisdiction's figures out of a page."""
    header = JACKPOT.search(text)
    if not header:
        return None

    places = {}
    blocks = list(PLACE.finditer(text))
    for i, block in enumerate(blocks):
        end = blocks[i + 1].start() if i + 1 < len(blocks) else len(text)
        figures = FIGURES.search(text, block.end(), end)
        if figures:
            per_year, lump, payments, total = figures.groups()
            places[block.group(1).strip()] = Payout(
                lump=int(lump.replace(",", "")),
                per_year=int(per_year.replace(",", "")),
                payments=int(payments),
                total=int(total.replace(",", "")),
            )
    if not places:
        return None

    return Draw(header.group(1).replace("\xa0", " "),
                int(header.group(2).replace(",", "")),
                int(header.group(3).replace(",", "")), places)


def fetch(url: str) -> Draw | None:
    """Load one jackpot page. One load carries every jurisdiction, so switching them is free."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900}, user_agent=UA)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)  # ponytail: numbers land via JS; needs a stable selector to do better
        text = page.inner_text("body")
        browser.close()
    return parse_draw(text)


class Scraper(QObject):
    """Runs the fetches off the GUI thread so the window stays responsive."""

    started_game = Signal(str)
    result = Signal(str, object, str)  # game, Draw or None, problem text
    done = Signal()

    def __init__(self, games: list[str]) -> None:
        super().__init__()
        self.games = games

    def run(self) -> None:
        for game in self.games:
            self.started_game.emit(game)
            try:
                draw = fetch(GAMES[game])
                if draw:
                    self.result.emit(game, draw, "")
                else:
                    self.result.emit(game, None, "usamega.com listed no payouts")
            except Exception as e:
                self.result.emit(game, None, f"Could not reach usamega.com ({type(e).__name__})")
        self.done.emit()


class ResultRow(QFrame):
    """One game: which draw it is, the figure, and what the figure means."""

    def __init__(self, game: str) -> None:
        super().__init__()
        self.setObjectName("row")
        self.marker = QFrame()
        self.marker.setObjectName("marker")
        self.marker.setFixedWidth(3)

        self.name = QLabel(game)
        self.name.setObjectName("game")
        self.meta = QLabel("")
        self.meta.setObjectName("meta")
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.name)
        top.addStretch()
        top.addWidget(self.meta)

        self.amount = QLabel("—")
        self.amount.setObjectName("amount")
        self.amount.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.note = QLabel("Not checked yet")
        self.note.setObjectName("note")
        self.note.setWordWrap(True)

        text = QVBoxLayout()
        text.setContentsMargins(0, 13, 0, 15)
        text.setSpacing(2)
        text.addLayout(top)
        text.addWidget(self.amount)
        text.addWidget(self.note)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 18, 0)
        layout.setSpacing(15)
        layout.addWidget(self.marker)
        layout.addLayout(text)

    def set_state(self, amount: str, note: str, meta: str = "", pending: bool = False,
                  failed: bool = False, missing: bool = False, fresh: bool = False) -> None:
        self.amount.setText(amount)
        self.note.setText(note)
        self.meta.setText(meta)
        for widget, prop, value in ((self.amount, "pending", pending),
                                    (self.amount, "failed", failed),
                                    (self.amount, "missing", missing),
                                    (self.marker, "fresh", fresh)):
            widget.setProperty(prop, "true" if value else "false")
            widget.style().unpolish(widget)
            widget.style().polish(widget)


class Window(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Lottery Payout")
        self.setObjectName("app")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(620)
        self.settings = QSettings(*SETTINGS)  # preferences only; the figures live in SQLite
        for stale in ("cache", "fetched"):
            self.settings.remove(stale)       # the old registry blob, superseded by the database
        self.db_path = DB_PATH
        self.fetched_at = ""
        self.place = self.settings.value("place", default_place())
        self.mode = self.settings.value("mode", LUMP)
        self.draws: dict[str, Draw] = {}
        self.problems: dict[str, str] = {}
        self.fresh_games: set[str] = set()  # fetched in this session, so safe to call fresh
        self.running: set[str] = set()

        self.title = QLabel(f"{self.place} take-home")
        self.title.setObjectName("title")
        self.status = QLabel("Never checked")
        self.status.setObjectName("status")
        header = QFrame()
        header.setObjectName("header")
        head = QHBoxLayout(header)
        head.setContentsMargins(18, 13, 18, 13)
        head.addWidget(self.title)
        head.addStretch()
        head.addWidget(self.status)

        # What to fetch.
        self.boxes = {game: QCheckBox(game) for game in GAMES}
        self.button = QPushButton("Check payouts")
        self.button.setObjectName("run")
        self.button.setCursor(Qt.PointingHandCursor)
        fetchbar = QFrame()
        fetchbar.setObjectName("fetchbar")
        fetch_row = QHBoxLayout(fetchbar)
        fetch_row.setContentsMargins(18, 12, 18, 12)
        fetch_row.setSpacing(18)
        for box in self.boxes.values():
            box.setChecked(True)
            box.setCursor(Qt.PointingHandCursor)
            fetch_row.addWidget(box)
        fetch_row.addStretch()
        fetch_row.addWidget(self.button)

        # How to read it. Everything here redraws from cache; nothing refetches.
        self.picker = QComboBox()
        self.picker.setObjectName("place")
        self.picker.addItem(self.place)
        self.picker.setCursor(Qt.PointingHandCursor)
        self.modes = QButtonGroup(self)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(12)
        for name in (LUMP, ANNUITY):
            radio = QRadioButton(name)
            radio.setCursor(Qt.PointingHandCursor)
            radio.setChecked(name == self.mode)
            self.modes.addButton(radio)
            mode_row.addWidget(radio)
        self.split = QSpinBox()
        self.split.setRange(1, 200)
        self.split.setPrefix("Split ")
        self.split.setSuffix(" way")
        self.split.valueChanged.connect(
            lambda n: self.split.setSuffix(" ways" if n > 1 else " way"))
        self.viewbar = QFrame()
        self.viewbar.setObjectName("viewbar")
        self.viewbar.setEnabled(False)
        view_row = QHBoxLayout(self.viewbar)
        view_row.setContentsMargins(18, 10, 18, 10)
        view_row.setSpacing(16)
        view_row.addWidget(self.picker)
        view_row.addLayout(mode_row)
        view_row.addStretch()
        view_row.addWidget(self.split)

        self.rows = {game: ResultRow(game) for game in GAMES}

        self.copy = QPushButton("Copy results")
        self.copy.setObjectName("copy")
        self.copy.setCursor(Qt.PointingHandCursor)
        self.copy.setEnabled(False)
        footer = QHBoxLayout()
        footer.setContentsMargins(12, 6, 12, 6)
        footer.addStretch()
        footer.addWidget(self.copy)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(fetchbar)
        layout.addWidget(self.viewbar)
        for row in self.rows.values():
            layout.addWidget(row)
        layout.addLayout(footer)
        layout.addStretch()

        self.button.clicked.connect(self.start)
        self.copy.clicked.connect(self.copy_results)
        self.picker.currentTextChanged.connect(self.change_place)
        self.modes.buttonClicked.connect(self.change_mode)
        self.split.valueChanged.connect(self.redraw)
        QShortcut(QKeySequence("Ctrl+R"), self, self.start)
        for game, box in self.boxes.items():
            box.toggled.connect(lambda on, g=game: self.rows[g].setVisible(on))

        self.load_cache()

    # --- last run's fetch -----------------------------------------------

    def save_cache(self) -> None:
        """Record this check. Columns are typed, so nothing here can poison a later launch."""
        self.fetched_at = datetime.now().isoformat(timespec="seconds")
        save_draws(self.draws, self.fetched_at, self.db_path)

    def load_cache(self) -> None:
        """Show the newest stored check straight away. Nothing here touches the network."""
        self.draws, self.fetched_at = load_latest(self.db_path)
        if not self.draws:
            self.start_fresh()
            return
        self.refresh_picker()
        self.viewbar.setEnabled(True)
        self.redraw()
        self.status.setText(f"Last checked {self.when_fetched()}")

    def start_fresh(self) -> None:
        """With nothing recorded, open at the default jurisdiction rather than a stale pick."""
        self.place = default_place()
        self.picker.blockSignals(True)
        self.picker.clear()
        self.picker.addItem(self.place)
        self.picker.blockSignals(False)
        self.title.setText(f"{self.place} take-home")

    def when_fetched(self) -> str:
        try:
            return datetime.fromisoformat(self.fetched_at).strftime(
                "%b %d, %I:%M %p").replace(" 0", " ")
        except (TypeError, ValueError):
            return "earlier"

    # --- how to read it -------------------------------------------------

    def change_place(self, place: str) -> None:
        """Switching jurisdiction costs nothing: they all came back in the same fetch."""
        if not place:
            return
        self.place = place
        self.settings.setValue("place", place)
        self.redraw()

    def change_mode(self, button) -> None:
        self.mode = button.text()
        self.settings.setValue("mode", self.mode)
        self.redraw()

    def refresh_picker(self) -> None:
        """Offer every jurisdiction the fetched games returned, keeping the current pick."""
        places = sorted({p for draw in self.draws.values() for p in draw.places})
        if not places:
            return
        self.picker.blockSignals(True)
        self.picker.clear()
        self.picker.addItems(places)
        self.picker.setCurrentText(self.place if self.place in places else default_place())
        self.picker.blockSignals(False)
        self.place = self.picker.currentText()
        if self.place != self.settings.value("place"):
            self.settings.setValue("place", self.place)  # make the fallback stick

    # --- running --------------------------------------------------------

    def start(self) -> None:
        if not self.button.isEnabled():
            return
        games = [name for name, box in self.boxes.items() if box.isChecked()]
        if not games:
            self.status.setText("Pick a game to check")
            return

        self.button.setEnabled(False)
        self.copy.setEnabled(False)
        self.copy.setText("Copy results")
        self.status.setText("Checking usamega.com")
        self.problems = {}
        self.running = set(games)
        self.fresh_games -= self.running  # in flight: no longer this session's news
        for game in games:
            self.rows[game].set_state("—", "Waiting", pending=True)

        self.thread = QThread(self)
        self.scraper = Scraper(games)
        self.scraper.moveToThread(self.thread)
        self.thread.started.connect(self.scraper.run)
        self.scraper.started_game.connect(self.show_pending)
        self.scraper.result.connect(self.store_result)
        self.scraper.done.connect(self.thread.quit)
        self.thread.finished.connect(self.finish)
        self.thread.start()

    def show_pending(self, game: str) -> None:
        self.rows[game].set_state("Checking…", "Loading the jackpot page", pending=True)

    def store_result(self, game: str, draw: Draw | None, problem: str) -> None:
        if draw:
            self.draws[game] = draw
            self.fresh_games.add(game)
        else:
            self.problems[game] = problem  # any earlier draw stays, marked as not fresh
        self.refresh_picker()
        self.viewbar.setEnabled(bool(self.draws))
        self.redraw()

    def finish(self) -> None:
        self.button.setEnabled(True)
        self.copy.setEnabled(bool(self.results()))
        if self.running & self.fresh_games:
            self.save_cache()
            self.status.setText(f"Checked at {datetime.now().strftime('%I:%M %p').lstrip('0')}")
        elif self.draws:
            self.status.setText(f"Could not refresh, last checked {self.when_fetched()}")
        else:
            self.status.setText("Nothing came back")
        self.running = set()

    # --- drawing --------------------------------------------------------

    def redraw(self) -> None:
        """Draw every row from cache: place, mode and split never refetch."""
        self.title.setText(f"{self.place} take-home")
        self.copy.setText("Copy results")
        self.copy.setEnabled(bool(self.results()))

        shares = self.split.value()
        for game, row in self.rows.items():
            draw = self.draws.get(game)
            if game in self.problems and not draw:
                row.set_state("Unavailable", f"{self.problems[game]}. Try again in a minute.",
                              failed=True)
                continue
            if not draw:
                continue  # still pending, or never checked

            meta = f"{money(draw.annuity)} jackpot, draw {draw.date}"
            payout = draw.places.get(self.place)
            if not payout:
                row.set_state("Not sold here", f"{game} has no payout listed for {self.place}",
                              meta=meta, missing=True)
                continue

            total = payout.by_mode(self.mode)
            if self.mode == LUMP:
                note = f"Lump sum, after federal and {self.place} tax."
            else:
                note = (f"{payout.payments} payments of about "
                        f"{money(payout.per_year // shares)} a year, after federal and "
                        f"{self.place} tax.")
            note = f"{note} {self.rank(draw, total)}"
            if shares > 1:
                note = f"Each of {shares} shares. {note}"
            if game in self.problems:
                note = f"{self.problems[game]}. Showing the check from {self.when_fetched()}. {note}"
            row.set_state(money(total // shares), note, meta=meta,
                          fresh=game in self.fresh_games)

    def rank(self, draw: Draw, total: int) -> str:
        """Where this jurisdiction lands among all of them, since we hold every one."""
        amounts = sorted((p.by_mode(self.mode) for p in draw.places.values()), reverse=True)
        return f"{ordinal(amounts.index(total) + 1)} highest of {len(amounts)} places."

    # --- output ---------------------------------------------------------

    def results(self) -> dict[str, str]:
        """The selected jurisdiction's figures, for the games that offer it."""
        shares = self.split.value()
        return {game: money(draw.places[self.place].by_mode(self.mode) // shares)
                for game, draw in self.draws.items() if self.place in draw.places}

    def copy_results(self) -> None:
        shares = self.split.value()
        share_note = f" split {shares} ways" if shares > 1 else ""
        lines = [f"{game} {self.place} {self.mode.lower()}{share_note}: {amount} "
                 f"(draw {self.draws[game].date})"
                 for game, amount in self.results().items()]
        QApplication.clipboard().setText("\n".join(lines))
        self.copy.setText("Copied")


# --- alert mode: no window, for a scheduled task ------------------------------

def load_env(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from .env beside this script, for the alert credentials.

    A real environment variable always wins, so a scheduled task or a shell export can
    override the file without editing it.
    """
    try:
        text = Path(path or ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        return  # no .env is fine: the environment may carry the values already
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def compact(n: int) -> str:
    """$56,492,543 -> $56.5M, for a subject line that reads on a lock screen."""
    return f"${n / 1_000_000:.1f}M"


def record_high(game: str, place: str, mode: str,
                path: Path | None = None) -> tuple[int, str] | None:
    """The best take-home ever recorded for this game and jurisdiction, and when."""
    column = "lump" if mode == LUMP else "total"  # never interpolate anything else
    try:
        with closing(connect(path)) as db:
            return db.execute(
                f"SELECT {column}, fetched_at FROM fetch WHERE game = ? AND place = ? "
                f"ORDER BY {column} DESC LIMIT 1", (game, place)).fetchone()
    except (sqlite3.Error, OSError):
        return None


class Hit(NamedTuple):
    """One game that cleared the threshold, with everything worth saying about it."""

    game: str
    draw: Draw
    payout: Payout
    rank: int
    best_place: str
    best_amount: int
    previous: tuple[int, str] | None  # best recorded before this check

    def amount(self, mode: str) -> int:
        return self.payout.by_mode(mode)


def describe(hit: Hit, place: str, mode: str) -> list[tuple[str, str]]:
    """The supporting facts, as label/value rows shared by both message formats."""
    draw, payout = hit.draw, hit.payout
    rows = [
        ("Draw", draw.date),
        ("Jackpot", f"{money(draw.annuity)} annuity, {money(draw.cash)} cash"),
    ]
    # show the other way of taking it, never a second copy of the headline figure
    if mode == LUMP:
        rows.append(("As annuity", f"{money(payout.total)} over {payout.payments} years, "
                                   f"about {money(payout.per_year)} a year"))
    else:
        rows.append(("As lump sum", money(payout.lump)))
    rows.append(("Rank", f"{ordinal(hit.rank)} highest of {len(draw.places)} places"))

    if hit.best_place != place:
        rows.append(("Best anywhere", f"{hit.best_place} {money(hit.best_amount)}, "
                                      f"{money(hit.best_amount - hit.amount(mode))} more"))
    if hit.previous:
        was, when = hit.previous
        day = when.split("T")[0]
        if hit.amount(mode) > was:
            rows.append(("Record", f"highest recorded, past best {money(was)} on {day}"))
        elif hit.amount(mode) == was:
            rows.append(("Record", f"ties the highest recorded, first seen {day}"))
        else:
            rows.append(("Record", f"below the {money(was)} recorded on {day}"))
    return rows


def alert_message(hits: list[Hit], place: str, mode: str,
                  threshold: int) -> tuple[str, str, str]:
    """Subject, plain text, and HTML for one alert. Plain text stays short: it may be an SMS."""
    top = max(hits, key=lambda h: h.amount(mode))
    if len(hits) == 1:
        subject = f"{compact(top.amount(mode))} {top.game} take-home in {place}"
    else:
        subject = (f"{compact(top.amount(mode))} {top.game} and "
                   + ", ".join(f"{compact(h.amount(mode))} {h.game}"
                               for h in hits if h is not top) + f" in {place}")

    plain = [f"{place} take-home after federal and state taxes",
             f"{mode}, over {money(threshold)}", ""]
    for hit in hits:
        plain.append(f"{hit.game}: {money(hit.amount(mode))}")
        plain += [f"  {label}: {value}" for label, value in describe(hit, place, mode)]
        plain.append("")
    plain.append(f"Checked {datetime.now().strftime('%b %d, %I:%M %p').replace(' 0', ' ')}")

    ink, muted, rule, accent = "#14181F", "#5C6672", "#DDE1E7", "#1F5F4A"
    font = "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
    cards = []
    for hit in hits:
        facts = "".join(
            f'<tr><td style="padding:5px 16px 5px 0;color:{muted};white-space:nowrap;'
            f'vertical-align:top">{label}</td>'
            f'<td style="padding:5px 0;color:{ink}">{value}</td></tr>'
            for label, value in describe(hit, place, mode))
        cards.append(f"""
      <tr><td style="padding:22px 0;border-top:1px solid {rule}">
        <div style="color:{muted};font-size:14px">{hit.game}</div>
        <div style="color:{ink};font-size:34px;font-weight:600;letter-spacing:-0.5px;
                    padding:2px 0 2px">{money(hit.amount(mode))}</div>
        <div style="color:{muted};font-size:14px;padding-bottom:12px">
          {mode} after federal and {place} tax</div>
        <table cellpadding="0" cellspacing="0" style="font-size:14px">{facts}</table>
      </td></tr>""")

    html = f"""<div style="font-family:{font};background:#F2F4F7;padding:24px 12px">
  <table cellpadding="0" cellspacing="0" width="100%" style="max-width:560px;margin:0 auto;
         background:#FFFFFF;border:1px solid {rule};border-radius:8px">
    <tr><td style="padding:20px 24px 4px">
      <div style="color:{ink};font-size:16px;font-weight:600">
        {place} take-home after federal and state taxes</div>
      <div style="color:{muted};font-size:14px;padding-top:2px">
        Passed your {money(threshold)} {mode.lower()} threshold</div>
    </td></tr>
    <tr><td style="padding:0 24px">
      <table cellpadding="0" cellspacing="0" width="100%">{"".join(cards)}</table>
    </td></tr>
    <tr><td style="padding:14px 24px 20px;border-top:1px solid {rule};color:{muted};
                   font-size:13px">
      Checked {datetime.now().strftime("%b %d, %I:%M %p").replace(" 0", " ")}.
      Figures from <a href="https://www.usamega.com" style="color:{accent}">usamega.com</a>.
    </td></tr>
  </table>
</div>"""
    return subject, "\n".join(plain), html


def send_alert(subject: str, plain: str, html: str) -> None:
    """Mail the alert. Credentials come from the environment or .env, never the code."""
    sender = os.environ.get("LOTTERY_GMAIL")
    password = os.environ.get("LOTTERY_GMAIL_APP_PASSWORD")
    to = os.environ.get("LOTTERY_SMS_TO")
    if not (sender and password and to):
        sys.exit(f"Set LOTTERY_GMAIL, LOTTERY_GMAIL_APP_PASSWORD and LOTTERY_SMS_TO to send "
                 f"alerts, in the environment or in {ENV_PATH}")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(plain)        # what an SMS gateway or a text-only client shows
    message.add_alternative(html, subtype="html")

    # An app password is a whole-mailbox credential: starttls() with no context skips
    # certificate checks entirely, so pass one.
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(sender, password)
        server.send_message(message)


def run_alert(threshold: int, place: str, mode: str, games: list[str], dry_run: bool) -> int:
    """Check each game and mail once if any take-home reaches the threshold."""
    hits: list[Hit] = []
    drawn: dict[str, Draw] = {}
    for game in games:
        draw = fetch(GAMES[game])
        if not draw:
            print(f"{game}: could not read usamega.com")
            continue
        drawn[game] = draw
        payout = draw.places.get(place)
        if not payout:
            print(f"{game}: no payout listed for {place}")
            continue
        amount = payout.by_mode(mode)
        print(f"{game} {place} {mode.lower()}: {money(amount)} (draw {draw.date})")
        if amount < threshold:
            continue
        ranked = sorted(draw.places.items(), key=lambda kv: kv[1].by_mode(mode), reverse=True)
        best_place, best = ranked[0]
        hits.append(Hit(game, draw, payout,
                        rank=[p for p, _ in ranked].index(place) + 1,
                        best_place=best_place, best_amount=best.by_mode(mode),
                        previous=record_high(game, place, mode)))  # before this check is stored

    save_draws(drawn, datetime.now().isoformat(timespec="seconds"))

    if not hits:
        print(f"Nothing reached {money(threshold)}.")
        return 0

    subject, plain, html = alert_message(hits, place, mode, threshold)
    if dry_run:
        print(f"\nWould have sent - subject: {subject}\n\n{plain}")
    else:
        send_alert(subject, plain, html)
        print(f"Mailed {len(hits)} alert(s): {subject}")
    return 0


def run_history(place: str, games: list[str], limit: int) -> int:
    """Print what past checks recorded, newest first."""
    for game in games:
        rows = history(game, place, limit)
        print()
        print(f"{game} - {place}" if rows else f"{game} - {place}: nothing recorded yet")
        for fetched_at, draw_date, annuity, lump, total in rows:
            print(f"  {fetched_at}  draw {draw_date:<18} jackpot {money(annuity):>14}"
                  f"  lump {money(lump):>14}  annuity {money(total):>15}")
    return 0


def main() -> None:
    load_env()  # before the parser, so LOTTERY_PLACE can supply the --place default
    parser = argparse.ArgumentParser(description="Lottery take-home after taxes.")
    parser.add_argument("--alert", type=int, metavar="AMOUNT",
                        help="skip the window: check, and email if take-home reaches this")
    parser.add_argument("--place", default=default_place(), help="jurisdiction to check")
    parser.add_argument("--mode", default=LUMP, choices=[LUMP, ANNUITY])
    parser.add_argument("--game", action="append", choices=list(GAMES),
                        help="repeatable; defaults to both")
    parser.add_argument("--dry-run", action="store_true", help="print the alert, never send it")
    parser.add_argument("--history", type=int, nargs="?", const=10, metavar="N",
                        help="print the last N recorded checks instead of fetching (default 10)")
    args = parser.parse_args()

    if args.history is not None:
        sys.exit(run_history(args.place, args.game or list(GAMES), args.history))
    if args.alert is None:
        app = QApplication(sys.argv)
        window = Window()
        window.show()
        sys.exit(app.exec())
    sys.exit(run_alert(args.alert, args.place, args.mode, args.game or list(GAMES), args.dry_run))


if __name__ == "__main__":
    main()
