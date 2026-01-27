"""Tests for RawMessage wrapper class."""

from korgalore.message import RawMessage


class TestRawMessage:
    """Tests for the RawMessage wrapper class."""

    def test_message_id_extraction(self) -> None:
        """Message-ID is correctly extracted from raw email."""
        raw = b"From: test@example.com\r\nMessage-ID: <abc123@example.com>\r\n\r\nBody"
        msg = RawMessage(raw)
        assert msg.message_id == "<abc123@example.com>"

    def test_message_id_missing(self) -> None:
        """Returns None when Message-ID is missing."""
        raw = b"From: test@example.com\r\n\r\nBody"
        msg = RawMessage(raw)
        assert msg.message_id is None

    def test_message_id_cached(self) -> None:
        """Message-ID extraction is cached."""
        raw = b"From: test@example.com\r\nMessage-ID: <test@example.com>\r\n\r\nBody"
        msg = RawMessage(raw)
        # Access twice
        _ = msg.message_id
        msgid = msg.message_id
        assert msgid == "<test@example.com>"
        # Verify it was extracted only once
        assert msg._message_id_extracted is True

    def test_message_id_with_whitespace(self) -> None:
        """Message-ID with surrounding whitespace is stripped."""
        raw = b"From: test@example.com\r\nMessage-ID:  <spaced@example.com>  \r\n\r\nBody"
        msg = RawMessage(raw)
        assert msg.message_id == "<spaced@example.com>"

    def test_raw_property(self) -> None:
        """Raw property returns original bytes."""
        raw = b"From: test@example.com\r\n\r\nBody"
        msg = RawMessage(raw)
        assert msg.raw is raw

    def test_parsed_property(self) -> None:
        """Parsed property returns EmailMessage object."""
        raw = b"From: test@example.com\r\nSubject: Test\r\n\r\nBody"
        msg = RawMessage(raw)
        parsed = msg.parsed
        assert parsed.get('From') == 'test@example.com'
        assert parsed.get('Subject') == 'Test'

    def test_parsed_cached(self) -> None:
        """Parsed message is cached."""
        raw = b"From: test@example.com\r\n\r\nBody"
        msg = RawMessage(raw)
        parsed1 = msg.parsed
        parsed2 = msg.parsed
        assert parsed1 is parsed2

    def test_as_bytes_from_lf(self) -> None:
        """Unix LF endings are converted to CRLF."""
        raw = b"From: test@example.com\nSubject: Test\n\nBody\nLine2"
        msg = RawMessage(raw)
        normalized = msg.as_bytes()
        assert normalized == b"From: test@example.com\r\nSubject: Test\r\n\r\nBody\r\nLine2"

    def test_as_bytes_already_normalized(self) -> None:
        """Already-normalized CRLF content is unchanged."""
        raw = b"From: test@example.com\r\nSubject: Test\r\n\r\nBody"
        msg = RawMessage(raw)
        normalized = msg.as_bytes()
        assert normalized == raw

    def test_as_bytes_mixed_endings(self) -> None:
        """Mixed line endings are all converted to CRLF."""
        raw = b"From: test@example.com\r\nSubject: Test\n\nBody\r\nLine2\nLine3"
        msg = RawMessage(raw)
        normalized = msg.as_bytes()
        assert b'\r\n' in normalized
        # All \n should be \r\n now
        assert normalized.replace(b'\r\n', b'').find(b'\n') == -1

    def test_invalid_message_message_id(self) -> None:
        """Invalid message content doesn't crash message_id extraction."""
        raw = b"\xff\xfe invalid utf-8 with Message-ID: maybe"
        msg = RawMessage(raw)
        # Should not raise, may return None or partial result
        _ = msg.message_id
