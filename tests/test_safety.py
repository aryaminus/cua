from cua.safety import Allowlist, redact, redact_text

CFG = {
    "origins": ["http://127.0.0.1:8791"],
    "routes": ["/", "/search", "/lookup", "/member/*"],
    "actions": ["goto", "click", "fill", "press_enter", "read"],
    "risky_policy": "block",
    "forbidden_elements": [
        {"role": "button", "name_contains": "freeze",
         "class": "irreversible: freezes all member accounts"}
    ],
}


def test_action_allow_and_deny():
    a = Allowlist(CFG)
    assert a.check_action("click").allowed
    v = a.check_action("delete")
    assert not v.allowed and v.risk == "BLOCKED"


def test_url_origin_guard():
    a = Allowlist(CFG)
    assert a.check_url("http://127.0.0.1:8791/search").allowed
    v = a.check_url("http://evil.example/search")
    assert not v.allowed and "origin" in v.reason


def test_url_route_guard():
    a = Allowlist(CFG)
    assert a.check_url("http://127.0.0.1:8791/member/1001").allowed
    v = a.check_url("http://127.0.0.1:8791/admin/wire")
    assert not v.allowed and "route" in v.reason


def test_risky_element_blocked():
    a = Allowlist(CFG)
    v = a.check_element("click", "button", "Freeze Accounts")
    assert not v.allowed and v.risk == "RISKY" and "irreversible" in v.reason
    assert a.check_element("click", "button", "Search").allowed
    assert a.check_element("fill", "textbox", "Member ID").allowed


def test_redaction_ssn_and_amounts():
    t = "SSN 411-23-8891 savings $2,451.90 card 4111111111111111"
    r = redact_text(t)
    assert "411-23-8891" not in r and "2,451.90" not in r and "4111111111111111" not in r
    assert "[REDACTED-SSN]" in r and "$[REDACTED]" in r and "[REDACTED-PAN]" in r


def test_redaction_recursive_structures():
    out = redact({"a": ["SSN 123-45-6789"], "b": {"c": "$9.99"}, "d": 5})
    assert "[REDACTED-SSN]" in out["a"][0]
    assert "$[REDACTED]" in out["b"]["c"]
    assert out["d"] == 5


def test_default_deny_when_unconfigured():
    a = Allowlist({})
    assert not a.check_action("click").allowed
    assert not a.check_url("http://127.0.0.1:8791/").allowed
