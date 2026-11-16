"""M4 roles: the permission table decided on 16.11.2026 and the four-eyes rule."""

import pytest

from personal_agent.roles import PERMISSIONS, Role, allowed, may_approve_level3, needs_passkey

O, B, M, G = Role.OWNER, Role.OFFICE, Role.EMPLOYEE, Role.DEVICE

# The table from _intern/VORSCHLAG_rollen_M4 (decided 16.11.2026), row by row: owner, office, employee, device
TABLE = {
    "dashboard.view":    (True, True, True, True),
    "focus.edit_own":    (True, True, True, False),
    "focus.edit_others": (True, True, False, False),
    "inbox.add":         (True, True, True, True),
    "mail.read":         (True, True, False, False),
    "drafts.edit":       (True, True, False, False),
    "approve.level3":    (True, False, False, False),
    "payee.trust":       (True, False, False, False),
    "revenue_goal.view": (True, True, True, True),  # Marco 16.11.: employees may see it
    "settings.edit":     (True, False, False, False),
    "people.manage":     (True, False, False, False),
}


@pytest.mark.parametrize("action,expected", TABLE.items())
def test_table(action, expected):
    assert tuple(allowed(role, action) for role in (O, B, M, G)) == expected


def test_every_action_is_covered_by_the_table_test():
    assert set(PERMISSIONS) == set(TABLE) | {"approve.level2"}


def test_level2_for_employees_only_on_their_own_things():
    assert allowed(O, "approve.level2") and allowed(B, "approve.level2")
    assert not allowed(M, "approve.level2") and allowed(M, "approve.level2", own=True)
    assert not allowed(G, "approve.level2", own=True)


def test_unknown_roles_and_actions_are_denied():
    assert not allowed("admin", "settings.edit") and not allowed(O, "rocket.launch")
    assert allowed("owner", "settings.edit")  # plain strings work, too


def test_sensitive_actions_need_a_passkey():
    assert {a for a in PERMISSIONS if needs_passkey(a)} == {"approve.level3", "payee.trust", "settings.edit",
                                                            "people.manage"}


def test_four_eyes_with_two_owners():
    # the family business: two owners
    assert not may_approve_level3("anna", "anna", O, owners=2)
    assert may_approve_level3("ben", "anna", O, owners=2)
    assert may_approve_level3("anna", "anna", O, owners=1)  # a single owner approves their own
    assert not may_approve_level3("cleo", "anna", B, owners=2)  # office never approves level 3
    assert may_approve_level3("ben", None, O, owners=2)  # started by the assistant, not by a person
