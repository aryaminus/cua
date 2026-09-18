from cua import mockapp


def client():
    mockapp.reset_state()
    return mockapp.app.test_client()


def test_home_and_search_render():
    c = client()
    assert "MemberServ Console" in c.get("/").get_data(as_text=True)
    assert "Member ID:" in c.get("/search").get_data(as_text=True)


def test_lookup_found_redirects_to_member():
    c = client()
    r = c.get("/lookup?member=1001")
    assert r.status_code == 302
    assert "/member/1001" in r.headers["Location"]


def test_member_detail_shows_accounts():
    c = client()
    body = c.get("/member/1001").get_data(as_text=True)
    assert "ALVAREZ" in body and "Savings" in body and "411-23-8891" in body


def test_not_found_is_a_page_not_an_error():
    c = client()
    r = c.get("/lookup?member=9999")
    assert r.status_code == 200
    assert "No member found for ID 9999" in r.get_data(as_text=True)


def test_validation_error_for_non_numeric():
    c = client()
    body = c.get("/lookup?member=40a").get_data(as_text=True)
    assert "Member ID must be numeric" in body


def test_server_error_fault(monkeypatch):
    monkeypatch.setenv("MOCKAPP_FAULT", "server_error_member_1003")
    c = client()
    r = c.get("/member/1003")
    assert r.status_code == 500
    assert "CMX-2201" in r.get_data(as_text=True)
    assert c.get("/member/1001").status_code == 200  # fault is targeted


def test_session_expiry_oneshot_and_relogin(monkeypatch):
    monkeypatch.setenv("MOCKAPP_SESSION_TTL", "1")
    c = client()
    c.get("/")  # login: resets
    assert "Session Expired" not in c.get("/search").get_data(as_text=True)  # load 1
    body = c.get("/search").get_data(as_text=True)  # load 2 > ttl -> expired once
    assert "Session Expired" in body
    assert "Session Expired" not in c.get("/search").get_data(as_text=True)  # re-armed
    c.get("/")  # login again
    assert "Session Expired" not in c.get("/search").get_data(as_text=True)


def test_frozen_member_renders_restricted_page_not_detail():
    c = client()
    r = c.get("/member/1005")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Access denied" in body and "frozen" in body
    assert "Accounts" not in body  # no balances leak on the restricted page


def test_slow_member_responds_within_wait_budget(monkeypatch):
    import time

    monkeypatch.setenv("MOCKAPP_SLOW_MEMBER", "1006")
    monkeypatch.setenv("MOCKAPP_SLOW_SECONDS", "0.2")
    c = client()
    t0 = time.monotonic()
    r = c.get("/member/1006")
    dt = time.monotonic() - t0
    assert r.status_code == 200
    assert 0.15 < dt < 3.0  # delayed but inside the replay wait budget
    assert "LINDQVIST" in r.get_data(as_text=True)


def test_transient_busy_fires_once_then_recovers(monkeypatch):
    monkeypatch.setenv("MOCKAPP_BUSY_MEMBER", "1002")
    c = client()
    first = c.get("/member/1002").get_data(as_text=True)
    assert "System Busy" in first
    second = c.get("/member/1002").get_data(as_text=True)
    assert "OKAFOR" in second  # the refresh succeeds


def test_freeze_is_irreversible_and_confirms(monkeypatch):
    monkeypatch.delenv("MOCKAPP_NO_DIALOG", raising=False)
    c = client()
    r = c.post("/member/1001/freeze")
    assert r.status_code == 200
    assert "have been frozen" in r.get_data(as_text=True)
    assert mockapp.MEMBERS["1001"]["status"] == "FROZEN"


def test_seed_data_is_deterministic():
    c = client()
    body = c.get("/member/1002").get_data(as_text=True)
    assert "OKAFOR" in body
