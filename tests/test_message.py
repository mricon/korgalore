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


class TestRawMessageTraceHeader:
    """Tests for X-Korgalore-Trace header injection."""

    def test_trace_header_injected(self) -> None:
        """Trace header is injected when feed_name and delivery_name provided."""
        raw = b"From: test@example.com\nSubject: Test\n\nBody"
        msg = RawMessage(raw)
        result = msg.as_bytes(feed_name="linux-kernel", delivery_name="my-delivery")

        assert b"X-Korgalore-Trace:" in result
        assert b"from feed=linux-kernel" in result
        assert b"for delivery=my-delivery" in result
        # Header may be wrapped, so check with continuation unfolded
        unfolded = result.replace(b"\r\n ", b" ")
        assert b"; v" in unfolded  # version marker

    def test_trace_header_not_injected_without_params(self) -> None:
        """Trace header is not injected when parameters are None."""
        raw = b"From: test@example.com\nSubject: Test\n\nBody"
        msg = RawMessage(raw)
        result = msg.as_bytes()

        assert b"X-Korgalore-Trace:" not in result

    def test_trace_header_not_injected_with_partial_params(self) -> None:
        """Trace header is not injected when only one parameter is provided."""
        raw = b"From: test@example.com\nSubject: Test\n\nBody"
        msg = RawMessage(raw)

        result1 = msg.as_bytes(feed_name="test-feed")
        assert b"X-Korgalore-Trace:" not in result1

        result2 = msg.as_bytes(delivery_name="test-delivery")
        assert b"X-Korgalore-Trace:" not in result2

    def test_trace_header_position(self) -> None:
        """Trace header is inserted at end of headers, before body."""
        raw = b"From: test@example.com\nSubject: Test\n\nBody content"
        msg = RawMessage(raw)
        result = msg.as_bytes(feed_name="feed", delivery_name="delivery")

        # Find positions
        trace_pos = result.find(b"X-Korgalore-Trace:")
        body_separator = result.find(b"\r\n\r\n")
        body_pos = result.find(b"Body content")

        # Trace should be in headers (before blank line)
        assert trace_pos < body_separator
        # Body should be after blank line
        assert body_pos > body_separator

    def test_trace_header_with_crlf_normalization(self) -> None:
        """Trace header injection works with CRLF normalization."""
        raw = b"From: test@example.com\r\nSubject: Test\r\n\r\nBody"
        msg = RawMessage(raw)
        result = msg.as_bytes(feed_name="feed", delivery_name="delivery")

        # Should have proper CRLF after trace header
        assert b"X-Korgalore-Trace:" in result
        # All line endings should be CRLF
        assert result.replace(b'\r\n', b'').find(b'\n') == -1

    def test_trace_header_contains_date(self) -> None:
        """Trace header contains RFC 2822 formatted date."""
        raw = b"From: test@example.com\nSubject: Test\n\nBody"
        msg = RawMessage(raw)
        result = msg.as_bytes(feed_name="feed", delivery_name="delivery")

        # RFC 2822 dates contain day abbreviations and timezone
        # e.g., "Tue, 27 Jan 2026 16:56:44 -0500"
        # Check for semicolon separator before date
        assert b"; " in result
        # Extract full trace header (may be multi-line with continuations)
        trace_start = result.find(b"X-Korgalore-Trace:")
        trace_end = trace_start
        while True:
            next_line = result.find(b"\r\n", trace_end)
            if next_line == -1:
                break
            # Check if next line is a continuation (starts with whitespace)
            if next_line + 2 < len(result) and result[next_line + 2:next_line + 3] in (b" ", b"\t"):
                trace_end = next_line + 2
            else:
                trace_end = next_line
                break
        trace_header = result[trace_start:trace_end]
        # Unfold continuations for checking
        trace_unfolded = trace_header.replace(b"\r\n ", b" ")
        assert b", " in trace_unfolded  # Day name comma, e.g., "Tue, "

    def test_trace_header_message_without_body(self) -> None:
        """Trace header works on message with headers only (no body)."""
        raw = b"From: test@example.com\nSubject: Test"
        msg = RawMessage(raw)
        result = msg.as_bytes(feed_name="feed", delivery_name="delivery")

        assert b"X-Korgalore-Trace:" in result
        assert b"from feed=feed" in result

    def test_trace_header_special_characters_in_names(self) -> None:
        """Feed/delivery names with special characters are included as-is."""
        raw = b"From: test@example.com\n\nBody"
        msg = RawMessage(raw)
        result = msg.as_bytes(
            feed_name="lei:/path/to/feed",
            delivery_name="my-delivery_v2"
        )

        assert b"from feed=lei:/path/to/feed" in result
        assert b"for delivery=my-delivery_v2" in result

    def test_trace_header_wrapped_at_75_chars(self) -> None:
        """Trace header lines are wrapped at 75 characters."""
        raw = b"From: test@example.com\nSubject: Test\n\nBody"
        msg = RawMessage(raw)
        result = msg.as_bytes(feed_name="linux-kernel", delivery_name="my-delivery")

        # Find the trace header and check line lengths
        trace_start = result.find(b"X-Korgalore-Trace:")
        # Find the end of the trace header (next header or body separator)
        trace_end = trace_start
        while True:
            next_line = result.find(b"\r\n", trace_end)
            if next_line == -1:
                break
            # Check if next line is a continuation (starts with whitespace)
            if next_line + 2 < len(result) and result[next_line + 2:next_line + 3] in (b" ", b"\t"):
                trace_end = next_line + 2
            else:
                trace_end = next_line
                break

        trace_header = result[trace_start:trace_end]
        # Check each line (split by CRLF)
        lines = trace_header.split(b"\r\n")
        for line in lines:
            assert len(line) <= 75, f"Line too long ({len(line)} chars): {line}"

    def test_trace_header_continuation_format(self) -> None:
        """Wrapped trace header uses proper continuation format (space prefix)."""
        raw = b"From: test@example.com\nSubject: Test\n\nBody"
        msg = RawMessage(raw)
        result = msg.as_bytes(feed_name="linux-kernel", delivery_name="my-delivery")

        # The trace header should span multiple lines due to length
        trace_start = result.find(b"X-Korgalore-Trace:")
        # Find continuation lines (CRLF followed by space)
        assert b"\r\n " in result[trace_start:], "Header should have continuation lines"
