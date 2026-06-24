"""
Prenotami (prenotami.esteri.it) appointment monitor.

Logs into the Italian embassy booking portal, reads the "Services provided by
the Embassy/Consulate" table, and notifies you on Telegram the moment any
service becomes bookable (a Book button appears) or the page otherwise changes.

It only DETECTS and NOTIFIES. It never books anything automatically.
"""

import os
import re
import time
import json
import random
import asyncio
import requests
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Config (all secrets come from environment variables - never hardcode them)
# ---------------------------------------------------------------------------
USERNAME    = os.environ["PRENOTAMI_USER"]     # the email you log in with
PASSWORD    = os.environ["PRENOTAMI_PASS"]
TG_TOKEN    = os.environ["TG_TOKEN"]           # from @BotFather
TG_CHAT_ID  = os.environ["TG_CHAT_ID"]         # your Telegram chat id

LOGIN_URL    = "https://prenotami.esteri.it/Account/Login"
SERVICES_URL = "https://prenotami.esteri.it/Services"

# Polling: 3 min base + up to 60s random jitter. Do NOT lower much: Prenotami
# blocks accounts that hammer it. 60s like you asked is risky; 180s is safer.
CHECK_EVERY = 180
JITTER_MAX  = 60

STATE_FILE = "last_state.json"

# Phrases (across the 3 site languages) that mean the row is NOT an open slot:
#  - "not yet available"  -> calendar not open
#  - "already made"       -> you already hold an appointment for this service
# If a row shows none of these AND has a BOOK button, it's a real open slot.
NON_BOOKABLE_MARKERS = [
    "booking calendar not yet available",      # ENG, not open
    "calendario di prenotazione non ancora",   # ITA, not open
    "календарь бронирования",                  # RUS, not open
    "booking for this service already made",   # ENG, already booked
    "prenotazione per questo servizio",        # ITA, already booked
    "запись на эту услугу уже",                # RUS, already booked
]

# Text that confirms a live Book button.
BOOK_BUTTON_TEXTS = ["book", "prenota", "записаться", "забронировать"]


def notify(text: str):
    """Send a Telegram message. Truncates to Telegram's limit."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": text[:4000],
                  "disable_web_page_preview": True},
            timeout=20,
        )
    except Exception as e:
        print("Telegram send failed:", e)


def looks_non_bookable(booking_cell_text: str) -> bool:
    t = booking_cell_text.strip().lower()
    return any(m in t for m in NON_BOOKABLE_MARKERS)


async def login(page):
    await page.goto(LOGIN_URL, wait_until="networkidle")
    # Prenotami login field ids
    await page.fill("#login-email", USERNAME)
    await page.fill("#login-password", PASSWORD)
    await page.click("button[type=submit]")
    await page.wait_for_load_state("networkidle")
    # If still on the login page, credentials/captcha failed
    if "Login" in page.url:
        raise RuntimeError("Login failed - check credentials or a CAPTCHA appeared.")


async def read_services(page):
    """
    Returns a dict: { "<Type> | <Service> | <Description>": booking_status }
    booking_status is "BOOKABLE" or the raw unavailable text.
    Walks through all pagination pages.
    """
    await page.goto(SERVICES_URL, wait_until="networkidle")
    services = {}

    while True:
        rows = page.locator("table tbody tr")
        count = await rows.count()
        for i in range(count):
            cells = rows.nth(i).locator("td")
            if await cells.count() < 4:
                continue
            type_  = (await cells.nth(0).inner_text()).strip()
            serv   = (await cells.nth(1).inner_text()).strip()
            desc   = (await cells.nth(2).inner_text()).strip()
            book   = (await cells.nth(3).inner_text()).strip()

            key = f"{type_} | {serv} | {desc}"

            # Decide the row's true state.
            # A "Link 1" in the booking column is NOT a Book button (those are
            # the separate Link column / info links), so we check the booking
            # cell specifically for an actual BOOK/PRENOTA control.
            book_btn = cells.nth(3).locator("a, button")
            btn_n = await book_btn.count()
            has_book_button = False
            for b in range(btn_n):
                label = (await book_btn.nth(b).inner_text()).strip().lower()
                if any(word in label for word in BOOK_BUTTON_TEXTS):
                    has_book_button = True
                    break

            if has_book_button and not looks_non_bookable(book):
                services[key] = "BOOKABLE"
            elif "already made" in book.lower() or "già" in book.lower() or "уже" in book.lower():
                services[key] = "ALREADY_BOOKED"
            else:
                services[key] = book or "unknown"

        # pagination: find a "next" arrow that's enabled
        nxt = page.locator("ul.pagination li:not(.disabled) a", has_text=">")
        if await nxt.count() == 0:
            # fall back: numbered next page
            nxt = page.locator("a.paginate_button.next:not(.disabled)")
        if await nxt.count() == 0:
            break
        await nxt.first.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)

    return services


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def diff_states(old: dict, new: dict):
    """Return human-readable list of meaningful changes."""
    changes = []
    # newly bookable services (the thing you care about most)
    for key, status in new.items():
        if status == "BOOKABLE" and old.get(key) != "BOOKABLE":
            changes.append(f"🟢 NOW BOOKABLE: {key}")
    # services that disappeared or changed text otherwise
    for key, status in new.items():
        if key in old and old[key] != status and not (
            status == "BOOKABLE" and old.get(key) != "BOOKABLE"
        ):
            changes.append(f"✏️ changed: {key}\n   {old[key]} → {status}")
    for key in old:
        if key not in new:
            changes.append(f"➖ removed: {key}")
    for key in new:
        if key not in old:
            changes.append(f"➕ new row: {key} ({new[key]})")
    return changes


async def main():
    notify("✅ Prenotami monitor started. Watching for open booking slots...")
    last = load_state()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-US",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0 Safari/537.36"),
        )
        while True:
            try:
                page = await context.new_page()
                await login(page)
                current = await read_services(page)
                await page.close()

                if last is None:
                    notify(f"📋 Baseline captured: {len(current)} services tracked. "
                           "You'll be pinged on any change.")
                else:
                    changes = diff_states(last, current)
                    bookable_now = [k for k, v in current.items() if v == "BOOKABLE"]
                    if changes:
                        msg = "🔔 Prenotami change detected:\n\n" + "\n".join(changes)
                        if bookable_now:
                            msg += ("\n\n➡️ Open now:\n" + "\n".join(bookable_now)
                                    + f"\n\nGo book: {SERVICES_URL}")
                        notify(msg)

                last = current
                save_state(current)

            except Exception as e:
                notify(f"⚠️ Monitor error: {e}")
                # re-create context if the browser died
                try:
                    await context.close()
                except Exception:
                    pass
                context = await browser.new_context()

            await asyncio.sleep(CHECK_EVERY + random.randint(0, JITTER_MAX))


if __name__ == "__main__":
    asyncio.run(main())
