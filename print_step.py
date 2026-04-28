import gzip
import pickle
import sys
import glob
import os

try:
    step_files = sorted(glob.glob('final_axt/claude/2_sheets/2026-04-27_03-06-20_GenericAgent-claude-opus-4-7_axt_on_knows.sheets_2_personal_recipe.4_20/step_*.pkl.gz'))
    if not step_files:
        print("No step files found.")
        sys.exit(0)
    
    last_step_file = step_files[-1]
    print(f"Loading {last_step_file}...")
    with gzip.open(last_step_file, 'rb') as f:
        step = pickle.load(f)
        print('Step loaded successfully.')
        if hasattr(step, "action"):
            print(f'Action: {step.action}')
        if hasattr(step, "agent_info") and step.agent_info:
            print(f'Think: {step.agent_info.think}')
        if hasattr(step, "error") and step.error:
            print(f'Error: {step.error}')
        if hasattr(step, "terminated"):
            print(f'Terminated: {step.terminated}')
        if hasattr(step, "truncated"):
            print(f'Truncated: {step.truncated}')
        if hasattr(step, "reward"):
            print(f'Reward: {step.reward}')
except Exception as e:
    import traceback
    traceback.print_exc()
