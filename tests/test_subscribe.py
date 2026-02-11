"""Tests for the subscribe command group."""

import gzip
import json
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import click
import pytest

from korgalore import PublicInboxError, RemoteError
from korgalore.lei_feed import LeiFeed
from korgalore.lore_feed import LoreFeed
from korgalore.cli import (
    find_subscription_file,
    generate_subscription_config,
)


class TestValidatePublicInboxUrl:
    """Tests for LoreFeed.validate_public_inbox_url static method."""

    @staticmethod
    def _make_manifest_response(manifest_data: Dict[str, Any]) -> MagicMock:
        """Create a mock response with gzipped manifest JSON."""
        json_bytes = json.dumps(manifest_data).encode()
        compressed = gzip.compress(json_bytes)
        response = MagicMock()
        response.content = compressed
        response.raise_for_status = MagicMock()
        return response

    def test_success(self) -> None:
        """Valid manifest with consistent list prefix returns list name."""
        manifest = {
            '/lkml/git/0.git': {'fingerprint': 'abc123'},
            '/lkml/git/1.git': {'fingerprint': 'def456'},
        }
        response = self._make_manifest_response(manifest)
        session = MagicMock()
        session.get.return_value = response

        with patch('korgalore.lore_feed.get_requests_session', return_value=session):
            result = LoreFeed.validate_public_inbox_url(
                'https://lore.kernel.org/lkml/')

        assert result == 'lkml'
        session.get.assert_called_once_with(
            'https://lore.kernel.org/lkml/manifest.js.gz'
        )

    def test_non_lore_server(self) -> None:
        """Works with any public-inbox server, not just lore."""
        manifest = {
            '/mylist/git/0.git': {'fingerprint': 'abc'},
        }
        response = self._make_manifest_response(manifest)
        session = MagicMock()
        session.get.return_value = response

        with patch('korgalore.lore_feed.get_requests_session', return_value=session):
            result = LoreFeed.validate_public_inbox_url(
                'https://inbox.example.org/mylist/')

        assert result == 'mylist'

    def test_mixed_prefixes_raises_remote_error(self) -> None:
        """Manifest with inconsistent list prefixes raises RemoteError."""
        manifest = {
            '/lkml/git/0.git': {'fingerprint': 'abc'},
            '/other/git/0.git': {'fingerprint': 'def'},
        }
        response = self._make_manifest_response(manifest)
        session = MagicMock()
        session.get.return_value = response

        with patch('korgalore.lore_feed.get_requests_session', return_value=session):
            with pytest.raises(RemoteError, match='inconsistent list prefixes'):
                LoreFeed.validate_public_inbox_url(
                    'https://lore.kernel.org/lkml/')

    def test_fetch_failure_raises_remote_error(self) -> None:
        """Failed manifest fetch raises RemoteError."""
        session = MagicMock()
        session.get.side_effect = Exception('Connection refused')

        with patch('korgalore.lore_feed.get_requests_session', return_value=session):
            with pytest.raises(RemoteError, match='Failed to fetch manifest'):
                LoreFeed.validate_public_inbox_url(
                    'https://lore.kernel.org/lkml/')

    def test_empty_manifest_raises_remote_error(self) -> None:
        """Empty manifest raises RemoteError."""
        response = self._make_manifest_response({})
        session = MagicMock()
        session.get.return_value = response

        with patch('korgalore.lore_feed.get_requests_session', return_value=session):
            with pytest.raises(RemoteError, match='Empty manifest'):
                LoreFeed.validate_public_inbox_url(
                    'https://lore.kernel.org/lkml/')


