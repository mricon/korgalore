"""Tests for rendering digests as text, HTML and email."""

import dataclasses
import html
import os
import time as time_mod
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from html.parser import HTMLParser
from pathlib import Path

import pytest

from korgalore.digest import (
    PATCHES_MAX,
    UPDATES_MAX,
    DigestInfo,
    DigestThread,
    NoSummary,
    Section,
    group_threads,
    mid_url,
    render_digest,
    render_digest_parts,
    render_html,
    render_text,
    roots_to_look_up,
    split_threads,
    summary_points,
)
from tests.digest_helpers import digest_html, digest_text, mkmsg

GOLDEN_DIR = Path(__file__).parent / 'golden'
# Set KGL_UPDATE_GOLDEN=1 to rewrite the golden files after a deliberate change
UPDATE_GOLDEN = os.environ.get('KGL_UPDATE_GOLDEN') == '1'

TZ = timezone(timedelta(hours=2))
RENDER_NOW = datetime(2026, 10, 1, 7, 0, tzinfo=TZ)


def make_info(model: str | None = None, body_format: str = 'both') -> DigestInfo:
    """Digest settings used by most tests."""
    return DigestInfo(
        feed_name='lkml',
        delivery_name='lkml-digest',
        link_base='https://lore.kernel.org/lkml',
        period_start=RENDER_NOW - timedelta(days=1),
        period_end=RENDER_NOW,
        from_addr='korgalore <digest@example.org>',
        body_format=body_format,
        model=model,
        tz=TZ,
    )


@pytest.fixture(scope='module')
def sample() -> list[DigestThread]:
    """One realistic day: a series with reviews, a discussion and an RFC.

    Rendering does not change threads, so all the tests share these.
    """
    msgs = [
        mkmsg('cover@x', '[PATCH v3 0/2] mm: frobnicate the widgets', date='Wed, 30 Sep 2026 09:12:00 +0200'),
        mkmsg('p1@x', '[PATCH v3 1/2] mm: add a tail pointer', irt='cover@x', date='Wed, 30 Sep 2026 09:12:01 +0200'),
        mkmsg('p2@x', '[PATCH v3 2/2] mm: use the tail pointer', irt='cover@x', date='Wed, 30 Sep 2026 09:12:02 +0200'),
        mkmsg(
            'old-r1@x',
            'Re: mm: why is this slow?',
            refs=['slow@x'],
            sender='Carol <carol@example.org>',
            date='Wed, 30 Sep 2026 11:00:00 +0000',
        ),
        mkmsg(
            'rev@x',
            'Re: [PATCH v3 1/2] mm: add a tail pointer',
            body='Looks good.\n\nReviewed-by: Bob Dev <bob@example.org>\n',
            refs=['cover@x', 'p1@x'],
            sender='Bob Dev <bob@example.org>',
            date='Wed, 30 Sep 2026 14:30:00 +0200',
        ),
        mkmsg(
            'troll@x',
            'Re: [PATCH v3 0/2] mm: frobnicate the widgets',
            body='Acked-by: Famous Person <famous@example.org>\n',
            refs=['cover@x'],
            sender='Troll <troll@example.org>',
            date='Wed, 30 Sep 2026 15:00:00 +0200',
        ),
        mkmsg(
            'rfc@x', '[RFC] net: a new idea', sender='Dana <dana@example.org>', date='Wed, 30 Sep 2026 20:00:00 +0200'
        ),
        mkmsg(
            'old-r2@x',
            'Re: mm: why is this slow?',
            refs=['slow@x', 'old-r1@x'],
            sender='P. Author <p@example.org>',
            date='Thu, 01 Oct 2026 06:45:00 +0200',
        ),
    ]
    return group_threads(msgs)


def check_golden(name: str, actual: str) -> None:
    """Compare output with a golden file, or rewrite it when asked to."""
    path = GOLDEN_DIR / name
    if UPDATE_GOLDEN:
        path.parent.mkdir(exist_ok=True)
        path.write_text(actual, encoding='utf-8')
    assert actual == path.read_text(encoding='utf-8')


