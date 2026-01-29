"""Raw email message wrapper with lazy parsing and common operations."""

from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import EmailPolicy
from email.utils import formatdate
from typing import Optional

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

    # Email parsing policy matching PIFeed.emlpolicy
    _policy: EmailPolicy = EmailPolicy(
        utf8=True,
        cte_type='8bit',
        max_line_length=0,  # No line length limit
        message_factory=EmailMessage
    )

    def __init__(self, raw_message: bytes) -> None:
        """Initialize with raw email bytes.

        Args:
            raw_message: Raw email bytes (RFC 2822/5322 format)
        """
        self._raw: bytes = raw_message
        self._parsed: Optional[EmailMessage] = None
        self._message_id: Optional[str] = None
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
            self._parsed = BytesParser(
                _class=EmailMessage,
                policy=self._policy
            ).parsebytes(self._raw)  # type: ignore[assignment]
        assert self._parsed is not None
        return self._parsed

    @property
    def message_id(self) -> Optional[str]:
        """Extract and return the Message-ID header.

        Returns:
            Message-ID string (including angle brackets) or None if not present.
            The value is cached after first access.
        """
        if not self._message_id_extracted:
            self._message_id_extracted = True
            try:
                msgid = self.parsed.get('Message-ID')
                if msgid and isinstance(msgid, str):
                    self._message_id = msgid.strip()
            except Exception:
                # If parsing fails, leave message_id as None
                pass
        return self._message_id

    def as_bytes(
        self,
        feed_name: Optional[str] = None,
        delivery_name: Optional[str] = None
    ) -> bytes:
        """Return message as binary data suitable for delivery.

        Performs any necessary transformations for target delivery:
        - Normalizes line endings to CRLF as required by RFC 2822/5322
        - Injects X-Korgalore-Trace header if feed_name and delivery_name provided

        Git stores messages with Unix LF endings, but mail protocols require CRLF.

        Args:
            feed_name: Optional feed name for trace header
            delivery_name: Optional delivery name for trace header

        Returns:
            Message bytes ready for delivery to a target.
        """
        # First normalize to LF, then we'll convert to CRLF at the end
        normalized = self._raw.replace(b'\r\n', b'\n')

        # Inject trace header if context is provided
        if feed_name is not None and delivery_name is not None:
            normalized = self._inject_trace_header(normalized, feed_name, delivery_name)

        # Convert to CRLF
        return normalized.replace(b'\n', b'\r\n')

    def _wrap_header(self, name: str, value: str, max_line: int = 75) -> str:
        """Wrap a header value with proper email header continuation.

        Args:
            name: Header name (e.g., 'X-Korgalore-Trace')
            value: Header value to wrap
            max_line: Maximum line length (default 75)

        Returns:
            Wrapped header string with continuation lines
        """
        first_line_max = max_line - len(name) - 2  # account for ": "
        if len(value) <= first_line_max:
            return f"{name}: {value}"

        # Split value into words for wrapping
        words = value.split(' ')
        lines = []
        current_line = f"{name}:"

        for word in words:
            # Check if adding this word exceeds max length
            test_line = current_line + ' ' + word if current_line.endswith(':') is False else current_line + ' ' + word
            if current_line == f"{name}:":
                test_line = current_line + ' ' + word
            else:
                test_line = current_line + ' ' + word

            if len(test_line) <= max_line:
                if current_line.endswith(':'):
                    current_line = current_line + ' ' + word
                else:
                    current_line = current_line + ' ' + word
            else:
                lines.append(current_line)
                # Continuation line starts with space
                current_line = ' ' + word

        lines.append(current_line)
        return '\n'.join(lines)

    def _inject_trace_header(
        self,
        message: bytes,
        feed_name: str,
        delivery_name: str
    ) -> bytes:
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
        trace_value = (
            f"from feed={feed_name} for delivery={delivery_name}; "
            f"v{__version__}; {date_str}"
        )
        trace_header = self._wrap_header('X-Korgalore-Trace', trace_value) + '\n'
        trace_bytes = trace_header.encode('utf-8')

        # Find the header/body boundary (empty line)
        # Headers end with \n\n (after LF normalization)
        boundary = message.find(b'\n\n')
        if boundary == -1:
            # No body, append header at the end
            return message + trace_bytes
        else:
            # Insert header before the blank line
            return message[:boundary + 1] + trace_bytes + message[boundary + 1:]
