from hashlib import sha256

from src.content import MAX_CONTENT_CHARS, clamp_content, content_hash


def test_content_hash_matches_sha256_hexdigest_of_known_input():
    assert content_hash("hello world") == sha256(b"hello world").hexdigest()


def test_clamp_content_under_cap_is_untouched():
    text = "short body"
    clamped, truncated = clamp_content(text)
    assert clamped == text
    assert truncated is False


def test_clamp_content_over_cap_is_truncated_to_exactly_max():
    text = "x" * (MAX_CONTENT_CHARS + 100)
    clamped, truncated = clamp_content(text)
    assert len(clamped) == MAX_CONTENT_CHARS
    assert truncated is True


def test_clamp_content_at_exact_cap_is_untouched():
    text = "x" * MAX_CONTENT_CHARS
    clamped, truncated = clamp_content(text)
    assert clamped == text
    assert truncated is False
