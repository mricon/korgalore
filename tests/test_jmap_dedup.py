"""Test JMAP deduplication behavior with messages differing only in List-Id.

This test is designed to run against a real JMAP server (e.g., Fastmail) to verify
whether JMAP's alreadyExists detection is based on exact message content or
Message-ID header alone.

Run with: pytest tests/test_jmap_dedup.py -v -s

Requires environment variables or a config file with JMAP credentials.
Skip with: pytest tests/test_jmap_dedup.py -v -s -k "not integration"
"""

import os
import time
import uuid
from typing import Optional

import pytest

from korgalore.jmap_target import JmapTarget


def make_test_message(
    message_id: str,
    list_id: str,
    subject: str = "Test deduplication",
    from_addr: str = "test@example.com",
    to_addr: str = "recipient@example.com",
) -> bytes:
    """Create a minimal RFC 2822 email message.

    Args:
        message_id: Value for Message-ID header (without angle brackets)
        list_id: Value for List-Id header
        subject: Email subject
        from_addr: From address
        to_addr: To address

    Returns:
        Raw email bytes with Unix line endings (will be normalized by JmapTarget)
    """
    # Generate a unique date to avoid any date-based dedup
    date = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())

    message = f"""\
From: {from_addr}
To: {to_addr}
Subject: {subject}
Message-ID: <{message_id}>
Date: {date}
List-Id: {list_id}
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

This is a test message for JMAP deduplication testing.
"""
    return message.encode('utf-8')


def get_jmap_target_from_env() -> Optional[JmapTarget]:
    """Create JmapTarget from environment variables.

    Required environment variables:
        JMAP_SERVER: JMAP server URL (e.g., https://api.fastmail.com)
        JMAP_USERNAME: Account username/email
        JMAP_TOKEN: Bearer token

    Optional:
        JMAP_TEST_FOLDER: Folder to import to (default: Trash)

    Returns:
        JmapTarget instance or None if env vars not set
    """
    server = os.environ.get('JMAP_SERVER')
    username = os.environ.get('JMAP_USERNAME')
    token = os.environ.get('JMAP_TOKEN')

    if not all([server, username, token]):
        return None

    return JmapTarget(
        identifier="test-dedup",
        server=server,  # type: ignore[arg-type]
        username=username,  # type: ignore[arg-type]
        token=token,
    )


