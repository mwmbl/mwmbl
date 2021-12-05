"""
Extract top domains from BigQuery result.
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(os.environ['HOME']) / 'data' / 'tinysearch'
ALL_DOMAINS_PATH = DATA_DIR / 'hn-top-domains.csv'
TOP_DOMAINS_PATH = 'hn-top-domains-filtered.py'

MIN_COUNT = 10
PROBABILITY_THRESHOLD = 0.8


def get_top_domains():
    data = pd.read_csv(ALL_DOMAINS_PATH, index_col='domain')
    data = data[data.index.notnull()]

    frequent = data[data['total'] >= MIN_COUNT]
    scores = frequent['mean_score'] * np.log(frequent['total']) ** 2
    median_score = np.median(scores)
    print("Median score", median_score)
    probabilities = scores / (scores + median_score)

    top_probabilities = probabilities[probabilities > PROBABILITY_THRESHOLD]
    top_probabilities.sort_values(ascending=False, inplace=True)
    with open(TOP_DOMAINS_PATH, 'w') as output_file:
        probabilities_str = str(top_probabilities.to_dict()).replace(', ', ',\n')
        output_file.write("DOMAINS = " + probabilities_str + '\n\n')
        # json.dump(probabilities.to_dict(), output_file, indent=2)

        # for row in probabilities.iterrows():
        #     output_file.write(json.dumps(row.to_dict()) + '\n')


if __name__ == '__main__':
    get_top_domains()
