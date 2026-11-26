"""Helpers shared by the delivery target tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

# The Date is two minutes before the Received stamp, so a target that ends
# up with SENT_AT took its date from Date, and RECEIVED_AT from Received.
DATED_MESSAGE = (
    b'Received: from mail.example.com by lore.example.org; Mon, 2 Nov 2026 10:00:00 +0000\n'
    b'Date: Mon, 2 Nov 2026 11:58:00 +0200\n'
    b'Message-ID: <dated@example.com>\n'
    b'\n'
    b'Body\n'
)
SENT_AT = datetime(2026, 11, 2, 9, 58, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
UNDATED_MESSAGE = b'Message-ID: <undated@example.com>\n\nBody\n'


def valid_token_file(
    tmp_path: Path,
    access_token: str = 'valid',
    refresh_token: str = 'refresh',
    expires_in: float = 3600,
) -> Path:
    """Write an OAuth2 token file that expires `expires_in` seconds from now.

    A negative `expires_in` gives an already expired token.
    """
    token_file = tmp_path / 'token.json'
    token_file.write_text(
        json.dumps(
            {
                'access_token': access_token,
                'refresh_token': refresh_token,
                'expires_at': datetime.now(UTC).timestamp() + expires_in,
            }
        )
    )
    return token_file
