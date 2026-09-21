"""Pure helpers for keeping chat history within context limits."""


def estimate_tokens(text: str) -> int:
    """Estimate token usage at roughly one token per four characters."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def trim_messages(
    messages: list[dict],
    *,
    system_text: str,
    max_tokens: int,
    max_turns: int,
) -> list[dict]:
    """Return recent non-system messages within turn and token limits."""
    if max_turns <= 0:
        kept: list[dict] = []
    else:
        user_indexes = [
            index
            for index, message in enumerate(messages)
            if message["role"] == "user"
        ]
        if len(user_indexes) > max_turns:
            kept = list(messages[user_indexes[-max_turns] :])
        else:
            kept = list(messages)

    total_tokens = estimate_tokens(system_text) + sum(
        estimate_tokens(message["content"]) for message in kept
    )
    while kept and total_tokens > max_tokens:
        remove_count = 1
        if (
            len(kept) >= 2
            and kept[0]["role"] == "user"
            and kept[1]["role"] == "assistant"
        ):
            remove_count = 2

        removed = kept[:remove_count]
        del kept[:remove_count]
        total_tokens -= sum(
            estimate_tokens(message["content"]) for message in removed
        )

    return kept
