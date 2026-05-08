"""Run a knows_* benchmark with DeepSeek V4 Pro using the screenshot + SoM only.

The split is selected via the ``KNOWS_BENCHMARK`` env var (defaults to
``knows_docs_1``).
"""

from _common import run_knows_benchmark

from agentlab.agents.generic_agent import AGENT_DEEPSEEK_V4_PRO_SCREENSHOT


if __name__ == "__main__":
    run_knows_benchmark(AGENT_DEEPSEEK_V4_PRO_SCREENSHOT)
