#!/usr/bin/env python3
"""Non-interactive Google auth refresh for the BrowserGym KNOWS benchmark.

Run this before every benchmark to (re-)mint a fresh ``storage_state.json``:
the script opens the persistent ``playwright_chrome_profile/`` Chromium
profile, recovers from any signed-out state by typing the credentials in
``GOOGLE_USER_EMAIL`` / ``GOOGLE_USER_PASSWORD``, and dumps the resulting
session cookies to ``storage_state.json`` for the agent to consume.

Why this exists
---------------
A long benchmark run eventually invalidates the persistent profile's
session (Google rotates ``__Secure-1PSIDTS`` while five parallel workers
are sharing the same cookie set, so eventually only one worker's
rotation is honored and everyone else gets bumped to the sign-in page).
There is no automated recovery once the profile reaches that state --
the agent just keeps landing on ``accounts.google.com/AccountChooser``
forever. Calling this script between splits forces a re-auth from the
known credentials and re-anchors every worker on a fresh snapshot.

What it handles
---------------
- **Already signed in**: visiting ``docs.google.com`` lands on Docs; we
  snapshot and exit.
- **Signed out** (account chooser / identifier page): we click the
  ``agentbenchmark@gmail.com`` chip if present, fill the email if asked,
  then fill the password. The persistent profile retains Google's
  device-trust cookie even when signed out, which lets the password
  step bypass the otherwise-mandatory passkey prompt.
- **Passkey / 2SV challenge** (no device trust on this machine): we
  capture a screenshot + HTML to ``.bg_storage_state_pool/debug/<pid>/``
  and exit non-zero with a clear "run --headed once to bootstrap" hint.

CLI
---
::

    # Headless, refresh storage_state.json (called by run.sh before
    # every benchmark):
    python extract_auth_state.py

    # Headed bootstrap (one-time, when Google demands a passkey):
    python extract_auth_state.py --headed

The legacy interactive ``input("Press Enter once you're logged in...")``
flow is gone -- this script is fully non-interactive so it can be wired
into ``run.sh`` as a normal preflight step.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

logger = logging.getLogger("extract_auth_state")

_REPO_ROOT = Path(__file__).resolve().parent
PROFILE_DIR = _REPO_ROOT / "playwright_chrome_profile"
OUTPUT_FILE = _REPO_ROOT / "storage_state.json"

# Same window dims and UA as the agent's launch path (browsergym.core.env).
# Keeping these aligned avoids "this session looks different" risk-signals
# from Google when we re-auth from the same profile.
_VIEWPORT = {"width": 1280, "height": 720}
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Modern Google routes the password input through ``/v3/signin/challenge/pwd``
# even on the normal email-then-password flow, so the path containing
# ``challenge`` is *not* a 2SV challenge -- only the more specific subpaths
# below are. Misclassifying ``/pwd`` aborts every login.
_PASSWORD_URL_FRAGMENTS = (
    "/signin/challenge/pwd",
    "/signin/v2/challenge/pwd",
)
_CHALLENGE_URL_FRAGMENTS = (
    "/signin/challenge/totp",
    "/signin/challenge/sk",
    "/signin/challenge/dp",
    "/signin/challenge/ipp",
    "/signin/challenge/iap",
    "/signin/challenge/selection",
    "/signin/challenge/recaptcha",
    "/signin/challenge/az",
    "/signin/challenge/kpe",
    "/v3/signin/rejected",
    "/speedbump/",
    "deniedsigninrejected",
)

# A snapshot is "real" only when at least one of these auth cookies is
# present. The presence check guards against a snapshot of the
# accounts.google.com sign-in page being saved as if it were a session.
_SID_COOKIE_NAMES = (
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
)


class AuthRefreshError(RuntimeError):
    """Raised when the non-interactive refresh cannot complete on its own."""


# -----------------------------------------------------------------------------
# Diagnostics
# -----------------------------------------------------------------------------


def _diagnostics_dir() -> Path:
    raw = os.environ.get("BROWSERGYM_AUTO_LOGIN_DEBUG_DIR")
    base = Path(raw) if raw else _REPO_ROOT / ".bg_storage_state_pool" / "debug"
    pid_dir = base / str(os.getpid())
    pid_dir.mkdir(parents=True, exist_ok=True)
    return pid_dir


def _capture(page: Page, label: str) -> str:
    """Save a screenshot + HTML + visible-error snapshot. Returns a brief note."""
    notes: list[str] = []
    try:
        out = _diagnostics_dir()
        ts = time.strftime("%Y%m%d_%H%M%S")
        stem = f"{ts}_{label}"

        try:
            shot = out / f"{stem}.png"
            page.screenshot(path=str(shot), full_page=True, timeout=5000)
            notes.append(f"screenshot={shot}")
        except Exception as exc:  # noqa: BLE001
            logger.debug("screenshot failed: %s", exc)

        try:
            (out / f"{stem}.html").write_text(page.content(), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.debug("html capture failed: %s", exc)

        try:
            error_text = page.evaluate(
                """() => {
                    const sel = '[role=\"alert\"], [jsname][role=\"alert\"], .dEOOab, .OyEIQ';
                    const els = [...document.querySelectorAll(sel)];
                    for (const el of els) {
                        const t = (el.innerText || '').trim();
                        if (t) return t;
                    }
                    return '';
                }"""
            )
            if error_text:
                notes.append(f"google_error={error_text!r}")
        except Exception as exc:  # noqa: BLE001
            logger.debug("error-text extraction failed: %s", exc)

        try:
            notes.append(f"title={page.title()!r}")
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("diagnostics crashed: %s", exc)
    return " | ".join(notes)


# -----------------------------------------------------------------------------
# Profile / session helpers
# -----------------------------------------------------------------------------


def _clean_profile_singletons(profile: Path) -> None:
    """Remove Chromium ``Singleton*`` lock files left behind by a crashed run."""
    for entry in (
        profile / "SingletonLock",
        profile / "SingletonCookie",
        profile / "SingletonSocket",
        profile / "Default" / "LOCK",
    ):
        try:
            if entry.is_symlink() or entry.exists():
                entry.unlink()
        except OSError as exc:  # noqa: BLE001 -- best-effort
            logger.debug("could not unlink %s: %s", entry, exc)


def _state_has_session(state: dict) -> bool:
    for cookie in state.get("cookies", []):
        if cookie.get("name", "") in _SID_COOKIE_NAMES:
            return True
    return False


def _save_state(context: BrowserContext, output: Path) -> None:
    """Save context.storage_state to *output*; raise if it isn't authed."""
    state = context.storage_state()
    if not _state_has_session(state):
        present = sorted(
            c.get("name", "")
            for c in state.get("cookies", [])
            if "SID" in c.get("name", "")
        )
        raise AuthRefreshError(
            "Snapshot would contain no Google SID-family cookies "
            f"(SID-like names present: {present!r}). "
            "The persistent profile is signed out and the auto-login flow "
            "couldn't recover. Run `python extract_auth_state.py --headed` "
            "once to clear any pending passkey / device-trust prompt."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(output))
    logger.info(
        "Wrote storage_state to %s (cookies=%d, signed-in=True)",
        output,
        len(state.get("cookies", [])),
    )


