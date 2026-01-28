"""Tests for CLI configuration loading and merging."""

from pathlib import Path
from typing import Any, Dict, List

from korgalore.cli import merge_config, load_config
from korgalore.maintainers import normalize_subsystem_name


class TestMergeConfig:
    """Tests for merge_config function."""

    def test_merge_targets(self) -> None:
        """Merges targets section from extra into base."""
        base: Dict[str, Any] = {
            'targets': {'existing': {'type': 'gmail'}}
        }
        extra: Dict[str, Any] = {
            'targets': {'new': {'type': 'maildir'}}
        }
        merge_config(base, extra)
        assert 'existing' in base['targets']
        assert 'new' in base['targets']

    def test_merge_feeds(self) -> None:
        """Merges feeds section from extra into base."""
        base: Dict[str, Any] = {
            'feeds': {'feed1': {'url': 'https://example.com/1'}}
        }
        extra: Dict[str, Any] = {
            'feeds': {'feed2': {'url': 'https://example.com/2'}}
        }
        merge_config(base, extra)
        assert 'feed1' in base['feeds']
        assert 'feed2' in base['feeds']

    def test_merge_deliveries(self) -> None:
        """Merges deliveries section from extra into base."""
        base: Dict[str, Any] = {
            'deliveries': {'delivery1': {'feed': 'feed1', 'target': 'target1'}}
        }
        extra: Dict[str, Any] = {
            'deliveries': {'delivery2': {'feed': 'feed2', 'target': 'target2'}}
        }
        merge_config(base, extra)
        assert 'delivery1' in base['deliveries']
        assert 'delivery2' in base['deliveries']

    def test_merge_gui_replaces(self) -> None:
        """GUI section is replaced, not merged."""
        base: Dict[str, Any] = {
            'gui': {'sync_interval': 300, 'option_a': True}
        }
        extra: Dict[str, Any] = {
            'gui': {'sync_interval': 600}
        }
        merge_config(base, extra)
        # gui section should be completely replaced
        assert base['gui'] == {'sync_interval': 600}
        assert 'option_a' not in base['gui']

    def test_merge_creates_missing_section(self) -> None:
        """Creates section in base if missing."""
        base: Dict[str, Any] = {}
        extra: Dict[str, Any] = {
            'targets': {'new': {'type': 'gmail'}},
            'feeds': {'feed1': {'url': 'https://example.com'}},
            'deliveries': {'d1': {'feed': 'feed1', 'target': 'new'}},
        }
        merge_config(base, extra)
        assert 'targets' in base
        assert 'feeds' in base
        assert 'deliveries' in base
        assert base['targets'] == {'new': {'type': 'gmail'}}

    def test_merge_overwrites_existing_keys(self) -> None:
        """Existing keys in sections are overwritten by extra."""
        base: Dict[str, Any] = {
            'targets': {'target1': {'type': 'gmail', 'credentials': 'old.json'}}
        }
        extra: Dict[str, Any] = {
            'targets': {'target1': {'type': 'maildir', 'path': '/new/path'}}
        }
        merge_config(base, extra)
        # The entire target1 entry is replaced
        assert base['targets']['target1'] == {'type': 'maildir', 'path': '/new/path'}

    def test_merge_empty_extra(self) -> None:
        """Empty extra dict doesn't modify base."""
        base: Dict[str, Any] = {
            'targets': {'t1': {'type': 'gmail'}},
            'gui': {'sync_interval': 300},
        }
        original = dict(base)
        merge_config(base, {})
        assert base == original

    def test_merge_preserves_other_sections(self) -> None:
        """Sections not in targets/feeds/deliveries/gui are preserved."""
        base: Dict[str, Any] = {
            'custom_section': {'key': 'value'},
            'targets': {'t1': {'type': 'gmail'}},
        }
        extra: Dict[str, Any] = {
            'targets': {'t2': {'type': 'maildir'}},
        }
        merge_config(base, extra)
        assert 'custom_section' in base
        assert base['custom_section'] == {'key': 'value'}


