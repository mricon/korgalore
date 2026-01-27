"""Raw email message wrapper with lazy parsing and common operations."""

from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import EmailPolicy
from typing import Optional


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

    def as_bytes(self) -> bytes:
        """Return message as binary data suitable for delivery.

        Performs any necessary transformations for target delivery:
        - Normalizes line endings to CRLF as required by RFC 2822/5322
        - Future: may insert additional headers

        Git stores messages with Unix LF endings, but mail protocols require CRLF.

        Returns:
            Message bytes ready for delivery to a target.
        """
        return self._raw.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
