"""MemberServ — a mock legacy credit-union back-office console.

Stand-in for a real legacy environment: server-rendered,
table-based layout, non-semantic markup, no test IDs, no label/input
association, JS-confirm dialogs on the irreversible action. Deterministic seed
data; fault injection via environment variables so evidence runs are
reproducible.

Faults (armed via env, read per request — deterministic per configuration):
  MOCKAPP_FAULT=server_error_member_1003   -> /member/1003 returns HTTP 500
  MOCKAPP_SESSION_TTL=<N>                  -> after N page loads, every page
                                              returns the session-expired screen
"""

from __future__ import annotations

import os
import threading
import time

from flask import Flask, redirect, request

# ---------------------------------------------------------------- seed data --

MEMBERS: dict[str, dict[str, str]] = {
    "1001": {
        "id": "1001",
        "name": "ALVAREZ, MARIA R",
        "ssn": "411-23-8891",
        "savings": "2,451.90",
        "checking": "810.22",
        "status": "ACTIVE",
        "branch": "NORTHGATE",
    },
    "1002": {
        "id": "1002",
        "name": "OKAFOR, DANIEL C",
        "ssn": "244-71-0935",
        "savings": "19,340.00",
        "checking": "1,205.77",
        "status": "ACTIVE",
        "branch": "RIVERSIDE",
    },
    "1003": {
        "id": "1003",
        "name": "TRAN, HANH T",
        "ssn": "590-33-1206",
        "savings": "612.45",
        "checking": "73.10",
        "status": "ACTIVE",
        "branch": "NORTHGATE",
    },
    "1004": {
        "id": "1004",
        "name": "WHITFIELD, GREGORY",
        "ssn": "337-88-4512",
        "savings": "44,980.13",
        "checking": "9,004.50",
        "status": "ACTIVE",
        "branch": "CENTRAL",
    },
    "1005": {
        "id": "1005",
        "name": "ROJAS, ELENA M",
        "ssn": "618-02-7749",
        "savings": "0.00",
        "checking": "214.63",
        "status": "FROZEN",
        "branch": "RIVERSIDE",
    },
    "1006": {
        "id": "1006",
        "name": "LINDQVIST, PER A",
        "ssn": "882-45-6317",
        "savings": "7,733.08",
        "checking": "7,733.08",
        "status": "ACTIVE",
        "branch": "CENTRAL",
    },
}

_page_loads = 0
_expired_fired = False
_busy_fired = False
_lock = threading.Lock()

app = Flask(__name__)


def _reset_session() -> None:
    global _page_loads
    with _lock:
        _page_loads = 0


def reset_state() -> None:
    """Reset counters — used by tests and between demo phases (determinism)."""
    global _page_loads, _expired_fired, _busy_fired
    with _lock:
        _page_loads = 0
        _expired_fired = False
        _busy_fired = False
        for m in MEMBERS.values():
            if m["id"] != "1005":
                m["status"] = "ACTIVE"


def _session_expired() -> bool:
    """Deterministic one-shot expiry: fires once after TTL loads since the
    last visit to / (the login screen), then re-arms. Root always resets."""
    global _expired_fired, _page_loads
    ttl = os.environ.get("MOCKAPP_SESSION_TTL")
    if not ttl or not ttl.isdigit():
        return False
    with _lock:
        _page_loads += 1
        n = _page_loads
        fired = _expired_fired
    # Outside the lock: expiry fires for loads strictly AFTER the TTL-th load,
    # and only once per arming (root re-arms).
    if n > int(ttl) and not fired:
        with _lock:
            if not _expired_fired:
                _expired_fired = True
                _page_loads = 0  # "re-login required" — the next / visit clears it
                return True
    return False


def _transient_busy(mid: str) -> bool:
    """Deterministic one-shot transient: with MOCKAPP_BUSY_MEMBER=<id> armed,
    the first request for that member renders the 'System Busy' page; the
    refresh (or replay reload) immediately succeeds — exactly the
    JayomOza-style known recoverable condition, and the engine's reload policy
    is what absorbs it."""
    global _busy_fired
    target = os.environ.get("MOCKAPP_BUSY_MEMBER", "")
    if not target or mid not in {x.strip() for x in target.split(",") if x.strip()}:
        return False
    with _lock:
        if not _busy_fired:
            _busy_fired = True
            return True
    return False


