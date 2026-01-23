"""Tests for User-Agent handling across korgalore."""

import os
from unittest import mock

import pytest

import korgalore
from korgalore import (
    __version__,
    set_user_agent_id,
    get_requests_session,
    close_requests_session,
    _init_git_user_agent,
    run_lei_command,
    GitError,
    PublicInboxError,
)


class TestSetUserAgentId:
    """Tests for set_user_agent_id function."""

    def teardown_method(self) -> None:
        """Reset user agent after each test."""
        korgalore.__user_agent__ = f"korgalore/{__version__}"

    def test_appends_id_with_plus(self) -> None:
        """User agent ID is appended with + separator."""
        set_user_agent_id("test123")
        assert korgalore.__user_agent__ == f"korgalore/{__version__}+test123"

    def test_uuid_format(self) -> None:
        """User agent ID works with UUID format."""
        set_user_agent_id("550e8400-e29b-41d4-a716-446655440000")
        assert "+550e8400-e29b-41d4-a716-446655440000" in korgalore.__user_agent__

    def test_overwrites_previous_id(self) -> None:
        """Setting ID twice overwrites the first."""
        set_user_agent_id("first")
        set_user_agent_id("second")
        assert korgalore.__user_agent__ == f"korgalore/{__version__}+second"
        assert "first" not in korgalore.__user_agent__


class TestGetRequestsSession:
    """Tests for get_requests_session function."""

    def teardown_method(self) -> None:
        """Clean up session and reset user agent after each test."""
        close_requests_session()
        korgalore.__user_agent__ = f"korgalore/{__version__}"

    def test_returns_session_with_user_agent(self) -> None:
        """Session has correct User-Agent header."""
        session = get_requests_session()
        assert session.headers["User-Agent"] == f"korgalore/{__version__}"

    def test_returns_same_instance(self) -> None:
        """Repeated calls return the same session instance."""
        session1 = get_requests_session()
        session2 = get_requests_session()
        assert session1 is session2

    def test_reflects_user_agent_id_if_set_first(self) -> None:
        """Session reflects user_agent_plus if set before session creation."""
        set_user_agent_id("myid")
        session = get_requests_session()
        assert session.headers["User-Agent"] == f"korgalore/{__version__}+myid"


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
        korgalore.__user_agent__ = f"korgalore/{__version__}"

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
        """User agent includes plus ID if set."""
        set_user_agent_id("testid")
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout=b"git version 2.45.0",
                stderr=b""
            )
            _init_git_user_agent()
            expected = f"git/2.45.0 (korgalore/{__version__}+testid)"
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
        """Reset user agent after each test."""
        korgalore.__user_agent__ = f"korgalore/{__version__}"

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
        """User agent includes plus ID if set."""
        set_user_agent_id("myid")
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
