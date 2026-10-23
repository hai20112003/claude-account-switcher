from claude_switcher.auto_switch import (
    account_key,
    should_auto_switch,
    choose_auto_switch_target,
)
from claude_switcher.config import AccountInfo
from claude_switcher.usage_state import UsageState, UsageWindow


def _account(email, provider="claude", active=False):
    return AccountInfo(email, "pro", "", active, email, provider=provider)


def _usage(percent):
    return UsageState(True, f"{percent}%", (UsageWindow("test", percent),))


def test_should_auto_switch_requires_enabled_and_exhausted_usage():
    assert should_auto_switch(_usage(100), True, 100) is True
    assert should_auto_switch(_usage(99.9), True, 100) is False
    assert should_auto_switch(_usage(100), False, 100) is False
    assert should_auto_switch(UsageState(False, "Usage unavailable"), True, 100) is False


def test_choose_target_same_provider_only():
    active = _account("active@test.com", "claude", True)
    target = _account("target@test.com", "claude")
    other_provider_same_email = _account("target@test.com", "codex")

    chosen = choose_auto_switch_target(
        "claude",
        [active, other_provider_same_email, target],
        "active@test.com",
        {account_key(target): _usage(12), account_key(other_provider_same_email): _usage(0)},
        lambda account: True,
    )

    assert chosen == target


def test_choose_target_ignores_missing_credentials():
    active = _account("active@test.com", "claude", True)
    no_creds = _account("no-creds@test.com", "claude")
    target = _account("target@test.com", "claude")

    chosen = choose_auto_switch_target(
        "claude",
        [active, no_creds, target],
        "active@test.com",
        {account_key(no_creds): _usage(0), account_key(target): _usage(10)},
        lambda account: account.email != "no-creds@test.com",
    )

    assert chosen == target


def test_choose_target_returns_none_when_all_targets_exhausted():
    active = _account("active@test.com", "claude", True)
    exhausted = _account("exhausted@test.com", "claude")

    chosen = choose_auto_switch_target(
        "claude",
        [active, exhausted],
        "active@test.com",
        {account_key(exhausted): _usage(100)},
        lambda account: True,
    )

    assert chosen is None


def test_choose_target_uses_unknown_usage_as_fallback():
    active = _account("active@test.com", "claude", True)
    unknown = _account("unknown@test.com", "claude")

    chosen = choose_auto_switch_target(
        "claude",
        [active, unknown],
        "active@test.com",
        {},
        lambda account: True,
    )

    assert chosen == unknown
