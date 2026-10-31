"""A mock OpenAI-compatible endpoint: records every request, answers with canned text."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = sys.argv[2]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(body) + "\n")
        reply = {"choices": [{"message": {"content":
                 "**Summary** — mock analysis.\n\n**Risk** — Low, mock."}}]}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
