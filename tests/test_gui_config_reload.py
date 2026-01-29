"""Tests for GUI config change detection and reload logic.

These tests exercise _get_config_mtime, _check_reload_config, and the
mtime update in _run_edit_config without requiring GTK or AppIndicator3.
"""

import os
import time
from pathlib import Path
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from korgalore.gui import KorgaloreApp
from unittest.mock import MagicMock, patch

import click
import pytest


def _make_ctx(config: Dict[str, Any], cfgpath: Path) -> click.Context:
    """Build a minimal click.Context with the keys the GUI expects."""
    ctx = click.Context(click.Command('test'))
    ctx.obj = {
        'config': config,
        'cfgpath': cfgpath,
        'targets': {},
        'feeds': {},
        'deliveries': {},
    }
    return ctx


def _make_app(ctx: click.Context) -> 'KorgaloreApp':
    """Construct a KorgaloreApp without GTK by stubbing __init__."""
    from korgalore.gui import KorgaloreApp

    app = object.__new__(KorgaloreApp)
    app.ctx = ctx
    app.cfgpath = ctx.obj['cfgpath']
    config = ctx.obj.get('config', {})
    gui_config = config.get('gui', {})
    app.sync_interval = gui_config.get('sync_interval', 300)
    app._config_mtime = app._get_config_mtime()
    return app


class TestGetConfigMtime:
    """Tests for _get_config_mtime."""

    def test_returns_mtime_of_main_config(self, tmp_path: Path) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        ctx = _make_ctx({}, cfgpath)
        app = _make_app(ctx)

        mtime = app._get_config_mtime()
        assert mtime == pytest.approx(cfgpath.stat().st_mtime)

    def test_returns_newest_across_conf_d(self, tmp_path: Path) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()

        # Create two conf.d files with different mtimes
        f1 = conf_d / 'a.toml'
        f1.write_text('[targets]\n')
        # Bump the second file's mtime into the future
        f2 = conf_d / 'b.toml'
        f2.write_text('[feeds]\n')
        future = time.time() + 100
        os.utime(f2, (future, future))

        ctx = _make_ctx({}, cfgpath)
        app = _make_app(ctx)

        assert app._get_config_mtime() == pytest.approx(future)

    def test_detects_conf_d_directory_change(self, tmp_path: Path) -> None:
        """Adding a file to conf.d changes the directory mtime."""
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()

        ctx = _make_ctx({}, cfgpath)
        app = _make_app(ctx)
        mtime_before = app._get_config_mtime()

        # Ensure wall-clock advances so the new file has a later mtime
        future = time.time() + 100
        new_file = conf_d / 'new.toml'
        new_file.write_text('[targets]\n')
        os.utime(new_file, (future, future))
        os.utime(conf_d, (future, future))

        assert app._get_config_mtime() > mtime_before

    def test_missing_config_returns_zero(self, tmp_path: Path) -> None:
        cfgpath = tmp_path / 'does-not-exist.toml'
        ctx = _make_ctx({}, cfgpath)
        app = _make_app(ctx)

        assert app._get_config_mtime() == 0.0

    def test_missing_conf_d_no_error(self, tmp_path: Path) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        # No conf.d directory at all
        ctx = _make_ctx({}, cfgpath)
        app = _make_app(ctx)

        # Should return main file mtime without raising
        assert app._get_config_mtime() == pytest.approx(cfgpath.stat().st_mtime)

    def test_ignores_non_toml_in_conf_d(self, tmp_path: Path) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()

        # Non-toml file with a very high mtime should be ignored
        txt = conf_d / 'notes.txt'
        txt.write_text('hello')
        future = time.time() + 200
        os.utime(txt, (future, future))

        ctx = _make_ctx({}, cfgpath)
        app = _make_app(ctx)

        # mtime should not include the .txt file
        assert app._get_config_mtime() < future


