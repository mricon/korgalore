"""Raw email message wrapper with lazy parsing and common operations."""

from email.message import EmailMessage
from email.utils import formatdate

from liblore.utils import get_clean_msgid, parse_message, wrap_header

from korgalore import __version__


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
