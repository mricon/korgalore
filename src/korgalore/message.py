"""Raw email message wrapper with lazy parsing and common operations."""

from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formatdate, parsedate_to_datetime

from liblore.utils import get_clean_msgid, msg_get_date, parse_message, wrap_header

from korgalore import ConfigurationError, __version__

MESSAGE_DATES = ('date', 'received', 'import')
DEFAULT_MESSAGE_DATE = 'date'

# Nothing older is a real mail date. The epoch itself is what a clock
# reset to zero or a placeholder timestamp gives, so it is not usable either.
EARLIEST_DATE = datetime(1970, 1, 1, tzinfo=UTC)


def is_plausible_date(when: datetime) -> bool:
    """Whether a timestamp can be when a message was really sent or received.

    A date in the future comes from a clock set ahead, and a date at or
    before the Unix epoch from a clock reset to zero.
    """
    return EARLIEST_DATE < when <= datetime.now(UTC)


def check_message_date(identifier: str, message_date: str) -> str:
    """Check a target's message_date option.

    Args:
        identifier: Target identifier, for the error message
        message_date: 'date' to date messages by the Date header first,
            'received' by the latest Received header first, or 'import'
            by the time of delivery

    Returns:
        message_date, once it is known to be valid.

    Raises:
        ConfigurationError: If message_date is unknown
    """
    # Reject anything unknown, instead of falling back to the default,
    # so a typo can't quietly change the dates of every message
    if message_date not in MESSAGE_DATES:
        allowed = ', '.join(repr(md) for md in MESSAGE_DATES)
        raise ConfigurationError(
            f"Target '{identifier}' has invalid message_date {message_date!r}; allowed values: {allowed}"
        )
    return message_date


