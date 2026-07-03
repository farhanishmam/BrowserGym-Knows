# BrowserGym-Knows

A fork of [BrowserGym](https://github.com/ServiceNow/BrowserGym) wired to run the
**Knows** benchmark: Google Workspace (Docs / Sheets / Slides) tasks for
evaluating LLM browser agents. An agent drives a real Chromium browser via
Playwright to complete a task against a freshly-created Workspace doc, then an
evaluator grades the result using the Google Workspace APIs.

This repo depends on two companion repos, cloned as submodules:

| Repo | Path | Purpose |
| --- | --- | --- |
| [alexgill321/Agent-Benchmark](https://github.com/alexgill321/Agent-Benchmark) | `browsergym/knows` | **The Knows benchmark** — task definitions, evaluators, and gold data. Required to *run the benchmark*. |
| [farhanishmam/AgentLab-Knows](https://github.com/farhanishmam/AgentLab-Knows) | `AgentLab-Knows` | The `GenericAgent` and `AGENT_*` model configs. Required to *run an agent*. |

## Setup

```bash
# 1. Clone this repo with its two submodules
git clone <this-repo-url> BrowserGym-Knows
cd BrowserGym-Knows
git submodule update --init --recursive          # pulls browsergym/knows + AgentLab-Knows

# 2. Create the environment
conda create -n knows python=3.10
conda activate knows

# 3. Install dependencies + the in-repo browsergym sub-packages (editable) + playwright
pip install -r requirements.txt
make install

# 4. Install the agent runner
cd AgentLab-Knows && pip install -e . && cd ..

# 5. IMPORTANT: AgentLab pulls in its own browsergym wheels — uninstall those so the
#    editable in-repo packages win (otherwise the "knows" action subset won't resolve)
pip uninstall -y browsergym browsergym-core browsergym-experiments browsergym-webarena
make install

# 6. Add credentials and validate the whole environment (see next section)
cp .env.example .env    # fill in GOOGLE_USER_EMAIL / GOOGLE_USER_PASSWORD + model API keys
./setup.sh --headed
```

> If you can't use submodules, clone the two repos manually instead of step 1's
> `submodule update`:
> ```bash
> git clone git@github.com:alexgill321/Agent-Benchmark.git browsergym/knows
> git clone https://github.com/farhanishmam/AgentLab-Knows AgentLab-Knows
> ```

## Credentials & the setup script

The benchmark logs into a real Google account with the credentials you put in
a gitignored `.env` at the repo root — every run mints its own browser session
from them. Start from the checked-in template:

```bash
cp .env.example .env
```

| `.env` key | Required | Purpose |
| --- | --- | --- |
| `GOOGLE_USER_EMAIL` / `GOOGLE_USER_PASSWORD` | **Yes** | Google account the agent signs in with. Must not have 2FA/passkeys enforced. |
| `OPENAI_API_KEY` | Only for `gpt55_*` scripts | Model API key. |
| `ANTHROPIC_API_KEY` | Only for `opus47_*` scripts | Model API key. |
| `DEEPSEEK_API_KEY` | Only for `deepseek_v4_*` scripts | Model API key. |
| `GEMINI_API_KEY` | Only for `gemini31_*` scripts | Model API key. |
| `SERVICE_ACCOUNT_PATH` | No | Override for the evaluator credential (defaults to `browsergym/knows/auth-data/service-account.json`). |

Then validate everything in one shot:

```bash
./setup.sh --headed         # FIRST run on a new machine/IP — opens a visible
                            # browser so you can clear Google's "Verify it's you"
./setup.sh                  # every run after that: fully headless
```

The script checks, in order:

1. **interpreter** — you're on the `knows` conda env (warn only);
2. **.env** — exists (auto-created from `.env.example` if missing — fill it in and re-run);
3. **credentials** — Google email/password are filled in; missing model API
   keys are warnings that name the benchmark scripts they block;
4. **playwright** — the chromium browser is installed;
5. **auth mint** — performs a *real* headless Google login via
   `scripts/google_auto_login.py` and writes `storage_state.json`;
6. **service account** — the evaluator credential parses and the Drive API
   accepts it (this credential is for *grading*, unrelated to the browser login);
7. **drive links** — every Google Drive link embedded in the task goals is
   publicly accessible (see next section).

Each step prints `[setup] PASS/WARN/FAIL` and the script exits non-zero if
anything FAILs. Re-running is always safe: nothing is destroyed and an
existing `.env` is never overwritten. Skip individual steps with
`--skip-mint`, `--skip-link-check`, `--skip-service-account`.

**If the auth mint fails:** check the console output and the screenshots under
`.bg_storage_state_pool/debug/`, then re-run `./setup.sh --headed` — the usual
cause is Google's one-time "Verify it's you" challenge on a new machine or IP.
Device trust persists after you clear it once. Full details in
[SETUP_AUTH.md](SETUP_AUTH.md).

## Drive links in tasks

Some task goals reference shared Google Drive files/folders (source data,
template presentations, images). The benchmark account has no special grants
on those, so each one must be shared as **"Anyone with the link"**.

Two layers enforce this:

- **Pre-task check (automatic).** Before a task starts — and after any
  per-family `setup_run.py` preparation script has run — the task probes every
  Drive link in its goal unauthenticated. A private/missing link fails the
  episode immediately with the offending URL, *before* any workspace doc is
  created. Bypass with `KNOWS_SKIP_LINK_CHECK=1` if you must.
- **Standalone sweep (on demand).** Audit link health across all tasks without
  running anything:

  ```bash
  python scripts/check_drive_links.py                 # sweep every task family
  python scripts/check_drive_links.py --split docs_1  # one family
  python scripts/check_drive_links.py --url <drive-url>   # one link
  python scripts/check_drive_links.py --api           # add service-account permission check
  python scripts/check_drive_links.py --json          # machine-readable output
  ```

  Exit code is non-zero when any link is PRIVATE / MISSING / ERROR. To fix a
  flagged link: open it in an incognito window — if it asks you to sign in,
  share the file as "Anyone with the link" from the owning account.

Task families that ship a `setup_run.py` (e.g. `sheets_10_paper_sorting`,
which creates fresh per-run destination folders and rewrites its task goal)
have it run automatically before each episode; any family can opt in by adding
a `setup_run.py` next to its `instance_<n>/` folders.

## Run

```bash
./run_one.sh <script> <benchmark> [n_jobs]
./run_one.sh opus47_axt.py knows_docs_1
./run_one.sh gpt55_axt.py  knows_slides_39
```

- `<script>` is one of [benchmarks/](benchmarks/) — named `<model>_<obs-mode>.py`
  (e.g. `opus47_axt.py`, `gpt55_axt_screenshot.py`); it selects the model and
  observation mode (accessibility tree / screenshot / both).
- `<benchmark>` is a split named `knows_<family>_<num>` (e.g. `knows_docs_1`,
  `knows_sheets_38`, `knows_slides_42`). Each split = 5 task instances.

Results route to `<obs>/<model>/<split>/`. Run a full sweep with `./run.sh`.

To run an evaluator standalone:

```bash
python scripts/run_evaluator.py --split docs_1 --instance 1 --id <google_file_id>
```

See [CLAUDE.md](CLAUDE.md) for repo internals (run wiring, env vars, output routing).
