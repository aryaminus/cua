"""FakePage: an in-memory deterministic double of the MemberServ console.

It implements the PageSurface protocol exactly like PlaywrightSurface would
experience the real app, so the replay engine, escalation handoff, and
discovery loop are fully testable offline. Fixture drift is guarded by the
browser-backed integration tests (test_integration.py).
"""

from __future__ import annotations

from cua.surface import Element, Snapshot

APP = "http://127.0.0.1:8791"

HOME_ELEMENTS = [
    Element(index=0, role="clicktext", name="Member Lookup", tag="div"),
    Element(index=1, role="clicktext", name="Reports", tag="div"),
    Element(index=2, role="clicktext", name="Administration", tag="div"),
]
SEARCH_ELEMENTS = [
    Element(index=0, role="clicktext", name="Member Lookup", tag="div"),
    Element(index=1, role="textbox", name="Member ID", tag="input"),
    Element(index=2, role="button", name="Search", tag="input"),
]


def _home_text() -> str:
    return (
        "Meridian FCU | MemberServ Console 4.2\nMenu\nMember Lookup\nReports\n"
        "Administration\nOperator Home\nWelcome back. Use Member Lookup to service an account."
    )


def _search_text(banner: str = "") -> str:
    return (
        "Meridian FCU | MemberServ Console 4.2\nMember Lookup\n" + banner
        + "Member ID:\n\nEnter the 4-6 digit member number."
    )


def _member_text(mid: str, name: str, ssn: str, savings: str) -> str:
    return (
        f"Meridian FCU | MemberServ Console 4.2\nMember Detail\nName: {name}\n"
        f"Member #: {mid}\nSSN: {ssn}\nStatus: ACTIVE\nBranch: NORTHGATE\nAccounts\n"
        f"Account Balance Last Activity\nSavings ${savings} 04/12/2026\nChecking $810.22 09/02/2026"
    )


MEMBERS = {
    "1001": ("ALVAREZ, MARIA R", "411-23-8891", "2,451.90"),
    "1002": ("OKAFOR, DANIEL C", "244-71-0935", "19,340.00"),
    "1003": ("TRAN, HANH T", "590-33-1206", "612.45"),
}

# Frozen records are not viewable by the teller role (mirrors the Flask app).
FROZEN = {"1005"}

EXPIRED_TEXT = (
    "Meridian FCU | MemberServ Console 4.2\nSession Expired\nYour session has expired due to "
    "inactivity.\nPlease log in again to continue."
)
ERROR_TEXT = (
    "Meridian FCU | MemberServ Console 4.2\nSystem Error\nUnhandled exception CMX-2201 "
    "while loading member record. The error has been logged."
)

BUSY_TEXT = (
    "Meridian FCU | MemberServ Console 4.2\nSystem Busy\nThe operator console is busy "
    "processing another request. Please wait a moment and refresh the page."
)

RESTRICTED_TEXT = (
    "Meridian FCU | MemberServ Console 4.2\nMember Detail \u2014 Restricted\n"
    "Access denied: record for member {mid} is frozen. Teller role cannot view frozen "
    "accounts. Contact a supervisor."
)


