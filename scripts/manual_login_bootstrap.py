"""One-shot interactive Google sign-in bootstrap for the KNOWS benchmark.

Opens a real (headed) Chrome window pointed at the persistent
``playwright_chrome_profile/`` directory and waits for the human
operator to sign in by hand. As soon as the page settles on
``docs.google.com`` (i.e. sign-in completed and any passkey / "Don't ask
again on this device" trust cookie was set), the script dumps the
context's ``storage_state`` to ``storage_state.json`` and exits.

This is the recommended bootstrap path when the persistent profile has
lost its device-trust cookie and Google demands passkey / 2-Step
Verification on every sign-in:

  - ``scripts/google_auto_login.py --mode profile`` aborts because the
    profile is already signed out (it doesn't drive the UI).
  - ``scripts/google_auto_login.py --mode credentials`` hits the same
    passkey gate the headless flow can't pass.

This script doesn't try to drive the UI -- it just gives the operator
an interactive browser, then captures the resulting cookies.

Usage
-----
::

    set -a; source .env; set +a   # load credentials into env (optional)
    python scripts/manual_login_bootstrap.py

When the Chrome window opens:
  1. Click your account / type the email if asked.
  2. Type the password (or choose passkey / Touch ID, etc.).
  3. If "Use your passkey to confirm it's really you" appears, leave
     the "Don't ask again on this device" box checked and click
     Continue + Touch ID. If Touch ID isn't available, click
     "Try another way" -> "Get a verification code on phone" and
     enter the SMS code.
  4. Wait until you land on https://docs.google.com/. The script will
     detect that and exit on its own with "Wrote storage_state ...".

The script also re-saves the persistent profile's storage_state into
both ``storage_state.json`` (for legacy callers) and the per-PID mint
pool dir, then prints next steps so the operator knows the benchmark
is unblocked.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

logger = logging.getLogger("manual_login_bootstrap")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PROFILE = _REPO_ROOT / "playwright_chrome_profile"
_DEFAULT_OUTPUT = _REPO_ROOT / "storage_state.json"

_VIEWPORT = {"width": 1280, "height": 720}
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# SID-family cookies are the hard signal that we have a real Google
# session in the snapshot. If none of these are present, refuse to
# save -- otherwise the agent gets handed a useless empty snapshot.
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


def _state_has_session(state: dict) -> bool:
    for cookie in state.get("cookies", []):
        if cookie.get("name", "") in _SID_COOKIE_NAMES:
            return True
    return False


def _clean_profile_singletons(profile: Path) -> None:
    """Best-effort removal of Chromium SingletonLock files from a crashed run."""
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


def _is_signed_in_url(url: str) -> bool:
    """Return True when *url* indicates we landed on a signed-in surface.

    docs.google.com is the canonical post-login destination for the
    benchmark. myaccount.google.com is also accepted in case the user
    happens to navigate there during sign-in.
    """
    if not url:
        return False
    if "ServiceLogin" in url or "/v3/signin/" in url or "AccountChooser" in url:
        return False
    return "docs.google.com" in url or "myaccount.google.com" in url


def _save_state_if_authed(context: BrowserContext, output: Path) -> bool:
    """Save context.storage_state to *output* iff it contains real auth.

    Returns True on success, False if no SID cookies are present yet
    (caller should keep waiting).
    """
    state = context.storage_state()
    if not _state_has_session(state):
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(output))
    print(
        f"Wrote storage_state to {output} "
        f"(cookies={len(state.get('cookies', []))}, signed-in=True)",
        flush=True,
    )
    return True


def bootstrap(
    *,
    profile_dir: Path,
    output: Path,
    timeout_s: int,
) -> int:
    """Open a headed Playwright Chrome and wait for manual sign-in.

    Returns 0 on success, non-zero on failure.
    """
    if not profile_dir.is_dir():
        # First-run case: create the directory so launch_persistent_context
        # can populate it. The on-disk profile is what holds Google's
        # device-trust cookie across runs.
        profile_dir.mkdir(parents=True, exist_ok=True)

    _clean_profile_singletons(profile_dir)

    args = [
        f"--window-size={_VIEWPORT['width']},{_VIEWPORT['height']}",
        "--profile-directory=Default",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--disable-features=OverlayScrollbars,ExtendedOverlayScrollbars",
    ]

    print("Opening Chrome with the persistent profile.", flush=True)
    print(
        "Please sign in to Google as agentbenchmark@gmail.com. "
        "Leave the 'Don't ask again on this device' box checked. "
        "If passkey/Touch ID is required, choose 'Try another way' "
        "if it doesn't work in this profile.",
        flush=True,
    )
    print(
        "The script will exit on its own as soon as you land on "
        "docs.google.com.",
        flush=True,
    )

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            args=args,
            ignore_default_args=["--enable-automation", "--hide-scrollbars"],
            viewport=_VIEWPORT,
            user_agent=_DEFAULT_UA,
            locale="en-US",
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(
                    "https://accounts.google.com/AccountChooser"
                    "?continue=https%3A%2F%2Fdocs.google.com%2F",
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
            except PlaywrightTimeoutError as exc:
                logger.warning("initial goto timed out: %s", exc)

            deadline = time.time() + timeout_s
            saved = False
            last_logged_url = ""
            while time.time() < deadline:
                # Page may have been closed by the user; bail gracefully.
                if page.is_closed():
                    print(
                        "Browser tab was closed before sign-in completed.",
                        flush=True,
                    )
                    return 2

                try:
                    url = page.url
                except Exception:  # noqa: BLE001 -- page might be navigating
                    url = ""

                if url and url != last_logged_url:
                    print(f"  current URL: {url}", flush=True)
                    last_logged_url = url

                if _is_signed_in_url(url):
                    # Give Google a moment to finish setting all cookies
                    # (the post-login redirect chain is async).
                    time.sleep(2.0)
                    if _save_state_if_authed(context, output):
                        saved = True
                        break
                    # Cookies not yet set -- keep waiting briefly.
                time.sleep(1.0)

            if not saved:
                print(
                    f"Timed out after {timeout_s}s waiting for sign-in. "
                    "Re-run the script and try again.",
                    flush=True,
                )
                return 3
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass

    print(
        "\nNext steps:\n"
        "  - The persistent profile is now signed in and trusted by Google.\n"
        "  - Subsequent benchmark runs will mint per-worker storage_state\n"
        "    snapshots silently via scripts/google_auto_login.py (--mode\n"
        "    auto, profile path).\n",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=_DEFAULT_PROFILE,
        help=f"Persistent Chromium profile path (default: {_DEFAULT_PROFILE}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"Where to write the snapshot (default: {_DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=600,
        help="Max seconds to wait for the operator to finish sign-in (default: 600).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable INFO logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )

    return bootstrap(
        profile_dir=args.profile_dir,
        output=args.output,
        timeout_s=args.timeout_s,
    )


if __name__ == "__main__":
    raise SystemExit(main())
