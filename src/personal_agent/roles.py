"""Roles and permissions of a business installation (M4), as decided on 16.11.2026.

One login per person (passkey) and a shared team view; no shared account. Four roles:

  owner     everything, incl. settings, costs, credentials (sensitive actions need a fresh passkey)
  office    mail, drafts, tasks, ordinary (level-2) approvals
  employee  own focus list, inbox, team view; level-2 approvals only for their own tasks
  device    a workshop tablet or wall display: team view and inbox thoughts, nothing else

`allowed(role, action)` is the single source for the table below; the web layer (L1) and the
assistant ask it, they never keep their own copy. `needs_passkey(action)` marks the actions that
need a fresh passkey check. `may_approve_level3` is the four-eyes rule: with more than one owner,
nobody approves a level-3 action they started themselves.

The revenue target is visible to every role (Marco 16.11.2026: employees may see it).
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    OWNER = "owner"
    OFFICE = "office"
    EMPLOYEE = "employee"
    DEVICE = "device"


OWN_ONLY = "own"  # allowed only for things the person owns (their own tasks)

# action -> {role: True | OWN_ONLY}; a role missing from the dict is not allowed
PERMISSIONS: dict[str, dict[Role, object]] = {
    "dashboard.view":        {Role.OWNER: True, Role.OFFICE: True, Role.EMPLOYEE: True, Role.DEVICE: True},
    "revenue_goal.view":     {Role.OWNER: True, Role.OFFICE: True, Role.EMPLOYEE: True, Role.DEVICE: True},
    "inbox.add":             {Role.OWNER: True, Role.OFFICE: True, Role.EMPLOYEE: True, Role.DEVICE: True},
    "focus.edit_own":        {Role.OWNER: True, Role.OFFICE: True, Role.EMPLOYEE: True},
    "focus.edit_others":     {Role.OWNER: True, Role.OFFICE: True},
    "mail.read":             {Role.OWNER: True, Role.OFFICE: True},
    "drafts.edit":           {Role.OWNER: True, Role.OFFICE: True},
    "approve.level2":        {Role.OWNER: True, Role.OFFICE: True, Role.EMPLOYEE: OWN_ONLY},
    "approve.level3":        {Role.OWNER: True},
    "payee.trust":           {Role.OWNER: True},
    "settings.edit":         {Role.OWNER: True},
    "people.manage":         {Role.OWNER: True},
}

PASSKEY_ACTIONS = frozenset({"approve.level3", "payee.trust", "settings.edit", "people.manage"})


def allowed(role: Role | str, action: str, *, own: bool = False) -> bool:
    """May this role do this action? `own`: the object belongs to the person (e.g. their task).
    Unknown roles or actions are denied."""
    try:
        role = Role(role)
    except ValueError:
        return False
    grant = PERMISSIONS.get(action, {}).get(role)
    if grant is True:
        return True
    return grant == OWN_ONLY and own


def needs_passkey(action: str) -> bool:
    return action in PASSKEY_ACTIONS


def may_approve_level3(approver: str, initiator: str | None, approver_role: Role | str, owners: int) -> bool:
    """Four-eyes rule: an owner approves level 3; with more than one owner never their own action."""
    if not allowed(approver_role, "approve.level3"):
        return False
    if owners > 1 and initiator is not None and approver == initiator:
        return False
    return True


__all__ = ["OWN_ONLY", "PASSKEY_ACTIONS", "PERMISSIONS", "Role", "allowed", "may_approve_level3", "needs_passkey"]
