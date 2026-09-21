from app.services.context_manager import estimate_tokens, trim_messages


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1  # 4 chars -> 1


def test_trim_by_turns():
    msgs = []
    for i in range(5):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})

    out = trim_messages(
        msgs, system_text="sys", max_tokens=10_000, max_turns=2
    )

    assert [m["content"] for m in out] == ["u3", "a3", "u4", "a4"]


def test_trim_by_token_budget():
    system = "S" * 40  # ~10 tokens
    msgs = [
        {"role": "user", "content": "U" * 40},
        {"role": "assistant", "content": "A" * 40},
        {"role": "user", "content": "recent"},
    ]

    out = trim_messages(
        msgs, system_text=system, max_tokens=20, max_turns=10
    )

    assert out[-1]["content"] == "recent"
    assert all(m["content"] != "U" * 40 for m in out) or (
        estimate_tokens(system)
        + sum(estimate_tokens(m["content"]) for m in out)
        <= 20
    )


def test_trailing_orphan_user_counts_as_a_turn():
    msgs = [
        {"role": "user", "content": "old-user"},
        {"role": "assistant", "content": "old-assistant"},
        {"role": "user", "content": "recent-user"},
    ]

    out = trim_messages(
        msgs, system_text="", max_tokens=10_000, max_turns=1
    )

    assert out == [{"role": "user", "content": "recent-user"}]


def test_token_budget_drops_oldest_pair():
    msgs = [
        {"role": "user", "content": "U" * 20},
        {"role": "assistant", "content": "A" * 20},
        {"role": "user", "content": "recent"},
    ]

    out = trim_messages(
        msgs, system_text="S" * 20, max_tokens=7, max_turns=10
    )

    assert out == [{"role": "user", "content": "recent"}]


def test_token_budget_drops_oldest_orphan_assistant_singly():
    msgs = [
        {"role": "assistant", "content": "A" * 20},
        {"role": "user", "content": "recent"},
    ]

    out = trim_messages(
        msgs, system_text="S" * 20, max_tokens=7, max_turns=10
    )

    assert out == [{"role": "user", "content": "recent"}]
