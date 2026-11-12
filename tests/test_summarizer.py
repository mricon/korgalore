"""Tests for the summarizers, with a real local HTTP server and real commands.

No language model is needed: the fake server answers like an
OpenAI-compatible one, and the commands are small shell scripts.
"""

import json
import logging
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import requests

from korgalore import ConfigurationError
from korgalore import summarizer as summarizer_mod
from korgalore.digest import DigestThread, NoSummary, group_threads
from korgalore.summarizer import (
    FAILURES_MAX,
    SYSTEM_PROMPT,
    CommandSummarizer,
    OpenAISummarizer,
    SummarizerError,
    SummaryCache,
    SummaryRun,
    clean_summary,
    estimate_summaries,
    make_summarizer,
    needs_summary,
    rank_threads,
    summarize_thread,
    system_prompt,
    thread_prompt,
)
from tests.digest_helpers import ALICE, BOB, RecordingSummarizer, mkmsg


class FakeServer:
    """An OpenAI-compatible endpoint that answers what the test sets up."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.reply: Any = self.completion('A short summary.')
        self.delay = 0.0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers['Content-Length'])
                server.requests.append(
                    {
                        'path': self.path,
                        'auth': self.headers.get('Authorization'),
                        'body': json.loads(self.rfile.read(length)),
                    }
                )
                time.sleep(server.delay)
                body = server.reply if isinstance(server.reply, bytes) else json.dumps(server.reply).encode()
                self.send_response(server.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                # The client may have timed out and hung up, which is what
                # test_timeout wants
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}/v1'
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    @staticmethod
    def completion(
        content: str | None, finish_reason: str = 'stop', prompt_tokens: int | None = None
    ) -> dict[str, Any]:
        reply: dict[str, Any] = {
            'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': finish_reason}]
        }
        if prompt_tokens is not None:
            reply['usage'] = {'prompt_tokens': prompt_tokens, 'completion_tokens': 20}
        return reply

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[FakeServer]:
    fake = FakeServer()
    yield fake
    fake.close()


@pytest.fixture
def session() -> Iterator[requests.Session]:
    sess = requests.Session()
    yield sess
    sess.close()


def openai(server: FakeServer, session: requests.Session, **kwargs: Any) -> OpenAISummarizer:
    return OpenAISummarizer('local', server.url, 'qwen3:32b', session=session, **kwargs)


class TestOpenAI:
    @pytest.mark.parametrize(
        ('api_key', 'url_suffix', 'instructions'),
        [
            pytest.param('sekrit', '', None, id='with-key'),
            pytest.param(None, '', None, id='no-key-no-auth-header'),
            pytest.param(None, '/', None, id='trailing-slash-in-url'),
            pytest.param(None, '', 'Tell me if anyone sounds upset.', id='instructions-in-system-message'),
        ],
    )
    def test_sends_the_prompt(
        self,
        server: FakeServer,
        session: requests.Session,
        api_key: str | None,
        url_suffix: str,
        instructions: str | None,
    ) -> None:
        summarizer = OpenAISummarizer('local', server.url + url_suffix, 'qwen3:32b', api_key=api_key, session=session)
        assert summarizer.summarize('Thread: mm: fix it', instructions) == 'A short summary.'

        [req] = server.requests
        assert req['path'] == '/v1/chat/completions'
        assert req['auth'] == (None if api_key is None else f'Bearer {api_key}')
        assert req['body']['model'] == 'qwen3:32b'
        assert req['body']['stream'] is False
        assert req['body']['messages'] == [
            {'role': 'system', 'content': SYSTEM_PROMPT if instructions is None else system_prompt(instructions)},
            {'role': 'user', 'content': 'Thread: mm: fix it'},
        ]

    def test_thinking_is_removed(self, server: FakeServer, session: requests.Session) -> None:
        server.reply = server.completion('<think>\nLet me see...\n</think>\n\nThe summary.')
        assert openai(server, session).summarize('x') == 'The summary.'

    def test_http_error(self, server: FakeServer, session: requests.Session) -> None:
        server.status = 500
        server.reply = {'error': 'model not loaded'}
        with pytest.raises(SummarizerError, match=r'HTTP 500.*model not loaded'):
            openai(server, session).summarize('x')

    @pytest.mark.parametrize(
        'reply',
        [b'not json', {'choices': []}, {'nope': 1}, {'choices': [{'message': {}}]}, FakeServer.completion(None)],
    )
    def test_strange_reply(self, server: FakeServer, session: requests.Session, reply: Any) -> None:
        server.reply = reply
        with pytest.raises(SummarizerError):
            openai(server, session).summarize('x')

    def test_timeout(self, server: FakeServer, session: requests.Session) -> None:
        server.delay = 0.3
        with pytest.raises(SummarizerError, match='could not reach'):
            openai(server, session, timeout=0.05).summarize('x')

    def test_server_down(self, session: requests.Session) -> None:
        fake = FakeServer()
        fake.close()
        with pytest.raises(SummarizerError, match='could not reach'):
            openai(fake, session).summarize('x')

    def test_cut_prompt_warns_once(
        self, server: FakeServer, session: requests.Session, caplog: pytest.LogCaptureFixture
    ) -> None:
        # About 5000 tokens sent, but the server says it read 2048
        server.reply = server.completion('Half a summary.', prompt_tokens=2048)
        summarizer = openai(server, session)
        with caplog.at_level(logging.WARNING, logger='korgalore'):
            assert summarizer.summarize('x' * 20000) == 'Half a summary.'
            summarizer.summarize('x' * 20000)
        warnings = [rec.getMessage() for rec in caplog.records]
        assert len(warnings) == 1
        assert 'OLLAMA_CONTEXT_LENGTH' in warnings[0]

    def test_full_prompt_does_not_warn(
        self, server: FakeServer, session: requests.Session, caplog: pytest.LogCaptureFixture
    ) -> None:
        server.reply = server.completion('Summary.', prompt_tokens=4500)
        with caplog.at_level(logging.WARNING, logger='korgalore'):
            openai(server, session).summarize('x' * 20000)
        assert caplog.records == []

    def test_cut_reply_warns(
        self, server: FakeServer, session: requests.Session, caplog: pytest.LogCaptureFixture
    ) -> None:
        server.reply = server.completion('The series adds', finish_reason='length')
        with caplog.at_level(logging.WARNING, logger='korgalore'):
            assert openai(server, session).summarize('x') == 'The series adds'
        assert 'output limit' in caplog.text

    def test_only_thinking_is_an_error(self, server: FakeServer, session: requests.Session) -> None:
        server.reply = server.completion('<think>I should start by', finish_reason='length')
        with pytest.raises(SummarizerError, match='only reasoning'):
            openai(server, session).summarize('x')

    @pytest.mark.parametrize(
        ('url', 'local'),
        [
            ('http://localhost:11434/v1', True),
            ('http://127.0.0.1:8080/v1', True),
            ('http://127.0.0.2/v1', True),
            ('http://[::1]:8000/v1', True),
            ('http://gpu.localhost/v1', True),
            ('http://192.168.1.5:11434/v1', False),
            ('https://openrouter.ai/api/v1', False),
            # Not a loopback name, only one that starts like it
            ('http://localhost.example.com/v1', False),
        ],
    )
    def test_is_local(self, url: str, local: bool) -> None:
        assert OpenAISummarizer('s', url, 'm').is_local is local


class TestCommand:
    @pytest.mark.parametrize('instructions', [None, 'Tell me if anyone sounds upset.'], ids=['plain', 'instructions'])
    def test_prompt_on_stdin(self, tmp_path: Path, instructions: str | None) -> None:
        seen = tmp_path / 'stdin'
        summarizer = CommandSummarizer('script', f"sh -c 'cat > {seen}; echo The summary.'")

        assert summarizer.summarize('Thread: mm: fix it', instructions) == 'The summary.'
        prompt = SYSTEM_PROMPT if instructions is None else system_prompt(instructions)
        assert seen.read_text() == f'{prompt}\n\nThread: mm: fix it'

    def test_model_is_the_program_name(self) -> None:
        """The command line may carry secrets, so only the program is the model."""
        assert CommandSummarizer('s', '/opt/bin/claude -p --api-key hunter2').model == 'claude'

    def test_failure_shows_last_error_line(self) -> None:
        summarizer = CommandSummarizer('s', "sh -c 'echo warming up >&2; echo quota exceeded >&2; exit 3'")
        with pytest.raises(SummarizerError, match='exited with 3: quota exceeded'):
            summarizer.summarize('x')

    def test_missing_program(self) -> None:
        with pytest.raises(SummarizerError, match='could not run'):
            CommandSummarizer('s', 'no-such-summarizer-here').summarize('x')

    def test_timeout(self) -> None:
        with pytest.raises(SummarizerError, match='longer than'):
            CommandSummarizer('s', 'sleep 5', timeout=0.05).summarize('x')

    def test_empty_output(self) -> None:
        with pytest.raises(SummarizerError, match='empty'):
            CommandSummarizer('s', "sh -c 'cat >/dev/null'").summarize('x')


class TestSystemPrompt:
    def test_no_instructions(self) -> None:
        assert system_prompt() == SYSTEM_PROMPT
        assert system_prompt(None) == SYSTEM_PROMPT

    def test_instructions_come_after_the_rules(self) -> None:
        """The fixed rules stay first, and say that they win."""
        prompt = system_prompt('Tell me if anyone sounds upset.')
        assert prompt.startswith(SYSTEM_PROMPT + '\n\n')
        assert prompt.endswith('\n\nTell me if anyone sounds upset.')
        assert 'as long as it fits the rules above' in prompt


class TestCleanSummary:
    @pytest.mark.parametrize(
        ('reply', 'summary'),
        [
            ('  Plain.\n', 'Plain.'),
            ('<think>a</think>B.', 'B.'),
            ('<THINK>a</THINK>\nB.', 'B.'),
            # The opening tag was in the prompt, so only the end is here
            ('reasoning...\n</think>\nB.', 'B.'),
            ('<think>a</think><think>b</think>B.', 'B.'),
            # A tag that is only mentioned in the text stays
            ('The patch adds a <thinkpad> quirk.', 'The patch adds a <thinkpad> quirk.'),
        ],
    )
    def test_cleaned(self, reply: str, summary: str) -> None:
        assert clean_summary(reply) == summary

    @pytest.mark.parametrize('reply', ['', '   \n', '<think>only</think>', '<think>never ends'])
    def test_nothing_left(self, reply: str) -> None:
        with pytest.raises(SummarizerError):
            clean_summary(reply)


def thread(count: int, body_size: int = 100) -> list[EmailMessage]:
    return [
        mkmsg(
            f'm{idx}@x',
            'Re: [PATCH] mm: fix it',
            body=f'Message number {idx}.\n' + 'x' * body_size,
            sender=f'P{idx} <p{idx}@x>',
        )
        for idx in range(1, count + 1)
    ]


class TestThreadPrompt:
    def test_small_thread_is_whole(self) -> None:
        prompt = thread_prompt('[PATCH] mm: fix it', thread(3))

        assert prompt.startswith('Thread: [PATCH] mm: fix it\n\n=== Message 1 of 3 ===\nFrom: P1 <p1@x>\n')
        assert 'Subject: Re: [PATCH] mm: fix it' in prompt
        for idx in (1, 2, 3):
            assert f'Message number {idx}.' in prompt
        assert 'left out' not in prompt

    def test_big_thread_keeps_first_and_newest(self) -> None:
        prompt = thread_prompt('s', thread(40, body_size=400), max_chars=5000)

        assert len(prompt) <= 5000
        assert 'Message number 1.' in prompt
        assert 'Message number 40.' in prompt
        assert 'Message number 2.' not in prompt
        kept = [idx for idx in range(1, 41) if f'Message number {idx}.' in prompt]
        # The newest ones are kept without a hole between them
        assert kept == [1, *range(kept[1], 41)]
        assert f'[{40 - len(kept)} messages left out here]' in prompt

    def test_huge_first_message_is_cut(self) -> None:
        msgs = thread(1, body_size=50000) + thread(2)[1:]
        prompt = thread_prompt('s', msgs, max_chars=5000)

        assert len(prompt) <= 5000
        assert '[message cut]' in prompt
        assert 'Message number 2.' in prompt

    def test_big_message_in_the_middle_ends_the_newest(self) -> None:
        # Older messages after a hole would read like the newest state
        msgs = thread(5)
        msgs[2].set_content('Message number 3.\n' + 'x' * 10000)
        prompt = thread_prompt('s', msgs, max_chars=5000)

        kept = [idx for idx in range(1, 6) if f'Message number {idx}.' in prompt]
        assert kept == [1, 4, 5]
        assert '[2 messages left out here]' in prompt

    def test_no_messages(self) -> None:
        assert thread_prompt('s', []) == 'Thread: s\n\n'


class TestMakeSummarizer:
    def test_openai(self, tmp_path: Path) -> None:
        key = tmp_path / 'llm.key'
        key.write_text('sekrit\n')
        summarizer = make_summarizer(
            'local',
            {
                'type': 'openai',
                'url': 'http://localhost:11434/v1',
                'model': 'qwen3:32b',
                'api_key_file': str(key),
                'timeout': 300,
                'max_input_chars': 16000,
            },
        )
        assert isinstance(summarizer, OpenAISummarizer)
        assert summarizer.api_key == 'sekrit'
        assert summarizer.timeout == 300
        assert summarizer.max_input_chars == 16000
        assert summarizer.allow_private_feeds is False

    @pytest.mark.parametrize(('extra', 'model'), [({}, 'llm'), ({'model': 'x'}, 'x')], ids=['program-name', 'named'])
    def test_command(self, extra: dict[str, Any], model: str) -> None:
        details = {'type': 'command', 'command': '~/bin/llm -m x', 'allow_private_feeds': True, **extra}
        summarizer = make_summarizer('cli', details)
        assert isinstance(summarizer, CommandSummarizer)
        assert summarizer.args == [f'{Path.home()}/bin/llm', '-m', 'x']
        assert summarizer.allow_private_feeds is True
        assert summarizer.model == model

    @pytest.mark.parametrize(
        ('details', 'error'),
        [
            ({}, 'type must be one of'),
            ({'type': 'anthropic'}, 'type must be one of'),
            ({'type': 'openai', 'model': 'm'}, 'url must be'),
            ({'type': 'openai', 'url': 'localhost:11434', 'model': 'm'}, 'url must be'),
            ({'type': 'openai', 'url': 'http://localhost/v1'}, 'model is required'),
            ({'type': 'openai', 'url': 'http://localhost/v1', 'model': 'm', 'command': 'x'}, 'unknown key command'),
            ({'type': 'command', 'command': 'x', 'modle': 'm', 'urls': 'u'}, 'unknown keys modle, urls'),
            ({'type': 'command'}, 'command is required'),
            ({'type': 'command', 'command': '  '}, 'command is required'),
            ({'type': 'command', 'command': "llm 'unclosed"}, 'cannot be parsed'),
            ({'type': 'command', 'command': 'x', 'model': ''}, 'model'),
            ({'type': 'command', 'command': 'x', 'model': 3}, 'model'),
            ({'type': 'command', 'command': 'x', 'timeout': True}, 'timeout'),
            ({'type': 'command', 'command': 'x', 'timeout': -1}, 'timeout'),
            ({'type': 'command', 'command': 'x', 'timeout': '60'}, 'timeout'),
            ({'type': 'command', 'command': 'x', 'max_input_chars': 10}, 'max_input_chars'),
            ({'type': 'command', 'command': 'x', 'max_input_chars': 2000.5}, 'max_input_chars'),
            ({'type': 'command', 'command': 'x', 'allow_private_feeds': 'yes'}, 'allow_private_feeds'),
        ],
    )
    def test_bad_config(self, details: dict[str, Any], error: str) -> None:
        with pytest.raises(ConfigurationError, match=error):
            make_summarizer('bad', details)

    @pytest.mark.parametrize(
        ('content', 'error'), [(None, 'cannot be read'), ('\n', 'is empty')], ids=['missing-file', 'empty-file']
    )
    def test_bad_key_file(self, tmp_path: Path, content: str | None, error: str) -> None:
        key = tmp_path / 'llm.key'
        if content is not None:
            key.write_text(content)
        details = {'type': 'openai', 'url': 'http://x/v1', 'model': 'm', 'api_key_file': str(key)}
        with pytest.raises(ConfigurationError, match=error):
            make_summarizer('s', details)


DAY1 = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
ROOT = 'root@x'


def msg(msgid: str, body: str | None = None) -> EmailMessage:
    return mkmsg(
        msgid, '[PATCH] mm: fix it', body=body or f'Text of {msgid}.\n', refs=None if msgid == ROOT else [ROOT]
    )


class TestSummaryCache:
    def test_find_needs_every_message(self, cache: SummaryCache) -> None:
        cache.store(ROOT, 'm', 'it is fine', ['a@x', 'b@x'], DAY1)

        hit = cache.find(ROOT, ['a@x', 'b@x'], 'm')
        assert hit is not None
        assert hit.summary == 'it is fine'
        assert cache.find(ROOT, ['b@x'], 'm') == hit
        assert cache.find(ROOT, ['a@x', 'c@x'], 'm') is None
        assert cache.find('other@x', ['a@x'], 'm') is None

    def test_other_model_is_a_miss(self, cache: SummaryCache) -> None:
        cache.store(ROOT, 'small', 'meh', ['a@x'], DAY1)
        assert cache.find(ROOT, ['a@x'], 'big') is None
        assert cache.latest(ROOT, 'big') is None

    def test_other_prompt_version_is_a_miss(self, cache: SummaryCache, monkeypatch: pytest.MonkeyPatch) -> None:
        cache.store(ROOT, 'm', 'old prompt', ['a@x'], DAY1)
        monkeypatch.setattr(summarizer_mod, 'PROMPT_VERSION', summarizer_mod.PROMPT_VERSION + 1)
        assert cache.find(ROOT, ['a@x'], 'm') is None

    def test_other_instructions_are_a_miss(self, cache: SummaryCache) -> None:
        cache.store(ROOT, 'm', 'plain', ['a@x'], DAY1)
        cache.store(ROOT, 'm', 'upset', ['a@x'], DAY1, 'Tell me if anyone sounds upset.')

        assert [cached.summary for cached in cache.entries(ROOT, 'm')] == ['plain']
        assert [cached.summary for cached in cache.entries(ROOT, 'm', 'Tell me if anyone sounds upset.')] == ['upset']
        assert cache.find(ROOT, ['a@x'], 'm', 'Tell me about swearing.') is None
        assert cache.latest(ROOT, 'm', 'Tell me about swearing.') is None

    def test_summaries_from_before_instructions_still_match(self, cache: SummaryCache) -> None:
        """Entries written before summary_instructions existed are used without them."""
        cache.store(ROOT, 'm', 'old', ['a@x'], DAY1)
        [cache_file] = cache.path.iterdir()
        [entry] = json.loads(cache_file.read_text())['entries']
        assert 'instructions' not in entry

        assert cache.find(ROOT, ['a@x'], 'm') is not None
        assert cache.find(ROOT, ['a@x'], 'm', 'Tell me if anyone sounds upset.') is None

    def test_only_a_hash_of_the_instructions_is_stored(self, cache: SummaryCache) -> None:
        cache.store(ROOT, 'm', 'upset', ['a@x'], DAY1, 'Tell me if anyone sounds upset.')
        [cache_file] = cache.path.iterdir()
        text = cache_file.read_text()
        assert 'upset.' not in text
        [entry] = json.loads(text)['entries']
        assert len(entry['instructions']) == 64

    def test_newer_summary_keeps_other_instructions(self, cache: SummaryCache) -> None:
        """Two deliveries of one feed with other instructions keep their own summaries."""
        cache.store(ROOT, 'm', 'upset day 1', ['a@x'], DAY1, 'Tell me if anyone sounds upset.')
        cache.store(ROOT, 'm', 'plain day 2', ['a@x', 'b@x'], DAY1 + timedelta(days=1))

        assert [cached.summary for cached in cache.entries(ROOT, 'm', 'Tell me if anyone sounds upset.')] == [
            'upset day 1'
        ]

    def test_newer_summary_replaces_the_one_it_covers(self, cache: SummaryCache) -> None:
        cache.store(ROOT, 'm', 'day 1', ['a@x'], DAY1)
        cache.store(ROOT, 'other', 'other model', ['a@x'], DAY1)
        cache.store(ROOT, 'm', 'day 2', ['a@x', 'b@x'], DAY1 + timedelta(days=1))

        assert [cached.summary for cached in cache.entries(ROOT, 'm')] == ['day 2']
        assert [cached.summary for cached in cache.entries(ROOT, 'other')] == ['other model']

    def test_latest_is_the_newest(self, cache: SummaryCache) -> None:
        # Two feeds saw different parts of a cross-posted thread
        cache.store(ROOT, 'm', 'from lkml', ['a@x', 'b@x'], DAY1)
        cache.store(ROOT, 'm', 'from netdev', ['a@x', 'c@x'], DAY1 + timedelta(hours=1))
        latest = cache.latest(ROOT, 'm')
        assert latest is not None
        assert latest.summary == 'from netdev'
        # Both cover a@x, and the newer one knows more
        hit = cache.find(ROOT, ['a@x'], 'm')
        assert hit is not None
        assert hit.summary == 'from netdev'

    def test_odd_message_id_is_a_safe_file_name(self, cache: SummaryCache) -> None:
        root = '../../etc/passwd/$(id)@x'
        cache.store(root, 'm', 'safe', ['a@x'], DAY1)
        [cache_file] = cache.path.iterdir()
        assert cache_file.parent == cache.path
        assert cache.find(root, ['a@x'], 'm') is not None

    def test_broken_file_is_ignored(self, cache: SummaryCache) -> None:
        cache.store(ROOT, 'm', 'fine', ['a@x'], DAY1)
        [cache_file] = cache.path.iterdir()
        cache_file.write_text('{not json')

        assert cache.find(ROOT, ['a@x'], 'm') is None
        cache.store(ROOT, 'm', 'again', ['a@x'], DAY1)
        assert cache.latest(ROOT, 'm') is not None

    def test_file_of_another_root_is_ignored(self, cache: SummaryCache) -> None:
        cache.store(ROOT, 'm', 'fine', ['a@x'], DAY1)
        [cache_file] = cache.path.iterdir()
        data = json.loads(cache_file.read_text())
        data['root'] = 'someone-else@x'
        cache_file.write_text(json.dumps(data))
        assert cache.latest(ROOT, 'm') is None

    def test_prune(self, cache: SummaryCache) -> None:
        now = DAY1 + timedelta(days=40)
        cache.store('old@x', 'm', 'stale', ['a@x'], DAY1)
        cache.store(ROOT, 'm', 'stale', ['a@x'], DAY1)
        cache.store(ROOT, 'other', 'fresh', ['a@x'], now - timedelta(days=1))
        (cache.path / 'junk.json').write_text('[]')

        assert cache.prune(now) == 2

        assert cache.latest('old@x', 'm') is None
        assert cache.latest(ROOT, 'm') is None
        assert cache.latest(ROOT, 'other') is not None
        # Empty and unreadable files are gone too
        assert len(list(cache.path.iterdir())) == 1

    def test_prune_without_a_cache(self, tmp_path: Path) -> None:
        assert SummaryCache(tmp_path / 'none').prune(DAY1) == 0


class TestSummarizeThread:
    def test_first_summary_sends_everything(self, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()

        summary = summarize_thread(fake, cache, ROOT, 'mm: fix it', [msg(ROOT), msg('a@x')], DAY1)

        assert summary == 'summary 1'
        [prompt] = fake.prompts
        assert 'Text of root@x.' in prompt
        assert 'Text of a@x.' in prompt
        assert 'earlier messages' not in prompt
        stored = cache.latest(ROOT, fake.model)
        assert stored is not None
        assert stored.covered == {ROOT, 'a@x'}

    def test_same_messages_again_use_the_cache(self, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        summarize_thread(fake, cache, ROOT, 's', [msg(ROOT), msg('a@x')], DAY1)

        # The same thread in a second feed, which only got one of the messages
        assert summarize_thread(fake, cache, ROOT, 's', [msg('a@x')], DAY1) == 'summary 1'
        assert len(fake.prompts) == 1

    def test_next_digest_sends_only_new_messages(self, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        summarize_thread(fake, cache, ROOT, 's', [msg(ROOT), msg('a@x')], DAY1)

        summary = summarize_thread(fake, cache, ROOT, 's', [msg('b@x')], DAY1 + timedelta(days=1))

        assert summary == 'summary 2'
        prompt = fake.prompts[1]
        assert 'Summary of the earlier messages:\nsummary 1\n' in prompt
        assert 'Text of b@x.' in prompt
        assert 'Text of a@x.' not in prompt
        stored = cache.latest(ROOT, fake.model)
        assert stored is not None
        assert stored.covered == {ROOT, 'a@x', 'b@x'}

    def test_partly_cached_thread_sends_the_rest(self, cache: SummaryCache) -> None:
        # Another feed summarized a and b; this one also has c
        fake = RecordingSummarizer()
        cache.store(ROOT, fake.model, 'about a and b', [ROOT, 'a@x', 'b@x'], DAY1)

        summarize_thread(fake, cache, ROOT, 's', [msg(ROOT), msg('a@x'), msg('b@x'), msg('c@x')], DAY1)

        [prompt] = fake.prompts
        assert 'about a and b' in prompt
        assert 'Text of c@x.' in prompt
        for old in (ROOT, 'a@x', 'b@x'):
            assert f'Text of {old}.' not in prompt

    def test_new_model_starts_again(self, cache: SummaryCache) -> None:
        summarize_thread(RecordingSummarizer('small'), cache, ROOT, 's', [msg(ROOT)], DAY1)
        big = RecordingSummarizer('big')

        summarize_thread(big, cache, ROOT, 's', [msg('a@x')], DAY1)

        assert 'earlier messages' not in big.prompts[0]

    def test_failure_stores_nothing(self, cache: SummaryCache) -> None:
        with pytest.raises(SummarizerError):
            summarize_thread(RecordingSummarizer(fail=True), cache, ROOT, 's', [msg(ROOT)], DAY1)
        assert cache.latest(ROOT, 'qwen3:32b') is None

    def test_nothing_new_to_say_keeps_the_summary(self, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        summarize_thread(fake, cache, ROOT, 's', [msg(ROOT)], DAY1)

        # Only quotes: nothing is left after shrinking
        summary = summarize_thread(fake, cache, ROOT, 's', [msg('a@x', body='> only a quote\n')], DAY1)

        assert summary == 'summary 1'
        assert len(fake.prompts) == 1
        assert cache.find(ROOT, ['a@x'], fake.model) is not None

    def test_nothing_at_all_has_no_summary(self, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        assert summarize_thread(fake, cache, ROOT, 's', [msg(ROOT, body='> only a quote\n')], DAY1) is None
        assert fake.prompts == []
        # Nothing was stored, so a later real message starts from scratch
        assert cache.latest(ROOT, fake.model) is None

    def test_previous_summary_counts_toward_the_cap(self) -> None:
        prompt = thread_prompt('s', thread(40, body_size=400), max_chars=5000, previous='p' * 1000)
        assert len(prompt) <= 5000
        assert 'p' * 1000 in prompt
        assert 'Message number 40.' in prompt


def post(msgid: str, root: str | None = None, sender: str = ALICE, body: str | None = None) -> EmailMessage:
    """A message of thread root, or a new thread's first message."""
    return mkmsg(
        msgid,
        f'[PATCH] {root or msgid}',
        body=body or f'Text of {msgid}.\n',
        refs=[root] if root else None,
        sender=sender,
    )


