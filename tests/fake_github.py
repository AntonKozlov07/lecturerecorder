"""A small in-memory stand-in for the parts of the GitHub REST API that sync uses.

Used by the PC and iPhone sync tests, so both apps are checked against the same
behaviour: blobs, trees, commits, refs (rejecting non-fast-forward updates)
and creating the first file of an empty repository.

Run on its own:  python tests/fake_github.py 8790
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "test-token"


class Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict] = {}
        self.head: str | None = None
        self.requests = 0
        self.reject_next_push = False  # tests set this to simulate another device pushing first

    def blob(self, data: bytes) -> str:
        sha = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        self.blobs[sha] = data
        return sha

    def tree(self, files: dict[str, str]) -> str:
        sha = hashlib.sha1(json.dumps(files, sort_keys=True).encode()).hexdigest()
        self.trees[sha] = dict(files)
        return sha

    def commit(self, tree: str, parents: list[str], message: str) -> str:
        sha = hashlib.sha1(json.dumps([tree, parents, message, len(self.commits)]).encode()).hexdigest()
        self.commits[sha] = {"tree": tree, "parents": parents, "message": message}
        return sha

    def files(self) -> dict[str, bytes]:
        if not self.head:
            return {}
        return {p: self.blobs[s] for p, s in self.trees[self.commits[self.head]["tree"]].items()}


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code: int, body=None):
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else {}

        def handle_any(self, method: str):
            store.requests += 1
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self.reply(401, {"message": "Bad credentials"})
            m = re.match(r"^/repos/[\w.-]+/[\w.-]+(.*?)(\?.*)?$", self.path)
            if not m:
                return self.reply(404, {"message": "Not Found"})
            path = m.group(1)
            with store.lock:
                if method == "GET" and path == "":
                    return self.reply(200, {"default_branch": "main", "private": True})
                if method == "GET" and path == "/git/ref/heads/main":
                    if not store.head:
                        return self.reply(409, {"message": "Git Repository is empty."})
                    return self.reply(200, {"object": {"sha": store.head}})
                if method == "PUT" and path.startswith("/contents/"):
                    if store.head:
                        return self.reply(422, {"message": "sha wasn't supplied"})
                    data = base64.b64decode(self.body()["content"])
                    tree = store.tree({path[len("/contents/"):]: store.blob(data)})
                    store.head = store.commit(tree, [], "init")
                    return self.reply(201, {"commit": {"sha": store.head}})
                if method == "GET" and path.startswith("/git/commits/"):
                    c = store.commits.get(path.rsplit("/", 1)[1])
                    return self.reply(200, {"sha": path.rsplit("/", 1)[1], "tree": {"sha": c["tree"]}}) if c else self.reply(404, {})
                if method == "GET" and path.startswith("/git/trees/"):
                    files = store.trees.get(path.rsplit("/", 1)[1])
                    if files is None:
                        return self.reply(404, {})
                    return self.reply(200, {"truncated": False, "tree": [
                        {"path": p, "mode": "100644", "type": "blob", "sha": s} for p, s in sorted(files.items())]})
                if method == "GET" and path.startswith("/git/blobs/"):
                    data = store.blobs.get(path.rsplit("/", 1)[1])
                    if data is None:
                        return self.reply(404, {})
                    return self.reply(200, {"content": base64.b64encode(data).decode(), "encoding": "base64"})
                if method == "POST" and path == "/git/blobs":
                    b = self.body()
                    data = base64.b64decode(b["content"]) if b.get("encoding") == "base64" else b["content"].encode()
                    return self.reply(201, {"sha": store.blob(data)})
                if method == "POST" and path == "/git/trees":
                    b = self.body()
                    files = dict(store.trees[b["base_tree"]]) if b.get("base_tree") else {}
                    for e in b["tree"]:
                        if e.get("sha") is None:
                            files.pop(e["path"], None)
                        else:
                            files[e["path"]] = e["sha"]
                    return self.reply(201, {"sha": store.tree(files)})
                if method == "POST" and path == "/git/commits":
                    b = self.body()
                    return self.reply(201, {"sha": store.commit(b["tree"], b["parents"], b.get("message", ""))})
                if method == "PATCH" and path == "/git/refs/heads/main":
                    b = self.body()
                    new = store.commits.get(b["sha"])
                    if not new:
                        return self.reply(422, {"message": "Object does not exist"})
                    if store.reject_next_push:
                        store.reject_next_push = False
                        return self.reply(422, {"message": "Update is not a fast forward"})
                    if not b.get("force") and store.head not in new["parents"]:
                        return self.reply(422, {"message": "Update is not a fast forward"})
                    store.head = b["sha"]
                    return self.reply(200, {"object": {"sha": store.head}})
            return self.reply(404, {"message": f"Not handled: {method} {path}"})

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

        def do_PUT(self):
            self.handle_any("PUT")

        def do_PATCH(self):
            self.handle_any("PATCH")

    return Handler


def serve(port: int = 0) -> tuple[ThreadingHTTPServer, Store]:
    store = Store()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, store


if __name__ == "__main__":
    server, store = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8790)
    print(f"Fake GitHub on http://127.0.0.1:{server.server_address[1]} (token: {TOKEN})", flush=True)
    threading.Event().wait()