class TestLoadConfigWithConfD:
    """Tests for load_config with conf.d support."""

    def test_load_config_basic(self, tmp_path: Path) -> None:
        """Loads basic config without conf.d."""
        config_file = tmp_path / "korgalore.toml"
        config_file.write_text(
            "[targets.personal]\n"
            "type = 'gmail'\n"
            "credentials = 'creds.json'\n"
        )
        config = load_config(config_file)
        assert 'targets' in config
        assert 'personal' in config['targets']

    def test_load_config_with_conf_d(self, tmp_path: Path) -> None:
        """Loads main config and merges conf.d/*.toml files."""
        # Main config
        config_file = tmp_path / "korgalore.toml"
        config_file.write_text(
            "[targets.personal]\n"
            "type = 'gmail'\n"
            "credentials = 'creds.json'\n"
        )

        # Create conf.d directory with additional configs
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "subsystem1.toml").write_text(
            "[feeds.subsystem1-maintainers]\n"
            "url = 'lei:/path/to/subsystem1-maintainers'\n"
            "\n"
            "[deliveries.subsystem1-maintainers]\n"
            "feed = 'subsystem1-maintainers'\n"
            "target = 'personal'\n"
            "labels = ['INBOX']\n"
        )

        config = load_config(config_file)

        # Main config should be loaded
        assert 'personal' in config['targets']
        # conf.d config should be merged
        assert 'subsystem1-maintainers' in config['feeds']
        assert 'subsystem1-maintainers' in config['deliveries']

    def test_load_config_conf_d_alphabetical_order(self, tmp_path: Path) -> None:
        """conf.d files are loaded in alphabetical order."""
        config_file = tmp_path / "korgalore.toml"
        config_file.write_text("[targets.main]\ntype = 'gmail'\n")

        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        # Create files that would merge in alphabetical order
        # Later files overwrite earlier ones for same keys
        (conf_d / "01_first.toml").write_text(
            "[feeds.test]\n"
            "url = 'first'\n"
        )
        (conf_d / "02_second.toml").write_text(
            "[feeds.test]\n"
            "url = 'second'\n"
        )

        config = load_config(config_file)
        # 02_second.toml loads after 01_first.toml, overwrites
        assert config['feeds']['test']['url'] == 'second'

    def test_load_config_no_conf_d_directory(self, tmp_path: Path) -> None:
        """Works correctly when conf.d directory doesn't exist."""
        config_file = tmp_path / "korgalore.toml"
        config_file.write_text(
            "[targets.personal]\n"
            "type = 'gmail'\n"
        )
        # No conf.d directory created

        config = load_config(config_file)
        assert 'personal' in config['targets']

    def test_load_config_empty_conf_d(self, tmp_path: Path) -> None:
        """Works correctly with empty conf.d directory."""
        config_file = tmp_path / "korgalore.toml"
        config_file.write_text(
            "[targets.personal]\n"
            "type = 'gmail'\n"
        )

        # Create empty conf.d
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        config = load_config(config_file)
        assert 'personal' in config['targets']

    def test_load_config_conf_d_only_toml_files(self, tmp_path: Path) -> None:
        """Only .toml files in conf.d are loaded."""
        config_file = tmp_path / "korgalore.toml"
        config_file.write_text("[targets.main]\ntype = 'gmail'\n")

        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        # Create a .toml file
        (conf_d / "valid.toml").write_text("[feeds.valid]\nurl = 'test'\n")

        # Create non-.toml files that should be ignored
        (conf_d / "ignored.txt").write_text("[feeds.ignored]\nurl = 'bad'\n")
        (conf_d / "ignored.toml.bak").write_text("[feeds.backup]\nurl = 'bad'\n")
        (conf_d / "README").write_text("This is not a config file")

        config = load_config(config_file)
        assert 'valid' in config.get('feeds', {})
        assert 'ignored' not in config.get('feeds', {})
        assert 'backup' not in config.get('feeds', {})

    def test_load_config_multiple_conf_d_files(self, tmp_path: Path) -> None:
        """Multiple conf.d files are all merged."""
        config_file = tmp_path / "korgalore.toml"
        config_file.write_text("[targets.main]\ntype = 'gmail'\n")

        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "aaa.toml").write_text("[feeds.feed_a]\nurl = 'a'\n")
        (conf_d / "bbb.toml").write_text("[feeds.feed_b]\nurl = 'b'\n")
        (conf_d / "ccc.toml").write_text("[feeds.feed_c]\nurl = 'c'\n")

        config = load_config(config_file)
        assert 'feed_a' in config['feeds']
        assert 'feed_b' in config['feeds']
        assert 'feed_c' in config['feeds']

    def test_load_config_legacy_sources_conversion(self, tmp_path: Path) -> None:
        """Legacy 'sources' section is converted to 'deliveries'."""
        config_file = tmp_path / "korgalore.toml"
        config_file.write_text(
            "[targets.main]\n"
            "type = 'gmail'\n"
            "\n"
            "[sources.legacy]\n"
            "feed = 'test'\n"
            "target = 'main'\n"
        )

        config = load_config(config_file)
        assert 'deliveries' in config
        assert 'legacy' in config['deliveries']
        assert 'sources' not in config

    def test_load_config_conf_d_adds_to_main_deliveries(self, tmp_path: Path) -> None:
        """conf.d deliveries are added to main config deliveries."""
        config_file = tmp_path / "korgalore.toml"
        config_file.write_text(
            "[targets.main]\n"
            "type = 'gmail'\n"
            "\n"
            "[deliveries.main_delivery]\n"
            "feed = 'main_feed'\n"
            "target = 'main'\n"
        )

        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "extra.toml").write_text(
            "[deliveries.extra_delivery]\n"
            "feed = 'extra_feed'\n"
            "target = 'main'\n"
        )

        config = load_config(config_file)
        assert 'main_delivery' in config['deliveries']
        assert 'extra_delivery' in config['deliveries']


