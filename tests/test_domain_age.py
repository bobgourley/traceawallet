"""Domain age (taw/domain_age.py): registration date from the registry's RDAP record,
first certificate from crt.sh, and the private-section wording. No network: a fake
transport answers, and records every host asked so we can show the user's own
website is never contacted."""
import datetime as dt
import json

import httpx
import pytest

from taw.domain_age import DomainAge, candidates, gap_days
from taw.report.build import build
from taw.report.render import render_html, render_text
from tests import report_fixtures as rf

BOOT = {"version": "1.0", "services": [[["com", "net"], ["https://rdap.verisign.com/com/v1/"]],
                                       [["uk"], ["https://rdap.nominet.uk/uk/"]]]}
TODAY = dt.date(2026, 11, 21)


class Raw:
    def __init__(self):
        self.saved = []

    def save(self, source, url, params, status, body):
        self.saved.append((source, url, status))
        return f"{len(self.saved):064x}"


def net(rdap: dict, crt=None, crt_fail=0):
    """rdap: {name: status or record}; crt: list of certificate rows, or None for a 502."""
    seen, fails = [], [crt_fail]

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "data.iana.org":
            return httpx.Response(200, json=BOOT)
        if request.url.host == "crt.sh":
            if fails[0] > 0 or crt is None:
                fails[0] -= 1
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, json=crt)
        name = request.url.path.rsplit("/", 1)[-1]
        answer = rdap.get(name, 404)
        if answer == 302:
            return httpx.Response(302, headers={"location": f"https://{name}/whois"})
        if isinstance(answer, int):
            return httpx.Response(answer, json={"errorCode": answer, "title": "Not Found"})   # as registries answer
        return httpx.Response(200, json=answer)
    raw = Raw()
    checker = DomainAge(raw, http=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
                        today=lambda: TODAY)
    return checker, seen, raw


def record(registered="2025-03-02T10:00:00Z", name="HCXZ.COM"):
    return {"ldhName": name, "events": [{"eventAction": "registration", "eventDate": registered},
                                        {"eventAction": "expiration", "eventDate": "2027-03-02T10:00:00Z"}]}


CERTS = [{"id": 99, "not_before": "2025-03-05T00:00:00", "issuer_name": "x"},
         {"id": 42, "not_before": "2025-03-03T12:00:00", "issuer_name": "y"}]


@pytest.mark.parametrize("host,names", [
    ("hcxz.com", ["hcxz.com"]), ("app.hcxz.com", ["app.hcxz.com", "hcxz.com"]),
    ("shop.example.co.uk", ["shop.example.co.uk", "example.co.uk"]), ("example.co.uk", ["example.co.uk"]),
])
def test_names_asked_of_the_registry(host, names):
    assert candidates(host) == names


def test_dates_and_gaps_and_only_the_registry_and_crtsh_are_asked():
    checker, seen, raw = net({"hcxz.com": record()}, CERTS)
    out = checker.check("app.hcxz.com", dt.date(2025, 4, 16))
    assert out["registration"] == {"status": "found", "domain": "hcxz.com", "registered": "2025-03-02",
                                   "server": "rdap.verisign.com", "read_on": "2026-11-21",
                                   "raw_sha256": out["registration"]["raw_sha256"]}
    assert out["certificate"]["first_not_before"] == "2025-03-03" and out["certificate"]["certificate_id"] == 42
    assert (out["registration_gap_days"], out["certificate_gap_days"]) == (45, 44)
    assert set(seen) == {"data.iana.org", "rdap.verisign.com", "crt.sh"}           # never hcxz.com itself
    assert [s for s, _, _ in raw.saved] == ["rdap", "rdap", "rdap", "crtsh"]     # every reply kept


def test_a_redirect_is_not_followed_to_anyones_website():
    checker, seen, _ = net({"hcxz.com": 302})
    reg = checker.registration("hcxz.com")
    assert reg["status"] == "not_read" and reg["reason"] == "HTTP 302"
    assert "hcxz.com" not in seen


def test_no_record_no_service_and_no_certificates():
    checker, _, _ = net({}, [])
    assert checker.registration("gone.com")["status"] == "no_record"
    assert checker.registration("example.io") == {"status": "no_service", "tld": "io", "read_on": "2026-11-21"}
    assert checker.first_certificate("gone.com")["status"] == "none"


