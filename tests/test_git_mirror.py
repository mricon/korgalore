"""Tests for git mirror failover via url.insteadOf."""

from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from korgalore import run_git_command
from korgalore.lore_feed import LoreFeed


class TestRunGitCommandConfig:
    """Tests for the git_config parameter in run_git_command."""

    def test_no_config(self) -> None:
        """Without git_config, no -c flags are added."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=b'output', stderr=b''
            )
            run_git_command(None, ['status'])
            cmd = mock_run.call_args[0][0]
            assert '-c' not in cmd
            assert cmd == ['git', 'status']

    def test_single_config(self) -> None:
        """-c key=value is inserted before --git-dir and subcommand."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=b'', stderr=b''
            )
            run_git_command('/some/dir', ['fetch', 'origin'],
                           git_config={'url.https://mirror/.insteadOf': 'https://canonical/'})
            cmd = mock_run.call_args[0][0]
            assert cmd == [
                'git',
                '-c', 'url.https://mirror/.insteadOf=https://canonical/',
                '--git-dir', '/some/dir',
                'fetch', 'origin',
            ]

    def test_multiple_configs(self) -> None:
        """Multiple config entries produce multiple -c flags."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=b'', stderr=b''
            )
            run_git_command(None, ['clone', 'url'],
                           git_config={'key1': 'val1', 'key2': 'val2'})
            cmd = mock_run.call_args[0][0]
            # Both -c flags should appear before the subcommand
            assert cmd[0] == 'git'
            c_indices = [i for i, v in enumerate(cmd) if v == '-c']
            assert len(c_indices) == 2
            clone_index = cmd.index('clone')
            for ci in c_indices:
                assert ci < clone_index

    def test_empty_config(self) -> None:
        """Empty git_config dict adds no -c flags."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=b'', stderr=b''
            )
            run_git_command(None, ['status'], git_config={})
            cmd = mock_run.call_args[0][0]
            assert '-c' not in cmd

    def test_none_config(self) -> None:
        """None git_config (default) adds no -c flags."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=b'', stderr=b''
            )
            run_git_command(None, ['status'], git_config=None)
            cmd = mock_run.call_args[0][0]
            assert '-c' not in cmd


class TestGitMirrorConfig:
    """Tests for LoreFeed._git_mirror_config()."""

    def _make_feed(self, origins: list[str], canonical: str) -> LoreFeed:
        """Create a LoreFeed with a mocked LoreNode."""
        mock_node = MagicMock()
        mock_node.origins = origins
        mock_node.canonical_origin = canonical
        feed = LoreFeed.__new__(LoreFeed)
        feed._node = mock_node
        return feed

    def test_preferred_differs_from_canonical(self) -> None:
        """Returns insteadOf config when fastest origin is a mirror."""
        feed = self._make_feed(
            origins=['https://tor.lore.kernel.org', 'https://lore.kernel.org'],
            canonical='https://lore.kernel.org',
        )
        config = feed._git_mirror_config()
        assert config == {
            'url.https://tor.lore.kernel.org/.insteadOf': 'https://lore.kernel.org/',
        }

    def test_preferred_is_canonical(self) -> None:
        """Returns empty dict when canonical is already the fastest."""
        feed = self._make_feed(
            origins=['https://lore.kernel.org', 'https://tor.lore.kernel.org'],
            canonical='https://lore.kernel.org',
        )
        config = feed._git_mirror_config()
        assert config == {}

    def test_single_origin(self) -> None:
        """Returns empty dict when there are no mirrors."""
        feed = self._make_feed(
            origins=['https://lore.kernel.org'],
            canonical='https://lore.kernel.org',
        )
        config = feed._git_mirror_config()
        assert config == {}

    def test_empty_origins(self) -> None:
        """Returns empty dict when origins list is empty."""
        feed = self._make_feed(origins=[], canonical='https://lore.kernel.org')
        config = feed._git_mirror_config()
        assert config == {}


class TestCloneEpochMirror:
    """Tests for mirror config being passed to clone_epoch git commands."""

    def test_clone_passes_mirror_config(self, tmp_path: Path) -> None:
        """clone_epoch passes _git_mirror_config to run_git_command."""
        mock_node = MagicMock()
        mock_node.origins = ['https://tor.lore.kernel.org', 'https://lore.kernel.org']
        mock_node.canonical_origin = 'https://lore.kernel.org'

        feed_dir = tmp_path / 'test-feed'
        feed_dir.mkdir()
        feed = LoreFeed('test', feed_dir, 'https://lore.kernel.org/lkml', lore_node=mock_node)

        with patch('korgalore.lore_feed.run_git_command', return_value=(0, b'', b'')) as mock_git:
            feed.clone_epoch(0)
            mock_git.assert_called_once()
            _, kwargs = mock_git.call_args
            assert kwargs['git_config'] == {
                'url.https://tor.lore.kernel.org/.insteadOf': 'https://lore.kernel.org/',
            }

    def test_clone_fallback_also_gets_mirror_config(self, tmp_path: Path) -> None:
        """When shallow clone fails and retries with --depth=1, mirror config is reused."""
        mock_node = MagicMock()
        mock_node.origins = ['https://sea.lore.kernel.org', 'https://lore.kernel.org']
        mock_node.canonical_origin = 'https://lore.kernel.org'

        feed_dir = tmp_path / 'test-feed'
        feed_dir.mkdir()
        feed = LoreFeed('test', feed_dir, 'https://lore.kernel.org/lkml', lore_node=mock_node)

        expected_config = {
            'url.https://sea.lore.kernel.org/.insteadOf': 'https://lore.kernel.org/',
        }

        with patch('korgalore.lore_feed.run_git_command') as mock_git:
            # First call (shallow) fails, second call (--depth=1) succeeds
            mock_git.side_effect = [(128, b'', b'shallow error'), (0, b'', b'')]
            feed.clone_epoch(0, shallow=True)
            assert mock_git.call_count == 2
            # Both calls should have the mirror config
            for c in mock_git.call_args_list:
                assert c[1]['git_config'] == expected_config

    def test_clone_no_mirror_when_canonical_is_fastest(self, tmp_path: Path) -> None:
        """No insteadOf config when canonical origin is already fastest."""
        mock_node = MagicMock()
        mock_node.origins = ['https://lore.kernel.org', 'https://tor.lore.kernel.org']
        mock_node.canonical_origin = 'https://lore.kernel.org'

        feed_dir = tmp_path / 'test-feed'
        feed_dir.mkdir()
        feed = LoreFeed('test', feed_dir, 'https://lore.kernel.org/lkml', lore_node=mock_node)

        with patch('korgalore.lore_feed.run_git_command', return_value=(0, b'', b'')) as mock_git:
            feed.clone_epoch(0)
            _, kwargs = mock_git.call_args
            assert kwargs['git_config'] == {}


class TestUpdateFeedMirror:
    """Tests for mirror config being passed to update_feed git fetch."""

    def test_fetch_passes_mirror_config(self, tmp_path: Path) -> None:
        """update_feed passes _git_mirror_config to the git fetch call."""
        mock_node = MagicMock()
        mock_node.origins = ['https://tor.lore.kernel.org', 'https://lore.kernel.org']
        mock_node.canonical_origin = 'https://lore.kernel.org'
        mock_node.request.return_value = MagicMock(status_code=200)

        feed_dir = tmp_path / 'test-feed'
        feed_dir.mkdir()
        (feed_dir / 'git' / '0.git').mkdir(parents=True)

        feed = LoreFeed('test', feed_dir, 'https://lore.kernel.org/lkml', lore_node=mock_node)

        # Set up minimal feed state so update_feed doesn't try to init
        feed_state = {
            'epochs': {'0': {'latest_commit': 'abc123'}},
            'last_update': '2026-01-01T00:00:00',
            'update_successful': True,
        }
        state_file = feed_dir / 'korgalore.feed'
        import json
        state_file.write_text(json.dumps(feed_state))

        # Epochs info matching remote (no new epochs, triggers fetch path)
        epochs_info = [{'epoch': 0, 'path': '/lkml/git/0.git', 'fpr': 'abc'}]
        (feed_dir / 'epochs.json').write_text(json.dumps(epochs_info))

        # Mock manifest to return same epochs (triggers fetch, not clone)
        import gzip
        import io
        manifest = {'/lkml/git/0.git': {'fingerprint': 'changed'}}
        manifest_bytes = gzip.compress(json.dumps(manifest).encode())
        mock_response = MagicMock()
        mock_response.content = manifest_bytes
        mock_response.raise_for_status = MagicMock()
        mock_node.request.return_value = mock_response

        expected_config = {
            'url.https://tor.lore.kernel.org/.insteadOf': 'https://lore.kernel.org/',
        }

        with patch('korgalore.lore_feed.run_git_command', return_value=(0, b'', b'')) as mock_git, \
             patch.object(feed, 'feed_updated', return_value=True), \
             patch.object(feed, 'save_feed_state'):
            feed.update_feed()

            # Find the fetch call (there may be others for local ops)
            fetch_calls = [c for c in mock_git.call_args_list
                           if len(c[0]) >= 2 and 'fetch' in c[0][1]]
            assert len(fetch_calls) == 1
            _, kwargs = fetch_calls[0]
            assert kwargs['git_config'] == expected_config
