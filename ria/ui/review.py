"""Display labels for review statuses."""
from __future__ import annotations



STATUS_BADGE = {
    "PENDING": "🕒 Pending", "APPROVED": "✅ Approved", "EDITED": "✏️ Approved (edited)", "REJECTED": "❌ Rejected",
    "NEEDS_CLARIFICATION": "❓ Needs clarification", "OUT_OF_SCOPE": "🚫 Out of scope", "RESOLVED": "✔️ Resolved",
}


def status_badge(status) -> str:
    return STATUS_BADGE.get(getattr(status, "value", status), str(status))