def test_crtsh_is_asked_twice_at_most():
    checker, seen, _ = net({}, CERTS, crt_fail=1)
    assert checker.first_certificate("hcxz.com")["status"] == "found" and seen.count("crt.sh") == 2
    checker, seen, _ = net({}, None)
    assert checker.first_certificate("hcxz.com") == {"status": "not_read", "reason": "HTTP 502", "domain": "hcxz.com",
                                                     "read_on": "2026-11-21"}
    assert seen.count("crt.sh") == 2


def test_gap_days():
    assert gap_days("2025-03-02", dt.date(2025, 4, 16)) == 45
    assert gap_days("2025-03-02", "2025-03-01") == -1 and gap_days("2025-03-02", None) is None


def section(domain_age, entered="https://app.hcxz.com/x"):
    data = build(rf.case_input("sdny-1-26-cv-08010-usdt")[0])
    plat = {"entered": {"domain": "app.hcxz.com", "domain_typed": entered, "name": None, "name_words": None},
            "usable": True, "lists": [{"source": "dfpi-tracker", "loaded": False,
                                       "source_name": "California DFPI Crypto Scam Tracker"}],
            "domain_age": domain_age}
    text = render_text(data, platform=plat)
    return text[text.index("== The platform you entered"):text.index("== Chains ==")], data, plat


def test_the_private_section_gives_dated_facts_with_their_source():
    checker, _, _ = net({"hcxz.com": record()}, CERTS)
    text, data, plat = section(checker.check("app.hcxz.com", dt.date(2025, 4, 16)))
    assert ("Dates for app.hcxz.com:\n  - Registration: 2025-03-02, in the registry's RDAP record for hcxz.com "
            "(rdap.verisign.com, read 2026-11-21). That is 45 days before the payment date you entered (2025-04-16).\n"
            "  - First certificate: valid from 2025-03-03, the earliest of 2 certificates crt.sh lists for hcxz.com "
            "(crt.sh certificate 42, searched 2026-11-21). That is 44 days before the payment date you entered "
            "(2025-04-16).\n") in text
    assert "Nothing was fetched from the website itself." in text
    for word in ("new domain", "young", "suspicious", "risk", "scam", "fraud", "red flag", "warning"):
        assert word not in text.replace("Crypto Scam Tracker", "").lower()
    html = str(render_html(data, platform=plat))
    assert "hcxz.com</a>" not in html and "Dates for app.hcxz.com:" in html


def test_without_a_payment_date_there_is_no_gap_and_other_outcomes_are_plain():
    checker, _, _ = net({}, None)
    text, _, _ = section(checker.check("app.hcxz.com"))
    assert ("  - Registration: the registry's RDAP service (rdap.verisign.com) had no record of hcxz.com on "
            "2026-11-21.") in text
    assert "  - First certificate: not read (HTTP 502).\n" in text and "payment date" not in text
    text, _, _ = section({"host": "app.hcxz.com", "error": "ConnectError"})
    assert "Dates for app.hcxz.com:\n  - Not read (ConnectError).\n" in text


def test_a_payment_after_or_on_the_day():
    checker, _, _ = net({"hcxz.com": record()}, CERTS)
    text, _, _ = section(checker.check("app.hcxz.com", dt.date(2025, 3, 2)))
    assert "That is the same day as the payment date you entered (2025-03-02)." in text
    checker, _, _ = net({"hcxz.com": record()}, CERTS)
    text, _, _ = section(checker.check("app.hcxz.com", dt.date(2025, 3, 1)))
    assert "That is 1 day after the payment date you entered (2025-03-01)." in text


def test_the_platform_check_asks_for_dates_only_for_a_website():
    from taw import platform

    class Checker:
        asked = []

        def check(self, host, paid_on):
            self.asked.append((host, paid_on))
            return {"host": host}

    class Conn:
        def execute(self, *a):
            class R:
                def fetchone(self):
                    return None
            return R()
    c = Checker()
    out = platform.check(Conn(), None, "https://app.hcxz.com/x", paid_on=dt.date(2025, 4, 16), domain_age=c)
    assert c.asked == [("app.hcxz.com", dt.date(2025, 4, 16))] and out["domain_age"] == {"host": "app.hcxz.com"}
    assert "domain_age" not in platform.check(Conn(), "Noel Crypto", None, domain_age=c)
    assert json.dumps(out)                                                       # stored as JSON


def test_long_gaps_also_say_years():
    checker, _, _ = net({"hcxz.com": record("2009-02-04T20:19:24Z")}, CERTS)
    text, _, _ = section(checker.check("app.hcxz.com", dt.date(2026, 3, 10)))
    assert "2009-02-04, in the registry's RDAP record for hcxz.com" in text
    assert "That is 6,243 days (about 17 years) before the payment date you entered (2026-03-10)." in text
