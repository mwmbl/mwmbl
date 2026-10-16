import json

from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.indexer import TOP_DOMAINS_JSON_PATH


def export_top_domains_to_json():
    with open(TOP_DOMAINS_JSON_PATH, "w") as output_file:
        json.dump(DOMAINS, output_file, indent=2)


if __name__ == "__main__":
    export_top_domains_to_json()