# -----------------------------------------------------------------------------
# Sign-in flow
# -----------------------------------------------------------------------------


def _is_on_password_page(url: str) -> bool:
    return any(f in url for f in _PASSWORD_URL_FRAGMENTS)


def _is_on_challenge(url: str) -> bool:
    if _is_on_password_page(url):
        return False
    return any(f in url for f in _CHALLENGE_URL_FRAGMENTS)


def _read_credentials() -> tuple[str, str]:
    email = os.environ.get("GOOGLE_USER_EMAIL", "").strip()
    password = os.environ.get("GOOGLE_USER_PASSWORD", "")
    if not email or not password:
        raise AuthRefreshError(
            "GOOGLE_USER_EMAIL and GOOGLE_USER_PASSWORD must be set "
            "(typically via .env). Add them and re-run."
        )
    return email, password


def _try_click_account_chip(page: Page, email: str, *, timeout_ms: int) -> bool:
    """If we're on the account chooser, click the chip for *email*.

    Returns True if a click happened (caller should wait for navigation),
    False if no chip matched (caller should fall back to typing the email
    on the identifier page).
    """
    selectors = (
        f'div[data-email="{email}"]',
        f'li[data-email="{email}"]',
        f'div[data-identifier="{email}"]',
        f'div[role="link"]:has-text("{email}")',
        f'li[role="link"]:has-text("{email}")',
    )
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=2000)
            loc.click()
            logger.info("Clicked account-chooser chip via %s", sel)
            return True
        except Exception:  # noqa: BLE001 -- many possible DOM variants
            continue
    return False


