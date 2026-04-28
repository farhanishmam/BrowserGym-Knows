"""Run a knows_* benchmark with Gemini 3.1 Pro using both AXT and screenshot + SoM.

The split is selected via the ``KNOWS_BENCHMARK`` env var (defaults to
``knows_docs_1``). See ``run.sh`` for the full set of splits we run.
"""

from _common import run_knows_benchmark

from agentlab.agents.generic_agent import AGENT_GEMINI_31_PRO_BOTH


if __name__ == "__main__":
    run_knows_benchmark(AGENT_GEMINI_31_PRO_BOTH)
