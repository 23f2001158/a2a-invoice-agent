
ALLOWED_ACTIONS = {
    "settle_invoice",
    "request_approval",
    "hold_invoice",
    "reject_duplicate",
    "open_exception",
}


def validate_proposal(proposal):
    """Check that a proposal has the required basic fields."""
    required = {
        "packageId",
        "actionId",
        "action",
        "facts",
        "evidenceRefs",
        "rationale",
    }

    if not isinstance(proposal, dict):
        return False

    if not required.issubset(proposal):
        return False

    if proposal["action"] not in ALLOWED_ACTIONS:
        return False

    if not isinstance(proposal["evidenceRefs"], list):
        return False

    if not isinstance(proposal["rationale"], str):
        return False

    if not 60 <= len(proposal["rationale"]) <= 1500:
        return False

    if len(proposal["actionId"]) < 12:
        return False

    return True
