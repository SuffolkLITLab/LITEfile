"""Local EFSP simulation; unexpected paths fail, never forward externally."""

import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(0.1)
        if self.path.endswith("/disclaimer_requirements"):
            data = []
        elif any(part in self.path for part in ("optional_services", "party_types")):
            data = []
        elif "payment" in self.path.lower():
            data = [
                {
                    "paymentAccountID": "capacity-card",
                    "accountName": "Synthetic card",
                    "paymentAccountTypeCode": "CC",
                }
            ]
        else:
            self.send_error(501, "Unimplemented test upstream")
            return
        self.reply(data)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.endswith("/filings"):
            time.sleep(2)
            self.reply({"confirmationNumber": str(uuid.uuid4())})
        else:
            self.send_error(501)

    def reply(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
