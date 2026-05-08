"""Run a knows_* benchmark with DeepSeek V4 Pro using the accessibility tree only.

The split is selected via the ``KNOWS_BENCHMARK`` env var (defaults to
``knows_docs_1``). See ``run.sh`` for the full set of splits we run.

Authenticates with ``DEEPSEEK_API_KEY`` (loaded from ``.env`` by
``run.sh``) against DeepSeek's official OpenAI-compatible endpoint at
``https://api.deepseek.com/v1``.
"""

from _common import run_knows_benchmark

from agentlab.agents.generic_agent import AGENT_DEEPSEEK_V4_PRO_AXT


if __name__ == "__main__":
    run_knows_benchmark(AGENT_DEEPSEEK_V4_PRO_AXT)
