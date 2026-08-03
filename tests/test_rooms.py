"""Stdlib-only tests for bot.rooms. Run with: python3 -m tests.test_rooms"""

from bot.rooms import SUITE_LETTERS, suite_letters, validate_room


def _assert_room(text: str, expected: str) -> None:
    result = validate_room(text)
    assert result.ok, f"validate_room({text!r}) expected ok, got error={result.error!r}"
    assert result.room == expected, (
        f"validate_room({text!r}) expected room={expected!r}, got {result.room!r}"
    )


def _assert_needs_letter(text: str, expected_base: str) -> None:
    result = validate_room(text)
    assert not result.ok, f"validate_room({text!r}) expected not ok"
    assert result.needs_letter, f"validate_room({text!r}) expected needs_letter=True"
    assert result.base == expected_base, (
        f"validate_room({text!r}) expected base={expected_base!r}, got {result.base!r}"
    )
    assert result.room is None, f"validate_room({text!r}) needs_letter case should have room=None"
    assert result.error is None, f"validate_room({text!r}) needs_letter case should not set error"


def _assert_error(text: str) -> None:
    result = validate_room(text)
    assert not result.ok, f"validate_room({text!r}) expected an error, got ok room={result.room!r}"
    assert not result.needs_letter, f"validate_room({text!r}) unexpectedly set needs_letter"
    assert result.room is None, f"validate_room({text!r}) error case should have room=None"
    assert result.error, f"validate_room({text!r}) expected a non-empty error message"


def test_plain_rooms_normalize() -> int:
    cases = ["#06-27", "06-27", "06 27", "0627", "6-27"]
    for text in cases:
        _assert_room(text, "#06-27")
    return len(cases)


def test_suite_rooms_with_letter_normalize() -> int:
    _assert_room("08-01C", "#08-01C")
    _assert_room("#08-01 c", "#08-01C")
    _assert_room("0801f", "#08-01F")
    _assert_room("07-11b", "#07-11B")
    return 4


def test_suite_room_missing_letter_needs_letter() -> int:
    _assert_needs_letter("08-01", "08-01")
    _assert_needs_letter("8-1", "08-01")
    return 2


def test_invalid_inputs_produce_errors() -> int:
    cases = ["09-01", "06-28", "06-00", "627", "hello", ""]
    for text in cases:
        _assert_error(text)
    return len(cases)


def test_08_11_is_not_a_room() -> int:
    # The RF's home. 06-11 and 07-11 are ordinary suites, so the gap is only
    # on floor 08 and the message has to say why rather than quote a range.
    result = validate_room("08-11")
    assert not result.ok and not result.needs_letter, result
    assert result.error and "RF" in result.error, result.error
    assert validate_room("06-11").needs_letter, "06-11 is still a suite"
    assert validate_room("07-11").needs_letter, "07-11 is still a suite"
    return 3


def test_letter_not_allowed_for_non_suite_room() -> int:
    result = validate_room("06-27A")
    assert not result.ok
    assert not result.needs_letter
    assert result.room is None
    assert result.error and "06-27" in result.error, (
        "error should mention the room, e.g. '#06-27 has no units'"
    )
    return 1


def test_invalid_letter_for_suite_room() -> int:
    result = validate_room("08-01G")
    assert not result.ok
    assert not result.needs_letter
    assert result.room is None
    assert result.error, "expected an 'invalid letter' error message"
    return 1


def test_suite_letters_returns_a_through_f() -> int:
    assert SUITE_LETTERS == "ABCDEF"
    assert suite_letters("08-01") == ["A", "B", "C", "D", "E", "F"]
    assert suite_letters("06-11") == ["A", "B", "C", "D", "E", "F"]
    return 1


def main() -> None:
    tests = (
        test_plain_rooms_normalize,
        test_suite_rooms_with_letter_normalize,
        test_suite_room_missing_letter_needs_letter,
        test_invalid_inputs_produce_errors,
        test_08_11_is_not_a_room,
        test_letter_not_allowed_for_non_suite_room,
        test_invalid_letter_for_suite_room,
        test_suite_letters_returns_a_through_f,
    )
    total_cases = 0
    for test in tests:
        total_cases += test()
    summary = f"{len(tests)}/{len(tests)} test functions, {total_cases} cases"
    print(f"PASS: {summary} — tests/test_rooms.py")


if __name__ == "__main__":
    main()
