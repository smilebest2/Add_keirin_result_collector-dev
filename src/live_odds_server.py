import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import DB_PATH, ROOT_DIR
from .db import connect, init_db
from .live_odds import live_odds_recheck


DOCS_DIR = ROOT_DIR / "docs"


class LiveOddsHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, db_path=DB_PATH, directory=None, **kwargs):
        self.db_path = db_path
        super().__init__(*args, directory=str(directory or DOCS_DIR), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/live-odds":
            self.handle_live_odds(parsed.query)
            return
        if parsed.path == "/":
            self.path = "/predictions.html"
        super().do_GET()

    def handle_live_odds(self, query: str):
        params = parse_qs(query)
        race_id = (params.get("race_id") or [""])[0].strip()
        if not race_id:
            self.send_json({"ok": False, "error": "race_id is required"}, status=400)
            return
        try:
            with connect(self.db_path) as conn:
                init_db(conn)
                payload = live_odds_recheck(conn, race_id)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=500)
            return
        self.send_json(payload)

    def send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve local prediction pages and one-race live odds recheck API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    class Handler(LiveOddsHandler):
        def __init__(self, *handler_args, **handler_kwargs):
            super().__init__(*handler_args, db_path=args.db, **handler_kwargs)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving predictions at http://{args.host}:{args.port}/predictions.html")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
