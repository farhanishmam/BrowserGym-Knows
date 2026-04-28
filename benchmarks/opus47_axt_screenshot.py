"""Run a knows_* benchmark with Claude Opus 4.7 using both AXT and screenshot + SoM.

The split is selected via the ``KNOWS_BENCHMARK`` env var (defaults to
``knows_docs_1``). See ``run.sh`` for the full set of splits we run.
"""

from _common import run_knows_benchmark

from agentlab.agents.generic_agent import AGENT_OPUS_47_BOTH


if __name__ == "__main__":
    run_knows_benchmark(AGENT_OPUS_47_BOTH)
