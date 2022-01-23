import json

from mwmbl.indexer.paths import TOP_DOMAINS_JSON_PATH
from mwmbl.tinysearchengine.hn_top_domains_filtered import DOMAINS


def export_top_domains_to_json():
    with open(TOP_DOMAINS_JSON_PATH, 'w') as output_file:
        json.dump(DOMAINS, output_file, indent=2)


if __name__ == '__main__':
    export_top_domains_to_json()