# --------------------------------------------------------------- templates --

def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><head><title>{title}</title></head>
<body bgcolor="#d6d2c4">
<table width="720" border="0" cellpadding="4"><tr><td>
<table width="100%" border="1" cellpadding="6" bgcolor="#f2efe6">
<tr><td bgcolor="#1f3b57"><font color="#ffffff" size="+1"><b>Meridian FCU
&nbsp;|&nbsp; MemberServ Console 4.2</b></font></td></tr>
<tr><td>{body}</td></tr>
<tr><td><font size="-1" color="#555555">Meridian Federal Credit Union -
internal use only. Session ID 88451-X.</font></td></tr>
</table></td></tr></table>
</body></html>"""


HOME_BODY = """
<table width="100%" border="0"><tr>
<td width="200" valign="top" bgcolor="#e8e4d8">
  <b><font color="#1f3b57">Menu</font></b>
  <p><div class="menubtn" onclick="location.href='/search'"
       style="cursor:hand;text-decoration:underline">Member Lookup</div></p>
  <p><div class="menubtn" onclick="location.href='/reports'"
       style="cursor:hand;text-decoration:underline">Reports</div></p>
  <p><div class="menubtn" onclick="location.href='/admin'"
       style="cursor:hand;text-decoration:underline">Administration</div></p>
</td>
<td valign="top">
  <h3>Operator Home</h3>
  <font size="-1">Welcome back. Use <b>Member Lookup</b> to service an account.
  Administration functions require supervisor credentials.</font>
</td></tr></table>
"""

SEARCH_BODY = """
<h3>Member Lookup</h3>
<form action="/lookup" method="get">
<table border="0" cellpadding="4">
<tr>
  <td align="right">Member ID:</td>
  <td><input type="text" name="member" size="12" maxlength="9"
             autocomplete="off"></td>
  <td><input type="submit" value="Search"></td>
