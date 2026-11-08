"""The scrubber: text from outside (planner output, upstream errors) before it is stored."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cellrt.planner.schemas import Abort
from cellrt.runtime.scrub import MAX_SCRUB_CHARS, REDACTED, scrub


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("api key sk-proj-AbCdEf0123456789xyz is invalid", "sk-proj-AbCdEf0123456789xyz"),
        ("token=ghp_0123456789abcdefABCDEF0123456789", "ghp_0123456789abcdefABCDEF0123456789"),
        ("Authorization: Bearer abc.def-123_456", "abc.def-123_456"),
        ("password: hunter2!", "hunter2!"),
        ("api_key = 'q1w2e3r4'", "q1w2e3r4"),
        ("aws AKIAIOSFODNN7EXAMPLE leaked", "AKIAIOSFODNN7EXAMPLE"),
        ("mail shaun.chen+cell@example.co.uk now", "shaun.chen+cell@example.co.uk"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpM", "eyJhbGciOi"),
        ("digest 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", "9f86d081"),
        # Credentials in a URL, quoted JSON keys, compound and spaced key names.
        ("fetch https://shaun:hunter2@db.local/x failed", "hunter2"),
        ("postgres://u:p4ss@db:5432/x", "p4ss"),
        ('{"api_key": "abc def ghi"}', "abc def ghi"),
        ('"password":"x y"', "x y"),
        ("client_secret=abc123", "abc123"),
        ("secret_key=abcd", "abcd"),
        ("private_key=zzz", "zzz"),
        ("AWS_SECRET_ACCESS_KEY=abc/def", "abc/def"),
        ("pass: 12345", "12345"),
        ("api key: hunter2", "hunter2"),
        ('token: "a b"', "a b"),
        ("sk_live_51HabcdefGHIJklmn", "sk_live_51HabcdefGHIJklmn"),
        ("key AIzaSyA1234567890abcdefghijklmnop", "AIzaSyA1234567890"),
        ("oauth ya29.a0AfH6SMBx-abcdefghij", "ya29.a0AfH6SMBx"),
        ("-----BEGIN RSA PRIVATE KEY-----MIIEow+IBAAKCAQEA", "MIIEow"),
        ("blob Zm9vYmFyMTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3BxcnN0", "Zm9vYmFy"),
    ],
)
def test_secrets_and_personal_data_are_redacted(text: str, secret: str) -> None:
    scrubbed = scrub(text)

    assert secret not in scrubbed
    assert REDACTED in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        'not json: {"kind": "switch_skill", "skill": "insert_act"} (insert_scripted, 4.5 mm)',
        "token budget exceeded; the password policy says nothing here",
        "passed 3 of 4 checks; auth flow unchanged",
        "test_a_skill_that_cancels_itself_aborts_its_run_not_the_eval",
    ],
)
def test_ordinary_text_is_kept(text: str) -> None:
    # Prose that names a keyword without a ":" or "=" after it, and long identifiers, stay.
    assert scrub(text) == text


UNSAFE = [
    0x00, 0x1B, 0x7F, 0x85, 0x9B,  # C0, DEL, C1 (0x9b alone is a terminal escape)
    0x061C, 0x200B, 0x200D, 0x200E, 0x200F,  # Arabic letter mark, zero-width, direction marks
    0x2028, 0x2029,  # line and paragraph separators: one event could read as two lines
    0x202A, 0x202E, 0x2060, 0x2066, 0x2069, 0xFEFF,  # embeddings, overrides, isolates, BOM
    0xE0001, 0xE007F,  # tag characters
]  # fmt: skip


@pytest.mark.parametrize("code", UNSAFE, ids=hex)
def test_characters_that_change_how_the_log_reads_are_replaced(code: int) -> None:
    assert scrub(f"ok{chr(code)}ok") == "ok?ok"


@pytest.mark.parametrize("code", UNSAFE, ids=hex)
def test_a_planner_abort_reason_with_such_a_character_is_refused(code: int) -> None:
    with pytest.raises(ValidationError):
        Abort(reason=f"looks fine{chr(code)}ok")


def test_only_a_bounded_prefix_is_scrubbed_and_kept() -> None:
    long = "a " * MAX_SCRUB_CHARS

    assert scrub(long) == long[:MAX_SCRUB_CHARS]
