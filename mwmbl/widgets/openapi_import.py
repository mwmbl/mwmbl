"""Draft a widget spec from an OpenAPI GET operation and one sample answer.
`python -m mwmbl.widgets.openapi_import <openapi.json URL> <path>`. For a person to finish, not to index as it
stands: examples become defaults (wrong for required inputs, which should come from the query), and the triggers
and query patterns are theirs to write.
"""

import argparse
import json
import re
from urllib.parse import urlsplit

import httpx

SAMPLE_TIMEOUT_SECONDS = 10
_KEY = re.compile(r"[A-Za-z_]\w*")


def scalar_paths(data, prefix: str = "") -> list[str]:
    """The JSON paths (as WidgetSpec outputs write them) of the string, number and boolean values in an answer."""
    paths = []
    if isinstance(data, dict):
        for key, value in data.items():
            if _KEY.fullmatch(key):
                paths += scalar_paths(value, f"{prefix}.{key}" if prefix else key)
    elif isinstance(data, list):
        for i, value in enumerate(data):
            paths += scalar_paths(value, f"{prefix}[{i}]")
    elif isinstance(data, (str, int, float)) and prefix:
        paths.append(prefix)
    return paths


def draft_spec(openapi: dict, path: str, sample: dict, answer_key: str = "result") -> dict:
    """A spec dict for a GET operation: a query parameter is an input, its example the default, and the
    scalars under `answer_key` in the sample answer are the outputs."""
    server = openapi["servers"][0]["url"]
    host = urlsplit(server).netloc
    parameters = [p for p in openapi["paths"][path]["get"]["parameters"] if p["in"] == "query"]
    inputs = []
    for parameter in parameters:
        input_spec = {
            "name": parameter["name"],
            "type": "number" if parameter["schema"]["type"] in ("number", "integer") else "text",
            "label": parameter["name"].replace("_", " ").capitalize(),
        }
        if "example" in parameter:
            input_spec["default"] = str(parameter["example"])
        inputs.append(input_spec)
    query = "&".join(f"{p['name']}={{{p['name']}}}" for p in parameters)
    outputs = [
        {"label": output_path.rsplit(".", 1)[-1].replace("_", " ").capitalize(), "path": output_path, "unit": ""}
        for output_path in scalar_paths(sample[answer_key], answer_key)
    ]
    return {
        "name": path.rstrip("/").rsplit("/", 1)[-1],
        "site": host,
        "source_name": host,
        "page_url": f"{server}/",  # to be replaced with the page people can use
        "api_url": f"{server}{path}?{query}",
        "api_host": host,
        "inputs": inputs,
        "outputs": outputs,
        "trigger_word": "",
        "trigger_tokens": [],
        "cache_ttl_seconds": 3600,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("openapi_url")
    parser.add_argument("path", help="the operation's path, for example /api/v1/trip-cost")
    args = parser.parse_args()
    openapi = httpx.get(args.openapi_url, timeout=SAMPLE_TIMEOUT_SECONDS).raise_for_status().json()
    parameters = openapi["paths"][args.path]["get"]["parameters"]
    examples = {p["name"]: p["example"] for p in parameters if p["in"] == "query" and "example" in p}
    server = openapi["servers"][0]["url"]
    sample = httpx.get(server + args.path, params=examples, timeout=SAMPLE_TIMEOUT_SECONDS).raise_for_status().json()
    print(json.dumps(draft_spec(openapi, args.path, sample), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