def digest_msgs() -> list[EmailMessage]:
    """busy@x has 4 messages, small@x has 2, and nobody answered lonely@x."""
    return [
        post('lonely@x'),
        post('busy@x'),
        post('small@x'),
        post('b1@x', 'busy@x', BOB),
        post('s1@x', 'small@x', BOB),
        post('b2@x', 'busy@x', BOB),
        post('b3@x', 'busy@x', BOB),
    ]


def by_root(threads: Sequence[DigestThread]) -> dict[str, DigestThread]:
    return {thread.root_msgid: thread for thread in threads}


def series(*subjects: str) -> list[EmailMessage]:
    """A new thread whose first message is the first subject."""
    return [mkmsg(f'p{n}@x', subj, refs=['p0@x'] if n else None) for n, subj in enumerate(subjects)]


class TestNeedsSummary:
    @pytest.mark.parametrize(
        ('msgs', 'expected'),
        [
            pytest.param([post('a@x')], False, id='new-thread-nobody-answered'),
            # A new series gets a summary of what it is about
            pytest.param(
                series('[PATCH 0/2] mm: a series', '[PATCH 1/2] mm: one', '[PATCH 2/2] mm: two'),
                True,
                id='new-series-with-cover-letter',
            ),
            pytest.param(series('[PATCH 1/2] mm: one', '[PATCH 2/2] mm: two'), True, id='new-series-no-cover-letter'),
            pytest.param(series('[PATCH 1/1] mm: one'), False, id='series-of-one'),
            pytest.param(series('mm: a question'), False, id='discussion-nobody-answered'),
            pytest.param([post('a@x'), post('r@x', 'a@x', BOB)], True, id='answered-thread'),
        ],
    )
    def test_needs_summary(self, msgs: list[EmailMessage], expected: bool) -> None:
        assert needs_summary(group_threads(msgs)[0]) is expected

    def test_continuing_thread(self) -> None:
        # The root is from an earlier digest, so this message answers it
        thread = group_threads([post('r@x', 'old@x')])[0]
        assert not thread.is_new
        assert needs_summary(thread)