class RawMessage:
    """Wrapper for raw email bytes with lazy parsing.

    This class provides a common interface for extracting email properties
    without parsing the entire message until needed. Properties are cached
    after first access.

    Usage:
        msg = RawMessage(raw_bytes)
        if msg.message_id:
            print(f"Message-ID: {msg.message_id}")
    """

    def __init__(self, raw_message: bytes) -> None:
        """Initialize with raw email bytes.

        Args:
            raw_message: Raw email bytes (RFC 2822/5322 format)
        """
        self._raw: bytes = raw_message
        self._parsed: EmailMessage | None = None
        self._message_id: str | None = None
        self._message_id_extracted: bool = False

    @property
    def raw(self) -> bytes:
        """Return the raw message bytes."""
        return self._raw

    @property
    def parsed(self) -> EmailMessage:
        """Parse and return the EmailMessage object.

        The parsed message is cached after first access.
        """
        if self._parsed is None:
            self._parsed = parse_message(self._raw)
        assert self._parsed is not None
        return self._parsed

    @property
    def message_id(self) -> str | None:
        """Extract and return the Message-ID header.

        Returns:
            Message-ID string (including angle brackets) or None if not present.
            The value is cached after first access.
        """
        if not self._message_id_extracted:
            self._message_id_extracted = True
            try:
                # get_clean_msgid() pulls the ID out of the angle brackets,
                # so a header carrying a trailing comment (Gnus writes
                # those) yields the ID alone instead of the whole header
                # value. Our callers search on the bracketed form, so put
                # the brackets back.
                msgid = get_clean_msgid(self.parsed)
                if msgid:
                    self._message_id = f'<{msgid}>'
                else:
                    # No brackets to extract from. The header is malformed,
                    # but it still identifies the message, so fall back to
                    # the bare value rather than giving up and letting the
                    # duplicate check be skipped. Deliberately not wrapped
                    # in brackets: the IMAP and JMAP lookups match on the
                    # header as written, so invented brackets would stop
                    # matching the very message we are looking for.
                    raw: object = self.parsed.get('Message-ID')
                    if raw and isinstance(raw, str) and raw.strip():
                        self._message_id = raw.strip()
            except Exception:
                # If parsing fails, leave message_id as None
                pass
        return self._message_id

    def delivery_date(self, message_date: str) -> datetime | None:
        """Return the date a target should store, as an aware UTC datetime.

        Args:
            message_date: a target's message_date option. 'date' prefers
                the Date header, which keeps the order that b4 and git
                send-email give a series even when delivery delays
                reorder the Received stamps. 'received' prefers the
                latest Received header, which the receiving servers set
                from clocks that are more likely to be right. Each falls
                back to the other header.

        Returns:
            None for 'import', or when neither header has a usable
            timestamp; the target then uses the time of delivery.

        Any clock can still be wrong, so the result is kept within the
        bounds of is_plausible_date(). In 'date' mode, a Date outside
        them is skipped in favour of Received. Otherwise a date in the
        future becomes the current time, so that a message from a clock
        set ahead can't stay at the top of the mailbox, and a date at or
        before the Unix epoch counts as no date at all.
        """
        if message_date == 'import':
            return None
        if message_date == 'date':
            sent = self.sent_date
            if sent is not None and is_plausible_date(sent):
                return sent
        # liblore prefers Received and falls back to Date
        when = msg_get_date(self.parsed)
        if when is None or when <= EARLIEST_DATE:
            return None
        return min(when, datetime.now(UTC))

    @property
    def sent_date(self) -> datetime | None:
        """Return the Date header as an aware UTC datetime, or None if it is missing or unparsable.

        Gmail dates a message by this header and accepts no timestamp from
        us, so its target needs the same value Gmail will use.
        """
        header = self.parsed.get('Date')
        if not header:
            return None
        try:
            when = parsedate_to_datetime(str(header))
            # RFC 5322 says a "-0000" zone, which parses as naive, is UTC
            if when.tzinfo is None:
                return when.replace(tzinfo=UTC)
            # A date at the edge of year 9999 can have no UTC equivalent
            return when.astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            return None

    def as_bytes(self, feed_name: str | None = None, delivery_name: str | None = None, *, crlf: bool = True) -> bytes:
        """Return message as binary data suitable for delivery.

        Performs any necessary transformations for target delivery:
        - Normalizes line endings to CRLF as required by RFC 2822/5322,
          or to LF when crlf is False
        - Injects X-Korgalore-Trace header if feed_name and delivery_name provided

        Mail protocols require CRLF, but local tools such as procmail and
        other MDAs expect native Unix LF, the same as git stores.

        Args:
            feed_name: Optional feed name for trace header
            delivery_name: Optional delivery name for trace header
            crlf: Use CRLF line endings (True) or plain LF (False)

        Returns:
            Message bytes ready for delivery to a target.
        """
        # Normalize to LF first, so a message stored with CRLF never comes
        # out with mixed endings, and the trace header only handles one form
        normalized = self._raw.replace(b'\r\n', b'\n')

        # Inject trace header if context is provided
        if feed_name is not None and delivery_name is not None:
            normalized = self._inject_trace_header(normalized, feed_name, delivery_name)

        if not crlf:
            return normalized
        return normalized.replace(b'\n', b'\r\n')

    def _inject_trace_header(self, message: bytes, feed_name: str, delivery_name: str) -> bytes:
        """Inject X-Korgalore-Trace header at the end of headers.

        Operates directly on bytes without using the parsed EmailMessage.

        Args:
            message: Message bytes with LF line endings
            feed_name: Feed name for trace header
            delivery_name: Delivery name for trace header

        Returns:
            Message bytes with trace header injected
        """
        # Build the trace header
        # Format: X-Korgalore-Trace: from feed=[feed] for delivery=[delivery]; v[ver]; [date]
        date_str = formatdate(localtime=True)
        trace_value = f'from feed={feed_name} for delivery={delivery_name}; v{__version__}; {date_str}'
        # wrap_header() folds at 75 columns with a leading-space continuation.
        # We are still working with LF endings here, so keep its default nl.
        trace_bytes = wrap_header(('X-Korgalore-Trace', trace_value)) + b'\n'

        # Find the header/body boundary (empty line)
        # Headers end with \n\n (after LF normalization)
        boundary = message.find(b'\n\n')
        if boundary == -1:
            # No body, append header at the end
            return message + trace_bytes
        # Insert header before the blank line
        return message[: boundary + 1] + trace_bytes + message[boundary + 1 :]
