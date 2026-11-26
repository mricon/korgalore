"""Tests for RawMessage wrapper class."""

import email
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from unittest.mock import patch

import pytest
from liblore.utils import get_clean_msgid

from korgalore import ConfigurationError
from korgalore.message import RawMessage, check_message_date
from tests.target_helpers import DATED_MESSAGE, RECEIVED_AT, SENT_AT, UNDATED_MESSAGE

RECEIVED_ONLY = b'Received: by lore.example.org; Mon, 2 Nov 2026 10:00:00 +0000\n\nBody\n'
SENT_ONLY = b'Date: Mon, 2 Nov 2026 09:58:00 +0000\n\nBody\n'


def trace_value(result: bytes) -> str:
    """The unfolded value of the X-Korgalore-Trace header of a serialized message."""
    value = email.message_from_bytes(result)['X-Korgalore-Trace']
    assert value is not None
    return ' '.join(value.split())


class TestRawMessage:
    """Tests for the RawMessage wrapper class."""

    @pytest.mark.parametrize(
        ('raw', 'expected'),
        [
            pytest.param(
                b'From: test@example.com\r\nMessage-ID: <abc123@example.com>\r\n\r\nBody',
                '<abc123@example.com>',
                id='plain',
            ),
            pytest.param(b'From: test@example.com\r\n\r\nBody', None, id='missing'),
            pytest.param(
                b'From: test@example.com\r\nMessage-ID:  <spaced@example.com>  \r\n\r\nBody',
                '<spaced@example.com>',
                id='surrounding-whitespace-stripped',
            ),
            # Gnus writes these. The whole header value used to come back, which
            # made the IMAP and JMAP duplicate lookups search for a string no
            # stored message has, so every run delivered the message again.
            pytest.param(
                b'From: test@example.com\r\nMessage-ID: <abc123@example.com> (raw)\r\n\r\nBody',
                '<abc123@example.com>',
                id='trailing-comment-dropped',
            ),
            pytest.param(
                b'From: test@example.com\r\nMessage-ID:\r\n <folded@example.com>\r\n\r\nBody',
                '<folded@example.com>',
                id='folded-onto-its-own-line',
            ),
            # The header is malformed, but returning None would skip the
            # duplicate check entirely and redeliver the message on every run.
            # The bare value is returned as-is: the IMAP and JMAP lookups match
            # on the header as written, so invented brackets would not match.
            pytest.param(
                b'From: test@example.com\r\nMessage-ID: bare@example.com\r\n\r\nBody',
                'bare@example.com',
                id='bracketless-kept',
            ),
            pytest.param(b'From: test@example.com\r\nMessage-ID: \r\n\r\nBody', None, id='empty-header-is-none'),
            pytest.param(b'\xff\xfe invalid utf-8 with Message-ID: maybe', None, id='invalid-content-does-not-crash'),
        ],
    )
    def test_message_id(self, raw: bytes, expected: str | None) -> None:
        msg = RawMessage(raw)

        with patch('korgalore.message.get_clean_msgid', wraps=get_clean_msgid) as extract:
            assert msg.message_id == expected
            # A second access is answered from the cache
            assert msg.message_id == expected
        assert extract.call_count <= 1

    def test_parsed_property(self) -> None:
        """Parsed property returns EmailMessage object."""
        raw = b'From: test@example.com\r\nSubject: Test\r\n\r\nBody'
        msg = RawMessage(raw)
        parsed = msg.parsed
        assert parsed.get('From') == 'test@example.com'
        assert parsed.get('Subject') == 'Test'

    def test_parsed_cached(self) -> None:
        """Parsed message is cached."""
        raw = b'From: test@example.com\r\n\r\nBody'
        msg = RawMessage(raw)
        parsed1 = msg.parsed
        parsed2 = msg.parsed
        assert parsed1 is parsed2

    @pytest.mark.parametrize(
        ('raw', 'expected'),
        [
            pytest.param(
                b'From: test@example.com\nSubject: Test\n\nBody\nLine2',
                b'From: test@example.com\r\nSubject: Test\r\n\r\nBody\r\nLine2',
                id='lf-converted',
            ),
            pytest.param(
                b'From: test@example.com\r\nSubject: Test\r\n\r\nBody',
                b'From: test@example.com\r\nSubject: Test\r\n\r\nBody',
                id='crlf-unchanged',
            ),
            pytest.param(
                b'From: test@example.com\r\nSubject: Test\n\nBody\r\nLine2\nLine3',
                b'From: test@example.com\r\nSubject: Test\r\n\r\nBody\r\nLine2\r\nLine3',
                id='mixed-all-converted',
            ),
        ],
    )
    def test_as_bytes_line_endings(self, raw: bytes, expected: bytes) -> None:
        assert RawMessage(raw).as_bytes() == expected

    @pytest.mark.parametrize(
        ('message_date', 'raw', 'expected'),
        [
            ('date', DATED_MESSAGE, SENT_AT),
            ('received', DATED_MESSAGE, RECEIVED_AT),
            ('import', DATED_MESSAGE, None),
            ('date', RECEIVED_ONLY, RECEIVED_AT),
            ('received', SENT_ONLY, SENT_AT),
            ('date', UNDATED_MESSAGE, None),
            ('received', UNDATED_MESSAGE, None),
        ],
        ids=[
            'date-wins-over-received',
            'received-wins-over-date',
            'import-is-left-to-the-target',
            'date-falls-back-to-received',
            'received-falls-back-to-date',
            'date-undated',
            'received-undated',
        ],
    )
    def test_delivery_date(self, message_date: str, raw: bytes, expected: datetime | None) -> None:
        """Each mode prefers its own header and falls back to the other one.

        'date' keeps the order b4 and git send-email encode in Date:,
        which delivery delays can shuffle in Received:.
        """
        assert RawMessage(raw).delivery_date(message_date) == expected

    @pytest.mark.parametrize(
        'date',
        [
            b'Mon, 2 Nov 2099 10:00:00 +0000',
            b'Thu, 1 Jan 1970 00:00:00 +0000',
            b'not a date',
            # Year 9999 west of UTC has no UTC equivalent, so converting it overflows
            b'Fri, 31 Dec 9999 23:59:59 -2359',
        ],
        ids=['future', 'epoch', 'malformed', 'overflow'],
    )
    def test_unusable_date_falls_back_to_received(self, date: bytes) -> None:
        """A Date from a wrong clock or a broken client must not beat a usable Received stamp."""
        raw = RECEIVED_ONLY.replace(b'\n\n', b'\nDate: ' + date + b'\n\n', 1)

        assert RawMessage(raw).delivery_date('date') == RECEIVED_AT

    @pytest.mark.parametrize(
        'received',
        [b'Thu, 1 Jan 1970 00:00:00 +0000', b'Wed, 31 Dec 1969 23:59:59 +0000'],
        ids=['epoch', 'before-epoch'],
    )
    def test_unusable_received_falls_back_to_date(self, received: bytes) -> None:
        """A Received stamp from a clock reset to zero must not hide a usable Date."""
        raw = b'Received: by mx.example.org; ' + received + b'\n' + SENT_ONLY

        assert RawMessage(raw).delivery_date('received') == SENT_AT

    @pytest.mark.parametrize('message_date', ['date', 'received'])
    @pytest.mark.parametrize(
        'raw',
        [
            b'Date: Thu, 1 Jan 1970 00:00:00 +0000\n\nBody\n',
            b'Date: 1 Jan 100 00:00:00 +0000\n\nBody\n',
        ],
        ids=['zero-timestamp', 'before-email-existed'],
    )
    def test_date_at_or_before_epoch_is_no_date(self, message_date: str, raw: bytes) -> None:
        """A clock reset to zero gives no real date, so the target falls back to import time."""
        assert RawMessage(raw).delivery_date(message_date) is None

    @pytest.mark.parametrize(
        ('message_date', 'headers'),
        [
            ('date', b'Date: Mon, 2 Nov 2099 10:00:00 +0000\n'),
            ('received', b'Date: Mon, 2 Nov 2099 10:00:00 +0000\n'),
            (
                'received',
                b'Received: by lore.example.org; Mon, 2 Nov 2026 10:00:00 +0000\n'
                b'Received: by mail.example.com; Mon, 2 Nov 2099 10:00:00 +0000\n'
                b'Date: Mon, 2 Nov 2026 09:58:00 +0000\n',
            ),
        ],
        ids=['date-future-date', 'received-future-date', 'received-future-received'],
    )
    def test_date_is_never_in_the_future(self, message_date: str, headers: bytes) -> None:
        """A clock set ahead would otherwise pin the message to the top of the mailbox."""
        before = datetime.now(UTC)
        date = RawMessage(headers + b'\nBody\n').delivery_date(message_date)
        after = datetime.now(UTC)

        assert date is not None
        assert before <= date <= after


