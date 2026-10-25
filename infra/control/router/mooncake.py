from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Store:
    def __init__(self) -> None:
        self.blocks: dict[str, dict] = {}
        self.hops: list[dict] = []

    def put(self, hop: dict) -> None:
        self.hops.append(hop)
        key = hop.get("req_id") or hop.get("src")
        if key:
            self.blocks[str(key)] = hop

    def tokens(self) -> int:
        return sum(int(h.get("tokens") or 0) for h in self.hops)

    def render_metrics(self) -> str:
        return (
            "# TYPE mooncake_hops_total counter\n"
            f"mooncake_hops_total {len(self.hops)}\n"
            "# TYPE mooncake_blocks gauge\n"
            f"mooncake_blocks {len(self.blocks)}\n"
            "# TYPE mooncake_hop_tokens_total counter\n"
            f"mooncake_hop_tokens_total {self.tokens()}\n"
        )

STORE = Store()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json(200, {"ok": True, "backend": "mooncake", "hops": len(STORE.hops)})
            return
        if path == "/hops":
            self._json(200, {"hops": STORE.hops, "blocks": len(STORE.blocks)})
            return
        if path == "/metrics":
            body = STORE.render_metrics().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/put":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n)
        hop = json.loads(raw or b"{}")
        STORE.put(hop)
        self._json(200, {"ok": True, "n": len(STORE.hops)})

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

def main(host: str = "0.0.0.0", port: int = 50051) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"mooncake store on http://{host}:{port}  (GET /hops /metrics)")
    server.serve_forever()

if __name__ == "__main__":
    main()
