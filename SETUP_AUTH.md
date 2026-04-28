# Setting Up Google Authentication for BrowserGym Benchmark

This guide explains how the BrowserGym benchmark keeps the agent's Playwright
browser signed into Google without any manual login step, and how to switch
to the legacy persistent-profile workflow if you need it.

## TL;DR

1. Put the Google account credentials into `.env`:

   ```bash
   export GOOGLE_USER_EMAIL="agentbenchmark@gmail.com"
   export GOOGLE_USER_PASSWORD="Universityofutah"
   ```

2. Run the benchmark normally:

   ```bash
   ./run.sh
   ```

   Each Ray worker now mints its own freshly-validated
   `storage_state.json` at startup — no more `extract_auth_state.py`,
   no more 30-minute manual re-logins, no more cookie-rotation collisions
   when 5 models run in parallel.

If Google fires a "Verify it's you" challenge on the first run from a new
machine / IP, do the one-time human bootstrap (see
[First-run device trust](#first-run-device-trust) below). Once the device
is trusted, every subsequent headless mint succeeds without intervention.

## How it works

The new auth path has three pieces:

1. [`scripts/google_auto_login.py`](scripts/google_auto_login.py) — a
   stealth headless Playwright login flow. Reads
   `GOOGLE_USER_EMAIL` / `GOOGLE_USER_PASSWORD` from the environment,
   types them into Google's email + password sign-in pages, waits to land
   on `docs.google.com`, and saves the resulting cookies / localStorage as
   a Playwright `storage_state.json` snapshot.

2. [`scripts/storage_state_pool.py`](scripts/storage_state_pool.py) —
   per-PID mint pool that lives at `.bg_storage_state_pool/` (configurable
   via `BROWSERGYM_STATE_POOL_DIR`). Each Ray worker calls
   `mint_for_current_pid()` once at launch; the helper either returns a
   recently-minted snapshot or spawns `google_auto_login.py` to make a
   fresh one. A directory-wide file lock serialises simultaneous mints so
   five parallel workers log in sequentially within ~10 s of startup
   instead of all racing each other.

3. [`browsergym/core/src/browsergym/core/env.py`](browsergym/core/src/browsergym/core/env.py) —
   the `BrowserEnv.reset()` method now picks an auth path in this order:

   - If `BROWSERGYM_AUTO_LOGIN=1`, mint a per-worker snapshot and inject
     it into `browser.new_context(storage_state=...)`.
   - Else if `BROWSERGYM_PERSISTENT_PROFILE` is set, reuse (or clone) an
     on-disk Chromium user-data-dir (legacy path).
   - Else fall back to the static `storage_state.json` snapshot at the
     repo root (last-resort path).

`run.sh` and `benchmarks/_common.py` flip on `BROWSERGYM_AUTO_LOGIN=1`
automatically when `BROWSERGYM_AUTH_MODE=auto_login` (the default) and
the `GOOGLE_USER_*` env vars are present.

## First-run device trust

Google sometimes asks for a one-time "Verify it's you" challenge when an
account signs in from an unfamiliar IP / device fingerprint. The headless
auto-login script can't clear that challenge on its own, but a human can
clear it once and the trust persists for weeks.

To do the bootstrap:

```bash
python scripts/google_auto_login.py --headed --output storage_state.json
```

A Chromium window opens with the email / password already typed in. If
Google asks for a phone code, an alternate email, or a CAPTCHA, complete
it in the window. The script will save `storage_state.json` once the
session lands on `docs.google.com`. Future headless mints from the same
machine will succeed without a challenge.

## Config knobs

All knobs are env vars; defaults are in parentheses.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BROWSERGYM_AUTH_MODE` | `auto_login` | `auto_login` (mint per worker) or `persistent_profile` (reuse on-disk Chromium dir). |
| `GOOGLE_USER_EMAIL` | _(required for auto-login)_ | Account that the auto-login flow signs in as. |
| `GOOGLE_USER_PASSWORD` | _(required for auto-login)_ | Password for that account. |
| `BROWSERGYM_STATE_POOL_DIR` | `<repo>/.bg_storage_state_pool` | Where per-PID `worker_<pid>.json` snapshots live. |
| `BROWSERGYM_STATE_POOL_TTL` | `1500` (25 min) | How fresh a snapshot must be (seconds) before re-minting on a worker's next call. |
| `BROWSERGYM_N_JOBS` | `5` | Parallel Ray worker count. Per-worker minting means there is no parallelism cap from the auth path. |

## Troubleshooting

### "Auto-login failed: GOOGLE_USER_EMAIL and GOOGLE_USER_PASSWORD must be set"

Add the two variables to `.env` (and make sure `run.sh` sources `.env` —
it does so by default at the top of the file).

### "Google is asking for an extra verification step (challenge URL: ...)"

Run the [first-run device trust](#first-run-device-trust) bootstrap once.
After that, retries from the same machine will succeed.

### Mints succeed but the agent still ends up on a "Sign in" page mid-task

The agent's session can still get bumped by Google if the account is
flagged for unusual activity (e.g. signing in from many different IPs in a
short window). Re-running the benchmark will mint a fresh snapshot for
each worker and recover automatically. If the problem persists, do the
headed bootstrap once on the same machine that runs the benchmark.

### I want to use the old persistent-profile path

Set `BROWSERGYM_AUTH_MODE=persistent_profile` (and make sure
`playwright_chrome_profile/` exists). `run.sh` will skip the auto-login
wiring and fall back to the cloned-profile-pool behavior.

## Files

- [scripts/google_auto_login.py](scripts/google_auto_login.py) — stealth Playwright email + password flow.
- [scripts/storage_state_pool.py](scripts/storage_state_pool.py) — per-PID mint pool with file lock.
- [.env](.env) — holds `GOOGLE_USER_EMAIL` / `GOOGLE_USER_PASSWORD` (gitignored).
- [run.sh](run.sh) — sources `.env`, picks the auth mode, runs the benchmarks.
- [benchmark.py](benchmark.py) / [benchmarks/_common.py](benchmarks/_common.py) — wire `BROWSERGYM_AUTO_LOGIN` for Ray workers.
- [browsergym/core/src/browsergym/core/env.py](browsergym/core/src/browsergym/core/env.py) — consults the mint pool inside `BrowserEnv.reset()`.

## Notes

- `.bg_storage_state_pool/` is gitignored; never commit minted snapshots
  (they contain live session cookies for the configured account).
- The service account at `browsergym/knows/auth-data/service-account.json`
  is unrelated to browser auth — it stays in its current evaluator-only
  role (Drive / Docs / Sheets / Slides API access for grading).
