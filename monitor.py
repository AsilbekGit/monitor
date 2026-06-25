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
# (The old heartbeat is replaced by a single status message that gets edited
# in place each cycle, so this value is no longer used.)
HEARTBEAT_EVERY = 10 * 60

# Set to False to watch the browser on screen while debugging. True = invisible.
HEADLESS = True

STATE_FILE = "last_state.json"

# ---------------------------------------------------------------------------
# Deep-check a specific service by actually CLICKING its Book button and seeing
# whether real dates appear. This is for services that always show a BOOK button
# but usually say "All appointments for this service are currently booked" when
# clicked (e.g. National D Visa). We alert only when that message is GONE.
#
# The page can be in English OR Italian, so we match the service Description
# against ANY of these phrases (case-insensitive substring). The National D
# visa row reads "National D Visa ..." in EN and "Visti Nazionali D ..." in IT.
# Set to None / empty list to disable deep-checking.
DEEP_CHECK_DESCRIPTIONS = [
    "national d visa",      # English
    "visti nazionali d",    # Italian
]
# Friendly name to show in Telegram messages.
DEEP_CHECK_LABEL = "National D Visa"

# The message shown when the service is clicked but has no free dates. If the
# page shows anything OTHER than these after clicking Book, we treat it as a
# possible opening and alert. (Multiple languages/wordings for safety.)
NO_DATES_MARKERS = [
    # English
    "all appointments for this service are currently booked",
    "currently booked",
    "no availability",
    # Italian — the real popup text seen on this portal:
    # "Stante l'elevata richiesta i posti disponibili per il servizio
    #  scelto sono esauriti."
    "posti disponibili per il servizio scelto sono esauriti",
    "sono esauriti",                      # "are sold out"
    "elevata richiesta",                  # "high demand"
    "non ci sono date disponibili",       # no dates available
    "al momento non ci sono",
    "tutti gli appuntamenti",             # all appointments...
    "non ci sono posti",
]

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

# Holds the id of the live status message so the shutdown handler can mark it
# stopped. Also remembers the interval so we can show "next check expected by".
STATUS = {"msg_id": None, "interval": 0}


