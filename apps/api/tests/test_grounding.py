from quantsieve_api.grounding import enforce_grounded_numbers


def test_unsupported_numbers_are_removed() -> None:
    text, grounded = enforce_grounded_numbers(
        "Revenue was 42 and the price rose 99%.", [{"revenue": 42}]
    )

    assert "42" in text
    assert "99%" not in text
    assert "[未经验证数字已省略]" in text
    assert grounded is False


def test_number_must_match_a_complete_evidence_token() -> None:
    text, grounded = enforce_grounded_numbers(
        "The value was 42.", [{"different_value": 142}]
    )

    assert "42" not in text
    assert grounded is False


def test_equivalent_number_format_is_allowed() -> None:
    text, grounded = enforce_grounded_numbers(
        "成交额为 1,234.50。", [{"turnover": 1234.5}]
    )

    assert "1,234.50" in text
    assert grounded is True
