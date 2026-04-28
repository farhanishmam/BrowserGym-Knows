"""Run a knows_* benchmark with GPT-5.5 using screenshot + Set-of-Mark only (no AXT).

The split is selected via the ``KNOWS_BENCHMARK`` env var (defaults to
``knows_docs_1``). See ``run.sh`` for the full set of splits we run.
"""

from _common import run_knows_benchmark

from agentlab.agents.generic_agent import AGENT_GPT55_SCREENSHOT


if __name__ == "__main__":
    run_knows_benchmark(AGENT_GPT55_SCREENSHOT)