class FakePage:
    def __init__(self, *, fault: str | None = None, session_ttl: int | None = None,
                 busy_once: bool = False, slow: bool = False):
        self.fault = fault
        self.session_ttl = session_ttl
        self.busy_once = busy_once
        self.slow = slow
        self._busy_served = False
        self._busy_mid = "1002"
        self._loads = 0
        self._expired_fired = False
        self._expired_page = False  # browser frozen on expired screen until navigation
        self._filled: dict[int, str] = {}
        self.url = f"{APP}/"
        self._dialogs: list[str] = []
        self.click_log: list[tuple[str, str]] = []

    # -- PageSurface protocol ---------------------------------------------------

    @property
    def dialogs_seen(self) -> list[str]:
        return self._dialogs

    def goto(self, url: str) -> None:
        self.url = url.split("#")[0]
        self._expired_page = False
        if self.url.rstrip("/") == APP:
            self.url = f"{APP}/"
            self._loads = 0  # re-login
        else:
            self._loads += 1  # a navigation is a page load (mirrors the Flask app)

    def reload(self) -> None:
        # A reload consumes the one-shot busy page: if the current URL would
        # render BUSY, the refresh instead reveals the real page underneath —
        # mirroring the Flask app's one-shot transient.
        if self._busy_served is True and self.url.replace(APP, "").startswith("/member/"):
            self._busy_served = "consumed"

    def _busy_armed_view(self, mid: str) -> bool:
        return False  # subsumed by _busy_once_armed at click time; kept for symmetry

    def screenshot(self, path: str) -> bool:
        from pathlib import Path

        Path(path).write_bytes(b"PNG-fixture")
        return True

    def snapshot(self) -> Snapshot:
        if self._expired():
            return Snapshot(url=self.url, text=EXPIRED_TEXT, elements=[])
        path = self.url.replace(APP, "")
        if path in ("", "/"):
            return Snapshot(url=self.url, text=_home_text(), elements=list(HOME_ELEMENTS))
        if path == "/search":
            return Snapshot(url=self.url, text=_search_text(), elements=self._search_elements())
        if path.startswith("/lookup"):
            q = path.split("member=")[-1]
            if not q.isdigit():
                return Snapshot(
                    url=self.url,
                    text=_search_text("Entry error: Member ID must be numeric.\n"),
                    elements=self._search_elements(),
                )
            return Snapshot(
                url=self.url,
                text=_search_text(f"No member found for ID {q}.\n"),
                elements=self._search_elements(),
            )
        if path.startswith("/member/"):
            mid = path.rsplit("/", 1)[-1]
            if self.fault == f"server_error_member_{mid}":
                return Snapshot(url=self.url, text=ERROR_TEXT, elements=[])
            if self.busy_once and mid == self._busy_mid and self._busy_served is True:
                # the click already advanced the URL while marking the view
                # busy: render the transient page until reload() consumes it
                return Snapshot(url=self.url, text=BUSY_TEXT, elements=[])
            if mid == "1005":
                return Snapshot(
                    url=self.url,
                    text=RESTRICTED_TEXT.format(mid=mid),
                    elements=self._search_elements(),
                )
            if mid in MEMBERS:
                name, ssn, sav = MEMBERS[mid]
                return Snapshot(
                    url=self.url,
                    text=_member_text(mid, name, ssn, sav),
                    elements=self._member_elements(mid),
                )
            return Snapshot(
                url=self.url, text=_search_text(f"No member found for ID {mid}.\n"),
                elements=self._search_elements(),
            )
        return Snapshot(url=self.url, text="not found", elements=[])

    def _busy_once_armed(self, q: str) -> bool:
        return bool(self.busy_once and q == self._busy_mid and self._busy_served is False)

    def click(self, el: Element) -> None:
        self.click_log.append((el.role, el.name))
        if el.role == "clicktext":
            target = {"Member Lookup": "/search", "Reports": "/reports",
                      "Administration": "/admin"}.get(el.name)
            if target:
                self.goto(APP + target)
            return
        if el.name == "Search":
            # navigation resolves at click time — snapshots stay read-only,
            # like a real browser (polling never re-requests)
            q = self._filled.get(1, "")
            self._loads += 1  # GET /lookup
            if self._busy_once_armed(q):
                # one-shot transient: the lookup resolves, but the member view
                # first renders the busy page (URL already advanced)
                self._busy_served = True
                self._loads += 1
                self.url = f"{APP}/member/{q}"
                return
            if self.session_ttl is not None and self._loads > self.session_ttl \
                    and not self._expired_fired:
                self._expired_fired = True
                self._expired_page = True  # expired rendered at /lookup; no redirect
                self.url = f"{APP}/lookup?member={q}"
                return
            self._loads += 1  # redirect GET /member/{q}
            self.url = (
                f"{APP}/member/{q}" if ((q.isdigit() and q in MEMBERS) or q in FROZEN)
                else f"{APP}/lookup?member={q}"
            )
            return
        elif "Freeze" in el.name:
            self._dialogs.append("confirm: Freeze all accounts for member 1001?")

    def fill(self, el: Element, value: str) -> None:
        self._filled[el.index] = value

    def press_enter(self, el: Element | None) -> None:
        if 2 in [e.index for e in self._search_elements()[1:]]:
            pass

    # -- internals ---------------------------------------------------------------

    def _search_elements(self) -> list[Element]:
        val = self._filled.get(1, "")
        return [
            HOME_ELEMENTS[0],
            Element(index=1, role="textbox", name="Member ID", value=val, tag="input"),
            SEARCH_ELEMENTS[2],
        ]

    def _member_elements(self, mid: str) -> list[Element]:
        return [
            Element(index=0, role="link", name="Return to Member Lookup", tag="a"),
            Element(index=1, role="button", name="Freeze Accounts", tag="input"),
        ]

    def _expired(self) -> bool:
        if self.session_ttl is None:
            return False
        if self._loads > self.session_ttl and not self._expired_fired:
            self._expired_fired = True  # one-shot; goto('/') re-arms via _loads=0
            self._expired_page = True
            return True
        return self._expired_page
