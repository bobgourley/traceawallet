"""Daily list refreshes. Phase 1: OFAC's SDN list."""
from __future__ import annotations

from taw import labels
from taw.adapters.labels.ofac import OfacSdn


def evm_chains(conn) -> list[str]:
    return [c for (c,) in conn.execute("SELECT id FROM chain WHERE family = 'evm' ORDER BY id").fetchall()]


def refresh_ofac(*, data_conn, raw_store, actor: str, source=None) -> str:
    source = source or OfacSdn(raw_store, evm_chains(data_conn))
    pub = source.load()
    result = labels.load_publication(data_conn, actor, pub)
    return (f"OFAC SDN list published {pub.source_date}: {result.summary()}; {pub.unmapped} address(es) in "
            f"formats we don't cover")