class _Collector(HTMLParser):
    """Collects tags, attributes and text from HTML, to check escaping."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def parse_html(doc: str) -> _Collector:
    """Parse rendered HTML."""
    collector = _Collector()
    collector.feed(doc)
    collector.close()
    return collector


class TestGolden:
    """Whole-digest output compared with reviewed golden files."""

    def test_plain_text(self, sample: list[DigestThread]) -> None:
        """Text part of a plain digest."""
        check_golden('digest-plain.txt', render_text(make_info(), sample))

    def test_plain_html(self, sample: list[DigestThread]) -> None:
        """HTML part of a plain digest."""
        check_golden('digest-plain.html', render_html(make_info(), sample))

    def test_summarized_text(self, sample: list[DigestThread]) -> None:
        """Text part of a summarized digest: a list, an old summary without markers, and one missing."""
        summaries = {
            'cover@x': (
                '- The series replaces a list walk with a tail pointer, so that adding a widget no longer '
                'takes longer as the list grows.\n'
                '- Bob Dev is happy with patch 1.'
            ),
            'slow@x': 'Carol asks why widget lookup got slower.\nThe author points to the new locking.',
        }
        check_golden('digest-summarized.txt', render_text(make_info('qwen3:32b'), sample, summaries))

    def test_summarized_html(self, sample: list[DigestThread]) -> None:
        """HTML part of a summarized digest: the summary sits in its own box."""
        summaries = {
            'cover@x': '- The series replaces a list walk with a tail pointer.\n- Bob Dev is happy with patch 1.',
        }
        check_golden('digest-summarized.html', render_html(make_info('qwen3:32b'), sample, summaries))


class TestSummaryPoints:
    """Tests for summary_points()."""

    @pytest.mark.parametrize(
        ('summary', 'expected'),
        [
            ('- One.\n- Two.', ['One.', 'Two.']),
            ('* One.\n\u2022 Two.\n3. Three.\n4) Four.', ['One.', 'Two.', 'Three.', 'Four.']),
            ('  - One.  \n\n  - Two.', ['One.', 'Two.']),
            ('- A long point\n  that wraps.\n- Two.', ['A long point that wraps.', 'Two.']),
            ('Saved before lists.\nSecond line.', ['Saved before lists.', 'Second line.']),
            ('- One.\n\nA closing remark.', ['One.', 'A closing remark.']),
            ('An intro:\n- One.', ['An intro:', 'One.']),
            ('-1 is a result, not a point.', ['-1 is a result, not a point.']),
            ('', []),
        ],
    )
    def test_points(self, summary: str, expected: list[str]) -> None:
        """Each marked line starts a point; unmarked lines continue one or stand alone."""
        assert summary_points(summary) == expected


class TestMidUrl:
    """Tests for mid_url()."""

    BASE = 'https://lore.kernel.org/lkml'

    @pytest.mark.parametrize(
        ('base', 'msgid', 'expected'),
        [
            (BASE, '20261001.1234-1-p@example.org', '20261001.1234-1-p@example.org'),
            (BASE, 'a/b@x', 'a%2Fb@x'),
            (BASE, 'a?b#c%d@x', 'a%3Fb%23c%25d@x'),
            (BASE, "a!$&'()*+,;=:~b@x", "a!$&'()*+,;=:~b@x"),
            (BASE, 'a b"c<d>@x', 'a%20b%22c%3Cd%3E@x'),
            (BASE, 'ünï@x', '%C3%BCn%C3%AF@x'),
            # A link base with a trailing slash gives no double slash
            (BASE + '/', 'a@x', 'a@x'),
        ],
    )
    def test_escaping_matches_public_inbox(self, base: str, msgid: str, expected: str) -> None:
        """Message-IDs are escaped the way public-inbox expects."""
        assert mid_url(base, msgid) == f'https://lore.kernel.org/lkml/{expected}/'


EVIL = '<script>alert(1)</script><a href="https://evil.example/">click</a>"\'&'
# A quoted local part is the one way odd characters survive Message-ID
# parsing
EVIL_MSGID = '"x onclick=alert(1) & <b"@x'


@pytest.fixture(scope='module')
def evil() -> _Collector:
    """The parsed HTML of a digest in which everything is hostile."""
    threads = group_threads(
        [
            mkmsg(EVIL_MSGID, f'[PATCH] {EVIL}', sender=formataddr((EVIL, 'e@example.org'))),
            mkmsg('b@x', f'Re: {EVIL} other', irt=EVIL_MSGID, sender='x@example.org'),
        ]
    )
    return parse_html(render_html(make_info('m'), threads, {EVIL_MSGID: EVIL}))


class TestHtmlSafety:
    """Mail content and summaries must never become markup."""

    def test_no_injected_markup(self, evil: _Collector) -> None:
        """Only the tags we write appear, and only links to the archive."""
        tags = {tag for tag, _attrs in evil.tags}
        assert 'script' not in tags
        assert tags <= {
            'html',
            'head',
            'meta',
            'title',
            'body',
            'h1',
            'h2',
            'h3',
            'p',
            'br',
            'div',
            'span',
            'ul',
            'li',
            'a',
        }
        for tag, attrs in evil.tags:
            if tag == 'a':
                assert (attrs['href'] or '').startswith('https://lore.kernel.org/lkml/')

    def test_evil_text_shown_as_text(self, evil: _Collector) -> None:
        """The hostile strings are still visible to the reader, as plain text."""
        text = ''.join(evil.text)
        assert '<script>alert(1)</script>' in text
        assert text.count(EVIL) >= 3  # subject, author, summary

    def test_trailer_and_looked_up_subject_shown_as_text(self) -> None:
        """A review trailer and an archive subject are mail content too."""
        # A quoted display name is the one way "<" survives trailer parsing
        trailer = '"Evil & <b>Co</b>" <rev@example.org>'
        reply = mkmsg(
            'r@x',
            'Re: [PATCH v3 2/7] mm: tail',
            body=f'Looks good.\n\nReviewed-by: {trailer}\n',
            refs=['cover@x', 'p2@x'],
            sender='rev@example.org',
        )
        threads = group_threads([reply], {'cover@x': f'[PATCH v3 0/7] {EVIL}'})
        doc = render_html(make_info(), threads)
        parsed = parse_html(doc)
        tags = {tag for tag, _attrs in parsed.tags}
        assert 'script' not in tags
        assert 'b' not in tags
        text = ''.join(parsed.text)
        assert EVIL in text
        assert f'Reviewed-by: {trailer}' in text

    def test_msgid_in_href_is_quoted(self, evil: _Collector) -> None:
        """A Message-ID with quotes and brackets gives a correct link."""
        hrefs = [attrs['href'] for tag, attrs in evil.tags if tag == 'a']
        assert 'https://lore.kernel.org/lkml/%22x%20onclick=alert(1)%20&%20%3Cb%22@x/' in hrefs


class TestContent:
    """Tests for what the digest shows."""

    def test_link_is_the_only_pointer(self) -> None:
        """No kgl commands: the link already works with kgl yank and kgl track."""
        threads = group_threads([mkmsg('a@x', 'hello')])
        text = render_text(make_info(), threads)
        assert '  Read:  https://lore.kernel.org/lkml/a@x/\n' in text
        assert 'kgl ' not in text
        assert 'kgl ' not in render_html(make_info(), threads)

    @pytest.mark.parametrize('model', [None, 'm'], ids=['plain', 'summarized'])
    def test_only_summaries_are_machine_generated(self, sample: list[DigestThread], model: str | None) -> None:
        """Without a model there is no summary section, and our own notes
        in a summarized digest are never labelled as the model's words."""
        info = make_info(model)
        threads = sample
        assert 'machine-generated' not in render_text(info, threads)
        assert 'machine-generated' not in render_html(info, threads)

    @pytest.mark.parametrize(
        ('summary', 'note', 'count'),
        [
            # A thread without a summary says so
            pytest.param('  ', 'Summary unavailable.', 3, id='missing'),
            # A thread over max_summaries says why it has no summary
            pytest.param(
                NoSummary.BUDGET, 'Summary skipped: this digest reached its max_summaries limit.', 1, id='budget'
            ),
            # A thread that needs no summary gets no summary and no note
            pytest.param(NoSummary.NOT_NEEDED, 'Summary unavailable.', 2, id='not-needed'),
        ],
    )
    def test_summary_notes(self, sample: list[DigestThread], summary: str | NoSummary, note: str, count: int) -> None:
        """What a thread shows in place of a summary it does not have."""
        summaries = {'cover@x': summary}
        assert render_text(make_info('m'), sample, summaries).count(note) == count
        assert render_html(make_info('m'), sample, summaries).count(note) == count

    def test_summary_is_a_list(self, sample: list[DigestThread]) -> None:
        """Each point of a summary is a list item, in both parts."""
        long_point = 'The first point is long enough that it must wrap onto a second line of the text part.'
        summaries = {'cover@x': f'- {long_point}\n- Two & more.'}
        text = render_text(make_info('m'), sample, summaries)
        assert (
            '  Summary (machine-generated):\n'
            '    - The first point is long enough that it must wrap onto a second\n'
            '      line of the text part.\n'
            '    - Two & more.\n'
        ) in text
        doc = render_html(make_info('m'), sample, summaries)
        assert f'<li>{long_point}</li>\n<li>Two &amp; more.</li>\n</ul>' in doc

    def test_summary_wrap_keeps_hyphenated_words(self, sample: list[DigestThread]) -> None:
        """A word like "kernel-mode" or a file name is never split across lines."""
        summaries = {'cover@x': '- Babu Moger proposes adding AMD PLZA support to the resctrl kernel-mode layer.'}
        text = render_text(make_info('m'), sample, summaries)
        assert '    - Babu Moger proposes adding AMD PLZA support to the resctrl\n      kernel-mode layer.\n' in text

    def test_forged_trailer_warning(self, sample: list[DigestThread]) -> None:
        """A trailer sent by someone else names the real sender."""
        expected = 'Acked-by: Famous Person <famous@example.org> (sent by troll@example.org)'
        assert expected in render_text(make_info(), sample)
        doc = render_html(make_info(), sample)
        assert '&#9888; ' + html.escape(expected) in doc

    def test_updates_are_capped(self) -> None:
        """Long threads list the first updates, then "and N more"."""
        extra = 2
        msgs = [mkmsg('root@x', 'busy')]
        msgs += [mkmsg(f'r{i}@x', 'Re: busy', irt='root@x') for i in range(UPDATES_MAX + extra - 1)]
        threads = group_threads(msgs)
        text = render_text(make_info(), threads)
        assert f'    ... and {extra} more' in text
        assert text.count('    Thu 09:12  P. Author') == UPDATES_MAX
        assert f'and {extra} more</li>' in render_html(make_info(), threads)

    def test_times_in_digest_timezone(self) -> None:
        """Update times are shown in the digest's time zone."""
        threads = group_threads([mkmsg('a@x', 'hello', date='Wed, 30 Sep 2026 23:30:00 +0000')])
        assert '    Thu 01:30  P. Author' in render_text(make_info(), threads)

    def test_local_times_follow_daylight_saving(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without a tz, each time is shown in the local zone of its own day.

        Montreal left daylight saving time on 2026-11-01. A digest sent
        after that still shows a Friday 09:00 EDT message as 09:00, not as
        08:00 EST.
        """
        monkeypatch.setenv('TZ', 'America/Montreal')
        time_mod.tzset()
        try:
            msgs = [
                mkmsg('fri@x', 'before the change', date='Fri, 30 Oct 2026 09:00:00 -0400'),
                mkmsg('mon@x', 'after the change', date='Mon, 02 Nov 2026 09:00:00 -0500'),
            ]
            sent = datetime(2026, 11, 2, 12, 0, tzinfo=UTC).astimezone()
            info = dataclasses.replace(make_info(), period_start=sent - timedelta(days=7), period_end=sent, tz=None)
            text = render_text(info, group_threads(msgs))
        finally:
            monkeypatch.undo()
            time_mod.tzset()
        assert '    Fri 09:00  P. Author' in text
        assert '    Mon 09:00  P. Author' in text

    def test_unknown_date(self) -> None:
        """A message without a usable date shows "?"."""
        threads = group_threads([mkmsg('a@x', 'hello', date=None)])
        assert '    ?  P. Author' in render_text(make_info(), threads)

    def test_empty_digest(self) -> None:
        """A digest with no threads says there was no activity."""
        info = make_info()
        assert 'No activity in this period.' in render_text(info, [])
        assert 'No activity in this period.' in render_html(info, [])


def patch_series(order: list[int], total: int, prefix: str = 'PATCH v6', cover: bool = True) -> list[EmailMessage]:
    """A series whose patches arrive in the given order, all at once."""
    width = len(str(total))
    root = 'cover@x' if cover else 'p1@x'
    msgs = [mkmsg('cover@x', f'[{prefix} {0:0{width}d}/{total}] resctrl: kernel mode')] if cover else []
    for num in order:
        irt = None if f'p{num}@x' == root else root
        msgs.append(mkmsg(f'p{num}@x', f'[{prefix} {num:0{width}d}/{total}] resctrl: step {num}', irt=irt))
    return msgs


class TestPatchList:
    """A series is listed once, in order, not as one update per patch."""

    @pytest.mark.parametrize(
        ('msgs', 'expected', 'absent'),
        [
            # The cover letter is the title, so it is not in the patch list
            pytest.param(
                patch_series([1, 3, 2], 3),
                [
                    '  Posted by P. Author, Thu 09:12:\n    1/3  resctrl: step 1\n    2/3  resctrl: step 2\n',
                    '    3/3  resctrl: step 3\n',
                ],
                'kernel mode',
                id='series-order',
            ),
            pytest.param(
                patch_series([10, 2], 18),
                ['    02/18  resctrl: step 2\n    10/18  resctrl: step 10\n'],
                'kernel mode',
                id='counter-width',
            ),
            # Without a cover letter, patch 1 is both the title and in the list
            pytest.param(
                patch_series([1, 2], 2, cover=False),
                ['    1/2  resctrl: step 1\n    2/2  resctrl: step 2\n'],
                '0/2',
                id='no-cover-keeps-first-patch',
            ),
            pytest.param(
                [mkmsg('a@x', '[PATCH] mm: one fix')],
                ['  Posted by P. Author, Thu 09:12\n  Read:'],
                '1/1',
                id='single-patch',
            ),
        ],
    )
    def test_patch_list(self, msgs: list[EmailMessage], expected: list[str], absent: str) -> None:
        text = render_text(make_info(), group_threads(msgs))
        for part in expected:
            assert part in text
        assert absent not in text.split('Posted by')[1]

    def test_no_redundant_updates(self) -> None:
        """The author and time are on the "Posted by" line only."""
        text = render_text(make_info(), group_threads(patch_series([1, 2], 2)))
        assert text.count('P. Author') == 1
        assert 'Follow-ups:' not in text

    def test_replies_stay_in_updates(self) -> None:
        msgs = patch_series([1], 1, cover=False)
        msgs.append(mkmsg('r@x', 'Re: [PATCH v6 1/1] resctrl: step 1', irt='p1@x', sender='Bob <bob@example.org>'))
        text = render_text(make_info(), group_threads(msgs))
        assert '  Follow-ups:\n    Thu 09:12  Bob\n' in text

    def test_other_author(self) -> None:
        """A patch that someone else posts in the thread names them."""
        msgs = patch_series([1, 2], 2)
        msgs.append(mkmsg('fix@x', '[PATCH 3/2] resctrl: fixup', irt='cover@x', sender='Bob <bob@example.org>'))
        threads = group_threads(msgs)
        assert '  resctrl: fixup  (Bob)\n' in render_text(make_info(), threads)
        assert '(Bob)</span></li>' in render_html(make_info(), threads)

    def test_new_version_in_the_thread(self) -> None:
        """A v7 posted as a reply to v6 comes after v6, with its version."""
        msgs = patch_series([1], 2)
        msgs.append(mkmsg('v7@x', '[PATCH v7 1/2] resctrl: step 1', irt='cover@x'))
        msgs.append(mkmsg('late@x', '[PATCH v6 2/2] resctrl: step 2', irt='cover@x'))
        text = render_text(make_info(), group_threads(msgs))
        assert '    1/2     resctrl: step 1\n    2/2     resctrl: step 2\n    v7 1/2  resctrl: step 1\n' in text

    def test_patches_are_capped(self) -> None:
        extra = 2
        total = PATCHES_MAX + extra
        text = render_text(make_info(), group_threads(patch_series(list(range(1, total + 1)), total)))
        assert f'    ... and {extra} more\n' in text
        assert f'  resctrl: step {PATCHES_MAX}\n' in text
        assert f'  resctrl: step {PATCHES_MAX + 1}\n' not in text
        doc = render_html(make_info(), group_threads(patch_series(list(range(1, total + 1)), total)))
        assert f'and {extra} more</li>' in doc
        assert f'resctrl: step {PATCHES_MAX + 1}</li>' not in doc

    def test_patch_in_a_discussion(self) -> None:
        """A patch posted in reply to a question has no number to show."""
        msgs = [
            mkmsg('q@x', 'resctrl: why is this slow?', sender='Carol <carol@example.org>'),
            mkmsg('fix@x', '[PATCH] resctrl: a fix', irt='q@x'),
        ]
        text = render_text(make_info(), group_threads(msgs))
        assert '  Posted by P. Author, Thu 09:12:\n    resctrl: a fix\n  Follow-ups:\n    Thu 09:12  Carol\n' in text

    @pytest.mark.parametrize(
        ('subject', 'line'),
        [
            ('Re: [PATCH v6 02/18] resctrl: step 2', '    Thu 09:12  Bob  on 02/18\n'),
            ('Re: [PATCH v7 02/18] resctrl: step 2', '    Thu 09:12  Bob  on v7 02/18\n'),
            ('Re: [PATCH] resctrl: a fix', '    Thu 09:12  Bob  resctrl: a fix\n'),
            (
                'Re: resctrl: kernel mode (was: something)',
                '    Thu 09:12  Bob  resctrl: kernel mode (was: something)\n',
            ),
            # A reply to the title has no note
            ('Re: [PATCH v6 00/18] resctrl: kernel mode', '    Thu 09:12  Bob\n'),
        ],
        ids=['patch-number', 'other-version', 'single-patch', 'discussion', 'title'],
    )
    def test_reply_says_what_it_answers(self, subject: str, line: str) -> None:
        msgs = patch_series([1], 18)
        msgs.append(mkmsg('r@x', subject, irt='cover@x', sender='Bob <bob@example.org>'))
        assert line in render_text(make_info(), group_threads(msgs))

    def test_html_links_each_patch(self) -> None:
        doc = render_html(make_info(), group_threads(patch_series([2, 1], 2)))
        first = doc.index('<li><a href="https://lore.kernel.org/lkml/p1@x/">1/2</a> resctrl: step 1</li>')
        assert first < doc.index('<li><a href="https://lore.kernel.org/lkml/p2@x/">2/2</a>')
        assert 'Posted by P. Author, <a href="https://lore.kernel.org/lkml/cover@x/">Thu 09:12</a>:' in doc


class TestRenderDigest:
    """Tests for the complete email."""

    def test_structure_and_headers(self, sample: list[DigestThread]) -> None:
        """The digest is multipart/alternative with our headers."""
        msg = render_digest(make_info(), sample, msgid='fixed@example.org', now=RENDER_NOW)
        assert msg.get_content_type() == 'multipart/alternative'
        assert [part.get_content_type() for part in msg.iter_parts()] == ['text/plain', 'text/html']
        assert msg['Subject'] == '[DIGEST] lkml: 2026-10-01 (3 threads, 8 messages)'
        assert msg['From'] == 'korgalore <digest@example.org>'
        assert msg['Message-ID'] == '<fixed@example.org>'
        assert msg['Date'] == 'Thu, 01 Oct 2026 07:00:00 +0200'
        assert msg['X-Korgalore-Digest'] == 'lkml-digest'
        assert msg['X-Korgalore-Digest-Model'] == 'none'

    @pytest.mark.parametrize(
        ('body_format', 'content_type', 'renderer'),
        [('plain', 'text/plain', render_text), ('html', 'text/html', render_html)],
    )
    def test_single_format(
        self,
        sample: list[DigestThread],
        body_format: str,
        content_type: str,
        renderer: Callable[[DigestInfo, list[DigestThread]], str],
    ) -> None:
        """digest_format makes an email with only the part that was asked for."""
        info = make_info(body_format=body_format)
        msg = render_digest(info, sample, msgid='fixed@example.org', now=RENDER_NOW)
        assert not msg.is_multipart()
        assert msg.get_content_type() == content_type
        assert msg.get_content_charset() == 'utf-8'
        assert msg.get_content() == renderer(info, sample)
        # The headers don't depend on the format
        assert msg['Subject'] == '[DIGEST] lkml: 2026-10-01 (3 threads, 8 messages)'
        assert msg['X-Korgalore-Digest'] == 'lkml-digest'

        # A summarized digest names its model, and without a fixed Message-ID
        # one is made with the From domain
        summarized = render_digest(make_info('qwen3:32b'), [], now=RENDER_NOW)
        assert summarized['X-Korgalore-Digest-Model'] == 'qwen3:32b'
        assert str(summarized['Message-ID']).endswith('@example.org>')

    def test_parts_match_renderers(self, sample: list[DigestThread]) -> None:
        """The parts hold exactly what render_text() and render_html() return."""
        info = make_info()
        threads = sample
        msg = render_digest(info, threads, now=RENDER_NOW)
        plain, rich = list(msg.iter_parts())
        assert plain.get_content() == render_text(info, threads)
        assert rich.get_content() == render_html(info, threads)


def many_threads(count: int) -> list[DigestThread]:
    """count new threads with one message each, in order."""
    return group_threads(
        [
            mkmsg(f'm{n}@x', f'[PATCH] thread number {n}', date=f'Thu, 01 Oct 2026 06:{n % 60:02d}:00 +0200')
            for n in range(count)
        ]
    )


def review_of_patch(subject: str = '[PATCH v21 02/15] KVM: arm64: refuse a guest') -> EmailMessage:
    """A review of one patch of a series whose cover letter is older than the period."""
    return mkmsg('review@x', f'Re: {subject}', refs=['cover@x', 'p2@x'], irt='p2@x')


class TestRootSubjects:
    """A continuing thread named after one patch can take its cover letter's subject."""

    @pytest.mark.parametrize(
        ('msgs', 'roots'),
        [
            pytest.param([review_of_patch()], ['cover@x'], id='review-of-a-patch'),
            pytest.param(
                [mkmsg('p2@x', '[PATCH v21 02/15] KVM: arm64: refuse a guest')], [], id='new-thread-has-its-subject'
            ),
            pytest.param([review_of_patch('[PATCH v21 00/15] KVM: arm64: GCS')], [], id='review-of-the-cover'),
            pytest.param([review_of_patch('[PATCH] KVM: arm64: one fix')], [], id='review-of-a-single-patch'),
            pytest.param([review_of_patch('[PATCH 1/1] KVM: arm64: one fix')], [], id='series-of-one'),
            pytest.param([review_of_patch('KVM: arm64: why so slow?')], [], id='discussion'),
        ],
    )
    def test_roots_to_look_up(self, msgs: list[EmailMessage], roots: list[str]) -> None:
        assert roots_to_look_up(group_threads(msgs)) == roots

    def test_root_subject_names_the_thread(self) -> None:
        (thread,) = group_threads([review_of_patch()], {'cover@x': '[PATCH v21 00/15] KVM: arm64: GCS'})

        assert thread.subject == '[PATCH v21 00/15] KVM: arm64: GCS'
        assert thread.section == Section.PATCH_UPDATES
        text = render_text(make_info(), [thread])
        assert '\n[PATCH v21 00/15] KVM: arm64: GCS\n' in text
        # The review now says which patch it is about
        assert 'P. Author  on 02/15\n' in text

    def test_new_thread_keeps_its_subject(self) -> None:
        msgs = [mkmsg('cover@x', '[PATCH v2 0/2] mm: the real cover')]

        (thread,) = group_threads(msgs, {'cover@x': 'something else'})

        assert thread.subject == '[PATCH v2 0/2] mm: the real cover'

    def test_other_roots_are_ignored(self) -> None:
        (thread,) = group_threads([review_of_patch()], {'elsewhere@x': '[PATCH v21 00/15] KVM: arm64: GCS'})

        assert thread.subject == '[PATCH v21 02/15] KVM: arm64: refuse a guest'


class TestSections:
    """Threads are shown as new patches, updates to earlier patches, bug reports and discussions."""

    @pytest.mark.parametrize(
        ('msgs', 'expected'),
        [
            # A new series
            (patch_series([1, 2], 2), Section.NEW_PATCHES),
            # Only the cover letter so far
            ([mkmsg('c@x', '[PATCH 0/2] mm: a series')], Section.NEW_PATCHES),
            # A new pull request
            ([mkmsg('pr@x', '[GIT PULL] mm fixes for 7.3')], Section.NEW_PATCHES),
            # A v2 sent in reply to v1, which is older than the period
            ([mkmsg('v2@x', '[PATCH v2] mm: fix it', refs=['v1@x'])], Section.NEW_PATCHES),
            # A fix posted in an older bug report thread
            ([mkmsg('fix@x', '[PATCH] mm: fix the oops', refs=['bug@x'])], Section.NEW_PATCHES),
            # Only replies to an older series
            ([mkmsg('r@x', 'Re: [PATCH v6 03/18] resctrl: step 3', refs=['c@x', 'p3@x'])], Section.PATCH_UPDATES),
            # Only replies to an older pull request
            ([mkmsg('r@x', 'Re: [GIT PULL] mm fixes for 7.3', refs=['pr@x'])], Section.PATCH_UPDATES),
            # A new question, and replies to an old one
            ([mkmsg('q@x', 'mm: why is this slow?')], Section.DISCUSSIONS),
            ([mkmsg('r@x', 'Re: mm: why is this slow?', refs=['q@x'])], Section.DISCUSSIONS),
            # A new bug report, and replies to an old one
            ([mkmsg('b@x', '[BUG] mm: oops in frob()')], Section.BUG_REPORTS),
            ([mkmsg('r@x', 'Re: [BUG] mm: oops in frob()', refs=['b@x'])], Section.BUG_REPORTS),
            # A fix posted in a bug report thread is still a new patch
            (
                [mkmsg('b@x', '[BUG] mm: oops'), mkmsg('f@x', '[PATCH] mm: fix the oops', irt='b@x')],
                Section.NEW_PATCHES,
            ),
            # Patches and pull requests that mention a bug stay with the patches
            ([mkmsg('p@x', '[PATCH] mm: fix a bug in frob()')], Section.NEW_PATCHES),
            ([mkmsg('r@x', 'Re: [PATCH] mm: fix a bug', refs=['p@x'])], Section.PATCH_UPDATES),
            ([mkmsg('pr@x', '[GIT PULL] mm bug fixes for 7.3')], Section.NEW_PATCHES),
        ],
    )
    def test_section(self, msgs: list[EmailMessage], expected: Section) -> None:
        """Each kind of thread goes in its section."""
        (thread,) = group_threads(msgs)
        assert thread.section is expected

    def test_bug_reports_before_discussions(self) -> None:
        """Bug reports come after the patch updates and before the discussions."""
        msgs = [mkmsg('q@x', 'mm: why is this slow?')]
        msgs += [mkmsg('b@x', '[BUG] mm: oops in frob()')]
        msgs += [mkmsg('r@x', 'Re: [PATCH] mm: old fix', refs=['old@x'])]
        threads = group_threads(msgs)
        assert [thread.root_msgid for thread in threads] == ['old@x', 'b@x', 'q@x']
        text = render_text(make_info(), threads)
        assert text.count('=' * 72 + '\nBUG REPORTS (1)\n' + '=' * 72 + '\n[BUG] mm: oops') == 1
        assert '>Bug reports (1)</h2>' in render_html(make_info(), threads)

    def test_sections_come_first_in_the_order(self) -> None:
        """A busy discussion still comes after a quiet new patch."""
        msgs = [mkmsg('q@x', 'mm: why is this slow?')]
        msgs += [mkmsg(f'r{n}@x', 'Re: mm: why is this slow?', irt='q@x') for n in range(5)]
        msgs += [mkmsg('r@x', 'Re: [PATCH] mm: old fix', refs=['old@x'])]
        msgs += [mkmsg('new@x', '[PATCH] mm: new fix')]
        threads = group_threads(msgs)
        assert [thread.root_msgid for thread in threads] == ['new@x', 'old@x', 'q@x']

    def test_headings_count_the_section(self, sample: list[DigestThread]) -> None:
        """Each section that has threads gets one heading, with its count."""
        text = render_text(make_info(), sample)
        assert text.count('=' * 72 + '\nNEW PATCHES AND PULL REQUESTS (2)\n' + '=' * 72 + '\n[PATCH v3') == 1
        assert text.count('=' * 72 + '\nDISCUSSIONS (1)\n' + '=' * 72 + '\nmm: why') == 1
        assert 'UPDATES TO EARLIER PATCHES' not in text
        doc = render_html(make_info(), sample)
        headings = [tag for tag, _attrs in parse_html(doc).tags if tag == 'h2']
        assert len(headings) == 2
        assert '>New patches and pull requests (2)</h2>' in doc
        assert '>Discussions (1)</h2>' in doc

    def test_part_in_the_middle_of_a_section(self) -> None:
        """A part that starts inside a section repeats its heading, marked as continued."""
        info = make_info()
        threads = group_threads([mkmsg('a@x', '[PATCH] one'), mkmsg('b@x', '[PATCH] two'), mkmsg('q@x', 'a question')])
        # A cap of 1 byte puts each thread in a part of its own
        first, second, third = render_digest_parts(info, threads, now=RENDER_NOW, max_size=1)
        assert '\nNEW PATCHES AND PULL REQUESTS (2)\n' in digest_text(first)
        assert '\nNEW PATCHES AND PULL REQUESTS (2), CONTINUED\n' in digest_text(second)
        assert '>New patches and pull requests (2), continued</h2>' in digest_html(second)
        assert '>Discussions (1)</h2>' in digest_html(third)
        assert 'continued' not in digest_text(third)

    def test_parts_with_headings_stay_under_the_cap(self) -> None:
        """The cap leaves room for section headings."""
        info = make_info()
        msgs = [mkmsg(f'p{n}@x', f'[PATCH] patch number {n}') for n in range(20)]
        msgs += [mkmsg(f'q{n}@x', f'question number {n}') for n in range(20)]
        threads = group_threads(msgs)
        for msg in render_digest_parts(info, threads, now=RENDER_NOW, max_size=3000):
            assert len(digest_html(msg).encode()) <= 3000


Thirty = tuple[list[DigestThread], list[EmailMessage], list[list[DigestThread]]]


@pytest.fixture(scope='module')
def thirty() -> Thirty:
    """30 threads in parts of at most 4000 bytes: (threads, parts, chunks)."""
    threads = many_threads(30)
    parts = render_digest_parts(make_info(), threads, now=RENDER_NOW, max_size=4000)
    return threads, parts, split_threads(make_info(), threads, max_size=4000)


class TestSplit:
    """Big digests are split into numbered parts."""

    def test_parts_stay_under_the_cap(self) -> None:
        info = make_info()
        threads = many_threads(40)
        chunks = split_threads(info, threads, max_size=4000)
        assert len(chunks) > 2
        # Nothing is lost or reordered
        assert [t for chunk in chunks for t in chunk] == threads
        for msg in render_digest_parts(info, threads, now=RENDER_NOW, max_size=4000):
            assert len(digest_html(msg).encode()) <= 4000

    def test_plain_parts_measure_the_text(self) -> None:
        """A plain-only digest is split by the size of its text, which is smaller."""
        threads = many_threads(40)
        plain = make_info(body_format='plain')
        chunks = split_threads(plain, threads, max_size=4000)
        assert len(chunks) < len(split_threads(make_info(), threads, max_size=4000))
        assert [t for chunk in chunks for t in chunk] == threads
        parts = render_digest_parts(plain, threads, now=RENDER_NOW, max_size=4000)
        assert len(parts) == len(chunks)
        for msg in parts:
            assert msg.get_content_type() == 'text/plain'
            assert len(digest_text(msg).encode()) <= 4000

    def test_html_parts_stay_under_the_cap(self) -> None:
        threads = many_threads(40)
        for msg in render_digest_parts(make_info(body_format='html'), threads, now=RENDER_NOW, max_size=4000):
            assert msg.get_content_type() == 'text/html'
            assert len(digest_html(msg).encode()) <= 4000

    def test_big_thread_gets_its_own_part(self) -> None:
        """A thread is never split, even when it is bigger than the cap."""
        info = make_info()
        threads = many_threads(3)
        chunks = split_threads(info, threads, max_size=1)
        assert chunks == [[thread] for thread in threads]

    def test_one_part_is_a_normal_digest(self, sample: list[DigestThread]) -> None:
        """When it fits, the digest looks exactly like render_digest() makes it."""
        info = make_info()
        threads = sample
        (msg,) = render_digest_parts(info, threads, now=RENDER_NOW)
        assert msg['Subject'] == '[DIGEST] lkml: 2026-10-01 (3 threads, 8 messages)'
        assert msg['X-Korgalore-Digest-Part'] is None
        assert digest_html(msg) == render_html(info, threads)

    def test_part_headers(self, thirty: Thirty) -> None:
        _, parts, _ = thirty
        total = len(parts)
        first_msgid = parts[0]['Message-ID']
        assert parts[0]['In-Reply-To'] is None
        for number, msg in enumerate(parts, start=1):
            # Every part shows the totals of the whole digest
            assert msg['Subject'] == f'[DIGEST {number}/{total}] lkml: 2026-10-01 (30 threads, 30 messages)'
            assert msg['X-Korgalore-Digest-Part'] == f'{number}/{total}'
            if number > 1:
                assert msg['In-Reply-To'] == first_msgid
                assert msg['References'] == first_msgid
        assert len({msg['Message-ID'] for msg in parts}) == total

    def test_part_labels(self, thirty: Thirty) -> None:
        threads, parts, chunks = thirty
        first = 1
        for number, (msg, chunk) in enumerate(zip(parts, chunks, strict=True), start=1):
            text = digest_text(msg)
            label = f'Part {number} of {len(parts)}: threads {first}-{first + len(chunk) - 1} of 30'
            assert label in text
            assert label in digest_html(msg)
            assert ('The other parts are replies to this one.' in text) == (number == 1)
            # Each part lists only its own threads
            for thread in threads:
                assert (f'/lkml/{thread.root_msgid}/' in text) == (thread in chunk)
            first += len(chunk)


class TestGapNotice:
    """When the local history has a gap, the digest says so."""

    @staticmethod
    def gap_info() -> DigestInfo:
        return dataclasses.replace(make_info(), history_start=RENDER_NOW - timedelta(hours=6))

    @pytest.mark.parametrize(
        ('gap', 'empty'),
        [
            pytest.param(False, False, id='no-gap'),
            # Even with no threads, missing messages are worth telling about
            pytest.param(True, True, id='gap-in-empty-digest'),
        ],
    )
    def test_notice(self, sample: list[DigestThread], gap: bool, empty: bool) -> None:
        info = self.gap_info() if gap else make_info()
        msg = render_digest(info, [] if empty else sample, now=RENDER_NOW)
        assert ('Some messages are missing' in digest_text(msg)) is gap
        assert ('Some messages are missing' in digest_html(msg)) is gap
        assert ('No activity in this period.' in digest_text(msg)) is empty

    def test_notice_text(self, sample: list[DigestThread]) -> None:
        msg = render_digest(self.gap_info(), sample, now=RENDER_NOW)
        text = ' '.join(digest_text(msg).split())
        assert (
            'Some messages are missing. Korgalore only has messages from 2026-10-01 01:00 on, '
            'so anything that arrived between 2026-09-30 07:00 and then is not in this digest. '
            'You can find it in the archive: https://lore.kernel.org/lkml/'
        ) in text
        assert '&#9888; Some messages are missing.' in digest_html(msg)
        # The notice is wrapped, and the archive link is never broken in two
        assert max(len(line) for line in digest_text(msg).splitlines() if 'missing' in line) <= 72
        assert 'https://lore.kernel.org/lkml/' in digest_text(msg).split()

    def test_notice_only_in_first_part(self) -> None:
        parts = render_digest_parts(self.gap_info(), many_threads(30), now=RENDER_NOW, max_size=4000)
        assert len(parts) > 1
        seen = ['Some messages are missing' in digest_text(msg) for msg in parts]
        assert seen == [True] + [False] * (len(parts) - 1)