</tr>
</table>
</form>
<font size="-1">Enter the 4-6 digit member number. Partial numbers are not
supported.</font>
"""

MEMBER_BODY = """
<h3>Member Detail</h3>
<table border="0" cellpadding="3">
<tr><td align="right"><b>Name:</b></td><td>{name}</td></tr>
<tr><td align="right"><b>Member #:</b></td><td>{id}</td></tr>
<tr><td align="right"><b>SSN:</b></td><td>{ssn}</td></tr>
<tr><td align="right"><b>Status:</b></td><td>{status}</td></tr>
<tr><td align="right"><b>Branch:</b></td><td>{branch}</td></tr>
</table>
<h4>Accounts</h4>
<table border="1" cellpadding="4" cellspacing="0">
<tr bgcolor="#c9d4e0"><th>Account</th><th>Balance</th><th>Last Activity</th></tr>
<tr><td>Savings</td><td align="right">${savings}</td><td>04/12/2026</td></tr>
<tr><td>Checking</td><td align="right">${checking}</td><td>09/02/2026</td></tr>
</table>
<p>
<form action="/member/{id}/freeze" method="post" onSubmit="return confirm_f()">
<input type="hidden" name="confirm" value="1">
<input type="submit" value="Freeze Accounts">
</form>
</p>
<script>
function confirm_f() {{
  return confirm("Freeze all accounts for member {id}? This requires supervisor approval.");
}}
</script>
"""

FROZEN_BODY = """
<h3>Confirmation</h3>
<table border="1" cellpadding="6" bgcolor="#f7e6e6">
<tr><td><b>All accounts for member {id} have been frozen.</b><br>
Ref: FRZ-{ref}. A supervisor must approve this action within 1 business day.
</td></tr></table>
<p><a href="/search">Return to Member Lookup</a></p>
"""

EXPIRED_BODY = """
<h3>Session Expired</h3>
<table border="1" cellpadding="6" bgcolor="#f7e6e6">
<tr><td>Your session has expired due to inactivity.<br>
Please <a href="/">log in again</a> to continue.</td></tr></table>
"""

RESTRICTED_BODY = """
<h3>Member Detail — Restricted</h3>
<table border="1" cellpadding="6" bgcolor="#f7eddf"><tr><td>
<b>Access denied:</b> record for member {id} is frozen. Teller role cannot
view frozen accounts. Contact a supervisor.
</td></tr></table>
"""

BUSY_BODY = """
<h3>System Busy</h3>
<table border="1" cellpadding="6" bgcolor="#f7eddf"><tr><td>
The operator console is busy processing another request. Please wait a moment
and refresh the page.
</td></tr></table>
"""


# ------------------------------------------------------------------ routes --

@app.get("/")
def home():
    _reset_session()  # the operator home doubles as the re-login screen
    return _page("Operator Home", HOME_BODY)


@app.get("/search")
def search():
    if _session_expired():
        return _page("Session Expired", EXPIRED_BODY)
    return _page("Member Lookup", SEARCH_BODY)


@app.get("/lookup")
def lookup():
    if _session_expired():
        return _page("Session Expired", EXPIRED_BODY)
    raw = (request.args.get("member") or "").strip()
    if not raw.isdigit():
        return _page(
            "Member Lookup",
            "<h3>Member Lookup</h3>"
            '<table border="1" cellpadding="6" bgcolor="#f7eddf"><tr><td>'
            "<b>Entry error:</b> Member ID must be numeric.</td></tr></table>"
            + SEARCH_BODY,
        ), 200
    if raw not in MEMBERS:
        return _page(
            "Member Lookup",
            "<h3>Member Lookup</h3>"
            '<table border="1" cellpadding="6" bgcolor="#f7eddf"><tr><td>'
            f"No member found for ID {raw}. Check the number and try again."
            "</td></tr></table>" + SEARCH_BODY,
        ), 200
    return redirect(f"/member/{raw}")


@app.get("/member/<mid>")
def member(mid: str):
    fault = os.environ.get("MOCKAPP_FAULT", "")
    if fault == f"server_error_member_{mid}":
        return _page(
            "System Error",
            "<h3>System Error</h3>"
            '<table border="1" cellpadding="6" bgcolor="#f7e6e6"><tr><td>'
            "Unhandled exception CMX-2201 while loading member record. "
            "The error has been logged.</td></tr></table>",
        ), 500
    if _session_expired():
        return _page("Session Expired", EXPIRED_BODY)
    m = MEMBERS.get(mid)
    if m is None:
        return _page(
            "Member Lookup",
            "<h3>Member Lookup</h3>"
            '<table border="1" cellpadding="6" bgcolor="#f7eddf"><tr><td>'
            f"No member found for ID {mid}. Check the number and try again."
            "</td></tr></table>" + SEARCH_BODY,
        ), 200
    if m["status"] == "FROZEN":
        return _page("Restricted Record", RESTRICTED_BODY.format(id=mid)), 200
    if _transient_busy(mid):
        return _page("System Busy", BUSY_BODY), 200
    slow = os.environ.get("MOCKAPP_SLOW_MEMBER", "")
    if slow and mid in {x.strip() for x in slow.split(",") if x.strip()}:
        time.sleep(float(os.environ.get("MOCKAPP_SLOW_SECONDS", "2.5")))
    return _page(f"Member {mid}", MEMBER_BODY.format(**m))


@app.post("/member/<mid>/freeze")
def freeze(mid: str):
    if mid not in MEMBERS:
        return _page("Error", "<h3>Unknown member</h3>"), 404
    MEMBERS[mid]["status"] = "FROZEN"
    ref = f"{mid}-77"
    return _page("Confirmation", FROZEN_BODY.format(id=mid, ref=ref))


@app.get("/reports")
def reports():
    if _session_expired():
        return _page("Session Expired", EXPIRED_BODY)
    return _page("Reports", "<h3>Reports</h3><p>No reports scheduled.</p>")


@app.get("/admin")
def admin():
    if _session_expired():
        return _page("Session Expired", EXPIRED_BODY)
    return _page(
        "Administration",
        "<h3>Administration</h3>"
        '<table border="1" cellpadding="6" bgcolor="#f7eddf"><tr><td>'
        "<b>Access denied:</b> supervisor credentials required.</td></tr></table>",
    )


def serve(port: int = 8791) -> None:
    """Run the console locally. threaded=False keeps the session counter
    deterministic for single-client automation runs."""
    app.run(host="127.0.0.1", port=port, threaded=False, use_reloader=False)
