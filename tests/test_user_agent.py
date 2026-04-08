"""Tests for User-Agent handling across korgalore."""

import os
from unittest import mock
from unittest.mock import MagicMock

import pytest

import korgalore
from korgalore import (
    __version__,
    get_requests_session,
    close_requests_session,
    _init_git_user_agent,
    make_lore_node,
    run_lei_command,
    GitError,
    PublicInboxError,
)


class TestGetRequestsSession:
    """Tests for get_requests_session function."""

    def teardown_method(self) -> None:
        """Clean up session after each test."""
        close_requests_session()

    def test_returns_session_with_user_agent(self) -> None:
        """Session has correct User-Agent header."""
        session = get_requests_session()
        assert session.headers["User-Agent"] == f"korgalore/{__version__}"

    def test_returns_same_instance(self) -> None:
        """Repeated calls return the same session instance."""
        session1 = get_requests_session()
        session2 = get_requests_session()
        assert session1 is session2

    def test_session_excludes_user_agent_plus(self) -> None:
        """Session User-Agent does NOT include _user_agent_plus (no leakage to JMAP etc.)."""
        korgalore._user_agent_plus = 'should-not-appear'
        try:
            session = get_requests_session()
            assert '+should-not-appear' not in session.headers["User-Agent"]
            assert session.headers["User-Agent"] == f"korgalore/{__version__}"
        finally:
            korgalore._user_agent_plus = None


class TestCloseRequestsSession:
    """Tests for close_requests_session function."""

    def teardown_method(self) -> None:
        """Ensure session is closed after each test."""
        close_requests_session()

    def test_clears_global_session(self) -> None:
        """Closing session clears the global reference."""
        get_requests_session()
        assert korgalore._REQSESSION is not None
        close_requests_session()
        assert korgalore._REQSESSION is None

    def test_new_session_after_close(self) -> None:
        """New session is created after closing."""
        session1 = get_requests_session()
        close_requests_session()
        session2 = get_requests_session()
        assert session1 is not session2

    def test_close_without_session_is_safe(self) -> None:
        """Closing when no session exists does not raise."""
        close_requests_session()
        close_requests_session()  # Should not raise


class TestInitGitUserAgent:
    """Tests for _init_git_user_agent function."""

    def teardown_method(self) -> None:
        """Clean up environment after each test."""
        if "GIT_HTTP_USER_AGENT" in os.environ:
            del os.environ["GIT_HTTP_USER_AGENT"]
        korgalore._user_agent_plus = None

    def test_sets_environment_variable(self) -> None:
        """Sets GIT_HTTP_USER_AGENT environment variable."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout=b"git version 2.45.0",
                stderr=b""
            )
            _init_git_user_agent()
            assert "GIT_HTTP_USER_AGENT" in os.environ

    def test_format_includes_git_and_korgalore_version(self) -> None:
        """User agent format is git/{version} (korgalore/{version})."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout=b"git version 2.45.0",
                stderr=b""
            )
            _init_git_user_agent()
            expected = f"git/2.45.0 (korgalore/{__version__})"
            assert os.environ["GIT_HTTP_USER_AGENT"] == expected

    def test_includes_user_agent_plus(self) -> None:
        """GIT_HTTP_USER_AGENT includes plus from _user_agent_plus."""
        korgalore._user_agent_plus = 'testid'
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout=b"git version 2.45.0",
                stderr=b""
            )
            _init_git_user_agent()
            expected = f"git/2.45.0 (korgalore/{__version__}+testid)"
            assert os.environ["GIT_HTTP_USER_AGENT"] == expected

    def test_no_plus_when_unset(self) -> None:
        """GIT_HTTP_USER_AGENT has no plus when _user_agent_plus is None."""
        korgalore._user_agent_plus = None
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout=b"git version 2.45.0",
                stderr=b""
            )
            _init_git_user_agent()
            expected = f"git/2.45.0 (korgalore/{__version__})"
            assert os.environ["GIT_HTTP_USER_AGENT"] == expected

    def test_raises_git_error_if_not_found(self) -> None:
        """Raises GitError if git command not found."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            with pytest.raises(GitError) as exc_info:
                _init_git_user_agent()
            assert "not found" in str(exc_info.value).lower()

    def test_raises_git_error_on_nonzero_return(self) -> None:
        """Raises GitError if git returns non-zero."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,
                stdout=b"",
                stderr=b"git: error message"
            )
            with pytest.raises(GitError) as exc_info:
                _init_git_user_agent()
            assert "error message" in str(exc_info.value)