def notify(text: str):
    """Send a NEW Telegram message. Returns the message_id (or None)."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": text[:4000],
                  "disable_web_page_preview": True},
            timeout=20,
        )
        data = r.json()
        if data.get("ok"):
            return data["result"]["message_id"]
    except Exception as e:
        print("Telegram send failed:", e)
    return None


def edit_message(message_id, text: str):
    """Edit an existing Telegram message in place. Returns True on success."""
    if not message_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText",
            data={"chat_id": TG_CHAT_ID, "message_id": message_id,
                  "text": text[:4000], "disable_web_page_preview": True},
            timeout=20,
        )
        return r.json().get("ok", False)
    except Exception as e:
        print("Telegram edit failed:", e)
        return False


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
    # Visit the Services page. If we're not authenticated, Prenotami sends us
    # first to a landing page (/Home) with a "LOG IN TO ACCESS THE PORTAL"
    # button, and only after clicking that do we reach the iam.esteri.it form.
    print("[login] going to Services page...")
    await page.goto(SERVICES_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    print("[login] landed on:", page.url)

    # Already logged in? Only if we actually landed ON the services page.
    # NOTE: when logged out, Prenotami sends us to
    #   /Home?ReturnUrl=%2fServices
    # which CONTAINS the word "Services" in the query string but is NOT the
    # services page. So we must check the path, and treat /Home as logged-out.
    from urllib.parse import urlparse
    path = urlparse(page.url).path.lower()
    on_home = path.startswith("/home")
    on_services = path.endswith("/services")
    if IAM_HOST not in page.url and on_services and not on_home:
        print("[login] already authenticated.")
        return

    # Step 1: reach the SSO sign-in form. The landing page's login button is a
    # plain <a> whose href is the full OAuth authorize URL on iam.esteri.it.
    # That URL contains a fresh PKCE code_challenge each load, so we must READ
    # the current href from the page rather than hardcode it. Navigating to the
    # href directly is far more reliable than clicking the styled link.
    if IAM_HOST not in page.url and not on_services:
        print("[login] reading the login link from the landing page...")
        try:
            await page.wait_for_selector("a[href*='iam.esteri.it']", timeout=15000)
        except Exception:
            pass

        # The login link is the <a> pointing at iam.esteri.it/login/oauth2.
        login_href = None
        links = page.locator("a[href*='iam.esteri.it']")
        n = await links.count()
        for i in range(n):
            href = await links.nth(i).get_attribute("href")
            if href and "oauth2/authorize" in href:
                login_href = href
                break
        if login_href is None and n > 0:
            # fall back to the first iam.esteri.it link
            login_href = await links.first.get_attribute("href")

        if not login_href:
            print("[login] LOGIN LINK NOT FOUND. Listing iam/portal links:")
            alllinks = page.locator("a")
            for i in range(min(await alllinks.count(), 40)):
                el = alllinks.nth(i)
                txt = (await el.inner_text()).strip().replace("\n", " ")
                href = await el.get_attribute("href")
                if href and ("iam" in href or "portal" in (txt or "").lower()
                             or "login" in href.lower()):
                    print(f"    text={txt!r}  href={href!r}")
            raise RuntimeError(
                "Could not find the iam.esteri.it login link on the landing page."
            )

        print("[login] navigating to the SSO login URL...")
        await page.goto(login_href, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        print("[login] after login navigation, on:", page.url)

    # Already authenticated (session cookie) and dropped on UserArea/Services?
    post_path = urlparse(page.url).path.lower()
    if IAM_HOST not in page.url and (
        post_path.endswith("/userarea") or post_path.endswith("/services")
    ) and not post_path.startswith("/home"):
        print("[login] appears already authenticated.")
        return

    # Step 2: we should now be on the iam.esteri.it "Sign in" page.
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
    print("[read] navigating to Services table...")
    # Navigate directly to /Services by URL.
    await page.goto(SERVICES_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    print("[read] on:", page.url)

    # If we got bounced to /Home, the session isn't valid - raise so the loop
    # re-runs login next cycle rather than silently reading 0 services.
    from urllib.parse import urlparse
    if urlparse(page.url).path.lower().startswith("/home"):
        raise RuntimeError("Bounced to /Home - not logged in; will retry login.")

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


async def deep_check_service(page, description_substrs):
    """
    For a specific service (matched if its Description contains ANY of the given
    substrings, case-insensitive), click its Book button and determine whether
    real appointment dates are available.

    Returns one of:
      "NO_DATES"  - clicked, but page says all appointments booked / no dates
      "MAYBE_OPEN"- clicked, and the 'no dates' message was NOT shown (could be
                    a real calendar -> worth alerting)
      "NOT_FOUND" - the service row / Book button wasn't found this cycle
      "ERROR"     - something went wrong (treated as inconclusive, no alert)

    IMPORTANT: this only READS the result. It never selects a date or confirms
    a booking. After checking it returns to the Services list.
    """
    # Accept either a single string or a list.
    if isinstance(description_substrs, str):
        description_substrs = [description_substrs]
    wanted = [s.lower() for s in description_substrs]
    print(f"[deep] checking for {description_substrs} by clicking its Book button...")
    try:
        # Always reload the Services list so we start at page 1 (read_services
        # may have left us on a later page).
        await page.goto(SERVICES_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Find the row whose description contains any wanted text, then its Book
        # button. Walk pages if needed.
        target_btn = None
        page_num = 1
        while target_btn is None:
            rows = page.locator("table tbody tr")
            count = await rows.count()
            print(f"[deep] scanning page {page_num}: {count} rows")
            for i in range(count):
                cells = rows.nth(i).locator("td")
                if await cells.count() < 4:
                    continue
                desc = (await cells.nth(2).inner_text()).strip().lower()
                if any(w in desc for w in wanted):
                    print(f"[deep] matched row: {desc!r}")
                    # Found the row. Grab the first clickable control in the
                    # booking cell (the PRENOTA/BOOK button). We accept ANY
                    # button/link here since we already matched the description.
                    booking_cell = cells.nth(3)
                    btns = booking_cell.locator("a, button")
                    nb = await btns.count()
                    print(f"[deep]   booking cell has {nb} clickable element(s)")
                    if nb > 0:
                        # prefer one whose text looks like a book button
                        chosen = None
                        for b in range(nb):
                            label = (await btns.nth(b).inner_text()).strip().lower()
                            print(f"[deep]   button[{b}] text={label!r}")
                            if any(w in label for w in BOOK_BUTTON_TEXTS):
                                chosen = btns.nth(b)
                                break
                        target_btn = chosen if chosen is not None else btns.first
                        break
                    else:
                        print("[deep]   matched row has NO button (no slot open).")
                        # Row exists but no button -> definitely no dates.
                        return "NO_DATES"
            if target_btn is not None:
                break
            # next page?
            nxt = page.locator("ul.pagination li:not(.disabled) a", has_text=">")
            if await nxt.count() == 0:
                nxt = page.locator("a.paginate_button.next:not(.disabled)")
            if await nxt.count() == 0:
                print("[deep] no more pages.")
                break
            await nxt.first.click()
            await page.wait_for_timeout(2000)
            page_num += 1

        if target_btn is None:
            print("[deep] target service/Book button not found.")
            return "NOT_FOUND"

        # Handle the JS alert/dialog that may pop up ("All appointments...").
        dialog_text = {"msg": None}

        async def on_dialog(dialog):
            dialog_text["msg"] = dialog.message
            await dialog.dismiss()

        page.on("dialog", on_dialog)

        # Click Book. This may: open a JS alert, show an in-page message, or
        # navigate to a calendar page.
        try:
            await target_btn.click()
        except Exception as e:
            print("[deep] click issue:", e)
        await page.wait_for_timeout(3000)

        # Gather all the text we can see to judge the outcome.
        combined = (dialog_text["msg"] or "")
        try:
            combined += " " + (await page.inner_text("body"))
        except Exception:
            pass
        combined_l = combined.lower()

        page.remove_listener("dialog", on_dialog)

        # Did we get the "no dates / all booked" message?
        no_dates = any(m in combined_l for m in NO_DATES_MARKERS)

        # Return to the Services list regardless of outcome (don't stay on a
        # calendar or leave a dialog around).
        try:
            await page.goto(SERVICES_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
        except Exception:
            pass

        if no_dates:
            print("[deep] result: still no dates (all booked).")
            return "NO_DATES"
        else:
            print("[deep] result: 'no dates' message NOT found -> possible opening!")
            return "MAYBE_OPEN"

    except Exception as e:
        print("[deep] error:", e)
        return "ERROR"


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
    import datetime

    def now_str():
        return datetime.datetime.now().strftime("%H:%M:%S")

    def next_check_str():
        # Latest time the next check should arrive (interval + max jitter + slack).
        secs = CHECK_EVERY + JITTER_MAX + 90
        t = datetime.datetime.now() + datetime.timedelta(seconds=secs)
        return t.strftime("%H:%M:%S")

    STATUS["interval"] = CHECK_EVERY

    # One persistent status message that we EDIT each cycle (no new spam).
    STATUS["msg_id"] = notify("🟢 Prenotami monitor is running.\nStarting first check…")

    last = load_state()
    last_deep = None  # previous deep-check result for the target service

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

                # Deep-check the target service by clicking its Book button.
                deep_result = None
                if DEEP_CHECK_DESCRIPTIONS:
                    deep_result = await deep_check_service(page, DEEP_CHECK_DESCRIPTIONS)

                await page.close()

                # Alert if the deep-checked service went from "no dates" to a
                # possible opening (MAYBE_OPEN). We alert on entering MAYBE_OPEN,
                # not every cycle, to avoid spam.
                if deep_result == "MAYBE_OPEN" and last_deep != "MAYBE_OPEN":
                    notify(
                        f"🚨 POSSIBLE OPENING: {DEEP_CHECK_LABEL}\n\n"
                        f"Clicking Book no longer shows the 'all appointments "
                        f"booked' message — dates may be available RIGHT NOW.\n\n"
                        f"Go book immediately: {SERVICES_URL}")
                    # fresh status line below the alert
                    STATUS["msg_id"] = notify("🟢 Monitoring continues…")
                if deep_result is not None:
                    last_deep = deep_result

                changed = False
                # Human-readable line for the deep-checked service.
                if deep_result == "NO_DATES":
                    deep_line = f"🎯 {DEEP_CHECK_LABEL}: no dates yet."
                elif deep_result == "MAYBE_OPEN":
                    deep_line = f"🎯 {DEEP_CHECK_LABEL}: DATES MAY BE OPEN!"
                elif deep_result == "NOT_FOUND":
                    deep_line = f"🎯 {DEEP_CHECK_LABEL}: not found this cycle."
                else:
                    deep_line = ""

                if last is None:
                    # First baseline.
                    bookable_now = [k for k, v in current.items() if v == "BOOKABLE"]
                    open_line = (f"{len(bookable_now)} open now."
                                 if bookable_now else "Nothing open right now.")
                    edit_message(STATUS["msg_id"],
                        f"🟢 Prenotami monitor running.\n"
                        f"📋 Baseline: {len(current)} services tracked.\n"
                        f"{open_line}\n"
                        + (deep_line + "\n" if deep_line else "")
                        + f"Last checked: {now_str()}\n"
                        f"Next check by: {next_check_str()}")
                else:
                    changes = diff_states(last, current)
                    bookable_now = [k for k, v in current.items() if v == "BOOKABLE"]
                    if changes:
                        changed = True
                        # Real change -> brand NEW message so it stands out.
                        msg = "🔔 Prenotami change detected:\n\n" + "\n".join(changes)
                        if bookable_now:
                            msg += ("\n\n➡️ Open now:\n" + "\n".join(bookable_now)
                                    + f"\n\nGo book: {SERVICES_URL}")
                        notify(msg)
                        # Start a fresh status message below the alert so the
                        # live status line stays at the bottom of the chat.
                        STATUS["msg_id"] = notify("🟢 Monitoring continues…")

                    # Update (edit) the persistent status line every cycle.
                    open_line = (f"{len(bookable_now)} open now."
                                 if bookable_now else "No changes.")
                    edit_message(STATUS["msg_id"],
                        f"🟢 Prenotami monitor running.\n"
                        f"{open_line}\n"
                        f"{len(current)} services tracked.\n"
                        + (deep_line + "\n" if deep_line else "")
                        + f"Last checked: {now_str()}\n"
                        f"Next check by: {next_check_str()}")

                last = current
                save_state(current)

            except Exception as e:
                import traceback
                traceback.print_exc()
                # Save a screenshot of where it got stuck, for diagnosis.
                try:
                    if page:
                        await page.screenshot(path="error_screenshot.png", full_page=True)
                        print("[error] saved error_screenshot.png  (look at this file!)")
                        print("[error] the browser was on:", page.url)
                except Exception:
                    pass
                notify(f"⚠️ Monitor error: {e}")

                if not HEADLESS:
                    # Debug mode: keep the window open so you can see the stuck
                    # page. Press Ctrl+C in the terminal to quit.
                    print("\n[error] PAUSED for inspection. The browser window is "
                          "kept open so you can see where it stopped.")
                    print("[error] Look at the browser, then press Ctrl+C to quit.\n")
                    while True:
                        await asyncio.sleep(5)

                # Headless mode: recover and keep monitoring.
                try:
                    await context.close()
                except Exception:
                    pass
                context = await browser.new_context()

            await asyncio.sleep(CHECK_EVERY + random.randint(0, JITTER_MAX))


def _mark_stopped(reason="stopped"):
    """Edit the live status message to show the bot is no longer checking."""
    import datetime
    t = datetime.datetime.now().strftime("%H:%M:%S")
    edit_message(STATUS["msg_id"],
        f"🔴 Prenotami monitor STOPPED — not checking.\n"
        f"Reason: {reason}\n"
        f"Stopped at: {t}\n"
        f"(Restart the script on the Mac Studio to resume.)")


if __name__ == "__main__":
    import signal

    # Handle `kill`/termination (e.g. logout, shutdown) gracefully too.
    def _on_term(signum, frame):
        raise KeyboardInterrupt()
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except Exception:
        pass

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[shutdown] stopping, marking status as stopped in Telegram...")
        _mark_stopped("manual stop (Ctrl+C) or terminal closed")
    except Exception as e:
        # Unexpected crash: mark stopped so you see it in Telegram.
        print("[shutdown] crashed:", e)
        _mark_stopped(f"crashed: {e}")
        raise
