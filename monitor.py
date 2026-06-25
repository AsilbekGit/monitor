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
# Config: your personal values live in config.py (next to this file).
# Open config.py and fill in the four values there. Keeping them in a separate
# file means you can share monitor.py without leaking your password.
# ---------------------------------------------------------------------------
try:
    import config
except ImportError:
    raise SystemExit(
        "Missing config.py. Create a file named config.py in this same folder "
        "with your PRENOTAMI_USER, PRENOTAMI_PASS, TG_TOKEN and TG_CHAT_ID."
    )

USERNAME    = config.PRENOTAMI_USER    # the email you log in with
PASSWORD    = config.PRENOTAMI_PASS
TG_TOKEN    = config.TG_TOKEN          # from @BotFather
TG_CHAT_ID  = config.TG_CHAT_ID        # your Telegram group chat id (with minus sign)

SERVICES_URL = "https://prenotami.esteri.it/Services"
# Login now happens on the central identity provider (iam.esteri.it). Visiting
# the Services page while logged out auto-redirects there, so we don't hardcode
# the full SSO URL.
IAM_HOST = "iam.esteri.it"

# Polling: 3 min base + up to 60s random jitter. Do NOT lower much: Prenotami
# blocks accounts that hammer it. 60s like you asked is risky; 180s is safer.
CHECK_EVERY = 180
JITTER_MAX  = 60

# Heartbeat: send a quiet "still watching" message every this-many seconds so
# you know the bot is alive even when nothing has changed. Set to 0 to disable.
HEARTBEAT_EVERY = 10 * 60   # every 10 minutes

# Set to False to watch the browser on screen while debugging. True = invisible.
HEADLESS = True

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


async def _fill_first(page, selectors, value, what):
    """Try each selector until one exists, fill it. Raise if none found."""
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if await loc.count() > 0:
                await loc.first.fill(value)
                return sel
        except Exception:
            continue
    raise RuntimeError(
        f"Could not find the {what} field. Tried: {selectors}. "
        "Send a screenshot of the login page so the selector can be fixed."
    )


async def login(page):
    # Visit the Services page. If we're not authenticated, Prenotami bounces us
    # to the iam.esteri.it sign-in page automatically.
    print("[login] going to Services page...")
    await page.goto(SERVICES_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    print("[login] landed on:", page.url)

    # Already logged in (session reused)? Then we're on Services already.
    if IAM_HOST not in page.url and "Services" in page.url:
        print("[login] already authenticated.")
        return

    # Otherwise we should now be on the iam.esteri.it "Sign in" page.
    # Fields are labelled "User Name" and "Password", submit button says "Next".
    username_selectors = [
        "input[name='username']", "input[name='userName']",
        "input[name='User Name']", "input[type='text']",
        "input[placeholder='User Name']", "#username", "#userNameInput",
    ]
    pass_selectors = [
        "input[name='password']", "input[type='password']",
        "input[placeholder='Password']", "#password",
    ]
    submit_selectors = [
        "button:has-text('Next')", "button:has-text('Avanti')",
        "button:has-text('Sign in')", "button:has-text('Accedi')",
        "button[type=submit]", "input[type=submit]",
    ]

    # The SSO form may load its inputs a moment after the page; wait for one.
    print("[login] waiting for password field...")
    try:
        await page.wait_for_selector("input[type='password']", timeout=20000)
    except Exception:
        print("[login] WARNING: password field never appeared.")

    print("[login] filling credentials...")
    await _fill_first(page, username_selectors, USERNAME, "User Name")
    await _fill_first(page, pass_selectors, PASSWORD, "password")

    print("[login] clicking Next...")
    clicked = False
    for sel in submit_selectors:
        loc = page.locator(sel)
        if await loc.count() > 0:
            await loc.first.click()
            clicked = True
            break
    if not clicked:
        raise RuntimeError("Could not find the 'Next' button. Send a login-page screenshot.")

    # After Next, the IdP redirects back through OAuth to prenotami Services.
    print("[login] waiting for redirect back to Services...")
    try:
        await page.wait_for_url("**/Services**", timeout=30000)
    except Exception:
        await page.wait_for_timeout(3000)
    print("[login] now on:", page.url)

    # If we're still on the identity provider, login didn't complete.
    if IAM_HOST in page.url:
        raise RuntimeError(
            "Login did not complete - wrong username/password, a second step "
            "(e.g. CAPTCHA or extra prompt), or the form changed. "
            "Send a screenshot of where it's stuck."
        )
    print("[login] success.")


async def read_services(page):
    """
    Returns a dict: { "<Type> | <Service> | <Description>": booking_status }
    booking_status is "BOOKABLE" or the raw unavailable text.
    Walks through all pagination pages.
    """
    print("[read] loading services table...")
    await page.goto(SERVICES_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    try:
        await page.wait_for_selector("table tbody tr", timeout=15000)
    except Exception:
        print("[read] WARNING: services table not found.")
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
        await page.wait_for_timeout(2000)

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
    last_heartbeat = time.time()
    cycles_since_heartbeat = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            locale="en-US",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0 Safari/537.36"),
        )
        while True:
            page = None
            try:
                print("\n[cycle] starting new check...")
                page = await context.new_page()
                await login(page)
                current = await read_services(page)
                print(f"[cycle] read {len(current)} services.")
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
                cycles_since_heartbeat += 1

                # Quiet heartbeat so you know it's alive when nothing changes.
                if HEARTBEAT_EVERY and (time.time() - last_heartbeat) >= HEARTBEAT_EVERY:
                    bookable_now = [k for k, v in current.items() if v == "BOOKABLE"]
                    open_line = (f"\n{len(bookable_now)} service(s) currently open."
                                 if bookable_now else "\nNothing open right now.")
                    notify(f"💓 Still watching. {cycles_since_heartbeat} checks since "
                           f"last heartbeat, {len(current)} services tracked.{open_line}")
                    last_heartbeat = time.time()
                    cycles_since_heartbeat = 0

            except Exception as e:
                import traceback
                traceback.print_exc()
                # Save a screenshot of where it got stuck, for diagnosis.
                try:
                    if page:
                        await page.screenshot(path="error_screenshot.png", full_page=True)
                        print("[error] saved error_screenshot.png")
                except Exception:
                    pass
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