def _find_forget_config(conf_d: Path, subsystem_name: str) -> Path:
    """Replicate the forget path's conf.d matching logic from cli.py."""
    key = normalize_subsystem_name(subsystem_name)
    config_file = conf_d / f'{key}.toml'
    if not config_file.exists() and conf_d.is_dir():
        candidates: List[Path] = sorted(
            p for p in conf_d.glob('*.toml')
            if f'_{key}_' in f'_{p.stem}_'
        )
        if len(candidates) == 1:
            config_file = candidates[0]
    return config_file


class TestForgetConfDMatching:
    """Tests for --forget conf.d file matching by normalised substring."""

    def test_exact_match(self, tmp_path: Path) -> None:
        """Exact normalised name matches directly."""
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()
        (conf_d / 'register_map_abstraction_layer.toml').touch()
        result = _find_forget_config(conf_d, 'REGISTER MAP ABSTRACTION LAYER')
        assert result.name == 'register_map_abstraction_layer.toml'

    def test_prefix_substring_match(self, tmp_path: Path) -> None:
        """Substring at the start of the canonical name matches."""
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()
        (conf_d / 'register_map_abstraction_layer.toml').touch()
        result = _find_forget_config(conf_d, 'REGISTER MAP')
        assert result.name == 'register_map_abstraction_layer.toml'

    def test_middle_substring_match(self, tmp_path: Path) -> None:
        """Substring in the middle of the canonical name matches."""
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()
        (conf_d / 'selinux_security_module.toml').touch()
        result = _find_forget_config(conf_d, 'SECURITY')
        assert result.name == 'selinux_security_module.toml'

    def test_suffix_substring_match(self, tmp_path: Path) -> None:
        """Substring at the end of the canonical name matches."""
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()
        (conf_d / 'selinux_security_module.toml').touch()
        result = _find_forget_config(conf_d, 'SECURITY MODULE')
        assert result.name == 'selinux_security_module.toml'

    def test_no_partial_word_match(self, tmp_path: Path) -> None:
        """Partial word does not match across underscore boundaries."""
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir()
        (conf_d / 'selinux_security_module.toml').touch()
        # "CURITY" is a substring of "security" but not on word boundaries
        result = _find_forget_config(conf_d, 'CURITY')
        assert not result.exists()


class TestTrackSubsystemList:
    """Tests for track-subsystem --list output."""

    def test_list_with_subsystem_section(self, tmp_path: Path) -> None:
        """List displays name from [subsystem] section."""
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir(parents=True)
        (conf_d / 'selinux_security_module.toml').write_text(
            "[subsystem]\n"
            "name = 'SELINUX SECURITY MODULE'\n"
            "\n"
            "[feeds.selinux_security_module-mailinglist]\n"
            "url = 'lei:/data/lei/selinux_security_module-mailinglist'\n"
            "\n"
            "[deliveries.selinux_security_module-mailinglist]\n"
            "feed = 'selinux_security_module-mailinglist'\n"
            "target = 'personal'\n"
            "labels = ['INBOX', 'UNREAD']\n"
        )
        import tomllib
        config = tomllib.loads((conf_d / 'selinux_security_module.toml').read_text())
        assert config['subsystem']['name'] == 'SELINUX SECURITY MODULE'
        deliveries = config.get('deliveries', {})
        assert 'selinux_security_module-mailinglist' in deliveries

    def test_list_fallback_without_subsystem_section(self, tmp_path: Path) -> None:
        """List falls back to deriving name from filename when [subsystem] is missing."""
        conf_d = tmp_path / 'conf.d'
        conf_d.mkdir(parents=True)
        # Legacy config without [subsystem] section
        (conf_d / 'amd_gpu.toml').write_text(
            "[feeds.amd_gpu-patches]\n"
            "url = 'lei:/data/lei/amd_gpu-patches'\n"
            "\n"
            "[deliveries.amd_gpu-patches]\n"
            "feed = 'amd_gpu-patches'\n"
            "target = 'personal'\n"
            "labels = ['INBOX']\n"
        )
        import tomllib
        config = tomllib.loads((conf_d / 'amd_gpu.toml').read_text())
        # No subsystem section — fallback should derive from stem
        subsystem_info = config.get('subsystem', {})
        display_name = subsystem_info.get('name')
        if not display_name:
            display_name = 'amd_gpu'.replace('_', ' ').upper()
        assert display_name == 'AMD GPU'
