from bisect import bisect_left, bisect_right

import pandas as pd
from pandas import DataFrame


class Completer:
    def __init__(self, terms: DataFrame):
        terms_dict = terms.sort_values('term').set_index('term')['count'].to_dict()
        self.terms = list(terms_dict.keys())
        self.counts = list(terms_dict.values())
        print("Terms", self.terms[:100], self.counts[:100])

    def complete(self, term):
        term_length = len(term)
        start = bisect_left(self.terms, term, key=lambda x: x[:term_length])
        end = bisect_right(self.terms, term, key=lambda x: x[:term_length])

        print("Start", self.terms[start])
        print("End", self.terms[end])



if __name__ == '__main__':
    data = pd.read_csv('data/mwmbl-crawl-terms.csv')
    completer = Completer(data)
    completer.complete('yo')
