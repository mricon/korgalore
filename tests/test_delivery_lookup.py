"""Tests for delivery lookup logic.

These tests verify the algorithm that maps updated feeds to deliveries.
The current implementation in cli.py uses a nested loop which we want to
optimize to use a reverse index for O(1) lookups.
"""

from typing import Dict, List, Tuple, Any
from unittest.mock import MagicMock


def find_deliveries_for_updated_feeds_original(
    deliveries: Dict[str, Tuple[Any, Any, List[str]]], updated_feeds: List[str]
) -> List[str]:
    """Original O(n*m) implementation from cli.py:749-754.

    This is extracted for testing to ensure optimization preserves behavior.
    """
    run_deliveries: List[str] = []
    for feed_key in updated_feeds:
        for dname in deliveries.keys():
            feed = deliveries[dname][0]
            if feed.feed_key == feed_key:
                run_deliveries.append(dname)
    return run_deliveries


def find_deliveries_for_updated_feeds_optimized(
    deliveries: Dict[str, Tuple[Any, Any, List[str]]], updated_feeds: List[str]
) -> List[str]:
    """Optimized O(m + n) implementation using reverse index.

    Build feed_key -> delivery_names mapping once, then O(1) lookup per feed.
    """
    # Build reverse index: feed_key -> delivery names
    feed_to_deliveries: Dict[str, List[str]] = {}
    for dname, (feed, _, _) in deliveries.items():
        feed_to_deliveries.setdefault(feed.feed_key, []).append(dname)

    # O(1) lookup per updated feed
    run_deliveries: List[str] = []
    for feed_key in updated_feeds:
        run_deliveries.extend(feed_to_deliveries.get(feed_key, []))
    return run_deliveries


def create_mock_deliveries(num_deliveries: int, num_feeds: int) -> Dict[str, Tuple[Any, Any, List[str]]]:
    """Create mock delivery data for testing.

    Args:
        num_deliveries: Total number of deliveries to create
        num_feeds: Number of unique feeds (deliveries distributed round-robin)
    """
    deliveries = {}
    for i in range(num_deliveries):
        feed = MagicMock()
        feed.feed_key = f"feed-{i % num_feeds}"
        target = MagicMock()
        target.identifier = f"target-{i}"
        deliveries[f"delivery-{i}"] = (feed, target, [f"label-{i}"])
    return deliveries


