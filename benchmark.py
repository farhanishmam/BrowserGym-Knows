from agentlab.agents.generic_agent import AGENT_4o_MINI
from agentlab.agents.generic_agent import AGENT_4o
from agentlab.agents.generic_agent import AGENT_GPT54
from agentlab.experiments.study import make_study
from pathlib import Path
from agentlab.experiments.study import Study

from browsergym.experiments.benchmark import DEFAULT_BENCHMARKS

# Load the benchmark configuration
benchmark = DEFAULT_BENCHMARKS["knows_1"]()

# Configure all tasks to use the extracted storage state for authentication
# This preserves Google login across all benchmark runs
# Run extract_auth_state.py first to create this file
STORAGE_STATE_FILE = "storage_state.json"

for env_args in benchmark.env_args_list:
    env_args.storage_state = STORAGE_STATE_FILE

study = make_study(
    benchmark=benchmark,
    agent_args=[AGENT_GPT54],
    comment="Knows Benchmark with Google Auth",
)

study.dir = Path("results")

# Run the study
study.run(n_jobs=1)