class TestJmapDeduplication:
    """Integration tests for JMAP message deduplication behavior.

    These tests verify whether JMAP servers deduplicate based on:
    - Exact message content (byte-for-byte match)
    - Message-ID header alone
    - Some combination of headers

    The tests import two messages with identical Message-ID but different
    List-Id headers to simulate the same message arriving via different
    mailing lists.
    """

    @pytest.fixture
    def jmap_target(self) -> JmapTarget:
        """Get JMAP target from environment, skip if not configured."""
        target = get_jmap_target_from_env()
        if target is None:
            pytest.skip(
                "JMAP credentials not configured. Set JMAP_SERVER, "
                "JMAP_USERNAME, and JMAP_TOKEN environment variables."
            )
        return target

    @pytest.fixture
    def test_folder(self) -> str:
        """Get test folder name from environment or use Trash."""
        return os.environ.get('JMAP_TEST_FOLDER', 'Trash')

    @pytest.mark.integration
    def test_same_msgid_different_listid(
        self, jmap_target: JmapTarget, test_folder: str
    ) -> None:
        """Test importing two messages with same Message-ID but different List-Id.

        This simulates the scenario where:
        1. A message is cross-posted to multiple mailing lists
        2. korgalore fetches from multiple list archives
        3. The same message (by Message-ID) arrives with different List-Id headers

        Expected behavior to determine:
        - If JMAP deduplicates by Message-ID: second import returns alreadyExists
        - If JMAP requires exact match: second import creates new message
        """
        # Connect to server
        jmap_target.connect()

        # Generate unique Message-ID for this test run
        unique_id = uuid.uuid4().hex[:12]
        message_id = f"test-dedup-{unique_id}@example.com"

        # Create two messages with same Message-ID but different List-Id
        msg1 = make_test_message(
            message_id=message_id,
            list_id="<linux-kernel.vger.kernel.org>",
            subject=f"[TEST] Dedup test {unique_id}",
        )

        msg2 = make_test_message(
            message_id=message_id,
            list_id="<linux-arm-kernel.lists.infradead.org>",
            subject=f"[TEST] Dedup test {unique_id}",
        )

        print(f"\n{'='*60}")
        print(f"Testing JMAP deduplication with Message-ID: <{message_id}>")
        print(f"Importing to folder: {test_folder}")
        print(f"{'='*60}")

        # Import first message
        print("\n--- Importing message 1 (List-Id: linux-kernel.vger.kernel.org) ---")
        result1 = jmap_target.import_message(msg1, [test_folder])
        print(f"Result 1: {result1}")

        # Small delay to ensure server processes first message
        time.sleep(1)

        # Import second message (same Message-ID, different List-Id)
        print("\n--- Importing message 2 (List-Id: linux-arm-kernel.lists.infradead.org) ---")
        result2 = jmap_target.import_message(msg2, [test_folder])
        print(f"Result 2: {result2}")

        # Analyze results
        print(f"\n{'='*60}")
        print("RESULTS:")
        print(f"{'='*60}")

        id1 = result1.get('id')
        id2 = result2.get('id')

        if id1 == id2:
            print(f"SAME ID returned: {id1}")
            print("=> JMAP detected duplicate (likely by Message-ID or content hash)")
            print("=> The alreadyExists mechanism works for this case")
        else:
            print("DIFFERENT IDs returned:")
            print(f"  Message 1: {id1}")
            print(f"  Message 2: {id2}")
            print("=> JMAP created two separate messages")
            print("=> Deduplication requires exact content match, not just Message-ID")
            print("=> Pre-import check by Message-ID header would be needed")

        # The test passes either way - we're just observing the behavior
        # In a real test suite, you might assert expected behavior:
        # assert id1 == id2, "Expected JMAP to deduplicate by Message-ID"

    @pytest.mark.integration
    def test_completely_identical_messages(
        self, jmap_target: JmapTarget, test_folder: str
    ) -> None:
        """Test importing two byte-identical messages.

        This is a control test to verify alreadyExists works for exact duplicates.
        """
        jmap_target.connect()

        unique_id = uuid.uuid4().hex[:12]
        message_id = f"test-identical-{unique_id}@example.com"

        # Create identical messages
        msg = make_test_message(
            message_id=message_id,
            list_id="<test-list.example.com>",
            subject=f"[TEST] Identical test {unique_id}",
        )

        print(f"\n{'='*60}")
        print(f"Testing with identical messages, Message-ID: <{message_id}>")
        print(f"{'='*60}")

        print("\n--- Importing message 1 ---")
        result1 = jmap_target.import_message(msg, [test_folder])
        print(f"Result 1: {result1}")

        time.sleep(1)

        print("\n--- Importing identical message 2 ---")
        result2 = jmap_target.import_message(msg, [test_folder])
        print(f"Result 2: {result2}")

        id1 = result1.get('id')
        id2 = result2.get('id')

        print(f"\n{'='*60}")
        if id1 == id2:
            print(f"SAME ID: {id1} - alreadyExists working for identical messages")
        else:
            print(f"DIFFERENT IDs: {id1} vs {id2} - unexpected for identical messages!")
        print(f"{'='*60}")

        # This should definitely deduplicate
        assert id1 == id2, "Expected identical messages to be deduplicated"


# Allow running as a script for quick testing
if __name__ == "__main__":
    target = get_jmap_target_from_env()
    if target is None:
        print("Error: Set JMAP_SERVER, JMAP_USERNAME, and JMAP_TOKEN environment variables")
        print("\nExample:")
        print("  export JMAP_SERVER=https://api.fastmail.com")
        print("  export JMAP_USERNAME=you@fastmail.com")
        print("  export JMAP_TOKEN=fmu1-xxxx-xxxx")
        print("  export JMAP_TEST_FOLDER=Trash")
        print("  python tests/test_jmap_dedup.py")
        exit(1)

    test = TestJmapDeduplication()
    folder = os.environ.get('JMAP_TEST_FOLDER', 'Trash')

    print("Running identical message test...")
    test.test_completely_identical_messages(target, folder)

    print("\n" + "="*60 + "\n")

    # Create fresh target to avoid state issues
    target = get_jmap_target_from_env()
    assert target is not None
    print("Running different List-Id test...")
    test.test_same_msgid_different_listid(target, folder)
