"""Local alert receiver: logs every Alertmanager webhook as one JSON line (stdout)."""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Sink(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        for alert in body.get("alerts", []):
            print(json.dumps({"receiver": self.path.strip("/"), "status": alert.get("status"),
                              "alertname": alert.get("labels", {}).get("alertname"),
                              "severity": alert.get("labels", {}).get("severity"),
                              "startsAt": alert.get("startsAt"), "endsAt": alert.get("endsAt")}), flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a: object) -> None:
        pass


HTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 8080), Sink).serve_forever()