class TestRankThreads:
    def test_most_messages_first(self) -> None:
        ranked = rank_threads(group_threads(digest_msgs()))
        assert [thread.root_msgid for thread in ranked] == ['busy@x', 'small@x', 'lonely@x']

    def test_newest_first_on_a_tie(self) -> None:
        msgs = [post('early@x'), post('e1@x', 'early@x', BOB), post('late@x'), post('l1@x', 'late@x', BOB)]
        assert [thread.root_msgid for thread in rank_threads(group_threads(msgs))] == ['late@x', 'early@x']


class TestSummaryRun:
    def summarize(
        self, run: SummaryRun, msgs: Sequence[EmailMessage], max_summaries: int | None = None
    ) -> dict[str, Any]:
        return run.summarize_threads('Digest t', group_threads(msgs), msgs, DAY1, max_summaries=max_summaries)

    def test_threads_with_answers_are_summarized(self, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        results = self.summarize(SummaryRun(fake, cache), digest_msgs())

        assert results == {'busy@x': 'summary 1', 'small@x': 'summary 2', 'lonely@x': NoSummary.NOT_NEEDED}
        assert len(fake.prompts) == 2
        assert 'Text of b3@x.' in fake.prompts[0]

    def test_limit_goes_to_the_busiest_thread(self, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        results = self.summarize(SummaryRun(fake, cache), digest_msgs(), max_summaries=1)

        assert results['busy@x'] == 'summary 1'
        assert results['small@x'] is NoSummary.BUDGET
        assert len(fake.prompts) == 1

    def test_cached_summaries_do_not_count(self, cache: SummaryCache) -> None:
        self.summarize(SummaryRun(RecordingSummarizer(), cache), digest_msgs())
        # The same digest again, plus one more answered thread
        msgs = digest_msgs() + [post('third@x'), post('t1@x', 'third@x', BOB)]
        fake = RecordingSummarizer()
        results = self.summarize(SummaryRun(fake, cache), msgs, max_summaries=1)

        assert results['busy@x'] == 'summary 1'
        assert results['small@x'] == 'summary 2'
        assert results['third@x'] == 'summary 1'
        assert len(fake.prompts) == 1

    def test_instructions_reach_the_summarizer(self, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        msgs = digest_msgs()
        SummaryRun(fake, cache).summarize_threads(
            'Digest t', group_threads(msgs), msgs, DAY1, instructions='Tell me if anyone sounds upset.'
        )

        assert fake.instructions == ['Tell me if anyone sounds upset.', 'Tell me if anyone sounds upset.']

    def test_other_instructions_summarize_again(self, cache: SummaryCache) -> None:
        """A second delivery of the same feed asks for its own summaries."""
        msgs = digest_msgs()
        threads = group_threads(msgs)
        SummaryRun(RecordingSummarizer(), cache).summarize_threads('Digest t', threads, msgs, DAY1)
        fake = RecordingSummarizer()
        run = SummaryRun(fake, cache)

        run.summarize_threads('Digest u', threads, msgs, DAY1, instructions='Tell me if anyone sounds upset.')
        assert len(fake.prompts) == 2
        # And then they are cached too
        run.summarize_threads('Digest u', threads, msgs, DAY1, instructions='Tell me if anyone sounds upset.')
        assert len(fake.prompts) == 2

    def test_failure_moves_on(self, cache: SummaryCache, caplog: pytest.LogCaptureFixture) -> None:
        run = SummaryRun(RecordingSummarizer(fail_calls=[1]), cache)
        with caplog.at_level(logging.WARNING, logger='korgalore'):
            results = self.summarize(run, digest_msgs())

        assert results['busy@x'] is NoSummary.UNAVAILABLE
        assert results['small@x'] == 'summary 2'
        # A success starts the count again
        assert run.failures == 0
        assert 'server said no' in caplog.text
        # Nothing is cached for the failed thread
        assert cache.latest('busy@x', 'qwen3:32b') is None

    def test_stops_after_failures_in_a_row(self, cache: SummaryCache, caplog: pytest.LogCaptureFixture) -> None:
        fake = RecordingSummarizer(fail=True)
        run = SummaryRun(fake, cache)
        msgs: list[EmailMessage] = []
        for number in range(FAILURES_MAX + 2):
            msgs += [post(f't{number}@x'), post(f'r{number}@x', f't{number}@x', BOB)]
        with caplog.at_level(logging.ERROR, logger='korgalore'):
            results = self.summarize(run, msgs)

        # The design promises to give up after 3
        assert len(fake.prompts) == 3
        assert set(results.values()) == {NoSummary.UNAVAILABLE}
        assert caplog.text.count('not calling it again') == 1

    def test_stop_lasts_for_the_run(self, cache: SummaryCache) -> None:
        fake = RecordingSummarizer(fail=True)
        run = SummaryRun(fake, cache)
        run.failures = FAILURES_MAX
        cache.store('small@x', fake.model, 'from before', ['small@x', 's1@x'], DAY1)

        results = self.summarize(run, digest_msgs())

        assert fake.prompts == []
        assert results['busy@x'] is NoSummary.UNAVAILABLE
        # The cache still works without the summarizer
        assert results['small@x'] == 'from before'

    def test_nothing_to_summarize(self, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        msgs = [post('q@x', body='> quote\n'), post('q1@x', 'q@x', BOB, body='> quote\n')]
        assert self.summarize(SummaryRun(fake, cache), msgs) == {'q@x': NoSummary.NOT_NEEDED}
        assert fake.prompts == []

    @pytest.mark.parametrize(
        ('is_local', 'answered', 'warns'),
        [
            pytest.param(False, True, True, id='remote'),
            pytest.param(True, True, False, id='local'),
            pytest.param(False, False, False, id='remote-but-no-calls'),
        ],
    )
    def test_remote_warning(
        self, cache: SummaryCache, caplog: pytest.LogCaptureFixture, is_local: bool, answered: bool, warns: bool
    ) -> None:
        fake = RecordingSummarizer()
        fake.is_local = is_local
        msgs = digest_msgs() if answered else [post('lonely@x')]
        with caplog.at_level(logging.WARNING, logger='korgalore'):
            self.summarize(SummaryRun(fake, cache), msgs)
        if warns:
            assert 'summarizer fake is not on this machine, sending it up to 2 threads' in caplog.text
        else:
            assert 'not on this machine' not in caplog.text


class TestEstimateSummaries:
    @staticmethod
    def estimate(
        fake: RecordingSummarizer,
        cache: SummaryCache,
        msgs: Sequence[EmailMessage],
        max_summaries: int | None = None,
    ) -> Any:
        return estimate_summaries(fake, cache, group_threads(msgs), msgs, max_summaries=max_summaries)

    @staticmethod
    def run(fake: RecordingSummarizer, cache: SummaryCache, msgs: Sequence[EmailMessage]) -> None:
        SummaryRun(fake, cache).summarize_threads('Digest t', group_threads(msgs), msgs, DAY1)

    def test_matches_the_real_run(self, cache: SummaryCache) -> None:
        est_fake = RecordingSummarizer()
        est = self.estimate(est_fake, cache, digest_msgs())
        fake = RecordingSummarizer()
        self.run(fake, cache, digest_msgs())

        # Estimating neither calls the model nor fills the cache (or the real run would hit it)
        assert est_fake.prompts == []
        assert est.calls == len(fake.prompts) == 2
        assert est.input_chars == sum(len(prompt) for prompt in fake.prompts)
        assert est.largest_chars == max(len(prompt) for prompt in fake.prompts)
        assert est.incremental == 0
        assert est.cut == 0

    def test_incremental_prompts_match_too(self, cache: SummaryCache) -> None:
        self.run(RecordingSummarizer(), cache, digest_msgs())
        # The next digest has one more reply in busy@x
        msgs = digest_msgs() + [post('b4@x', 'busy@x', BOB)]
        est = self.estimate(RecordingSummarizer(), cache, msgs)
        fake = RecordingSummarizer()
        self.run(fake, cache, msgs)

        assert est.calls == est.incremental == len(fake.prompts) == 1
        assert 'Summary of the earlier messages' in fake.prompts[0]
        assert est.input_chars == len(fake.prompts[0])
        assert est.cached == 1

    def test_instructions_pick_the_cache_entries(self, cache: SummaryCache) -> None:
        self.run(RecordingSummarizer(), cache, digest_msgs())
        msgs = digest_msgs()
        threads = group_threads(msgs)

        assert estimate_summaries(RecordingSummarizer(), cache, threads, msgs).calls == 0
        est = estimate_summaries(
            RecordingSummarizer(), cache, threads, msgs, instructions='Tell me if anyone sounds upset.'
        )
        assert est.calls == 2
        assert est.cached == 0

    def test_counts(self, cache: SummaryCache) -> None:
        cache.store('small@x', 'qwen3:32b', 'from before', ['small@x', 's1@x'], DAY1)
        msgs = digest_msgs() + [post('third@x'), post('t1@x', 'third@x', BOB)]
        est = self.estimate(RecordingSummarizer(), cache, msgs, max_summaries=1)

        assert est.threads == 4
        assert est.not_needed == 1  # lonely@x
        assert est.cached == 1  # small@x
        assert est.over_limit == 1  # third@x, which has fewer messages than busy@x
        assert est.calls == 1  # busy@x

    def test_cut_prompts(self, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        fake.max_input_chars = 600
        msgs = [post('long@x'), post('l1@x', 'long@x', BOB, body='words ' * 500 + '\n')]
        est = self.estimate(fake, cache, msgs)
        self.run(fake, cache, msgs)

        [sent] = fake.prompts
        assert est.cut == 1
        assert est.input_chars == len(sent) <= 600
        # What the model would see without the cut, minus what it gets
        full = thread_prompt('[PATCH] long@x', msgs, max_chars=10**6)
        assert est.cut_chars == len(full) - len(sent)

    def test_nothing_left_to_summarize(self, cache: SummaryCache) -> None:
        msgs = [post('q@x', body='> quote\n'), post('q1@x', 'q@x', BOB, body='> quote\n')]
        est = self.estimate(RecordingSummarizer(), cache, msgs)
        assert (est.not_needed, est.calls) == (1, 0)

    def test_only_quotes_since_the_last_summary(self, cache: SummaryCache) -> None:
        cache.store('busy@x', 'qwen3:32b', 'from before', ['busy@x', 'b1@x'], DAY1)
        msgs = [post('busy@x'), post('b1@x', 'busy@x', BOB), post('b2@x', 'busy@x', BOB, body='> quote\n')]
        est = self.estimate(RecordingSummarizer(), cache, msgs)
        # The real run reuses the old summary without a call
        assert (est.cached, est.calls) == (1, 0)
