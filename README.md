# BrowserGym-Knows

A fork of [BrowserGym](https://github.com/ServiceNow/BrowserGym) wired to run the
**Knows** benchmark: Google Workspace (Docs / Sheets / Slides) tasks for
evaluating LLM browser agents. An agent drives a real Chromium browser to
complete a task against a fresh Workspace doc, then an evaluator grades the
result via the Google Workspace APIs.

It pulls in two companion repos as submodules:

| Repo | Path | Purpose |
| --- | --- | --- |
| [alexgill321/Agent-Benchmark](https://github.com/alexgill321/Agent-Benchmark) | `browsergym/knows` | The Knows benchmark — tasks, evaluators, gold data. |
| [farhanishmam/AgentLab-Knows](https://github.com/farhanishmam/AgentLab-Knows) | `AgentLab-Knows` | The `GenericAgent` and `AGENT_*` model configs. |

## Prerequisites

- **Python 3.10** (conda recommended).
- **A Google account** for the agent to sign in as — no 2FA/passkeys enforced.
  Use a throwaway account, not a personal one.
- **A Google Cloud service account** for grading (see [below](#evaluator-service-account-for-grading)).
- **An API key** for each model you run (OpenAI / Anthropic / Gemini / DeepSeek).

## Setup

```bash
# Clone with submodules
git clone <this-repo-url> BrowserGym-Knows
cd BrowserGym-Knows
git submodule update --init --recursive

# Environment
conda create -n knows python=3.10 && conda activate knows

# Install deps + in-repo browsergym packages (editable) + the agent runner
pip install -r requirements.txt
make install
cd AgentLab-Knows && pip install -e . && cd ..

# AgentLab pulls its own browsergym wheels — drop them so the editable ones win
# (otherwise the "knows" action subset won't resolve)
pip uninstall -y browsergym browsergym-core browsergym-experiments browsergym-webarena
make install

# Credentials + validate everything
cp .env.example .env      # fill in Google login + model API keys
./setup.sh --headed       # first run on a new machine; ./setup.sh afterwards
```

> No submodule access? Clone the two repos manually:
> ```bash
> git clone git@github.com:alexgill321/Agent-Benchmark.git browsergym/knows
> git clone https://github.com/farhanishmam/AgentLab-Knows AgentLab-Knows
> ```

## Credentials

Fill in the gitignored `.env` (copied from `.env.example`):

| Key | Required | Purpose |
| --- | --- | --- |
| `GOOGLE_USER_EMAIL` / `GOOGLE_USER_PASSWORD` | **Yes** | Account the agent signs in with (no 2FA). |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` / `DEEPSEEK_API_KEY` | Per model | Only the models you run. |
| `SERVICE_ACCOUNT_PATH` | Optional | Override the grading key path (default `browsergym/knows/auth-data/service-account.json`). |

`./setup.sh` validates the environment end-to-end (login, service account, task
Drive links) and prints `PASS/WARN/FAIL` per step. Run it `--headed` the first
time so you can clear Google's one-time "Verify it's you" challenge; details in
[SETUP_AUTH.md](SETUP_AUTH.md).

## Evaluator service account (for grading)

The evaluators read the agent's finished document through the Workspace APIs as
a **Google Cloud service account** — separate from the browser login. Create one
once in the [Cloud Console](https://console.cloud.google.com/):

1. Create/pick a project.
2. Enable the **Drive, Docs, Sheets, and Slides** APIs.
3. IAM & Admin → *Service Accounts* → create one. **No roles needed** — the
   runner auto-shares each finished doc with the account, so access is granted
   per-document at grading time.
4. Open it → *Keys* → *Add key* → *Create new key* → **JSON**.
5. Save the key as `browsergym/knows/auth-data/service-account.json` (gitignored),
   or point `SERVICE_ACCOUNT_PATH` at it.
6. Confirm with `./setup.sh` — the service-account step prints `PASS`.

> The JSON key is a live credential — never commit it. If exposed, delete it in
> the Cloud Console and create a new one.

## Drive links in tasks

Some goals reference shared Drive files that must be shared as **"Anyone with
the link"**. The task checks this automatically before each run (bypass with
`KNOWS_SKIP_LINK_CHECK=1`). Audit all links on demand:

```bash
python scripts/check_drive_links.py [--split docs_1 | --url <drive-url>]
```

To fix a flagged link, open it in an incognito window; if it asks you to sign
in, reshare it as "Anyone with the link".

## Run

```bash
./run_one.sh <script> <benchmark>       # e.g. ./run_one.sh opus47_axt.py knows_docs_1
```

- `<script>` — one of [benchmarks/](benchmarks/), named `<model>_<obs-mode>.py`
  (accessibility tree / screenshot / both).
- `<benchmark>` — a split `knows_<family>_<num>` (`knows_docs_1`, `knows_sheets_38`,
  `knows_slides_42`); each is 5 instances.

Results route to `<obs>/<model>/<split>/`. Full sweep: `./run.sh`. Run an
evaluator standalone:

```bash
python scripts/run_evaluator.py --split docs_1 --instance 1 --id <google_file_id>
```

See [CLAUDE.md](CLAUDE.md) for repo internals.
</content>
