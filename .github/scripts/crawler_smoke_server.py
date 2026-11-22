"""A stand-in for the Mwmbl API, for the crawler smoke run (.github/workflows/crawler-smoke.yml).

Serves the endpoints the crawler calls, checks the first batch of results it submits has the
shape the real API takes, and answers with a curated result for one of its terms, so the run
also covers the crawler reading the response and merging it into its index. It exits once
it has answered, non-zero if the batch was malformed.

Standard library only, so the workflow can run it with the runner's python3.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
RESULT_FIELDS = {"url", "title", "extract", "score", "term", "state", "last_crawled"}
REQUEST_FIELDS = {"results", "crawler_version", "device_name", "include_new_items"}
# A document as the API's new_items carries it: curated, so state is an integer.
CURATED_STATE = 7


def check_results(body: dict) -> list[str]:
    problems = []
    if set(body) != REQUEST_FIELDS:
        problems.append(f"request fields {sorted(body)}")
    if not body.get("results"):
        problems.append("no results")
    for result in body.get("results", []):
        if set(result) != RESULT_FIELDS:
            problems.append(f"result fields {sorted(result)}")
        elif not (isinstance(result["url"], str) and isinstance(result["title"], str)):
            problems.append(f"result {result}")
    return problems


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, value: dict) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/v1/crawler/curated-domains":
            self._send_json({"domains": [{"name": "en.wikipedia.org"}]})
        elif self.path == "/api/v1/platform/user":
            self._send_json({"username": "smoke"})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/v1/crawler/results":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        problems = check_results(body)
        terms = [result["term"] for result in body["results"] if result.get("term")]
        new_item = {
            "title": "Smoke test",
            "url": "https://example.com/smoke",
            "extract": "A curated result from the smoke test's stand-in API.",
            "score": 1.0,
            "term": terms[0] if terms else None,
            "state": CURATED_STATE,
            "user_ids": None,
            "last_crawled": None,
            "source": None,
        }
        self._send_json({"status": "ok", "url": None, "new_items": [new_item]})
        print(f"Received {len(body['results'])} results from crawler {body.get('crawler_version')}", flush=True)
        for problem in problems:
            print(f"::error::Malformed results: {problem}", flush=True)
        self.server.exit_code = 1 if problems else 0


server = HTTPServer(("127.0.0.1", PORT), Handler)
server.exit_code = None
while server.exit_code is None:
    server.handle_request()
sys.exit(server.exit_code)