class TestValidateLeiPath:
    """Tests for LeiFeed.validate_lei_path static method."""

    def test_success(self, tmp_path: Path) -> None:
        """Valid lei v2 search path returns the path."""
        lei_path = tmp_path / 'lei' / 'my-search'
        lei_path.mkdir(parents=True)

        ls_data = [
            {'output': f'v2:{lei_path}'},
            {'output': 'v2:/some/other/path'},
        ]
        output = json.dumps(ls_data).encode()

        with patch('korgalore.lei_feed.run_lei_command', return_value=(0, output)):
            result = LeiFeed.validate_lei_path(str(lei_path))

        assert result == str(lei_path)

    def test_not_found_raises_public_inbox_error(self, tmp_path: Path) -> None:
        """Unknown lei path raises PublicInboxError."""
        lei_path = tmp_path / 'lei' / 'nonexistent'

        ls_data = [
            {'output': 'v2:/some/other/path'},
        ]
        output = json.dumps(ls_data).encode()

        with patch('korgalore.lei_feed.run_lei_command', return_value=(0, output)):
            with pytest.raises(PublicInboxError, match='not found as a v2 lei search'):
                LeiFeed.validate_lei_path(str(lei_path))

    def test_lei_command_failure_raises_public_inbox_error(self) -> None:
        """Failed lei ls-search raises PublicInboxError."""
        with patch('korgalore.lei_feed.run_lei_command', return_value=(1, b'error')):
            with pytest.raises(PublicInboxError, match='LEI list searches failed'):
                LeiFeed.validate_lei_path('/some/path')


class TestGenerateSubscriptionConfig:
    """Tests for generate_subscription_config function."""

    def test_generates_valid_toml(self) -> None:
        """Generated config is valid TOML."""
        content = generate_subscription_config(
            feed_key='lkml',
            url='https://lore.kernel.org/lkml/',
            target='personal',
            labels=['INBOX', 'UNREAD'],
        )

        config = tomllib.loads(content)

        assert 'lkml' in config['feeds']
        assert config['feeds']['lkml']['url'] == 'https://lore.kernel.org/lkml/'
        assert 'lkml' in config['deliveries']
        assert config['deliveries']['lkml']['feed'] == 'lkml'
        assert config['deliveries']['lkml']['target'] == 'personal'
        assert config['deliveries']['lkml']['labels'] == ['INBOX', 'UNREAD']

    def test_lore_url_preserved(self) -> None:
        """Lore URL is stored as-is."""
        content = generate_subscription_config(
            feed_key='lkml',
            url='https://lore.kernel.org/lkml/',
            target='gmail',
            labels=['INBOX'],
        )

        config = tomllib.loads(content)
        assert config['feeds']['lkml']['url'] == 'https://lore.kernel.org/lkml/'

    def test_lei_path_gets_prefix(self) -> None:
        """Non-URL path gets lei: prefix."""
        content = generate_subscription_config(
            feed_key='my-search',
            url='/home/user/lei/my-search',
            target='maildir',
            labels=['INBOX'],
        )

        config = tomllib.loads(content)
        assert config['feeds']['my-search']['url'] == 'lei:/home/user/lei/my-search'

    def test_labels_formatted_correctly(self) -> None:
        """Labels are formatted as TOML array."""
        content = generate_subscription_config(
            feed_key='test',
            url='https://lore.kernel.org/test/',
            target='personal',
            labels=['INBOX', 'UNREAD', 'IMPORTANT'],
        )

        assert "labels = ['INBOX', 'UNREAD', 'IMPORTANT']" in content

    def test_includes_metadata_comments(self) -> None:
        """Config includes metadata comments."""
        content = generate_subscription_config(
            feed_key='lkml',
            url='https://lore.kernel.org/lkml/',
            target='personal',
            labels=['INBOX'],
        )

        assert "# Auto-generated by: kgl subscribe add" in content
        assert "# Generated:" in content


class TestFindSubscriptionFile:
    """Tests for find_subscription_file function."""

    def test_finds_active(self, tmp_path: Path) -> None:
        """Finds active subscription file."""
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()
        active = conf_d / 'sub-lkml.toml'
        active.write_text('[feeds.lkml]\n')

        result = find_subscription_file(conf_d, 'lkml')
        assert result == active

    def test_finds_paused(self, tmp_path: Path) -> None:
        """Finds paused subscription file."""
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()
        paused = conf_d / 'sub-lkml.toml.paused'
        paused.write_text('[feeds.lkml]\n')

        result = find_subscription_file(conf_d, 'lkml')
        assert result == paused

    def test_prefers_active_over_paused(self, tmp_path: Path) -> None:
        """Active file is returned when both exist."""
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()
        active = conf_d / 'sub-lkml.toml'
        active.write_text('[feeds.lkml]\n')
        paused = conf_d / 'sub-lkml.toml.paused'
        paused.write_text('[feeds.lkml]\n')

        result = find_subscription_file(conf_d, 'lkml')
        assert result == active

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        """Returns None when no subscription file exists."""
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()

        result = find_subscription_file(conf_d, 'nonexistent')
        assert result is None