class TestDeliveryLookup:
    """Tests ensuring original and optimized implementations match."""

    def test_empty_deliveries(self) -> None:
        """Both return empty list for empty deliveries."""
        deliveries: Dict[str, Tuple[Any, Any, List[str]]] = {}
        updated_feeds = ["feed-0", "feed-1"]

        original = find_deliveries_for_updated_feeds_original(deliveries, updated_feeds)
        optimized = find_deliveries_for_updated_feeds_optimized(deliveries, updated_feeds)

        assert original == []
        assert optimized == []

    def test_empty_updated_feeds(self) -> None:
        """Both return empty list when no feeds updated."""
        deliveries = create_mock_deliveries(10, 3)
        updated_feeds: List[str] = []

        original = find_deliveries_for_updated_feeds_original(deliveries, updated_feeds)
        optimized = find_deliveries_for_updated_feeds_optimized(deliveries, updated_feeds)

        assert original == []
        assert optimized == []

    def test_single_feed_single_delivery(self) -> None:
        """Single feed with single delivery."""
        deliveries = create_mock_deliveries(1, 1)
        updated_feeds = ["feed-0"]

        original = find_deliveries_for_updated_feeds_original(deliveries, updated_feeds)
        optimized = find_deliveries_for_updated_feeds_optimized(deliveries, updated_feeds)

        assert original == ["delivery-0"]
        assert optimized == ["delivery-0"]

    def test_multiple_deliveries_per_feed(self) -> None:
        """Multiple deliveries for same feed."""
        deliveries = create_mock_deliveries(6, 2)  # 3 deliveries per feed
        updated_feeds = ["feed-0"]

        original = find_deliveries_for_updated_feeds_original(deliveries, updated_feeds)
        optimized = find_deliveries_for_updated_feeds_optimized(deliveries, updated_feeds)

        # feed-0 has delivery-0, delivery-2, delivery-4
        assert sorted(original) == ["delivery-0", "delivery-2", "delivery-4"]
        assert sorted(optimized) == ["delivery-0", "delivery-2", "delivery-4"]

    def test_multiple_feeds_updated(self) -> None:
        """Multiple feeds updated at once."""
        deliveries = create_mock_deliveries(9, 3)  # 3 deliveries per feed
        updated_feeds = ["feed-0", "feed-2"]

        original = find_deliveries_for_updated_feeds_original(deliveries, updated_feeds)
        optimized = find_deliveries_for_updated_feeds_optimized(deliveries, updated_feeds)

        # feed-0: delivery-0, delivery-3, delivery-6
        # feed-2: delivery-2, delivery-5, delivery-8
        expected = ["delivery-0", "delivery-2", "delivery-3", "delivery-5", "delivery-6", "delivery-8"]
        assert sorted(original) == expected
        assert sorted(optimized) == expected

    def test_nonexistent_feed_updated(self) -> None:
        """Updated feed that has no deliveries."""
        deliveries = create_mock_deliveries(3, 3)
        updated_feeds = ["feed-nonexistent"]

        original = find_deliveries_for_updated_feeds_original(deliveries, updated_feeds)
        optimized = find_deliveries_for_updated_feeds_optimized(deliveries, updated_feeds)

        assert original == []
        assert optimized == []

    def test_mixed_existing_and_nonexistent(self) -> None:
        """Mix of existing and nonexistent feeds."""
        deliveries = create_mock_deliveries(6, 3)
        updated_feeds = ["feed-0", "feed-nonexistent", "feed-2"]

        original = find_deliveries_for_updated_feeds_original(deliveries, updated_feeds)
        optimized = find_deliveries_for_updated_feeds_optimized(deliveries, updated_feeds)

        expected = ["delivery-0", "delivery-2", "delivery-3", "delivery-5"]
        assert sorted(original) == expected
        assert sorted(optimized) == expected

    def test_all_feeds_updated(self) -> None:
        """All feeds updated returns all deliveries."""
        deliveries = create_mock_deliveries(10, 5)
        updated_feeds = [f"feed-{i}" for i in range(5)]

        original = find_deliveries_for_updated_feeds_original(deliveries, updated_feeds)
        optimized = find_deliveries_for_updated_feeds_optimized(deliveries, updated_feeds)

        expected = [f"delivery-{i}" for i in range(10)]
        assert sorted(original) == expected
        assert sorted(optimized) == expected

    def test_duplicate_feed_in_updated(self) -> None:
        """Duplicate feed keys in updated_feeds list."""
        deliveries = create_mock_deliveries(3, 3)
        updated_feeds = ["feed-0", "feed-0", "feed-1"]

        original = find_deliveries_for_updated_feeds_original(deliveries, updated_feeds)
        optimized = find_deliveries_for_updated_feeds_optimized(deliveries, updated_feeds)

        # Original adds delivery-0 twice, optimized does too (same behavior)
        assert original == ["delivery-0", "delivery-0", "delivery-1"]
        assert optimized == ["delivery-0", "delivery-0", "delivery-1"]

    def test_large_scale(self) -> None:
        """Verify correctness with larger dataset."""
        deliveries = create_mock_deliveries(1000, 50)
        updated_feeds = [f"feed-{i}" for i in range(0, 50, 2)]  # Every other feed

        original = find_deliveries_for_updated_feeds_original(deliveries, updated_feeds)
        optimized = find_deliveries_for_updated_feeds_optimized(deliveries, updated_feeds)

        assert sorted(original) == sorted(optimized)
        assert len(original) == 500  # Half the deliveries


class TestDeliveryLookupPreservesOrder:
    """Tests verifying order characteristics (not strict equality due to dict ordering)."""

    def test_order_by_feed_update_sequence(self) -> None:
        """Results are grouped by the order feeds appear in updated_feeds."""
        deliveries = create_mock_deliveries(6, 3)
        updated_feeds = ["feed-2", "feed-0"]  # Reverse order

        original = find_deliveries_for_updated_feeds_original(deliveries, updated_feeds)
        optimized = find_deliveries_for_updated_feeds_optimized(deliveries, updated_feeds)

        # Both should have feed-2 deliveries before feed-0 deliveries
        # feed-2: delivery-2, delivery-5
        # feed-0: delivery-0, delivery-3
        # Check that all feed-2 deliveries come before feed-0 deliveries
        def get_feed_indices(results: List[str]) -> Dict[str, List[int]]:
            indices: Dict[str, List[int]] = {"feed-0": [], "feed-2": []}
            for i, d in enumerate(results):
                num = int(d.split("-")[1])
                feed_key = f"feed-{num % 3}"
                if feed_key in indices:
                    indices[feed_key].append(i)
            return indices

        orig_indices = get_feed_indices(original)
        opt_indices = get_feed_indices(optimized)

        # All feed-2 indices should be less than all feed-0 indices
        assert max(orig_indices["feed-2"]) < min(orig_indices["feed-0"])
        assert max(opt_indices["feed-2"]) < min(opt_indices["feed-0"])