class TestCheckMessageDate:
    """Tests for the message_date target option."""

    @pytest.mark.parametrize('value', ['date', 'received', 'import'])
    def test_valid_values(self, value: str) -> None:
        assert check_message_date('test', value) == value

    @pytest.mark.parametrize('value', ['Date', 'header', ''])
    def test_unknown_value_raises(self, value: str) -> None:
        """A typo is an error, so it can't quietly change the dates every message gets."""
        with pytest.raises(ConfigurationError, match=f"Target 'test' has invalid message_date {value!r}"):
            check_message_date('test', value)


class TestRawMessageTraceHeader:
    """Tests for X-Korgalore-Trace header injection."""

    def test_trace_header_fields(self) -> None:
        """Trace header names the feed and delivery, a version and an RFC 2822 date."""
        raw = b'From: test@example.com\nSubject: Test\n\nBody'
        result = RawMessage(raw).as_bytes(feed_name='linux-kernel', delivery_name='my-delivery')

        value = trace_value(result)
        feed_part, version, date = value.split('; ')
        assert feed_part == 'from feed=linux-kernel for delivery=my-delivery'
        assert version.startswith('v')
        # Raises ValueError unless this is a real RFC 2822 date
        assert parsedate_to_datetime(date).tzinfo is not None

    @pytest.mark.parametrize(
        ('feed_name', 'delivery_name'),
        [
            pytest.param(None, None, id='no-params'),
            pytest.param('test-feed', None, id='feed-only'),
            pytest.param(None, 'test-delivery', id='delivery-only'),
        ],
    )
    def test_trace_header_not_injected_without_both_params(
        self, feed_name: str | None, delivery_name: str | None
    ) -> None:
        raw = b'From: test@example.com\nSubject: Test\n\nBody'

        assert b'X-Korgalore-Trace:' not in RawMessage(raw).as_bytes(feed_name, delivery_name)

    @pytest.mark.parametrize(
        'raw',
        [
            pytest.param(b'From: test@example.com\nSubject: Test\n\nBody content', id='lf-input'),
            pytest.param(b'From: test@example.com\r\nSubject: Test\r\n\r\nBody content', id='crlf-input'),
        ],
    )
    @pytest.mark.parametrize('crlf', [True, False], ids=['crlf-output', 'lf-output'])
    def test_trace_header_is_last_header_with_uniform_endings(self, raw: bytes, crlf: bool) -> None:
        """Trace header goes at the end of the headers, before the body, in the requested line endings.

        Both output modes must agree on placement and format, and no line,
        including the trace header, may end differently from the rest.
        """
        result = RawMessage(raw).as_bytes(feed_name='feed', delivery_name='delivery', crlf=crlf)

        parsed = email.message_from_bytes(result)
        assert list(parsed.keys()) == ['From', 'Subject', 'X-Korgalore-Trace']
        assert parsed.get_payload() == 'Body content'
        if crlf:
            assert result.replace(b'\r\n', b'').find(b'\n') == -1
        else:
            assert b'\r' not in result

    def test_trace_header_message_without_body(self) -> None:
        """Trace header works on message with headers only (no body)."""
        raw = b'From: test@example.com\nSubject: Test'
        result = RawMessage(raw).as_bytes(feed_name='feed', delivery_name='delivery')

        assert b'X-Korgalore-Trace:' in result
        assert b'from feed=feed' in result

    def test_trace_header_special_characters_in_names(self) -> None:
        """Feed/delivery names with special characters are included as-is."""
        raw = b'From: test@example.com\n\nBody'
        result = RawMessage(raw).as_bytes(feed_name='lei:/path/to/feed', delivery_name='my-delivery_v2')

        assert b'from feed=lei:/path/to/feed' in result
        assert b'for delivery=my-delivery_v2' in result

    def test_trace_header_wrapped_at_75_chars(self) -> None:
        """Trace header lines are wrapped at 75 characters, with space-prefixed continuations."""
        raw = b'From: test@example.com\nSubject: Test\n\nBody'
        result = RawMessage(raw).as_bytes(feed_name='linux-kernel', delivery_name='my-delivery')

        value = email.message_from_bytes(result)['X-Korgalore-Trace']
        assert value is not None
        lines = f'X-Korgalore-Trace: {value}'.split('\r\n')
        # The header is too long for one line, so it must continue on the next
        assert len(lines) > 1
        assert all(line.startswith(' ') for line in lines[1:])
        for line in lines:
            assert len(line) <= 75, f'Line too long ({len(line)} chars): {line!r}'
