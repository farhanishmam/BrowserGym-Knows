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
```

> If you can't use submodules, clone the two repos manually instead of step 1's
> `submodule update`:
> ```bash
> git clone git@github.com:alexgill321/Agent-Benchmark.git browsergym/knows
> git clone https://github.com/farhanishmam/AgentLab-Knows AgentLab-Knows
> ```

## Authentication

The benchmark logs into a real Google account. Set credentials in a gitignored
`.env`, then let each run mint its own browser session:

```bash
GOOGLE_USER_EMAIL=you@example.com
GOOGLE_USER_PASSWORD=...
```

A first run from a new machine/IP may hit a "Verify it's you" challenge — clear
it once with `python scripts/google_auto_login.py --headed --output storage_state.json`.
Grading uses a separate service-account credential at `auth-data/service-account.json`.
See [SETUP_AUTH.md](SETUP_AUTH.md) for full details.

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
