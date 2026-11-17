"""Replay saved TronGrid responses through httpx, so tests never touch the network.

A fixture folder is what LocalRawStore wrote during a live run: index.jsonl plus
raw/ab/<sha256> bodies. Requests are matched on path, query and JSON body.
"""
from __future__ import annotations

import json
import pathlib
from urllib.parse import parse_qsl, urlsplit

import httpx

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "tron"


def _key(path: str, query: dict | None, body: dict | None) -> str:
    q = {k: str(v) for k, v in (query or {}).items()}
    return json.dumps([path, q, body], sort_keys=True)


class Replay:
    def __init__(self, folder: pathlib.Path):
        self.folder = folder
        self.responses: dict[str, tuple[int, bytes]] = {}
        self.calls: list[str] = []
        for line in (folder / "index.jsonl").read_text().splitlines():
            e = json.loads(line)
            params = e["request_params"] or {}
            path = urlsplit(e["request_url"]).path
            body = (folder / "raw" / e["sha256"][:2] / e["sha256"]).read_bytes()
            self.responses[_key(path, params.get("query"), params.get("body"))] = (e["http_status"], body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        query = dict(parse_qsl(request.url.query.decode()))
        body = json.loads(request.content) if request.content else None
        key = _key(request.url.path, query, body)
        self.calls.append(request.url.path)
        if key not in self.responses:
            raise AssertionError(f"no saved response for {key}")
        status, content = self.responses[key]
        return httpx.Response(status, content=content, headers={"Content-Type": "application/json"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), base_url="https://api.trongrid.io")


class FakeClock:
    """A clock that only moves when the adapter sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
