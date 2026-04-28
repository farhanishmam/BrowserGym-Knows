import gzip
import pickle
import sys
import glob
import os

try:
    with gzip.open('final_axt/claude/2_sheets/study.pkl.gz', 'rb') as f:
        study = pickle.load(f)
        print('Study loaded successfully.')
        
        results = study.get_results()
        if isinstance(results, tuple):
            df = results[0]
            if hasattr(df, "columns"):
                for index, row in df.iterrows():
                    print(f'  - Index: {index} | Status: {row.get("status", "N/A")} | Error: {row.get("err_msg", "N/A")}')
        else:
            print("Results is not a tuple")
except Exception as e:
    import traceback
    traceback.print_exc()
