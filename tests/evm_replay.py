"""Replay saved Etherscan responses through httpx, so tests never touch the network.

A fixture folder is what LocalRawStore wrote during a live run: index.jsonl plus
raw/ab/<sha256> bodies. Etherscan-style calls are GETs matched on the query,
with the API key taken out (it is never stored). Blockscout's JSON-RPC calls are
POSTs matched on the path and JSON body.
"""
from __future__ import annotations

import json
import pathlib
from urllib.parse import parse_qsl, urlsplit

import httpx

from tests.tron_replay import FakeClock  # noqa: F401  (re-exported for the EVM tests)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "evm"


def _key(query: dict) -> str:
    return json.dumps({k: str(v) for k, v in query.items() if k != "apikey"}, sort_keys=True)


def _rpc_key(path: str, body: dict) -> str:
    return json.dumps([path, body], sort_keys=True)


class Replay:
    def __init__(self, folder: pathlib.Path):
        self.responses: dict[str, tuple[int, bytes]] = {}
        self.calls: list[dict] = []
        for line in (folder / "index.jsonl").read_text().splitlines():
            e = json.loads(line)
            if e["http_status"] != 200:
                continue
            body = (folder / "raw" / e["sha256"][:2] / e["sha256"]).read_bytes()
            params = e["request_params"]
            if "body" in params:
                self.responses[_rpc_key(urlsplit(e["request_url"]).path, params["body"])] = (200, body)
            else:
                self.responses[_key(params["query"])] = (200, body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            self.calls.append({"rpc": body["method"]})
            key = _rpc_key(request.url.path, body)
        else:
            query = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
            self.calls.append(query)
            key = _key(query)
        if key not in self.responses:
            raise AssertionError(f"no saved response for {key}")
        status, content = self.responses[key]
        return httpx.Response(status, content=content, headers={"Content-Type": "application/json"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))