def _wait_and_fill(
    page: Page,
    selector: str,
    value: str,
    *,
    timeout_ms: int,
    description: str,
) -> None:
    """Wait for *selector* to be visible, fill *value*, verify it landed."""
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout_ms)
        loc.click()
        loc.fill("")
        loc.fill(value)
    except PlaywrightTimeoutError as exc:
        raise AuthRefreshError(
            f"Timed out waiting for the {description} field "
            f"(selector={selector!r}). Re-run with --headed to inspect."
        ) from exc

    actual = loc.input_value(timeout=timeout_ms) or ""
    if actual == value:
        return

    logger.warning(
        "%s field empty after fill(); retrying with type().", description
    )
    try:
        loc.click()
        loc.fill("")
        loc.type(value, delay=45)
    except PlaywrightTimeoutError as exc:
        raise AuthRefreshError(
            f"Timed out re-typing the {description} field. "
            "Re-run with --headed to inspect."
        ) from exc

    if (loc.input_value(timeout=timeout_ms) or "") != value:
        raise AuthRefreshError(
            f"Could not enter {description} into {selector!r}. "
            "Google is silently dropping our keystrokes -- this usually "
            "means the bot fingerprint is being rejected; bootstrap with "
            "--headed."
        )


def _submit(page: Page) -> None:
    """Press Enter to submit the current Google sign-in form.

    Pressing Enter on the focused input is more reliable than guessing
    which "Next" button selector Google's current A/B variant rendered.
    """
    try:
        page.keyboard.press("Enter")
    except Exception as exc:  # noqa: BLE001 -- diagnostic only
        logger.debug("Enter press failed (will rely on form auto-submit): %s", exc)


def _click_next_button(page: Page, *, timeout_ms: int) -> bool:
    """Best-effort click of any visible "Next" button on the page.

    Used to advance through Google interstitials that have no input
    field (e.g. ``/v3/signin/confirmidentifier`` "Verify it's you")
    where pressing Enter on a focused input is not an option.
    Returns True if a click happened, False if no Next button matched.
    """
    candidates = (
        ("role", "Next"),
        ("selector", "#identifierNext button"),
        ("selector", "#passwordNext button"),
        ("selector", 'button:has-text("Next")'),
        ("selector", 'div[role="button"]:has-text("Next")'),
    )
    for kind, value in candidates:
        try:
            if kind == "role":
                btn = page.get_by_role("button", name=value).first
            else:
                btn = page.locator(value).first
            if btn.count() == 0:
                continue
            btn.wait_for(state="visible", timeout=2000)
            btn.click()
            logger.info("Clicked Next via %s=%r", kind, value)
            return True
        except Exception:  # noqa: BLE001 -- many DOM variants, try them all
            continue
    return False


