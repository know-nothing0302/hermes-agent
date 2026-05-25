"""
Pending notification table for WeCom offline routing.

Maintains an in-memory mapping from wecom user IDs to lists of pending
notifications. Used by the TUI gateway to register notifications when
the user is offline, and by the Gateway WeCom adapter to route replies.

Thread-safe: all public functions acquire a re-entrant lock.
"""

import threading
import time

_lock = threading.RLock()

# pending = {
#     "wecom:100002013029": [
#         {
#             "session_id": "20260523_231204",
#             "session_name": "总体执行计划",
#             "notified_at": 1716480000.0,
#             "status": "pending"  # pending | replied | expired | in_progress
#         }
#     ]
# }
_pending: dict[str, list[dict]] = {}


def record(wecom_user: str, session_id: str, session_name: str = "") -> None:
    """Record a pending notification for a WeCom user.

    Called when the TUI agent pushes a CC task completion to WeCom
    because the user is offline. Safe to call multiple times for the
    same session — re-recording a pending entry resets its status and
    notified_at timestamp.
    """
    wecom_user = str(wecom_user).strip()
    session_id = str(session_id).strip()
    session_name = str(session_name or "").strip()
    if not wecom_user or not session_id:
        return

    with _lock:
        entry = {
            "session_id": session_id,
            "session_name": session_name,
            "notified_at": time.time(),
            "status": "pending",
        }
        items = _pending.setdefault(wecom_user, [])

        # Update existing entry for same session, otherwise append
        for i, item in enumerate(items):
            if item.get("session_id") == session_id:
                items[i] = entry
                return
        items.append(entry)


def resolve(wecom_user: str) -> dict:
    """Resolve pending notifications for a WeCom user.

    Returns:
        {"action": "route", "session_id": "xxx"}
            Exactly one pending notification — auto-route.
        {"action": "ask", "options": [{"session_id": "...", "session_name": "..."}, ...]}
            Multiple pending notifications — user must choose.
        {"action": "passthrough"}
            No pending notifications — normal WeCom conversation.
    """
    wecom_user = str(wecom_user).strip()

    with _lock:
        items = _pending.get(wecom_user, [])
        active = [i for i in items if i.get("status") == "pending"]

        if not active:
            return {"action": "passthrough"}

        if len(active) == 1:
            return {"action": "route", "session_id": active[0]["session_id"]}

        return {
            "action": "ask",
            "options": [
                {
                    "session_id": i["session_id"],
                    "session_name": i.get("session_name", ""),
                    "notified_at": i.get("notified_at", 0),
                }
                for i in active
            ],
        }


def mark_replied(wecom_user: str, session_id: str) -> None:
    """Mark a pending notification as replied."""
    wecom_user = str(wecom_user).strip()
    session_id = str(session_id).strip()

    with _lock:
        for item in _pending.get(wecom_user, []):
            if item.get("session_id") == session_id:
                item["status"] = "replied"
                item["replied_at"] = time.time()
                return


def mark_in_progress(wecom_user: str, session_id: str) -> None:
    """Mark a pending notification as in_progress to prevent duplicate routing.

    Called when the user has been shown a choice list (ask action) but
    hasn't selected yet. Prevents the same choice list from being sent
    again on the next message.
    """
    wecom_user = str(wecom_user).strip()
    session_id = str(session_id).strip()

    with _lock:
        for item in _pending.get(wecom_user, []):
            if item.get("session_id") == session_id:
                item["status"] = "in_progress"
                return


def clear_pending(wecom_user: str) -> None:
    """Clear all pending notifications for a WeCom user (manual back-to-normal)."""
    wecom_user = str(wecom_user).strip()
    with _lock:
        _pending.pop(wecom_user, None)


def expire(timeout_seconds: float = 1800.0) -> int:
    """Mark pending notifications older than timeout_seconds as expired.

    Should be called periodically (e.g. by a background timer).
    Returns the number of entries expired.
    """
    cutoff = time.time() - timeout_seconds
    count = 0

    with _lock:
        for items in _pending.values():
            for item in items:
                if item.get("status") == "pending" and item.get("notified_at", 0) < cutoff:
                    item["status"] = "expired"
                    count += 1
    return count


def resolve_numeric(wecom_user: str, choice: int) -> dict:
    """Resolve a numeric choice from multi-session ask.

    Called when the user replies with a number after being shown a list
    of pending sessions. Searches in_progress items (which were set by
    mark_in_progress during the ask action) and routes to the Nth item.

    Returns:
        {"action": "route", "session_id": "xxx"}  on match
        {"action": "passthrough"}                   on invalid choice
    """
    wecom_user = str(wecom_user).strip()
    choice = int(choice)

    with _lock:
        items = _pending.get(wecom_user, [])
        in_progress = [i for i in items if i.get("status") == "in_progress"]
        if 1 <= choice <= len(in_progress):
            return {"action": "route", "session_id": in_progress[choice - 1]["session_id"]}
        return {"action": "passthrough"}


def _reset():
    """Clear all pending data. For testing only."""
    with _lock:
        _pending.clear()
