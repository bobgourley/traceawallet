"""Replay saved Etherscan responses through httpx, so tests never touch the network.

A fixture folder is what LocalRawStore wrote during a live run: index.jsonl plus
raw/ab/<sha256> bodies. Every Etherscan call is GET /v2/api, so requests are
matched on the query alone, with the API key taken out (it is never stored).
"""
from __future__ import annotations

import json
import pathlib
from urllib.parse import parse_qsl

import httpx

from tests.tron_replay import FakeClock  # noqa: F401  (re-exported for the EVM tests)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "evm"


def _key(query: dict) -> str:
    return json.dumps({k: str(v) for k, v in query.items() if k != "apikey"}, sort_keys=True)


class Replay:
    def __init__(self, folder: pathlib.Path):
        self.responses: dict[str, tuple[int, bytes]] = {}
        self.calls: list[dict] = []
        for line in (folder / "index.jsonl").read_text().splitlines():
            e = json.loads(line)
            if e["http_status"] != 200:
                continue
            body = (folder / "raw" / e["sha256"][:2] / e["sha256"]).read_bytes()
            self.responses[_key(e["request_params"]["query"])] = (200, body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        query = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
        self.calls.append(query)
        key = _key(query)
        if key not in self.responses:
            raise AssertionError(f"no saved response for {key}")
        status, content = self.responses[key]
        return httpx.Response(status, content=content, headers={"Content-Type": "application/json"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))
