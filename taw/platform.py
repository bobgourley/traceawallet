"""The platform a user entered (a website or a name), checked against the site lists
we hold. Private: it comes from the user's own input, so the result is stored on
their lookup row (lookup.platform_result), shown only in their private section,
and erased with the lookup. Nothing here is fetched from the user's website.

  website   the host name the user typed (taw.adapters.labels.dfpi.host_of), equal to
            a listed host or a subdomain of one
  name      the name the user typed, as whole words of at least NAME_MIN characters,
            inside a listed entry's subject

Each match quotes the entry's subject in the source's words. The source's own
categories are not repeated.
"""
from __future__ import annotations

import re

from taw.adapters.labels.dfpi import host_of, short_subject

NAME_MIN = 4
SITE_LISTS = ("dfpi-tracker",)
SHOWN = 10                   # entries listed per match; the count says how many in all


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", (text or "").casefold()).split())


def entered(name: str | None, url: str | None) -> dict:
    """What the user typed, as we read it: a host name, a name, or nothing usable."""
    host = host_of(url) if url else None
    words = _words(name) if name else ""
    return {"domain": host, "domain_typed": (url or "").strip()[:200] or None,
            "name": (name or "").strip()[:200] or None, "name_words": words if len(words) >= NAME_MIN else None}


def site_lists(conn, typed: dict) -> list[dict]:
    out = []
    for slug in SITE_LISTS:
        load = conn.execute(
            "SELECT l.id, l.read_on, s.name, s.url FROM site_list_load l JOIN source s ON s.id = l.source_id"
            " WHERE s.slug = %s ORDER BY l.id DESC LIMIT 1", (slug,)).fetchone()
        if load is None:
            name = conn.execute("SELECT name FROM source WHERE slug = %s", (slug,)).fetchone()
            out.append({"source": slug, "loaded": False, "source_name": name[0] if name else slug})
            continue
        load_id, read_on, name, url = load
        by_site, by_name = [], []
        host = typed["domain"]
        if host:
            parents = [host] + [host.split(".", i)[-1] for i in range(1, host.count("."))]
            for subject, websites in conn.execute(
                    "SELECT subject, websites FROM site_list_entry WHERE load_id = %s AND websites && %s::text[]"
                    " ORDER BY position", (load_id, parents)).fetchall():
                hit = next(w for w in parents if w in websites)
                by_site.append({"subject": short_subject(subject), "website": hit, "exact": hit == host})
        words = typed["name_words"]
        if words:
            for subject, in conn.execute("SELECT subject FROM site_list_entry WHERE load_id = %s ORDER BY position",
                                         (load_id,)).fetchall():
                if f" {words} " in f" {_words(subject)} ":
                    by_name.append({"subject": short_subject(subject)})
        out.append({"source": slug, "loaded": True, "source_name": name, "url": url, "read_on": str(read_on),
                    "website_matches": len(by_site), "website_entries": by_site[:SHOWN],
                    "name_matches": len(by_name), "name_entries": by_name[:SHOWN]})
    return out


def check(conn, name: str | None, url: str | None) -> dict | None:
    """The private platform result, or None when nothing usable was entered."""
    typed = entered(name, url)
    if not typed["domain"] and not typed["name_words"]:
        return None if not (name or url) else {"entered": typed, "usable": False}
    return {"entered": typed, "usable": True, "lists": site_lists(conn, typed)}