def _handle_verify_its_you(page: Page, *, timeout_ms: int) -> bool:
    """If we're on Google's "Verify it's you" interstitial, click Next.

    Returns True if the interstitial was handled (caller should let the
    page navigate and proceed to the password step), False otherwise.

    Detection signals (any of):
      - URL contains ``/signin/confirmidentifier`` or ``/v3/signin/v2/confirmidentifier``.
      - Page H1 / heading text contains "Verify it's you".
      - There's a Next button visible AND no email/password input.
    """
    url = page.url
    title = ""
    try:
        title = page.title() or ""
    except Exception:  # noqa: BLE001
        pass

    on_interstitial = (
        "/signin/confirmidentifier" in url
        or "/signin/v2/confirmidentifier" in url
    )

    if not on_interstitial:
        try:
            heading_text = page.evaluate(
                """() => {
                    const els = [
                        ...document.querySelectorAll('h1, h2, [role="heading"]'),
                    ];
                    for (const el of els) {
                        const t = (el.innerText || '').trim();
                        if (t) return t;
                    }
                    return '';
                }"""
            )
        except Exception:  # noqa: BLE001
            heading_text = ""
        if "verify it's you" in (heading_text or "").lower():
            on_interstitial = True

    if not on_interstitial:
        return False

    logger.info(
        "Hit 'Verify it's you' interstitial (url=%s, title=%r); clicking Next.",
        url,
        title,
    )
    if not _click_next_button(page, timeout_ms=timeout_ms):
        # Some variants have no button at all -- pressing Enter is the
        # last-ditch escape because Google sometimes binds Enter to the
        # primary action via a hidden form.
        try:
            page.keyboard.press("Enter")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Enter press on interstitial failed: %s", exc)

    # Settle so the post-click navigation can fire before the caller
    # checks for the password input again.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass
    return True


def _wait_until(
    page: Page,
    *,
    timeout_ms: int,
    predicate,
    description: str,
) -> str:
    """Poll page.url until *predicate(url)* returns True or we time out."""
    deadline = time.time() + timeout_ms / 1000.0
    last_url = ""
    while time.time() < deadline:
        url = page.url
        last_url = url

        if _is_on_challenge(url):
            raise AuthRefreshError(
                f"Google fired a 2SV challenge while {description} "
                f"(URL: {url}). The persistent profile lost its "
                "device-trust cookie. Bootstrap with `python "
                "extract_auth_state.py --headed` and complete the "
                "passkey prompt once -- subsequent headless runs will "
                "be silent."
            )

        if predicate(url):
            return url
        time.sleep(0.4)

    raise AuthRefreshError(
        f"Timed out {description} within {timeout_ms} ms (last URL: {last_url!r})."
    )