class TestDefaultCommandGroup:
    """Tests for DefaultCommandGroup falling back to 'add'."""

    def test_url_without_add_subcommand(self) -> None:
        """subscribe <url> is treated as subscribe add <url>."""
        from korgalore.cli import DefaultCommandGroup

        group = DefaultCommandGroup(name='subscribe')

        @group.command('add')
        @click.argument('url')
        def add_cmd(url: str) -> None:
            pass

        @group.command('list')
        def list_cmd() -> None:
            pass

        # A known command resolves normally
        cmd_name, cmd, args = group.resolve_command(
            click.Context(group), ['list'])
        assert cmd_name == 'list'

        # An unknown token falls back to 'add'
        cmd_name, cmd, args = group.resolve_command(
            click.Context(group), ['https://lore.kernel.org/lkml/'])
        assert cmd_name == 'add'
        assert args == ['https://lore.kernel.org/lkml/']


class TestSubscribeAdd:
    """Tests for the subscribe add command."""

    def _make_context(self, tmp_path: Path, targets: Dict[str, Any],
                      feeds: Optional[Dict[str, Any]] = None,
                      deliveries: Optional[Dict[str, Any]] = None) -> click.Context:
        """Create a mock Click context for subscribe tests."""
        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('[targets]\n')

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['config'] = {
            'targets': targets,
            'feeds': feeds or {},
            'deliveries': deliveries or {},
        }
        ctx.obj['cfgpath'] = cfgpath
        ctx.obj['targets'] = {}
        ctx.obj['feeds'] = {}
        ctx.obj['deliveries'] = {}
        ctx.obj['data_dir'] = tmp_path / 'data'
        return ctx

    def test_add_lore(self, tmp_path: Path) -> None:
        """subscribe add creates conf.d file for lore URL."""
        from korgalore.cli import subscribe_add

        ctx = self._make_context(tmp_path, {'gmail': {'type': 'gmail'}})
        mock_target = MagicMock()
        mock_target.DEFAULT_LABELS = ['INBOX', 'UNREAD']

        with patch.object(LoreFeed, 'validate_public_inbox_url', return_value='lkml'), \
             patch('korgalore.cli.get_target', return_value=mock_target):
            ctx.invoke(subscribe_add, url='https://lore.kernel.org/lkml/',
                       target='gmail', labels=())

        conf_d = tmp_path / 'config' / 'conf.d'
        config_file = conf_d / 'sub-lkml.toml'
        assert config_file.exists()

        config = tomllib.loads(config_file.read_text())
        assert 'lkml' in config['feeds']
        assert config['deliveries']['lkml']['target'] == 'gmail'

    def test_add_lei(self, tmp_path: Path) -> None:
        """subscribe add creates conf.d file for lei path."""
        from korgalore.cli import subscribe_add

        lei_path = '/home/user/lei/my-search'
        ctx = self._make_context(tmp_path, {'maildir': {'type': 'maildir'}})
        mock_target = MagicMock()
        mock_target.DEFAULT_LABELS = ['INBOX']

        with patch.object(LeiFeed, 'validate_lei_path', return_value=lei_path), \
             patch('korgalore.cli.get_target', return_value=mock_target):
            ctx.invoke(subscribe_add, url=lei_path,
                       target='maildir', labels=())

        conf_d = tmp_path / 'config' / 'conf.d'
        # Lei paths use the directory basename as feed key
        config_file = conf_d / 'sub-my-search.toml'
        assert config_file.exists()

        config = tomllib.loads(config_file.read_text())
        assert 'my-search' in config['feeds']
        assert config['feeds']['my-search']['url'] == f'lei:{lei_path}'

    def test_add_duplicate_conf_d(self, tmp_path: Path) -> None:
        """subscribe add aborts on existing subscription file in conf.d."""
        from korgalore.cli import subscribe_add

        ctx = self._make_context(tmp_path, {'gmail': {'type': 'gmail'}})
        conf_d = tmp_path / 'config' / 'conf.d'
        conf_d.mkdir()
        (conf_d / 'sub-lkml.toml').write_text('[feeds.lkml]\n')

        with patch.object(LoreFeed, 'validate_public_inbox_url', return_value='lkml'):
            with pytest.raises(click.exceptions.Abort):
                ctx.invoke(subscribe_add, url='https://lore.kernel.org/lkml/',
                           target='gmail', labels=())

    def test_add_duplicate_feed_in_main_config(self, tmp_path: Path) -> None:
        """subscribe add aborts when feed key exists in main config."""
        from korgalore.cli import subscribe_add

        ctx = self._make_context(
            tmp_path, {'gmail': {'type': 'gmail'}},
            feeds={'lkml': {'url': 'https://lore.kernel.org/lkml/'}},
        )

        with patch.object(LoreFeed, 'validate_public_inbox_url', return_value='lkml'):
            with pytest.raises(click.exceptions.Abort):
                ctx.invoke(subscribe_add, url='https://lore.kernel.org/lkml/',
                           target='gmail', labels=())

    def test_add_duplicate_delivery_in_main_config(self, tmp_path: Path) -> None:
        """subscribe add aborts when delivery key exists in main config."""
        from korgalore.cli import subscribe_add

        ctx = self._make_context(
            tmp_path, {'fastmail': {'type': 'fastmail'}},
            deliveries={'kernelnewbies': {
                'feed': 'https://lore.kernel.org/kernelnewbies',
                'target': 'fastmail',
                'labels': ['kernelnewbies'],
            }},
        )

        with patch.object(LoreFeed, 'validate_public_inbox_url', return_value='kernelnewbies'):
            with pytest.raises(click.exceptions.Abort):
                ctx.invoke(subscribe_add, url='https://lore.kernel.org/kernelnewbies/',
                           target='fastmail', labels=())

    def test_add_auto_selects_single_target(self, tmp_path: Path) -> None:
        """subscribe add auto-selects target when only one configured."""
        from korgalore.cli import subscribe_add

        ctx = self._make_context(tmp_path, {'only-target': {'type': 'gmail'}})
        mock_target = MagicMock()
        mock_target.DEFAULT_LABELS = ['INBOX']

        with patch.object(LoreFeed, 'validate_public_inbox_url', return_value='lkml'), \
             patch('korgalore.cli.get_target', return_value=mock_target):
            ctx.invoke(subscribe_add, url='https://lore.kernel.org/lkml/',
                       target=None, labels=())

        conf_d = tmp_path / 'config' / 'conf.d'
        config_file = conf_d / 'sub-lkml.toml'
        assert config_file.exists()
        config = tomllib.loads(config_file.read_text())
        assert config['deliveries']['lkml']['target'] == 'only-target'

    def test_add_custom_labels(self, tmp_path: Path) -> None:
        """subscribe add uses custom labels when provided."""
        from korgalore.cli import subscribe_add

        ctx = self._make_context(tmp_path, {'gmail': {'type': 'gmail'}})
        mock_target = MagicMock()
        mock_target.DEFAULT_LABELS = ['INBOX', 'UNREAD']

        with patch.object(LoreFeed, 'validate_public_inbox_url', return_value='lkml'), \
             patch('korgalore.cli.get_target', return_value=mock_target):
            ctx.invoke(subscribe_add, url='https://lore.kernel.org/lkml/',
                       target='gmail', labels=('CUSTOM', 'LABELS'))

        conf_d = tmp_path / 'config' / 'conf.d'
        config_file = conf_d / 'sub-lkml.toml'
        config = tomllib.loads(config_file.read_text())
        assert config['deliveries']['lkml']['labels'] == ['CUSTOM', 'LABELS']


