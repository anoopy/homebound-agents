"""Tests for adapter ABC contracts and built-in implementations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from homebound.adapters.runtime import AgentRuntime
from homebound.adapters.tracker import ClassifiedCommand, CommandLevel, Tracker, TrackerResult
from homebound.adapters.transport import IncomingMessage, Transport
from homebound.runtimes.claude_code import ClaudeCodeRuntime
from homebound.runtimes.generic_cli import GenericCLIRuntime


class TestClaudeCodeRuntime:
    """Verify ClaudeCodeRuntime implementation."""

    def test_start_command(self):
        runtime = ClaudeCodeRuntime()
        cmd = runtime.start_command(Path("/tmp/project"))
        assert "cd" in cmd
        assert "/tmp/project" in cmd
        assert "claude" in cmd

    def test_start_command_unsets_env(self):
        runtime = ClaudeCodeRuntime()
        cmd = runtime.start_command(Path("/tmp"))
        assert "unset CLAUDECODE" in cmd

    def test_idle_markers(self):
        runtime = ClaudeCodeRuntime()
        markers = runtime.idle_prompt_markers()
        assert "\u276f" in markers
        assert "> " in markers

    def test_exit_command(self):
        runtime = ClaudeCodeRuntime()
        assert runtime.exit_command() == "/exit"

    def test_env_overrides(self):
        runtime = ClaudeCodeRuntime()
        overrides = runtime.env_overrides()
        assert "CLAUDECODE" in overrides
        assert overrides["CLAUDECODE"] is None

    def test_custom_command(self):
        runtime = ClaudeCodeRuntime(command="claude --dangerously-skip-permissions")
        cmd = runtime.start_command(Path("/tmp"))
        assert "claude --dangerously-skip-permissions" in cmd

    def test_default_safe(self):
        """Default command must NOT contain --dangerously-skip-permissions."""
        runtime = ClaudeCodeRuntime()
        assert "--dangerously-skip-permissions" not in runtime.command


class TestGenericCLIRuntime:
    """Verify GenericCLIRuntime implementation."""

    def test_custom_command(self):
        runtime = GenericCLIRuntime(
            command="python3 -i",
            prompt_markers=[">>> "],
            exit_cmd="quit()",
        )
        cmd = runtime.start_command(Path("/tmp"))
        assert "python3 -i" in cmd
        assert runtime.idle_prompt_markers() == [">>> "]
        assert runtime.exit_command() == "quit()"

    def test_defaults(self):
        runtime = GenericCLIRuntime(command="bash")
        assert runtime.idle_prompt_markers() == ["$ ", "> "]
        assert runtime.exit_command() == "exit"


class TestRuntimeRegistry:
    """Verify runtime registry."""

    def test_registry_has_claude_code(self):
        from homebound.runtimes import RUNTIME_REGISTRY

        assert "claude-code" in RUNTIME_REGISTRY
        assert RUNTIME_REGISTRY["claude-code"] is ClaudeCodeRuntime

    def test_registry_has_generic(self):
        from homebound.runtimes import RUNTIME_REGISTRY

        assert "generic" in RUNTIME_REGISTRY
        assert RUNTIME_REGISTRY["generic"] is GenericCLIRuntime


class TestGitHubTracker:
    """Verify GitHubTracker classify and dispatch."""

    @staticmethod
    def _make_mock_proc(stdout=b"", stderr=b"", returncode=0):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (stdout, stderr)
        mock_proc.returncode = returncode
        return mock_proc

    @patch(
        "homebound.trackers.github.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    )
    def test_dispatch_list_issues(self, mock_exec):
        mock_exec.return_value = self._make_mock_proc(b"issue output")

        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        result = asyncio.run(gw.dispatch("ls"))
        assert result is not None
        assert result.success
        assert "issue output" in result.output

    @patch(
        "homebound.trackers.github.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    )
    def test_dispatch_create_issue_with_body(self, mock_exec):
        mock_exec.return_value = self._make_mock_proc(b"created")

        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        result = asyncio.run(gw.dispatch("new Fix tests // Some body text"))
        assert result is not None
        assert result.success

        args_list = list(mock_exec.call_args.args) if mock_exec.call_args.args else list(mock_exec.call_args[0])
        assert "Fix tests" in args_list
        assert "Some body text" in args_list

    @patch(
        "homebound.trackers.github.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    )
    def test_dispatch_view_issue(self, mock_exec):
        mock_exec.return_value = self._make_mock_proc(b"issue details")

        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        result = asyncio.run(gw.dispatch("view 215"))
        assert result is not None
        assert result.success

    @patch(
        "homebound.trackers.github.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    )
    def test_dispatch_close_issue(self, mock_exec):
        mock_exec.return_value = self._make_mock_proc(b"closed")

        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        result = asyncio.run(gw.dispatch("rm 42"))
        assert result is not None
        assert result.success

    @patch(
        "homebound.trackers.github.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    )
    def test_dispatch_pr_list(self, mock_exec):
        mock_exec.return_value = self._make_mock_proc(b"pr output")

        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        result = asyncio.run(gw.dispatch("ls pr"))
        assert result is not None
        assert result.success

    @patch(
        "homebound.trackers.github.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    )
    def test_dispatch_pr_view(self, mock_exec):
        mock_exec.return_value = self._make_mock_proc(b"pr details")

        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        result = asyncio.run(gw.dispatch("view pr 42"))
        assert result is not None
        assert result.success

    def test_dispatch_unknown_returns_none(self):
        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        result = asyncio.run(gw.dispatch("do something random"))
        assert result is None

    @patch(
        "homebound.trackers.github.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    )
    def test_run_handles_timeout(self, mock_exec):
        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = asyncio.TimeoutError()
        mock_proc.kill = MagicMock()
        mock_exec.return_value = mock_proc

        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        result = asyncio.run(gw.dispatch("ls"))
        assert result is not None
        assert not result.success
        assert "timed out" in result.error.lower()

    @patch(
        "homebound.trackers.github.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    )
    def test_run_handles_timeout_even_if_drain_hangs(self, mock_exec):
        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = [asyncio.TimeoutError(), asyncio.TimeoutError()]
        mock_proc.kill = MagicMock()
        mock_exec.return_value = mock_proc

        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        result = asyncio.run(gw.dispatch("ls"))
        assert result is not None
        assert not result.success
        assert "timed out" in result.error.lower()

    @patch(
        "homebound.trackers.github.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    )
    def test_run_handles_missing_gh(self, mock_exec):
        mock_exec.side_effect = FileNotFoundError("gh not found")

        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        result = asyncio.run(gw.dispatch("ls"))
        assert result is not None
        assert not result.success
        assert "not found" in result.error.lower()

    def test_classify_levels(self):
        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))

        ls_result = gw.classify("ls")
        assert ls_result is not None
        assert ls_result.level == CommandLevel.READ

        new_result = gw.classify("new Fix the test")
        assert new_result is not None
        assert new_result.level == CommandLevel.WRITE

        rm_result = gw.classify("rm 42")
        assert rm_result is not None
        assert rm_result.level == CommandLevel.DESTRUCTIVE

    def test_double_slash_body(self):
        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        classified = gw.classify("new Fix test // Body text here")
        assert classified is not None
        assert classified.handler == "_create_issue"
        assert classified.args == ("Fix test", "Body text here")

    def test_ls_pr_distinct_from_ls(self):
        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        ls_result = gw.classify("ls")
        ls_pr_result = gw.classify("ls pr")
        assert ls_result.handler == "_list_issues"
        assert ls_pr_result.handler == "_pr_list"

    def test_view_pr_distinct_from_view(self):
        from homebound.trackers.github import GitHubTracker

        gw = GitHubTracker(Path("/tmp"))
        view_result = gw.classify("view 42")
        view_pr_result = gw.classify("view pr 42")
        assert view_result.handler == "_view_issue"
        assert view_pr_result.handler == "_pr_view"


class TestIncomingMessage:
    """Verify IncomingMessage dataclass."""

    def test_basic_construction(self):
        msg = IncomingMessage(text="hello", ts="123.456", user="WFAKE01")
        assert msg.text == "hello"
        assert msg.ts == "123.456"
        assert msg.user == "WFAKE01"

    def test_default_extra(self):
        msg = IncomingMessage(text="hello", ts="123.456")
        assert msg.extra == {}
        assert msg.user == ""


class _FakeResponse:
    def __init__(self, ok: bool, status_code: int, data=None, json_error: bool = False):
        self.ok = ok
        self.status_code = status_code
        self._data = data if data is not None else {}
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("bad json")
        return self._data


class TestSlackTransport:
    def test_post_raises_on_slack_api_error(self):
        from homebound.transports.slack import SlackTransport, SlackTransportError

        transport = SlackTransport(channel_id="C123", token="test-token")
        fake_resp = _FakeResponse(
            ok=True, status_code=200, data={"ok": False, "error": "invalid_auth"}
        )

        with patch("homebound.transports.slack.requests.post", return_value=fake_resp):
            with pytest.raises(SlackTransportError, match="invalid_auth"):
                transport.post("hello")

    def test_poll_raises_on_non_json_response(self):
        from homebound.transports.slack import SlackTransport, SlackTransportError

        transport = SlackTransport(channel_id="C123", token="test-token")
        fake_resp = _FakeResponse(ok=True, status_code=200, json_error=True)

        with patch("homebound.transports.slack.requests.get", return_value=fake_resp):
            with pytest.raises(SlackTransportError, match="non-JSON"):
                transport.poll(since_ts=0)

    def test_poll_raises_on_unexpected_json_shape(self):
        from homebound.transports.slack import SlackTransport, SlackTransportError

        transport = SlackTransport(channel_id="C123", token="test-token")
        fake_resp = _FakeResponse(ok=True, status_code=200, data=[])

        with patch("homebound.transports.slack.requests.get", return_value=fake_resp):
            with pytest.raises(SlackTransportError, match="unexpected JSON payload type"):
                transport.poll(since_ts=0)

    def test_poll_raises_on_non_list_messages_payload(self):
        from homebound.transports.slack import SlackTransport, SlackTransportError

        transport = SlackTransport(channel_id="C123", token="test-token")
        fake_resp = _FakeResponse(
            ok=True, status_code=200, data={"ok": True, "messages": {"ts": "1"}}
        )

        with patch("homebound.transports.slack.requests.get", return_value=fake_resp):
            with pytest.raises(SlackTransportError, match="expected list"):
                transport.poll(since_ts=0)

    def test_poll_raises_on_non_object_message_item(self):
        from homebound.transports.slack import SlackTransport, SlackTransportError

        transport = SlackTransport(channel_id="C123", token="test-token")
        fake_resp = _FakeResponse(ok=True, status_code=200, data={"ok": True, "messages": ["x"]})

        with patch("homebound.transports.slack.requests.get", return_value=fake_resp):
            with pytest.raises(SlackTransportError, match="expected object"):
                transport.poll(since_ts=0)

    def test_poll_raises_on_request_exception(self):
        from homebound.transports.slack import SlackTransport, SlackTransportError

        transport = SlackTransport(channel_id="C123", token="test-token")

        with patch(
            "homebound.transports.slack.requests.get",
            side_effect=requests.RequestException("network down"),
        ):
            with pytest.raises(SlackTransportError, match="request failed"):
                transport.poll(since_ts=0)

    def test_poll_thread_replies_basic(self):
        """Replies are parsed and parent message is excluded."""
        from homebound.transports.slack import SlackTransport

        transport = SlackTransport(channel_id="C123", token="test-token")
        parent_ts = "1000.0000"
        reply_ts = "1000.1111"
        fake_resp = _FakeResponse(
            ok=True,
            status_code=200,
            data={
                "ok": True,
                "messages": [
                    {"ts": parent_ts, "user": "U001", "text": "parent"},
                    {"ts": reply_ts, "user": "U002", "text": "reply text", "thread_ts": parent_ts},
                ],
            },
        )

        with patch("homebound.transports.slack.requests.get", return_value=fake_resp):
            results = transport.poll_thread_replies([parent_ts], since_ts=0.0)

        assert len(results) == 1
        assert results[0].ts == reply_ts
        assert results[0].text == "reply text"
        assert results[0].thread_ts == parent_ts
        assert results[0].user == "U002"

    def test_poll_thread_replies_filters_parent_message(self):
        """The parent message itself must not appear in results."""
        from homebound.transports.slack import SlackTransport

        transport = SlackTransport(channel_id="C123", token="test-token")
        parent_ts = "1000.0000"
        fake_resp = _FakeResponse(
            ok=True,
            status_code=200,
            data={"ok": True, "messages": [{"ts": parent_ts, "user": "U001", "text": "parent"}]},
        )

        with patch("homebound.transports.slack.requests.get", return_value=fake_resp):
            results = transport.poll_thread_replies([parent_ts], since_ts=0.0)

        assert results == []

    def test_poll_thread_replies_graceful_per_thread_error(self):
        """A failed thread does not prevent remaining threads from being polled."""
        from homebound.transports.slack import SlackTransport

        transport = SlackTransport(channel_id="C123", token="test-token")
        good_parent = "2000.0000"
        good_reply = "2000.1111"

        def _fake_get(url, **kwargs):
            ts_param = kwargs.get("params", {}).get("ts", "")
            if ts_param == "1000.0000":
                raise requests.RequestException("network error")
            return _FakeResponse(
                ok=True,
                status_code=200,
                data={
                    "ok": True,
                    "messages": [
                        {"ts": good_parent, "user": "U001", "text": "parent"},
                        {"ts": good_reply, "user": "U002", "text": "good reply", "thread_ts": good_parent},
                    ],
                },
            )

        with patch("homebound.transports.slack.requests.get", side_effect=_fake_get):
            results = transport.poll_thread_replies(["1000.0000", good_parent], since_ts=0.0)

        # Only the good thread's reply should be returned
        assert len(results) == 1
        assert results[0].ts == good_reply

    def test_poll_thread_replies_returns_chronological_order(self):
        """Replies from multiple threads are sorted chronologically."""
        from homebound.transports.slack import SlackTransport

        transport = SlackTransport(channel_id="C123", token="test-token")
        call_count = {"n": 0}
        responses = [
            # Thread A: one reply at ts 3000.2
            _FakeResponse(
                ok=True,
                status_code=200,
                data={
                    "ok": True,
                    "messages": [
                        {"ts": "3000.0", "user": "U1", "text": "parent A"},
                        {"ts": "3000.2", "user": "U2", "text": "reply A2", "thread_ts": "3000.0"},
                    ],
                },
            ),
            # Thread B: one reply at ts 3000.1
            _FakeResponse(
                ok=True,
                status_code=200,
                data={
                    "ok": True,
                    "messages": [
                        {"ts": "3000.1", "user": "U3", "text": "parent B"},
                        {"ts": "3000.3", "user": "U4", "text": "reply B1", "thread_ts": "3000.1"},
                    ],
                },
            ),
        ]

        def _fake_get(url, **kwargs):
            resp = responses[call_count["n"]]
            call_count["n"] += 1
            return resp

        with patch("homebound.transports.slack.requests.get", side_effect=_fake_get):
            results = transport.poll_thread_replies(["3000.0", "3000.1"], since_ts=0.0)

        assert len(results) == 2
        assert results[0].ts == "3000.2"
        assert results[1].ts == "3000.3"


class TestTransportABCDefault:
    """Verify default no-op behavior of poll_thread_replies on the ABC."""

    def test_abc_default_returns_empty_list(self):
        """A minimal Transport subclass gets poll_thread_replies for free."""
        from homebound.adapters.transport import Transport

        class _MinimalTransport(Transport):
            def post(self, message, thread_ts=""):
                return ""

            def poll(self, since_ts, limit=20):
                return []

            def format_agent_message(self, name, msg):
                return msg

            def is_from_agent(self, msg, prefixes):
                return False

        t = _MinimalTransport()
        result = t.poll_thread_replies(["1234.5678"], since_ts=0.0)
        assert result == []
