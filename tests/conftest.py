"""Shared test fixtures: allowlist config and the standard lookup artifact."""

from cua.schema import Check, Outcome

CFG = {
    "origins": ["http://127.0.0.1:8791", "http://127.0.0.1:8792"],
    "routes": ["/", "/search", "/lookup", "/member/*"],
    "actions": ["goto", "click", "fill", "press_enter", "read"],
    "risky_policy": "block",
    "transient_markers": ["System Busy", "please wait a moment"],
    "forbidden_elements": [
        {"role": "button", "name_contains": "freeze",
         "class": "irreversible: freezes all member accounts"}
    ],
}

OUTCOMES = [
    Outcome(
        id="NOT_FOUND",
        description="No member exists for the requested ID — a legitimate answer, not a failure.",
        detect=Check(text_contains="No member found"),
        returns={"message": "No member found for ID {member_id}"},
    ),
    Outcome(
        id="INVALID_INPUT",
        description="The supplied ID failed validation (non-numeric).",
        detect=Check(text_contains="Member ID must be numeric"),
        returns={"message": "Invalid member ID supplied ({member_id})"},
    ),
]
