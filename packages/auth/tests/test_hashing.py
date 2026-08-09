from platform_auth.hashing import generate_key, parse_prefix, verify_key, PREFIX_LENGTH


def test_roundtrip_with_envelope():
    plaintext, prefix, key_hash = generate_key("tt_")
    assert plaintext.startswith("tt_")
    assert len(prefix) == PREFIX_LENGTH
    assert parse_prefix(plaintext, "tt_") == prefix
    assert verify_key(plaintext, key_hash) is True
    assert verify_key("tt_wrong_key", key_hash) is False


def test_parse_prefix_rejects_foreign_envelope():
    plaintext, _, _ = generate_key("doc_")
    assert parse_prefix(plaintext, "tt_") is None  # wrong envelope
    assert parse_prefix("garbage", "doc_") is None
    assert parse_prefix(plaintext, "doc_") is not None