def _ensure_signed_in(page: Page, *, email: str, password: str, timeout_ms: int) -> None:
    """Drive Google's sign-in flow until we land on a signed-in surface.

    Detects the current page state from the URL / DOM and dispatches:
      - already on docs.google.com (signed in): return
      - account chooser: click the chip; pretend we're now on the
        password page if Google jumps straight there, else fall through
        to identifier
      - identifier (email) page: type email + Enter
      - password page: type password + Enter, then wait for signed-in surface
    """
    # Step 0: quick check for already-signed-in.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass

    url = page.url
    if "docs.google.com" in url and "ServiceLogin" not in url:
        logger.info("Profile already signed in (URL=%s)", url)
        return

    # Step 1: account chooser shortcut. Saves a typing round.
    if "AccountChooser" in url or "signin/v2/identifier" in url or "signin/identifier" in url:
        if _try_click_account_chip(page, email, timeout_ms=timeout_ms):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                pass

    # Step 2: identifier (email) page if we're still there.
    url = page.url
    if "signin/identifier" in url or "AccountChooser" in url:
        # Look for the email input. If we're on the chooser without a
        # matching chip, we land here next.
        try:
            page.wait_for_selector(
                'input[type="email"]', state="visible", timeout=timeout_ms
            )
        except PlaywrightTimeoutError:
            # Maybe the chip click already advanced us past identifier.
            pass
        else:
            _wait_and_fill(
                page, 'input[type="email"]', email,
                timeout_ms=timeout_ms, description="email",
            )
            _submit(page)

    # Step 3: password page. Wait for the input to be visible. Some
    # post-chip flows hit /signin/challenge/pwd directly; others detour
    # through ``/signin/confirmidentifier`` (the "Verify it's you"
    # interstitial that Google adds when reauth-ing a previously signed-in
    # account on a slightly stale device). The latter has no password
    # input -- only a Next button -- so we have to click through it
    # before the password field appears. Try waiting once, handle the
    # interstitial if needed, then wait again.
    for attempt in range(3):
        try:
            page.wait_for_selector(
                'input[type="password"]', state="visible", timeout=timeout_ms
            )
            break  # Password input is up; fall through to type+submit.
        except PlaywrightTimeoutError:
            url = page.url
            if "docs.google.com" in url and "ServiceLogin" not in url:
                return  # Already signed in; nothing to do.
            if _handle_verify_its_you(page, timeout_ms=timeout_ms):
                # Interstitial cleared; loop and re-wait for the
                # password input on the post-Next page.
                continue
            notes = _capture(page, "no_password_input")
            raise AuthRefreshError(
                "Password input never appeared after submitting email "
                f"(URL: {url}). Diagnostics: {notes}"
            )
    else:
        # Three retries didn't surface a password input even after
        # interstitial handling -- something else is wrong.
        notes = _capture(page, "no_password_input_after_retries")
        raise AuthRefreshError(
            f"Password input never appeared (URL: {page.url}). Diagnostics: {notes}"
        )

    time.sleep(1.0)
    _wait_and_fill(
        page, 'input[type="password"]', password,
        timeout_ms=timeout_ms, description="password",
    )
    _submit(page)

    # Step 4: validate that the password submit established a session.
    #
    # Google's post-password redirect chain runs cross-domain
    # ``checkConnection=youtube`` calls via XHRs and hidden iframes; in
    # headless Chromium these often complete *without* triggering a
    # top-level navigation, so ``page.url`` can stay on
    # ``/v3/signin/challenge/pwd?checkConnection=...`` indefinitely even
    # after the session cookies are fully set. Polling the URL alone is
    # therefore unreliable. Two-phase strategy:
    #
    #   1. Give Google a short window to redirect naturally (covers the
    #      common case where the parent frame does navigate).
    #   2. If that times out, force-navigate to ``docs.google.com``. If
    #      the password submit actually established a session, this
    #      lands on Docs; if it didn't, we bounce back to a sign-in URL
    #      and we can surface a clear error.
    quick_settle_ms = min(20000, timeout_ms)
    settled = False
    try:
        _wait_until(
            page,
            timeout_ms=quick_settle_ms,
            predicate=lambda u: (
                ("docs.google.com" in u and "ServiceLogin" not in u)
                or "myaccount.google.com" in u
            ),
            description="quick post-password redirect",
        )
        settled = True
    except AuthRefreshError:
        logger.info(
            "Post-password redirect didn't fire within %d ms; "
            "navigating to docs.google.com manually.",
            quick_settle_ms,
        )

    if not settled:
        try:
            page.goto(
                "https://docs.google.com/",
                timeout=timeout_ms,
                wait_until="domcontentloaded",
            )
        except PlaywrightTimeoutError:
            pass

        # Brief grace period so any post-load redirects can fire (e.g.
        # accounts.google.com -> docs.google.com chain after sign-in).
        deadline = time.time() + 10.0
        while time.time() < deadline:
            url = page.url
            if "docs.google.com" in url and "ServiceLogin" not in url:
                break
            if (
                "ServiceLogin" in url
                or "AccountChooser" in url
                or "/v3/signin/" in url
                or "/signin/identifier" in url
            ):
                # Bounced back to a sign-in surface -- session never
                # got established. Capture diagnostics so the operator
                # can see what Google rejected on.
                notes = _capture(page, "post_password_bounced_back")
                raise AuthRefreshError(
                    "Forced-navigate to docs.google.com bounced back to "
                    f"the sign-in flow ({url}). Password submit didn't "
                    "establish a session. This usually means the password "
                    "is wrong, or Google's rate-limiting kicked in. "
                    f"Diagnostics: {notes}"
                )
            time.sleep(0.5)