class TestSubscribeList:
    """Tests for the subscribe list command."""

    def test_list_active_and_paused(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """subscribe list shows both active and paused subscriptions."""
        from korgalore.cli import subscribe_list

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')

        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()

        # Active subscription
        active_content = generate_subscription_config(
            'lkml', 'https://lore.kernel.org/lkml/', 'gmail', ['INBOX']
        )
        (conf_d / 'sub-lkml.toml').write_text(active_content)

        # Paused subscription
        paused_content = generate_subscription_config(
            'netdev', 'https://lore.kernel.org/netdev/', 'gmail', ['INBOX']
        )
        (conf_d / 'sub-netdev.toml.paused').write_text(paused_content)

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath

        ctx.invoke(subscribe_list, paused=False)
        # No assertion on output since it goes through logger, but no exception is good

    def test_list_paused_only(self, tmp_path: Path) -> None:
        """subscribe list --paused shows only paused subscriptions."""
        from korgalore.cli import subscribe_list

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')

        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()

        active_content = generate_subscription_config(
            'lkml', 'https://lore.kernel.org/lkml/', 'gmail', ['INBOX']
        )
        (conf_d / 'sub-lkml.toml').write_text(active_content)

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath

        # Should not raise, and with --paused should report no paused subs
        ctx.invoke(subscribe_list, paused=True)

    def test_list_empty(self, tmp_path: Path) -> None:
        """subscribe list with no subscriptions reports empty."""
        from korgalore.cli import subscribe_list

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath

        # No conf.d directory at all
        ctx.invoke(subscribe_list, paused=False)


class TestSubscribePauseResume:
    """Tests for subscribe pause and resume commands."""

    def test_pause(self, tmp_path: Path) -> None:
        """subscribe pause renames .toml to .toml.paused."""
        from korgalore.cli import subscribe_pause

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')
        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()

        active = conf_d / 'sub-lkml.toml'
        active.write_text('[feeds.lkml]\n')

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath

        ctx.invoke(subscribe_pause, feed_key='lkml')

        assert not active.exists()
        assert (conf_d / 'sub-lkml.toml.paused').exists()

    def test_pause_already_paused(self, tmp_path: Path) -> None:
        """subscribe pause on already-paused subscription warns."""
        from korgalore.cli import subscribe_pause

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')
        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()

        paused = conf_d / 'sub-lkml.toml.paused'
        paused.write_text('[feeds.lkml]\n')

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath

        # Should not raise, just warn
        ctx.invoke(subscribe_pause, feed_key='lkml')
        assert paused.exists()

    def test_resume(self, tmp_path: Path) -> None:
        """subscribe resume renames .toml.paused to .toml."""
        from korgalore.cli import subscribe_resume

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')
        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()

        paused = conf_d / 'sub-lkml.toml.paused'
        paused.write_text('[feeds.lkml]\n')

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath
        ctx.obj['data_dir'] = tmp_path / 'data'

        ctx.invoke(subscribe_resume, feed_key='lkml', skip=False)

        assert not paused.exists()
        assert (conf_d / 'sub-lkml.toml').exists()

    def test_resume_already_active(self, tmp_path: Path) -> None:
        """subscribe resume on already-active subscription warns."""
        from korgalore.cli import subscribe_resume

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')
        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()

        active = conf_d / 'sub-lkml.toml'
        active.write_text('[feeds.lkml]\n')

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath
        ctx.obj['data_dir'] = tmp_path / 'data'

        # Should not raise, just warn
        ctx.invoke(subscribe_resume, feed_key='lkml', skip=False)
        assert active.exists()

    def test_resume_skip(self, tmp_path: Path) -> None:
        """subscribe resume --skip deletes delivery info files."""
        from korgalore.cli import subscribe_resume

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')
        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()

        paused = conf_d / 'sub-lkml.toml.paused'
        paused.write_text('[feeds.lkml]\n')

        # Create feed data directory with delivery info files
        data_dir = tmp_path / 'data'
        feed_dir = data_dir / 'lkml'
        feed_dir.mkdir(parents=True)
        info1 = feed_dir / 'korgalore.lkml.info'
        info1.write_text('{"epochs": {"0": {"last": "abc"}}}')
        info2 = feed_dir / 'korgalore.other-delivery.info'
        info2.write_text('{"epochs": {"0": {"last": "def"}}}')
        # This file should NOT be deleted (not an info file)
        feed_state = feed_dir / 'korgalore.feed'
        feed_state.write_text('{"epochs": {}}')

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath
        ctx.obj['data_dir'] = data_dir

        ctx.invoke(subscribe_resume, feed_key='lkml', skip=True)

        assert not paused.exists()
        assert (conf_d / 'sub-lkml.toml').exists()
        # Delivery info files should be deleted
        assert not info1.exists()
        assert not info2.exists()
        # Feed state should be preserved
        assert feed_state.exists()

    def test_resume_skip_epoch_rollover(self, tmp_path: Path) -> None:
        """subscribe resume --skip works when new epochs appeared during pause.

        The --skip flag deletes delivery info files. On the next pull,
        update_all_feeds() clones any new epochs first, then
        load_delivery_info() auto-creates state from the current
        highest epoch tip. This test verifies that delivery info files
        are indeed removed so re-creation can happen.
        """
        from korgalore.cli import subscribe_resume

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')
        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()

        paused = conf_d / 'sub-lkml.toml.paused'
        paused.write_text('[feeds.lkml]\n')

        # Simulate feed data with old delivery info referencing epoch 0
        data_dir = tmp_path / 'data'
        feed_dir = data_dir / 'lkml'
        feed_dir.mkdir(parents=True)
        old_info = feed_dir / 'korgalore.lkml.info'
        old_info.write_text('{"epochs": {"0": {"last": "oldcommit"}}}')

        # Simulate epoch directories (epoch 0 and a new epoch 1 that
        # appeared while paused)
        git_dir = feed_dir / 'git'
        (git_dir / '0.git').mkdir(parents=True)
        (git_dir / '1.git').mkdir(parents=True)

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath
        ctx.obj['data_dir'] = data_dir

        ctx.invoke(subscribe_resume, feed_key='lkml', skip=True)

        # Delivery info deleted so it can be re-created from latest epoch tip
        assert not old_info.exists()
        # Epoch directories should still exist
        assert (git_dir / '0.git').exists()
        assert (git_dir / '1.git').exists()


class TestSubscribeStop:
    """Tests for the subscribe stop command."""

    def test_stop(self, tmp_path: Path) -> None:
        """subscribe stop removes the config file."""
        from korgalore.cli import subscribe_stop

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')
        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()

        sub_file = conf_d / 'sub-lkml.toml'
        sub_file.write_text('[feeds.lkml]\n')

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath
        ctx.obj['data_dir'] = tmp_path / 'data'

        ctx.invoke(subscribe_stop, feed_key='lkml', delete=False)

        assert not sub_file.exists()

    def test_stop_paused(self, tmp_path: Path) -> None:
        """subscribe stop works on paused subscriptions."""
        from korgalore.cli import subscribe_stop

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')
        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()

        sub_file = conf_d / 'sub-lkml.toml.paused'
        sub_file.write_text('[feeds.lkml]\n')

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath
        ctx.obj['data_dir'] = tmp_path / 'data'

        ctx.invoke(subscribe_stop, feed_key='lkml', delete=False)

        assert not sub_file.exists()

    def test_stop_not_found(self, tmp_path: Path) -> None:
        """subscribe stop aborts when subscription not found."""
        from korgalore.cli import subscribe_stop

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')
        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath
        ctx.obj['data_dir'] = tmp_path / 'data'

        with pytest.raises(click.exceptions.Abort):
            ctx.invoke(subscribe_stop, feed_key='nonexistent', delete=False)

    def test_stop_delete(self, tmp_path: Path) -> None:
        """subscribe stop --delete removes config file and feed data."""
        from korgalore.cli import subscribe_stop

        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        cfgpath = config_dir / 'korgalore.toml'
        cfgpath.write_text('')
        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()

        sub_file = conf_d / 'sub-lkml.toml'
        sub_file.write_text('[feeds.lkml]\n')

        # Create feed data directory
        data_dir = tmp_path / 'data'
        feed_dir = data_dir / 'lkml'
        feed_dir.mkdir(parents=True)
        (feed_dir / 'korgalore.feed').write_text('{}')
        (feed_dir / 'korgalore.lkml.info').write_text('{}')
        git_dir = feed_dir / 'git' / '0.git'
        git_dir.mkdir(parents=True)

        ctx = click.Context(click.Command('test'))
        ctx.ensure_object(dict)
        ctx.obj['cfgpath'] = cfgpath
        ctx.obj['data_dir'] = data_dir

        ctx.invoke(subscribe_stop, feed_key='lkml', delete=True)

        assert not sub_file.exists()
        assert not feed_dir.exists()
