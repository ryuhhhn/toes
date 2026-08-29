#!/usr/bin/env python3
"""Static server for both frontends.

    python web/serve.py            # http://localhost:8090

Serves the whole `web/` tree rather than one frontend, so the storefront and the
merchant console are reachable from a single origin and can link to each other:

    /                          -> /storefront/chatbot.html
    /storefront/chatbot.html   the shopper's chat
    /merchant-console/         the merchant's upload + profile approval screen

WHY 8090: the port map is fixed repo-wide — merchant 8001, agent 8002, payments
8003, stubs 9001 — and the version of this file that shipped with the storefront
drop defaulted to 8002, which would have silently shadowed the agent. 8080 was the
next obvious choice and is already taken on this machine, so 8090 it is. Override
with --port or WEB_PORT.

WHY a server at all rather than opening the file: `file://` origins are opaque,
so every fetch to the agent would be a CORS failure and EventSource would not
connect. Both backends allow-list origins via CORS_ORIGINS.
"""

from __future__ import annotations

import argparse
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8090


class WebHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        if self.path in ("", "/"):
            self.send_response(302)
            self.send_header("Location", "/storefront/chatbot.html")
            self.end_headers()
            return None
        return super().send_head()

    def end_headers(self):
        # WHY: without this the browser serves a stale chatbot.js after every
        # edit, which during a demo looks exactly like a broken backend.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # Keep 200s quiet; a 404 on a renamed asset is the thing worth seeing.
        if args and str(args[1]).startswith("2"):
            return
        super().log_message(fmt, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("WEB_PORT", DEFAULT_PORT)),
        help=f"Port to bind on (default: {DEFAULT_PORT})",
    )
    args = parser.parse_args()

    directory = Path(__file__).resolve().parent
    handler = partial(WebHandler, directory=str(directory))

    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    print(f"  storefront       http://localhost:{args.port}/storefront/chatbot.html")
    print(f"  merchant console http://localhost:{args.port}/merchant-console/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
