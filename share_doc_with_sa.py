"""Share a Google Doc with the eval service account using a Playwright UI flow.

The KNOWS evaluator authenticates as a service account
(``doc-evaluator@agent-benchmark-annotation.iam.gserviceaccount.com`` by
default), but agent-created docs are owned by whichever Google account is
present in ``storage_state.json``. Without an explicit share, the service
account gets a 404 when it tries to fetch the doc, which crashes the
evaluator with ``'NoneType' object is not iterable`` further down the
pipeline.

This script drives the Google Docs "Share" dialog with a fresh Playwright
browser context loaded from ``storage_state.json``, adds the service
account email as an Editor, and disables the notification email. It is
idempotent: re-running it after the share succeeds is a no-op.

Usage::

    python share_doc_with_sa.py --doc-id 1abcDEF...

Optional flags let you point at a different storage state, service-account
file, or run headed for debugging.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

_REPO_ROOT = Path(__file__).resolve().parent
_DEFAULT_STORAGE_STATE = _REPO_ROOT / "storage_state.json"
_DEFAULT_SERVICE_ACCOUNT = (
    _REPO_ROOT / "browsergym" / "knows" / "auth-data" / "service-account.json"
)


def _load_service_account_email(sa_path: Path) -> str:
    if not sa_path.exists():
        raise FileNotFoundError(f"Service account file not found: {sa_path}")
    with open(sa_path) as f:
        data = json.load(f)
    email = data.get("client_email")
    if not email:
        raise ValueError(f"No client_email in {sa_path}")
    return email


def _open_share_dialog(page: Page, timeout_ms: int) -> None:
    """Click the document's Share button and wait for the dialog to render."""
    candidates = (
        'button[aria-label*="Share"]',
        'div[role="button"][aria-label*="Share"]',
        'div[aria-label="Share. Private to only me."]',
    )
    last_err: Exception | None = None
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            btn.wait_for(state="visible", timeout=timeout_ms)
            btn.click()
            page.wait_for_selector(
                'input[aria-label*="people"], input[aria-label*="Add people"], input[aria-label*="email"]',
                timeout=timeout_ms,
            )
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    raise RuntimeError(
        f"Could not open Share dialog (last error: {last_err})"
    )


def _add_service_account_email(page: Page, email: str, timeout_ms: int) -> None:
    """Type the SA email into the people-input and select the suggestion."""
    selectors = (
        'input[aria-label*="Add people"]',
        'input[aria-label*="people, groups"]',
        'input[aria-label*="email"]',
    )
    last_err: Exception | None = None
    for sel in selectors:
        try:
            inp = page.locator(sel).first
            inp.wait_for(state="visible", timeout=timeout_ms)
            inp.click()
            inp.fill("")
            inp.type(email, delay=20)
            time.sleep(0.5)
            inp.press("Enter")
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    raise RuntimeError(
        f"Could not enter service account email (last error: {last_err})"
    )


def _disable_notification(page: Page) -> None:
    """Best-effort: untick "Notify people" checkbox if present."""
    candidates = (
        'div[role="checkbox"][aria-label*="Notify"]',
        'input[type="checkbox"][aria-label*="Notify"]',
    )
    for sel in candidates:
        try:
            box = page.locator(sel).first
            if box.count() == 0:
                continue
            checked = box.get_attribute("aria-checked")
            if checked == "true":
                box.click()
            return
        except Exception:  # noqa: BLE001
            continue


def _click_send_or_share(page: Page, timeout_ms: int) -> None:
    """Click the final Send/Share confirmation button."""
    candidates = (
        'button:has-text("Send")',
        'button:has-text("Share")',
        'div[role="button"]:has-text("Send")',
        'div[role="button"]:has-text("Share")',
    )
    last_err: Exception | None = None
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            btn.wait_for(state="visible", timeout=timeout_ms)
            btn.click()
            time.sleep(2)
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    raise RuntimeError(f"Could not click Send/Share (last error: {last_err})")


def share_doc(
    doc_id: str,
    storage_state: Path,
    service_account_path: Path,
    *,
    headless: bool = True,
    timeout_ms: int = 20000,
) -> str:
    """Share *doc_id* with the service-account email and return that email."""
    sa_email = _load_service_account_email(service_account_path)
    print(f"Service account: {sa_email}")
    print(f"Doc id         : {doc_id}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(storage_state))
        page = context.new_page()
        try:
            page.goto(
                f"https://docs.google.com/document/d/{doc_id}/edit",
                timeout=timeout_ms,
            )
            try:
                page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                pass

            time.sleep(2)

            _open_share_dialog(page, timeout_ms)
            _add_service_account_email(page, sa_email, timeout_ms)
            _disable_notification(page)
            _click_send_or_share(page, timeout_ms)

            print(f"Shared {doc_id} with {sa_email}.")
        finally:
            context.close()
            browser.close()

    return sa_email


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc-id", required=True, help="Google Doc id to share")
    parser.add_argument(
        "--storage-state",
        type=Path,
        default=_DEFAULT_STORAGE_STATE,
        help=f"Path to Playwright storage_state.json (default: {_DEFAULT_STORAGE_STATE})",
    )
    parser.add_argument(
        "--service-account",
        type=Path,
        default=_DEFAULT_SERVICE_ACCOUNT,
        help=f"Path to service-account.json (default: {_DEFAULT_SERVICE_ACCOUNT})",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible browser (handy for debugging Share-UI changes).",
    )
    args = parser.parse_args(argv)

    try:
        share_doc(
            doc_id=args.doc_id,
            storage_state=args.storage_state,
            service_account_path=args.service_account,
            headless=not args.headed,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Share failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
