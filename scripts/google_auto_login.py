"""Automated Google sign-in helper for the KNOWS benchmark.

Mints a Playwright ``storage_state.json`` snapshot for
``agentbenchmark@gmail.com`` so the benchmark's agent-side Chromium can
load a fresh, signed-in Google session without any manual UI step. Each
Ray worker calls this script via :mod:`scripts.storage_state_pool` to
get its own per-PID snapshot, which sidesteps the cookie-rotation
collisions that broke the previous 5-way parallel setup.

Two modes
---------
Picked automatically by :func:`perform_login` based on what's available on
disk; both can be forced via the ``--mode`` CLI flag:

- **profile** *(preferred when ``playwright_chrome_profile/`` exists)*:
  open the existing persistent Chromium profile, navigate to
  ``docs.google.com`` to confirm the session, then dump
  ``context.storage_state(...)``. The persistent profile is the
  long-term trust anchor: it holds the "Don't ask again on this
  device" cookie that bypasses Google's passkey / 2-Step Verification
  challenge, and visiting Docs from it also passively refreshes
  ``__Secure-1PSIDTS`` etc. so the source profile stays signed in
  indefinitely. The mint pool's file lock serialises calls so the
  per-profile SingletonLock never collides.

- **credentials** *(fallback when no persistent profile is available)*:
  the original headless email + password flow driven by
  ``GOOGLE_USER_EMAIL`` / ``GOOGLE_USER_PASSWORD``. This works only on
  accounts that don't trigger Google's passkey-required risk path --
  on accounts that do (the common case for ``@gmail.com`` accounts
  signing in from a fresh IP), this mode will time out on the
  passkey prompt; bootstrap with the headed profile flow instead.

Why service accounts can't be used
----------------------------------
Google **service accounts** authenticate only against API endpoints
(Drive / Docs / Sheets / Slides), never the web UI. The agent drives
the actual ``docs.google.com`` UI through Playwright, so it needs real
user-account session cookies; the SA at
``browsergym/knows/auth-data/service-account.json`` stays in its
existing evaluator-only role.

Stealth notes
-------------
Headless Chromium triggers Google's bot detection more aggressively
than a real Chrome window does, which is why the credentials-mode
flow is the fallback rather than the default. The persistent-profile
flow is robust against this because Google has already trusted the
device fingerprint stored in ``playwright_chrome_profile/``.

CLI
---
::

    # First-run bootstrap (one-time human login):
    python scripts/google_auto_login.py --headed --output storage_state.json

    # Automated per-worker mint (default, headless):
    python scripts/google_auto_login.py --output storage_state.json
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

logger = logging.getLogger(__name__)

# Default destination for the minted snapshot. Callers (the per-PID mint
# pool, the run.sh bootstrap step, etc.) can override via --output.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTPUT = _REPO_ROOT / "storage_state.json"

# Persistent Chromium profile used as the long-term Google session anchor.
# When this directory exists and contains a signed-in session, the
# preferred mint mode is to extract storage_state from it (no login flow,
# no passkey challenge, just open + visit + dump). Bootstrap is a one-time
# headed run that leaves the trust cookie in this dir.
_DEFAULT_PROFILE_DIR = _REPO_ROOT / "playwright_chrome_profile"

# Mode-selector values used by the CLI / :func:`perform_login`.
MODE_AUTO = "auto"
MODE_PROFILE = "profile"
MODE_CREDENTIALS = "credentials"

# How long to give the persistent-profile path to settle on a signed-in
# Docs page before we declare the profile session dead and either fall
# back or surface a re-bootstrap message.
_PROFILE_SETTLE_TIMEOUT_MS = 30000

# SID-family cookies are the hard signal that we have a real Google
# session in the snapshot. If the storage_state lacks all of them,
# something is wrong (often: we landed on the sign-in page instead of
# Docs) and we should refuse to save the snapshot rather than handing
# the agent a useless one.
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

# Realistic desktop UA. Google's bot detection is happy with this string as
# long as the rest of the fingerprint (plugins, languages, webdriver flag)
# is consistent, which the launch args below take care of.
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Same window dims the agent's Chromium uses (see browsergym.core.env). Keeping
# them aligned avoids "session looks different" risk-signals from Google.
_VIEWPORT = {"width": 1280, "height": 720}

# Google's identifier-first sign-in URL. ``service=mail`` plus the
# ``continue=https://docs.google.com`` parameter routes us straight to Docs
# after the login completes, which is also a quick smoke test that the
# resulting cookies actually work.
_LOGIN_URL = (
    "https://accounts.google.com/v3/signin/identifier"
    "?continue=https%3A%2F%2Fdocs.google.com%2F"
    "&service=mail&flowName=GlifWebSignIn&flowEntry=ServiceLogin"
)

# In Google's modern sign-in flow, the password input lives at
# ``/v3/signin/challenge/pwd`` -- the path looks like an extra verification
# step but it's just the second page of the normal email -> password flow.
# Treating it as a challenge would falsely abort every login.
_PASSWORD_URL_FRAGMENTS = (
    "/signin/challenge/pwd",
    "/signin/v2/challenge/pwd",
)

# 2FA / risk-based challenge subpaths Google actually uses when it wants a
# human in the loop. We bail out only when the URL matches one of these and
# is *not* the password-entry step above.
_CHALLENGE_URL_FRAGMENTS = (
    "/signin/challenge/totp",       # TOTP authenticator code
    "/signin/challenge/sk",         # hardware security key
    "/signin/challenge/dp",         # display prompt (mobile push)
    "/signin/challenge/ipp",        # phone PIN / SMS
    "/signin/challenge/iap",        # alternate email verification
    "/signin/challenge/selection",  # "How do you want to verify?"
    "/signin/challenge/recaptcha",  # CAPTCHA
    "/signin/challenge/az",         # account verification (unusual activity)
    "/signin/challenge/kpe",        # device prompt for known device
    "/v3/signin/rejected",
    "/speedbump/",
    "deniedsigninrejected",
)


class AutoLoginError(RuntimeError):
    """Raised when the automated sign-in cannot complete on its own."""


def _read_credentials() -> tuple[str, str]:
    """Pull email + password from the environment.

    Returns ``(email, password)``. Raises :class:`AutoLoginError` with a
    descriptive message when either is missing so callers can fall back to
    the legacy persistent-profile path.
    """
    email = os.environ.get("GOOGLE_USER_EMAIL", "").strip()
    password = os.environ.get("GOOGLE_USER_PASSWORD", "")
    if not email or not password:
        raise AutoLoginError(
            "GOOGLE_USER_EMAIL and GOOGLE_USER_PASSWORD must be set in the "
            "environment (typically via .env). Add them and retry."
        )
    return email, password


def _is_on_password_page(url: str) -> bool:
    """Return True iff *url* is the password-entry step of the normal flow."""
    return any(fragment in url for fragment in _PASSWORD_URL_FRAGMENTS)


def _is_on_challenge(url: str) -> bool:
    """Return True iff *url* is a real 2FA / risk challenge a human must clear.

    The password-entry page (``/v3/signin/challenge/pwd``) is *not* a
    challenge -- it's the second step of the normal email-then-password
    flow. We explicitly exclude it so the login flow doesn't bail out mid
    sign-in.
    """
    if _is_on_password_page(url):
        return False
    return any(fragment in url for fragment in _CHALLENGE_URL_FRAGMENTS)


def _wait_and_fill(
    page: Page,
    selector: str,
    value: str,
    *,
    timeout_ms: int,
    description: str,
    verify: bool = True,
) -> None:
    """Wait for *selector* to be visible and ready, then put *value* into it.

    Strategy:
      1. Use ``fill()`` first (sets the value via the element's value
         property in one shot). This is more reliable than ``type()``
         because some Google fields drop keystrokes during the
         transitioning second-step animation -- the failure mode where
         a click "succeeds" but the field stays empty was directly
         visible in the field's ``value`` attribute being empty.
      2. Optionally verify the field actually holds *value*. If it
         doesn't, retry once with ``type(..., delay=...)`` which feeds
         per-key events more deliberately. This covers cases where
         ``fill()`` is intercepted by an event listener.

    ``description`` is only used for log messages.
    """
    try:
        locator = page.locator(selector).first
        locator.wait_for(state="visible", timeout=timeout_ms)
        locator.click()
        locator.fill("")
        locator.fill(value)
    except PlaywrightTimeoutError as exc:
        raise AutoLoginError(
            f"Timed out waiting for the {description} field "
            f"(selector={selector!r}). Google may have changed the UI; "
            f"re-run with --headed to inspect."
        ) from exc

    if not verify:
        return

    actual = locator.input_value(timeout=timeout_ms) or ""
    if actual == value:
        return

    logger.warning(
        "%s field did not hold the typed value after fill() "
        "(actual length=%d, expected length=%d). Retrying with type().",
        description,
        len(actual),
        len(value),
    )
    try:
        locator.click()
        locator.fill("")
        locator.type(value, delay=45)
    except PlaywrightTimeoutError as exc:
        raise AutoLoginError(
            f"Timed out re-typing the {description} field "
            f"(selector={selector!r}). Re-run with --headed to inspect."
        ) from exc

    actual = locator.input_value(timeout=timeout_ms) or ""
    if actual != value:
        raise AutoLoginError(
            f"Could not enter the {description} into "
            f"{selector!r} (length got={len(actual)} expected={len(value)}). "
            "Re-run with --headed to inspect; this usually means Google "
            "is silently blocking automation on this profile."
        )


def _submit_form(
    page: Page,
    *,
    submit_locator: Optional[str] = None,
    description: str,
) -> None:
    """Submit the current Google sign-in form.

    Pressing ``Enter`` on the focused input is the single most reliable
    submission mechanism: it doesn't depend on which Google A/B variant
    of the "Next" button is currently rendered, and it triggers the same
    form-submit handler the button would. We optionally try a button
    click as a backup for the rare case where the active element loses
    focus before our Enter press is dispatched.
    """
    try:
        page.keyboard.press("Enter")
    except Exception as exc:  # noqa: BLE001 -- best-effort, fall through to button
        logger.debug("Enter-press failed during %s: %s; falling back to button.", description, exc)

    if submit_locator is None:
        return

    # Best-effort secondary submit so we don't lose the click in race conditions
    # where Enter was swallowed (e.g. focus on a hidden input). Failure is
    # silent -- if Enter worked we'll be navigating away in a moment anyway.
    try:
        btn = page.locator(submit_locator).first
        if btn.is_visible():
            btn.click(timeout=2000)
    except Exception:  # noqa: BLE001
        pass


def _wait_for_navigation_off_password(page: Page, *, timeout_ms: int) -> None:
    """Block until the page navigates away from the password-entry URL.

    Called immediately after we click Next on the password form. Until
    Google starts the post-password redirect chain, ``page.url`` is still
    ``/v3/signin/challenge/pwd`` -- if we polled for success right away we
    would either time out or (worse) misclassify the in-flight password
    URL as a 2FA challenge. Waiting for the URL to change first makes the
    subsequent classification reliable.

    If we never leave the password page, that almost always means the
    password was rejected (Google shows an inline error instead of
    redirecting). We surface that as a clear ``AutoLoginError`` so the
    caller doesn't blame the wrong subsystem.
    """
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        if not _is_on_password_page(page.url):
            return
        time.sleep(0.4)
    raise AutoLoginError(
        "Stayed on the password page for too long after submitting credentials "
        f"(last URL: {page.url!r}). The password may be wrong or Google may "
        "have shown an inline error. Re-run with --headed to inspect."
    )


def _wait_for_login_complete(page: Page, *, timeout_ms: int) -> None:
    """Block until we land on a signed-in Google surface.

    "Signed in" means either:
      - we've redirected to docs.google.com (the ``continue=`` target), OR
      - we're on accounts.google.com but with ``myaccount.google.com`` / the
        Google account chooser instead of a sign-in form.
    """
    deadline = time.time() + timeout_ms / 1000.0
    last_url = ""
    while time.time() < deadline:
        url = page.url
        last_url = url

        if _is_on_challenge(url):
            raise AutoLoginError(
                "Google is asking for an extra verification step "
                f"(challenge URL: {url}). Re-run with --headed once to "
                "clear the challenge interactively; subsequent headless "
                "runs from this machine will succeed."
            )

        if "docs.google.com" in url:
            return

        if "myaccount.google.com" in url:
            return

        # Mid-flow: still on accounts.google.com but no challenge yet. Keep
        # waiting briefly. Polling (rather than wait_for_url) is more robust
        # against multi-hop redirects through accounts.youtube.com etc.
        time.sleep(0.5)

    raise AutoLoginError(
        f"Login did not reach docs.google.com within {timeout_ms} ms "
        f"(last URL: {last_url!r}). Re-run with --headed to debug."
    )


def _save_state(context: BrowserContext, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(output))
    logger.info("Wrote storage_state to %s", output)


def _state_has_session(state: dict) -> bool:
    """Return True iff *state* contains at least one Google SID-family cookie.

    A snapshot without any of ``SID`` / ``__Secure-1PSID`` / ``HSID`` /
    ``SAPISID`` etc. is effectively a logged-out session: the agent will
    land on the sign-in page instead of Docs. We refuse to save those.
    """
    for cookie in state.get("cookies", []):
        name = cookie.get("name", "")
        if name in _SID_COOKIE_NAMES:
            return True
    return False


def _save_state_if_authed(context: BrowserContext, output: Path) -> None:
    """Save storage_state to *output*, raising if the session is not signed in.

    Distinct from :func:`_save_state` which writes unconditionally;
    callers that already verified the session (e.g. landed on
    ``docs.google.com/document/...``) can use the unconditional helper.
    The defensive variant exists so the profile-extract path can detect
    a stale ``playwright_chrome_profile/`` and surface a clear
    re-bootstrap message instead of writing a useless snapshot.
    """
    state = context.storage_state()
    if not _state_has_session(state):
        sid_present = sorted(
            c.get("name", "")
            for c in state.get("cookies", [])
            if "SID" in c.get("name", "")
        )
        raise AutoLoginError(
            "Snapshot would contain no Google SID-family cookies "
            f"(present SID-like names: {sid_present!r}). The persistent "
            "profile is not signed in. Re-bootstrap with: "
            "`python scripts/google_auto_login.py --headed "
            "--mode profile --output storage_state.json` and complete "
            "the sign-in / passkey prompt once."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    # ``BrowserContext.storage_state(path=...)`` writes the snapshot
    # atomically; calling it again re-serializes the same data, but
    # that's OK -- the cost is negligible and we keep the call site
    # symmetric with :func:`_save_state`.
    context.storage_state(path=str(output))
    logger.info(
        "Wrote storage_state to %s (cookies=%d, signed-in=True)",
        output,
        len(state.get("cookies", [])),
    )


def _clean_profile_singletons(profile_dir: Path) -> None:
    """Remove Chromium ``Singleton*`` lock files from a persistent profile.

    Chromium leaves these symlinks behind when a process crashes; opening
    the profile while they exist makes ``launch_persistent_context`` hang
    waiting for the previous holder. The mint pool's directory-wide file
    lock guarantees we're the only Python process opening the profile, so
    deleting these is safe. Also clears any 0-byte ``LOCK`` files in the
    Default subdir for the same reason.
    """
    candidates = (
        profile_dir / "SingletonLock",
        profile_dir / "SingletonCookie",
        profile_dir / "SingletonSocket",
        profile_dir / "Default" / "LOCK",
    )
    for entry in candidates:
        try:
            if entry.is_symlink() or entry.exists():
                entry.unlink()
        except OSError as exc:  # noqa: BLE001 -- best-effort cleanup
            logger.debug("Could not unlink %s: %s", entry, exc)


def _perform_profile_extract(
    *,
    profile_dir: Path,
    output: Path,
    headless: bool,
    timeout_ms: int,
) -> Path:
    """Open the persistent Chromium profile and extract a fresh storage_state.

    This is the preferred mint mode: no email/password flow runs, no
    passkey challenge fires, and the simple act of visiting
    ``docs.google.com`` from a signed-in profile rotates Google's
    ``__Secure-1PSIDTS`` etc. so the source profile stays fresh
    indefinitely. The mint pool's file lock serialises calls so the
    Chromium SingletonLock never collides between workers.
    """
    if not profile_dir.is_dir():
        raise AutoLoginError(
            f"Persistent profile {profile_dir} does not exist. Run "
            "`python scripts/google_auto_login.py --headed --mode profile` "
            "once to create it (or set --mode credentials to use the "
            "email + password flow as a fallback)."
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
            try:
                # Visit Docs from the profile. If the profile holds a valid
                # session, this lands on the Drive home page; if it
                # doesn't, we end up on accounts.google.com and surface a
                # bootstrap error rather than save a useless snapshot.
                page.goto(
                    "https://docs.google.com/",
                    timeout=timeout_ms,
                    wait_until="domcontentloaded",
                )
                # Brief settle so any post-load redirects + cookie sets
                # complete before we snapshot.
                deadline = time.time() + (_PROFILE_SETTLE_TIMEOUT_MS / 1000.0)
                while time.time() < deadline:
                    url = page.url
                    if "accounts.google.com" in url and "ServiceLogin" in url:
                        raise AutoLoginError(
                            "Persistent profile is signed out (landed on "
                            f"{url!r}). Re-bootstrap with: "
                            "`python scripts/google_auto_login.py --headed "
                            "--mode profile --output storage_state.json`"
                        )
                    if "docs.google.com" in url and "ServiceLogin" not in url:
                        break
                    time.sleep(0.4)

                time.sleep(1.5)
                _save_state_if_authed(context, output)
            except AutoLoginError:
                # Best-effort diagnostics for the operator.
                _capture_failure_diagnostics(page, "profile_extract_failure")
                raise
        finally:
            context.close()

    return output


def _diagnostics_dir() -> Path:
    """Where to drop screenshots / HTML when login fails.

    Defaults to ``<repo>/.bg_storage_state_pool/debug/<pid>``. Configurable
    via ``BROWSERGYM_AUTO_LOGIN_DEBUG_DIR`` so callers can pin it elsewhere
    when running outside the repo.
    """
    raw = os.environ.get("BROWSERGYM_AUTO_LOGIN_DEBUG_DIR")
    if raw:
        base = Path(raw)
    else:
        base = _REPO_ROOT / ".bg_storage_state_pool" / "debug"
    pid_dir = base / str(os.getpid())
    pid_dir.mkdir(parents=True, exist_ok=True)
    return pid_dir


def _capture_failure_diagnostics(page: Page, label: str) -> str:
    """Save a screenshot + HTML snapshot + visible error text to disk.

    Returns a short human-readable note describing where the artifacts
    went, suitable for appending to the error message that we re-raise.
    Best-effort: if any of the captures fail, the failure is logged but
    the original error is still raised by the caller.
    """
    notes: list[str] = []
    try:
        out_dir = _diagnostics_dir()
        ts = time.strftime("%Y%m%d_%H%M%S")
        stem = f"{ts}_{label}"

        try:
            shot_path = out_dir / f"{stem}.png"
            page.screenshot(path=str(shot_path), full_page=True, timeout=5000)
            notes.append(f"screenshot={shot_path}")
        except Exception as exc:  # noqa: BLE001 -- best-effort
            logger.debug("Screenshot capture failed: %s", exc)

        try:
            html_path = out_dir / f"{stem}.html"
            html_path.write_text(page.content(), encoding="utf-8")
            notes.append(f"html={html_path}")
        except Exception as exc:  # noqa: BLE001 -- best-effort
            logger.debug("HTML capture failed: %s", exc)

        # Also try to surface the visible error message Google rendered
        # (e.g. "Wrong password. Try again or click Forgot password..."),
        # which is by far the most useful single piece of debug info.
        try:
            error_text = page.evaluate(
                """() => {
                    const candidates = [
                      ...document.querySelectorAll('[jsname][role="alert"]'),
                      ...document.querySelectorAll('[role="alert"]'),
                      ...document.querySelectorAll('.dEOOab'),
                      ...document.querySelectorAll('.OyEIQ'),
                    ];
                    for (const el of candidates) {
                      const t = (el.innerText || '').trim();
                      if (t) return t;
                    }
                    return '';
                }"""
            )
            if error_text:
                notes.append(f"google_error={error_text!r}")
        except Exception as exc:  # noqa: BLE001 -- best-effort
            logger.debug("Visible-error extraction failed: %s", exc)

        try:
            notes.append(f"title={page.title()!r}")
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001 -- diagnostics must never crash
        logger.debug("Failure diagnostics crashed: %s", exc)

    return " | ".join(notes) if notes else ""


def perform_login(
    *,
    output: Path,
    headless: bool,
    timeout_ms: int,
    mode: str = MODE_AUTO,
    profile_dir: Optional[Path] = None,
) -> Path:
    """Mint a fresh ``storage_state.json`` and return its path.

    Dispatches on *mode*:

    - ``MODE_AUTO`` (default): try profile mode if the persistent
      profile exists, otherwise fall back to credentials mode. This is
      what the per-worker mint pool calls.
    - ``MODE_PROFILE``: always use profile extraction; raise if the
      profile is missing or signed out.
    - ``MODE_CREDENTIALS``: always run the email + password flow.

    Raises :class:`AutoLoginError` on any failure that needs human
    intervention (no profile *and* no creds, signed-out profile, 2FA
    challenge during credentials login, etc.).
    """
    profile = profile_dir or _DEFAULT_PROFILE_DIR

    if mode == MODE_AUTO:
        if profile.is_dir():
            mode = MODE_PROFILE
            logger.info(
                "Auto mode resolved to 'profile' (using %s).", profile
            )
        else:
            mode = MODE_CREDENTIALS
            logger.info(
                "Auto mode resolved to 'credentials' (no profile at %s).",
                profile,
            )

    if mode == MODE_PROFILE:
        return _perform_profile_extract(
            profile_dir=profile,
            output=output,
            headless=headless,
            timeout_ms=timeout_ms,
        )

    if mode != MODE_CREDENTIALS:
        raise AutoLoginError(
            f"Unknown auto-login mode {mode!r}; "
            f"expected one of {MODE_AUTO!r}, {MODE_PROFILE!r}, "
            f"{MODE_CREDENTIALS!r}."
        )

    email, password = _read_credentials()
    logger.info(
        "Signing in via credentials flow as %s (headless=%s)",
        email,
        headless,
    )

    args = [
        f"--window-size={_VIEWPORT['width']},{_VIEWPORT['height']}",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--disable-features=OverlayScrollbars,ExtendedOverlayScrollbars",
    ]

    with sync_playwright() as pw:
        # ``channel="chrome"`` (not the bundled chromium build) makes the UA
        # / fingerprint match what the benchmark's agent-side launch uses,
        # which keeps Google's risk-signal score low.
        browser = pw.chromium.launch(
            channel="chrome",
            headless=headless,
            args=args,
            ignore_default_args=["--enable-automation"],
        )
        try:
            context = browser.new_context(
                viewport=_VIEWPORT,
                user_agent=_DEFAULT_UA,
                locale="en-US",
            )
            # Hide the most obvious automation tell. ``new_context`` cannot
            # take an init script directly, so we add it on the context so
            # every page (including the first one we open below) inherits it.
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{ get: () => undefined });"
            )

            page = context.new_page()
            try:
                page.goto(_LOGIN_URL, timeout=timeout_ms, wait_until="domcontentloaded")

                try:
                    # Step 1: identifier (email) page.
                    _wait_and_fill(
                        page,
                        'input[type="email"]',
                        email,
                        timeout_ms=timeout_ms,
                        description="email",
                    )
                    _submit_form(
                        page,
                        submit_locator="#identifierNext button",
                        description="identifier",
                    )

                    # Step 2: password page. Google routes the password input
                    # through ``/v3/signin/challenge/pwd`` in the modern flow
                    # -- confusing because the URL contains ``challenge`` even
                    # though this is the normal next step, not a 2FA prompt.
                    # We wait for either the password form to render or that
                    # URL to appear so the rest of the script knows we're
                    # ready to type.
                    page.wait_for_selector(
                        'input[type="password"]',
                        state="visible",
                        timeout=timeout_ms,
                    )
                    time.sleep(1.2)
                    _wait_and_fill(
                        page,
                        'input[type="password"]',
                        password,
                        timeout_ms=timeout_ms,
                        description="password",
                    )
                    # Submit via Enter instead of clicking a Next button.
                    # Earlier failures were caused by the button-click path
                    # silently no-op'ing (some Google A/B variants render the
                    # Next button outside the input's containing form, so the
                    # JS submit handler never fired). Pressing Enter on the
                    # focused password input is the most reliable trigger.
                    _submit_form(
                        page,
                        submit_locator="#passwordNext button",
                        description="password",
                    )

                    # Step 3a: wait for navigation away from the password URL.
                    # Without this, _wait_for_login_complete below would
                    # immediately observe ``/signin/challenge/pwd`` (because
                    # the post-submit redirect hasn't started yet) and could
                    # time out without any actionable diagnostics.
                    _wait_for_navigation_off_password(page, timeout_ms=timeout_ms)

                    # Step 3b: settle on docs.google.com (or
                    # myaccount.google.com) to confirm we have a working
                    # session, or detect a real 2FA challenge.
                    _wait_for_login_complete(page, timeout_ms=timeout_ms)

                    # Give Google a beat to set every cookie before snapshot.
                    time.sleep(1.5)

                    # ``_save_state_if_authed`` raises if the resulting
                    # snapshot has no Google SID-family cookies. Without
                    # this guard, a silent "Verify it's you" interstitial
                    # that ends up redirecting back to docs.google.com
                    # would be treated as a successful login by
                    # ``_wait_for_login_complete`` and we'd write a tiny
                    # (~800-byte) anonymous storage_state. The per-worker
                    # mint pool would then hand that file to Chromium,
                    # and every task would land on the sign-in page.
                    _save_state_if_authed(context, output)
                except AutoLoginError as exc:
                    # Capture screenshot + HTML + visible error so the
                    # operator has something to look at instead of a bare
                    # URL. The artifacts land under
                    # ``.bg_storage_state_pool/debug/<pid>/`` by default.
                    notes = _capture_failure_diagnostics(page, "login_failure")
                    if notes:
                        raise AutoLoginError(f"{exc}\nDiagnostics: {notes}") from exc
                    raise
            finally:
                context.close()
        finally:
            browser.close()

    return output


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"Where to write the minted storage_state (default: {_DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--mode",
        choices=(MODE_AUTO, MODE_PROFILE, MODE_CREDENTIALS),
        default=MODE_AUTO,
        help=(
            f"Mint mode. {MODE_AUTO!r} (default) picks {MODE_PROFILE!r} "
            "when playwright_chrome_profile/ exists and falls back to "
            f"{MODE_CREDENTIALS!r} otherwise. {MODE_PROFILE!r} extracts "
            "from the persistent profile (preferred -- bypasses Google's "
            f"passkey 2SV). {MODE_CREDENTIALS!r} runs the headless email + "
            "password flow."
        ),
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help=(
            "Override the persistent Chromium profile path. Defaults to "
            f"{_DEFAULT_PROFILE_DIR}."
        ),
    )
    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument(
        "--headed",
        dest="headless",
        action="store_false",
        help="Run with a visible browser window (recommended for the one-time human bootstrap).",
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
        default=45000,
        help="Per-step timeout in milliseconds (default: 45000).",
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
    )

    try:
        path = perform_login(
            output=args.output,
            headless=args.headless,
            timeout_ms=args.timeout_ms,
            mode=args.mode,
            profile_dir=args.profile_dir,
        )
    except AutoLoginError as exc:
        print(f"Auto-login failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- last-resort logging
        print(f"Auto-login crashed: {exc}", file=sys.stderr)
        return 2

    print(str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