class TestRunLeiCommand:
    """Tests for run_lei_command function."""

    def teardown_method(self) -> None:
        """Reset user agent plus after each test."""
        korgalore._user_agent_plus = None

    def test_adds_user_agent_for_q_command(self) -> None:
        """Adds --user-agent flag for 'q' subcommand."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b"")
            run_lei_command(["q", "search term"])

            called_cmd = mock_run.call_args[0][0]
            assert "--user-agent" in called_cmd
            ua_index = called_cmd.index("--user-agent")
            assert called_cmd[ua_index + 1] == f"korgalore/{__version__}"

    def test_adds_user_agent_for_up_command(self) -> None:
        """Adds --user-agent flag for 'up' subcommand."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b"")
            run_lei_command(["up", "/path/to/search"])

            called_cmd = mock_run.call_args[0][0]
            assert "--user-agent" in called_cmd

    def test_no_user_agent_for_ls_search(self) -> None:
        """Does NOT add --user-agent for 'ls-search' subcommand."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b"")
            run_lei_command(["ls-search", "-l"])

            called_cmd = mock_run.call_args[0][0]
            assert "--user-agent" not in called_cmd

    def test_no_user_agent_for_forget_search(self) -> None:
        """Does NOT add --user-agent for 'forget-search' subcommand."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b"")
            run_lei_command(["forget-search", "/path"])

            called_cmd = mock_run.call_args[0][0]
            assert "--user-agent" not in called_cmd

    def test_user_agent_placed_after_subcommand(self) -> None:
        """--user-agent is placed after the subcommand."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b"")
            run_lei_command(["q", "term", "--threads"])

            called_cmd = mock_run.call_args[0][0]
            # Should be: lei q --user-agent <ua> term --threads
            assert called_cmd[0] == "lei"
            assert called_cmd[1] == "q"
            assert called_cmd[2] == "--user-agent"

    def test_includes_user_agent_plus(self) -> None:
        """Lei user-agent includes plus from _user_agent_plus."""
        korgalore._user_agent_plus = 'myid'
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b"")
            run_lei_command(["q", "term"])

            called_cmd = mock_run.call_args[0][0]
            ua_index = called_cmd.index("--user-agent")
            assert called_cmd[ua_index + 1] == f"korgalore/{__version__}+myid"

    def test_raises_public_inbox_error_if_not_found(self) -> None:
        """Raises PublicInboxError if lei command not found."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            with pytest.raises(PublicInboxError) as exc_info:
                run_lei_command(["q", "term"])
            assert "not found" in str(exc_info.value).lower()

    def test_returns_returncode_and_stdout(self) -> None:
        """Returns tuple of (returncode, stdout)."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout=b"output data"
            )
            retcode, output = run_lei_command(["ls-search"])
            assert retcode == 0
            assert output == b"output data"


class TestMakeLoreNode:
    """Tests for make_lore_node factory function."""

    def test_calls_from_git_config(self) -> None:
        """Creates node via LoreNode.from_git_config with correct args."""
        mock_node = MagicMock()
        with mock.patch('korgalore.LoreNode.from_git_config', return_value=mock_node) as mock_fgc:
            node = make_lore_node(url='https://example.com/list', cache_dir='/tmp/cache')
            mock_fgc.assert_called_once_with('https://example.com/list', cache_dir='/tmp/cache')
            mock_node.set_user_agent.assert_called_once_with('korgalore', __version__)
            assert node is mock_node

    def test_default_url(self) -> None:
        """Default URL is lore.kernel.org/all."""
        mock_node = MagicMock()
        with mock.patch('korgalore.LoreNode.from_git_config', return_value=mock_node) as mock_fgc:
            make_lore_node()
            mock_fgc.assert_called_once_with('https://lore.kernel.org/all', cache_dir=None)

    def test_default_cache_dir_is_none(self) -> None:
        """Cache dir defaults to None (no caching)."""
        mock_node = MagicMock()
        with mock.patch('korgalore.LoreNode.from_git_config', return_value=mock_node) as mock_fgc:
            make_lore_node()
            assert mock_fgc.call_args[1]['cache_dir'] is None