class TestCheckReloadConfig:
    """Tests for _check_reload_config."""

    @patch('korgalore.gui.load_config')
    @patch('korgalore.gui.validate_config_file')
    def test_no_reload_when_unchanged(
        self, mock_validate: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        ctx = _make_ctx({'gui': {'sync_interval': 300}}, cfgpath)
        app = _make_app(ctx)

        app._check_reload_config()

        mock_validate.assert_not_called()
        mock_load.assert_not_called()

    @patch('korgalore.gui.load_config')
    @patch('korgalore.gui.validate_config_file', return_value=(True, ''))
    def test_reloads_when_mtime_changes(
        self, mock_validate: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        new_config = {'gui': {'sync_interval': 600}, 'targets': {}}
        mock_load.return_value = new_config

        ctx = _make_ctx({'gui': {'sync_interval': 300}}, cfgpath)
        app = _make_app(ctx)

        # Simulate external modification
        future = time.time() + 100
        os.utime(cfgpath, (future, future))

        app._check_reload_config()

        mock_validate.assert_called_once_with(cfgpath)
        mock_load.assert_called_once_with(cfgpath)
        assert ctx.obj['config'] is new_config
        assert ctx.obj['targets'] == {}
        assert ctx.obj['feeds'] == {}
        assert ctx.obj['deliveries'] == {}
        assert app.sync_interval == 600

    @patch('korgalore.gui.load_config')
    @patch('korgalore.gui.validate_config_file', return_value=(True, ''))
    def test_updates_stored_mtime_after_reload(
        self, mock_validate: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        mock_load.return_value = {'gui': {}}

        ctx = _make_ctx({}, cfgpath)
        app = _make_app(ctx)
        old_mtime = app._config_mtime

        future = time.time() + 100
        os.utime(cfgpath, (future, future))

        app._check_reload_config()

        assert app._config_mtime > old_mtime
        # Second call should not reload again
        mock_validate.reset_mock()
        app._check_reload_config()
        mock_validate.assert_not_called()

    @patch('korgalore.gui.load_config')
    @patch('korgalore.gui.validate_config_file',
           return_value=(False, 'syntax error'))
    def test_keeps_old_config_on_validation_failure(
        self, mock_validate: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        original_config = {'gui': {'sync_interval': 300}}

        ctx = _make_ctx(original_config, cfgpath)
        app = _make_app(ctx)

        future = time.time() + 100
        os.utime(cfgpath, (future, future))

        app._check_reload_config()

        mock_validate.assert_called_once()
        mock_load.assert_not_called()
        # Original config should be preserved
        assert ctx.obj['config'] is original_config

    @patch('korgalore.gui.load_config')
    @patch('korgalore.gui.validate_config_file',
           return_value=(False, 'syntax error'))
    def test_updates_mtime_on_validation_failure(
        self, mock_validate: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        """Mtime is updated even on failure to avoid retrying every cycle."""
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')

        ctx = _make_ctx({}, cfgpath)
        app = _make_app(ctx)

        future = time.time() + 100
        os.utime(cfgpath, (future, future))

        app._check_reload_config()

        assert app._config_mtime == pytest.approx(future)
        # Second call should not retry
        mock_validate.reset_mock()
        app._check_reload_config()
        mock_validate.assert_not_called()

    @patch('korgalore.gui.load_config')
    @patch('korgalore.gui.validate_config_file', return_value=(True, ''))
    def test_clears_cached_instances(
        self, mock_validate: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        mock_load.return_value = {'gui': {}}

        ctx = _make_ctx({}, cfgpath)
        ctx.obj['targets'] = {'t1': MagicMock()}
        ctx.obj['feeds'] = {'f1': MagicMock()}
        ctx.obj['deliveries'] = {'d1': MagicMock()}
        app = _make_app(ctx)

        future = time.time() + 100
        os.utime(cfgpath, (future, future))

        app._check_reload_config()

        assert ctx.obj['targets'] == {}
        assert ctx.obj['feeds'] == {}
        assert ctx.obj['deliveries'] == {}


class TestEditConfigMtimeUpdate:
    """Test that _run_edit_config updates _config_mtime after reload."""

    @patch('korgalore.gui.load_config')
    @patch('korgalore.gui.validate_config_file', return_value=(True, ''))
    @patch('subprocess.Popen')
    def test_edit_config_updates_mtime(
        self, mock_popen: MagicMock, mock_validate: MagicMock,
        mock_load: MagicMock, tmp_path: Path
    ) -> None:
        cfgpath = tmp_path / 'korgalore.toml'
        cfgpath.write_text('[main]\n')
        mock_load.return_value = {'gui': {'sync_interval': 300}}
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        ctx = _make_ctx({'gui': {}}, cfgpath)
        app = _make_app(ctx)

        # Patch get_xdg_config_dir so _run_edit_config uses our temp path
        with patch('korgalore.gui.get_xdg_config_dir', return_value=tmp_path):
            old_mtime = app._config_mtime
            # Bump file mtime so there is something newer to record
            future = time.time() + 100
            os.utime(cfgpath, (future, future))

            app._run_edit_config()

        assert app._config_mtime > old_mtime
        # Subsequent _check_reload_config should not trigger a reload
        with patch('korgalore.gui.validate_config_file') as v:
            app._check_reload_config()
            v.assert_not_called()