# -----------------------------------------------------------------------------
# Top-level entrypoint
# -----------------------------------------------------------------------------


def extract_auth_state(
    *,
    profile_dir: Path = PROFILE_DIR,
    output: Path = OUTPUT_FILE,
    headless: bool = True,
    timeout_ms: int = 60000,
) -> Path:
    """Refresh ``output`` from ``profile_dir`` and return the output path."""
    if not profile_dir.is_dir():
        raise AuthRefreshError(
            f"Persistent profile {profile_dir} does not exist. Run "
            "`python extract_auth_state.py --headed` once to create it."
        )

    email, password = _read_credentials()
    logger.info(
        "Refreshing auth state for %s from %s (headless=%s)",
        email,
        profile_dir,
        headless,
    )

    _clean_profile_singletons(profile_dir)

    args = [
        f"--window-size={_VIEWPORT['width']},{_VIEWPORT['height']}",
        "--profile-directory=Default",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--disable-features=OverlayScrollbars,ExtendedOverlayScrollbars",
    ]

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=headless,
            args=args,
            ignore_default_args=["--enable-automation", "--hide-scrollbars"],
            viewport=_VIEWPORT,
            user_agent=_DEFAULT_UA,
            locale="en-US",
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()

            # Land somewhere that triggers Google's redirect chain when
            # signed out. ``docs.google.com`` would do, but using the
            # explicit AccountChooser URL with a login_hint pre-fills the
            # email and skips a step when the account is remembered.
            try:
                page.goto(
                    f"https://accounts.google.com/AccountChooser?Email={email}"
                    "&continue=https%3A%2F%2Fdocs.google.com%2F",
                    timeout=timeout_ms,
                    wait_until="domcontentloaded",
                )
            except PlaywrightTimeoutError as exc:
                raise AuthRefreshError(
                    f"Could not load AccountChooser ({exc})."
                ) from exc

            # Brief settle so any client-side redirects can fire.
            time.sleep(1.5)

            try:
                _ensure_signed_in(
                    page, email=email, password=password, timeout_ms=timeout_ms
                )
                # Final redirect sometimes lands on accounts.google.com
                # before the continue=docs.google.com kicks in. Force the
                # last hop so storage_state captures Docs cookies too.
                if "docs.google.com" not in page.url:
                    page.goto(
                        "https://docs.google.com/",
                        timeout=timeout_ms,
                        wait_until="domcontentloaded",
                    )
                time.sleep(1.5)
                _save_state(context, output)
            except AuthRefreshError as exc:
                notes = _capture(page, "auth_refresh_failure")
                if notes:
                    raise AuthRefreshError(f"{exc}\nDiagnostics: {notes}") from exc
                raise
        finally:
            context.close()

    return output


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_FILE,
        help=f"Where to write the refreshed storage_state (default: {OUTPUT_FILE}).",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=PROFILE_DIR,
        help=f"Persistent Chromium profile path (default: {PROFILE_DIR}).",
    )
    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument(
        "--headed",
        dest="headless",
        action="store_false",
        help="Show the browser window (use for the one-time human bootstrap).",
    )
    headless_group.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        help="Run without a visible window (default).",
    )
    parser.set_defaults(headless=True)
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=60000,
        help="Per-step timeout in ms (default: 60000).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable INFO logging from this script.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )

    try:
        path = extract_auth_state(
            profile_dir=args.profile_dir,
            output=args.output,
            headless=args.headless,
            timeout_ms=args.timeout_ms,
        )
    except AuthRefreshError as exc:
        print(f"Auth refresh failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- last-resort logging
        print(f"Auth refresh crashed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 2

    print(str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
