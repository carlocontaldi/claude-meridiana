try:
    import fcntl
except ImportError:
    fcntl = None
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import limitwatch as lw  # noqa: E402

UTC = timezone.utc
AT = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


def stamp(at):
    return at.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def limit_row(text="You've hit your 5-hour limit · resets 9:30pm (UTC)", at=None, **extra):
    """A transcript row like the real ones (quotaLimits and apiErrorParams carry the same rate_limit_info)."""
    return {
        'type': 'assistant',
        'timestamp': stamp(at or datetime.now(UTC)),
        'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]},
        'error': 'rate_limit',
        'isApiErrorMessage': True,
        **extra,
    }


def info(kind='five_hour', reset=1794346200, **extra):
    return {'status': 'rejected', 'resetsAt': reset, 'rateLimitType': kind, 'overageStatus': 'rejected', **extra}


class ParseResetTest(unittest.TestCase):
    def test_a_time_later_today_is_today(self):
        self.assertEqual(lw.parse_reset('resets 2:20pm (UTC)', AT), datetime(2026, 10, 20, 14, 20, tzinfo=UTC))

    def test_a_time_already_past_is_tomorrow(self):
        self.assertEqual(lw.parse_reset('resets 9am (UTC)', AT), datetime(2026, 10, 21, 9, 0, tzinfo=UTC))

    def test_noon_and_midnight(self):
        self.assertEqual(lw.parse_reset('resets 12pm (UTC)', AT), datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
        self.assertEqual(lw.parse_reset('resets 12am (UTC)', AT), datetime(2026, 10, 21, 0, 0, tzinfo=UTC))

    def test_a_dated_reset_is_converted_from_its_zone(self):
        self.assertEqual(
            lw.parse_reset('resets Oct 26, 3am (Europe/Lisbon)', AT), datetime(2026, 10, 26, 3, 0, tzinfo=UTC)
        )
        self.assertEqual(
            lw.parse_reset('resets Oct 6, 3am (Europe/Lisbon)', datetime(2026, 10, 1, tzinfo=UTC)),
            datetime(2026, 10, 6, 2, 0, tzinfo=UTC),
        )

    def test_a_dated_reset_without_year_that_has_passed_is_next_year(self):
        at = datetime(2026, 12, 30, 10, 0, tzinfo=UTC)
        self.assertEqual(lw.parse_reset('resets Jan 2, 3am (UTC)', at), datetime(2027, 1, 2, 3, 0, tzinfo=UTC))

    def test_an_explicit_year_is_used(self):
        at = datetime(2026, 12, 30, 10, 0, tzinfo=UTC)
        self.assertEqual(lw.parse_reset('resets Jan 2, 2027, 3am (UTC)', at), datetime(2027, 1, 2, 3, 0, tzinfo=UTC))

    def test_an_unknown_zone_is_none(self):
        self.assertIsNone(lw.parse_reset('resets 2pm (Mars/Olympus)', AT))

    def test_text_without_a_reset_is_none(self):
        self.assertIsNone(lw.parse_reset('API Error: overloaded', AT))


class CapsFromInfoTest(unittest.TestCase):
    def test_each_rate_limit_type_has_its_kind_and_label(self):
        expected = {
            'five_hour': '5-hour limit',
            'seven_day': 'weekly limit',
            'seven_day_opus': 'Opus limit',
            'seven_day_sonnet': 'Sonnet limit',
            'seven_day_overage_included': 'Fable limit',
            'overage': 'usage credit limit',
        }
        for raw, label in expected.items():
            [cap] = lw.caps_from_info(info(raw))
            self.assertEqual((cap.label, cap.reset), (label, datetime.fromtimestamp(1794346200, UTC)), raw)

    def test_a_spend_cap_that_blocks_the_credits_is_a_second_cap(self):
        extra = {'overageDisabledReason': 'org_spend_cap_reached', 'overageResetsAt': 1796000000}
        five_hour, spend = lw.caps_from_info(info(**extra))
        self.assertEqual((five_hour.kind, spend.kind), ('five_hour', 'spend'))
        self.assertEqual(
            (spend.label, spend.reset), ('individual spend limit', datetime.fromtimestamp(1796000000, UTC))
        )

    def test_an_org_wide_disable_is_the_monthly_spend_limit(self):
        extra = {'overageDisabledReason': 'org_level_disabled_until', 'overageResetsAt': 1796000000}
        self.assertEqual([cap.kind for cap in lw.caps_from_info(info(**extra))], ['five_hour', 'monthly'])

    def test_running_out_of_credits_is_not_a_cap_with_a_reset(self):
        caps = lw.caps_from_info(info(overageDisabledReason='out_of_credits'))
        self.assertEqual([cap.kind for cap in caps], ['five_hour'])

    def test_the_credit_limit_itself_takes_its_reset_from_the_overage_time(self):
        [cap] = lw.caps_from_info({'rateLimitType': 'overage', 'overageResetsAt': 1796000000})
        self.assertEqual((cap.kind, cap.reset), ('credits', datetime.fromtimestamp(1796000000, UTC)))

    def test_the_credit_limit_reached_through_a_spend_cap_is_named_as_that_cap_once(self):
        extra = {'overageDisabledReason': 'org_spend_cap_reached', 'overageResetsAt': 1796000000}
        caps = lw.caps_from_info({'rateLimitType': 'overage', **extra})
        self.assertEqual([cap.kind for cap in caps], ['spend'])

    def test_an_unknown_type_keeps_its_name(self):
        [cap] = lw.caps_from_info(info('seven_day_haiku'))
        self.assertEqual(cap.label, 'seven day haiku limit')

    def test_a_missing_or_unreadable_reset_gives_no_cap(self):
        self.assertEqual(lw.caps_from_info({'rateLimitType': 'five_hour'}), [])
        self.assertEqual(lw.caps_from_info(info(reset=10**30)), [])
        self.assertEqual(lw.caps_from_info(info(reset='soon')), [])


def utc_in(**offset):
    return datetime.now(UTC).replace(microsecond=0) + timedelta(**offset)


class SpendKindTest(unittest.TestCase):
    NOW = datetime(2026, 11, 12, 20, 0, tzinfo=UTC)  # a Thursday

    def kind(self, wait, **flags):
        return lw.spend_kind(flags, self.NOW + wait, self.NOW)

    def test_a_reset_more_than_a_week_off_is_monthly(self):
        self.assertEqual(self.kind(timedelta(days=18)), 'monthly')
        self.assertEqual(self.kind(timedelta(days=7, hours=2)), 'monthly')

    def test_a_reset_up_to_a_week_off_is_weekly(self):
        self.assertEqual(self.kind(timedelta(days=7)), 'spend_weekly')
        self.assertEqual(self.kind(timedelta(days=7, minutes=30)), 'spend_weekly')
        self.assertEqual(self.kind(timedelta(days=3)), 'spend_weekly')

    def test_a_reset_at_the_turn_of_the_month_is_monthly_even_when_near(self):
        self.assertEqual(
            lw.spend_kind({}, datetime(2026, 12, 1, tzinfo=UTC), datetime(2026, 11, 29, tzinfo=UTC)), 'monthly'
        )

    def test_a_reset_within_a_day_could_be_a_daily_or_a_weekly_cap_so_it_stays_unnamed(self):
        self.assertEqual(self.kind(timedelta(hours=6)), 'spend')
        self.assertEqual(self.kind(timedelta(days=1)), 'spend')

    def test_the_errors_own_flags_come_first(self):
        self.assertEqual(self.kind(timedelta(days=3), dailyCapPaused=True), 'spend_daily')
        self.assertEqual(self.kind(timedelta(days=3), overagePeriodMonthly={'utilization': 1}), 'monthly')
        self.assertEqual(self.kind(timedelta(days=3), overagePeriodMonthly={'utilization': 0.4}), 'spend_weekly')

    def test_without_the_time_of_the_error_nothing_is_inferred(self):
        self.assertEqual(lw.spend_kind({}, self.NOW + timedelta(days=3), None), 'spend')

    def test_the_period_shows_in_the_label(self):
        self.assertEqual(lw.Cap('spend_weekly', self.NOW).label, 'weekly spend limit')
        self.assertEqual(lw.Cap('spend_daily', self.NOW).label, 'daily spend limit')


class CapFromTextTest(unittest.TestCase):
    def kind(self, text):
        cap = lw.cap_from_text(text, AT)
        return cap and cap.kind

    def test_the_five_hour_wording_is_not_a_generic_usage_limit(self):
        self.assertEqual(self.kind("You've hit your 5-hour limit · resets 9:30pm (UTC)"), 'five_hour')

    def test_the_older_session_and_the_weekly_wordings(self):
        self.assertEqual(self.kind("You've hit your session limit · resets 2:20pm (UTC)"), 'five_hour')
        self.assertEqual(self.kind("You've hit your weekly limit · resets Oct 26, 3am (UTC)"), 'weekly')

    def test_model_limits(self):
        self.assertEqual(self.kind("You've hit your Opus limit · resets 2pm (UTC)"), 'opus')
        self.assertEqual(self.kind("You've hit your Fable limit · resets 2pm (UTC)"), 'fable')

    def test_an_org_spend_limit_message_gives_the_window_it_states(self):
        text = "You've hit your org's monthly spend limit · ask your admin · your session limit resets 7:10pm (UTC)"
        cap = lw.cap_from_text(text, AT)
        self.assertEqual((cap.kind, cap.reset), ('five_hour', datetime(2026, 10, 20, 19, 10, tzinfo=UTC)))

    def test_a_monthly_limit_with_its_own_reset(self):
        self.assertEqual(self.kind("You've hit your monthly spend limit · resets Nov 1, 12am (UTC)"), 'monthly')

    def test_an_individual_spend_limit_with_its_own_reset(self):
        self.assertEqual(self.kind("You've hit your individual spend limit · resets Nov 1, 12am (UTC)"), 'spend')

    def test_a_limit_without_a_name_is_usage(self):
        self.assertEqual(self.kind("You've hit your limit · resets 2pm (UTC)"), 'usage')

    def test_no_clock_time_or_an_unknown_zone_is_none(self):
        self.assertIsNone(lw.cap_from_text('spend limit reached · resets Aug 8', AT))
        self.assertIsNone(lw.cap_from_text('limit · resets 2pm (Mars/Olympus)', AT))


class LimitOfTest(unittest.TestCase):
    def test_a_real_row_reads_from_its_structured_info(self):
        row = limit_row(apiErrorParams={'rate_limit_info': info()}, quotaLimits=info())
        limit = lw.limit_of(row)
        self.assertEqual((limit.window, limit.reset), ('five_hour', datetime.fromtimestamp(1794346200, UTC)))

    def test_structured_info_wins_over_the_text(self):
        row = limit_row("You've hit your weekly limit · resets 3am (UTC)", quotaLimits=info('five_hour'))
        self.assertEqual(lw.limit_of(row).window, 'five_hour')

    def test_a_row_without_structure_falls_back_to_its_text(self):
        self.assertEqual(lw.limit_of(limit_row()).window, 'five_hour')

    def test_the_credit_cap_is_reported_with_the_limit_that_names_it_and_its_period_from_the_reset(self):
        extra = {'overageDisabledReason': 'org_spend_cap_reached', 'overageResetsAt': int(utc_in(days=3).timestamp())}
        limit = lw.limit_of(limit_row(quotaLimits=info(**extra)))
        self.assertEqual([cap.kind for cap in limit.caps], ['five_hour', 'spend_weekly'])

    def test_unreadable_structured_info_falls_back_to_the_text(self):
        bad = info(
            overageDisabledReason='org_spend_cap_reached',
            overageResetsAt=1794400000,
            overagePeriodMonthly={'utilization': '1'},
        )
        self.assertEqual(lw.limit_of(limit_row(quotaLimits=bad)).window, 'five_hour')
        self.assertEqual(lw.limit_of(limit_row(quotaLimits=['not', 'a', 'dict'])).window, 'five_hour')

    def test_other_errors_and_rows_are_not_limits(self):
        self.assertIsNone(lw.limit_of(limit_row('API Error: overloaded')))
        self.assertIsNone(lw.limit_of({'type': 'user', 'timestamp': stamp(AT)}))


class FindLimitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name, 'transcript.jsonl')

    def write(self, *rows):
        self.path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')

    def test_returns_a_fresh_limit_error(self):
        self.write(limit_row())
        self.assertEqual(lw.find_limit(self.path, datetime.now(UTC)).window, 'five_hour')

    def test_the_newest_error_wins(self):
        self.write(limit_row('limit · resets 1pm (UTC)'), limit_row("You've hit your weekly limit · resets 5pm (UTC)"))
        self.assertEqual(lw.find_limit(self.path, datetime.now(UTC)).window, 'weekly')

    def test_ignores_an_error_older_than_five_minutes(self):
        self.write(limit_row(at=datetime.now(UTC) - timedelta(minutes=6)))
        self.assertIsNone(lw.find_limit(self.path, datetime.now(UTC)))

    def test_ignores_errors_that_are_not_limits(self):
        self.write({'type': 'user'}, limit_row('API Error: overloaded'))
        self.assertIsNone(lw.find_limit(self.path, datetime.now(UTC)))

    def test_a_newer_error_that_is_no_limit_makes_the_limit_before_it_stale(self):
        self.write(limit_row(), limit_row('API Error: overloaded'))
        self.assertIsNone(lw.find_limit(self.path, datetime.now(UTC)))

    def test_a_subagents_error_after_the_limit_does_not_hide_it(self):
        self.write(limit_row(), limit_row('API Error: overloaded', isSidechain=True))
        self.assertEqual(lw.find_limit(self.path, datetime.now(UTC)).window, 'five_hour')

    def test_a_missing_transcript_is_none(self):
        self.assertIsNone(lw.find_limit(self.path, datetime.now(UTC)))
        self.assertIsNone(lw.find_limit(None, datetime.now(UTC)))

    def test_lookup_retries_until_the_error_is_written(self):
        with mock.patch.object(lw.time, 'sleep') as sleep:
            self.assertIsNone(lw.lookup(self.path, 3))
        self.assertEqual(sleep.call_count, 2)


class LabelsTest(unittest.TestCase):
    def test_labels_read_as_prose(self):
        five, weekly, spend = (lw.Cap(k, AT) for k in ('five_hour', 'weekly', 'spend'))
        self.assertEqual(lw.describe_caps([five]), '5-hour limit')
        self.assertEqual(lw.describe_caps([five, spend]), '5-hour limit and individual spend limit')
        self.assertEqual(
            lw.describe_caps([five, weekly, spend]), '5-hour limit, weekly limit and individual spend limit'
        )

    def test_a_reset_is_shown_in_utc(self):
        self.assertEqual(lw.format_reset(datetime(2026, 12, 1, 0, 0, tzinfo=UTC)), 'Dec 1, 00:00 UTC')

    def test_a_spend_cap_may_be_waited_for_longer_than_a_window(self):
        for kind in lw.CREDIT_KINDS:
            self.assertGreater(lw.Cap(kind, AT).max_wait, lw.Cap('weekly', AT).max_wait, kind)


class TopicTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name, 'topic')

    def test_a_private_topic_is_made_on_first_use_and_kept(self):
        topic = lw.stored_topic(self.path, 'claude-goon')
        self.assertRegex(topic, r'^claude-goon-[A-Za-z0-9]{22}$')
        self.assertEqual(lw.stored_topic(self.path, 'claude-goon'), topic)
        if os.name != 'nt':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_every_topic_is_one_ntfy_accepts_different_and_has_only_letters_and_digits_after_the_prefix(self):
        made = {lw.stored_topic(self.path, 'claude-portavoce', rotate=True) for _ in range(200)}
        self.assertEqual(len(made), 200)
        self.assertTrue(all(lw.TOPIC.fullmatch(topic) for topic in made))
        self.assertTrue(all(topic.removeprefix('claude-portavoce-').isalnum() for topic in made))

    def test_rotating_replaces_the_saved_topic(self):
        old = lw.stored_topic(self.path, 'claude-goon')
        new = lw.stored_topic(self.path, 'claude-goon', rotate=True)
        self.assertNotEqual(old, new)
        self.assertEqual(lw.read_topic(self.path), new)

    def test_a_file_that_is_not_a_topic_is_replaced(self):
        self.path.write_text('not a topic!\n')
        self.assertRegex(lw.stored_topic(self.path, 'claude-goon'), r'^claude-goon-')

    def test_a_set_option_wins_and_an_empty_one_counts_as_unset(self):
        self.assertEqual(lw.effective_topic(' mine ', self.path, 'claude-goon'), 'mine')
        self.assertFalse(self.path.exists())
        topic = lw.effective_topic('', self.path, 'claude-goon')
        self.assertRegex(topic, r'^claude-goon-')
        self.assertEqual(topic, lw.read_topic(self.path))

    def test_an_unset_option_uses_the_saved_topic(self):
        topic = lw.effective_topic(None, self.path, 'claude-goon')
        self.assertRegex(topic, r'^claude-goon-')
        self.assertEqual(topic, lw.read_topic(self.path))

    def test_the_subscribe_steps_name_the_topic_and_the_test_command(self):
        text = lw.subscribe_steps('claude-goon-abc', '/goon test')
        self.assertIn('enter claude-goon-abc', text)
        self.assertIn('ntfy://ntfy.sh/claude-goon-abc', text)
        self.assertTrue(text.endswith('Then run /goon test.'))


class ClaimTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.marker = Path(tmp.name, '1900000000.lock')

    def test_a_held_marker_cannot_be_claimed_again_until_released(self):
        fd = lw.claim(self.marker)
        self.assertIsNotNone(fd)
        self.assertIsNone(lw.claim(self.marker))
        lw.release(fd, self.marker)
        self.assertFalse(self.marker.exists())
        lw.release(lw.claim(self.marker), self.marker)

    def test_a_marker_left_by_a_dead_watcher_is_taken_over(self):
        self.marker.write_text('999999')
        fd = lw.claim(self.marker)
        self.assertIsNotNone(fd)
        lw.release(fd, self.marker)

    def test_a_marker_replaced_between_open_and_lock_is_not_claimed_by_its_old_inode(self):
        if os.name == 'nt':
            self.skipTest('fcntl replacement tests run on POSIX only')
        real_flock, calls = fcntl.flock, []

        def flock_after_a_release(fd, operation):
            if not calls:
                self.marker.unlink()
                self.marker.touch()
            calls.append(operation)
            return real_flock(fd, operation)

        with mock.patch.object(fcntl, 'flock', flock_after_a_release):
            fd = lw.claim(self.marker)
        self.assertEqual(os.fstat(fd).st_ino, self.marker.stat().st_ino)
        lw.release(fd, self.marker)

    def test_only_one_of_many_simultaneous_claims_over_a_stale_marker_wins(self):
        self.marker.write_text('999999')
        barrier = threading.Barrier(8)
        won = []

        def contend():
            barrier.wait()
            won.append(lw.claim(self.marker))

        threads = [threading.Thread(target=contend) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        winners = [fd for fd in won if fd is not None]
        self.assertEqual(len(winners), 1)
        lw.release(winners[0], self.marker)


class Recorder(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.requests.append((self.path, dict(self.headers), body))
        self.send_response(self.server.status)
        if self.server.truncate:
            self.send_header('Content-Length', '10')
            self.end_headers()
            self.wfile.write(b'abc')
            return
        self.end_headers()

    def log_message(self, *args):
        pass


class PushTest(unittest.TestCase):
    def setUp(self):
        self.server = http.server.HTTPServer(('127.0.0.1', 0), Recorder)
        self.server.requests, self.server.status, self.server.truncate = [], 200, False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_port}/'
        sleeper = mock.patch.object(lw.time, 'sleep')
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.lines = []

    def push(self, topic='secret-topic', text='hello', **kwargs):
        kwargs = {'title': 'T', 'tags': 'tag', 'email': 'me@example.com', 'token': 'tk_secret', **kwargs}
        return lw.push(self.url, topic, text, self.lines.append, **kwargs)

    def test_posts_the_text_with_title_tag_email_and_token(self):
        self.assertTrue(self.push())
        [(path, headers, body)] = self.server.requests
        self.assertEqual(path, '/secret-topic')
        self.assertEqual(
            (headers['Title'], headers['Tags'], headers['Email'], headers['Authorization']),
            ('T', 'tag', 'me@example.com', 'Bearer tk_secret'),
        )
        self.assertEqual(body, b'hello')
        self.assertEqual(self.lines, ['pushed (attempt 1)'])

    def test_the_email_and_the_token_are_optional(self):
        self.push(email='', token='')
        headers = self.server.requests[0][1]
        self.assertNotIn('Email', headers)
        self.assertNotIn('Authorization', headers)

    def test_a_title_with_emoji_travels_as_an_encoded_word_that_reads_back_as_the_title(self):
        title = '🐕‍🦺▶️ Interrupted Claude sessions resumed'
        self.assertTrue(self.push(title=title))
        raw = self.server.requests[0][1]['Title']
        self.assertTrue(raw.isascii())
        self.assertEqual(str(make_header(decode_header(raw))), title)

    def test_the_tags_are_optional(self):
        self.push(tags='')
        self.assertNotIn('Tags', self.server.requests[0][1])

    def test_the_icon_url_travels_as_a_header_only_when_set(self):
        self.push(icon='https://example.com/icon.png')
        self.push()
        with_icon, without_icon = (headers for _, headers, _ in self.server.requests)
        self.assertEqual(with_icon['Icon'], 'https://example.com/icon.png')
        self.assertNotIn('Icon', without_icon)

    def test_an_email_ends_with_the_signature_and_a_push_without_one_does_not(self):
        self.push(signature='Claude Goon')
        self.push(signature='Claude Goon', email='')
        self.push(text='unsigned')
        emailed, phone_only, unsigned = (body for _, _, body in self.server.requests)
        self.assertEqual(emailed, 'hello\n\n— Claude Goon'.encode())
        self.assertEqual(phone_only, b'hello')
        self.assertEqual(unsigned, b'unsigned')

    def test_a_client_error_is_logged_without_the_topic_and_not_retried(self):
        self.server.status = 400
        self.assertFalse(self.push())
        self.assertEqual(len(self.server.requests), 1)
        self.sleep.assert_not_called()
        self.assertEqual(self.lines, ['push failed (attempt 1): HTTP 400'])

    def test_a_server_error_is_retried_three_times_then_given_up(self):
        self.server.status = 503
        self.assertFalse(self.push())
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual(self.lines[-1], 'gave up pushing')

    def test_it_waits_between_attempts_but_not_after_the_last(self):
        self.server.status = 503
        self.push()
        self.assertEqual(self.sleep.call_args_list, [mock.call(lw.PUSH_RETRY_WAIT)] * (lw.PUSH_ATTEMPTS - 1))

    def test_too_many_requests_is_retried(self):
        self.server.status = 429
        self.push()
        self.assertEqual(len(self.server.requests), 3)

    def test_a_response_cut_off_mid_body_is_retried(self):
        self.server.truncate = True
        self.push()
        self.assertEqual(len(self.server.requests), 3)
        self.assertIn('IncompleteRead', self.lines[0])

    def test_a_refused_connection_is_retried(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}/'
        self.push()
        self.assertIn('push failed (attempt 3): URLError', self.lines)

    def test_the_topic_is_url_quoted(self):
        self.push(topic='my topic/x')
        self.assertEqual(self.server.requests[0][0], '/my%20topic%2Fx')

    def test_a_malformed_token_is_neither_logged_nor_retried(self):
        self.push(token='abc\ndef')
        self.assertEqual(self.server.requests, [])
        self.sleep.assert_not_called()
        self.assertEqual(self.lines, ['push failed (attempt 1): ValueError'])


class AnnouncementTest(unittest.TestCase):
    def test_what_reset_and_what_still_holds_is_said_in_one_text(self):
        five, weekly, credits = (lw.Cap(kind, AT) for kind in ('five_hour', 'weekly', 'spend_weekly'))
        self.assertEqual(lw.announcement([five], []), 'Your 5-hour limit has reset. You can continue.')
        self.assertEqual(
            lw.announcement([five], [weekly]),
            'Your 5-hour limit has reset. Still in force: weekly limit (until Oct 20, 10:00 UTC).',
        )
        self.assertEqual(
            lw.announcement([five], [credits]),
            'Your 5-hour limit has reset. You can continue.'
            ' Usage credits stay capped by your weekly spend limit until Oct 20, 10:00 UTC.',
        )


class SharedDirTest(unittest.TestCase):
    def test_the_folder_follows_the_claude_folder_unless_overridden(self):
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': '/cfg'}, clear=True):
            self.assertEqual(lw.shared_dir(), Path('/cfg/meridiana'))
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': '/cfg', 'MERIDIANA_DIR': '/elsewhere'}, clear=True):
            self.assertEqual(lw.shared_dir(), Path('/elsewhere'))
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, 'home', return_value=Path('/home/x')):
            self.assertEqual(lw.shared_dir(), Path('/home/x/.claude/meridiana'))

    def test_two_spellings_of_an_address_share_a_key_and_two_addresses_do_not(self):
        self.assertEqual(lw.email_key(' Me@Example.com '), lw.email_key('me@example.com'))
        self.assertNotEqual(lw.email_key('me@example.com'), lw.email_key('you@example.com'))
        self.assertNotIn('example', lw.email_key('me@example.com'))

    def test_caps_survive_the_note(self):
        caps = [lw.Cap('five_hour', AT), lw.Cap('spend_weekly', AT + timedelta(days=3))]
        self.assertEqual(lw.decode_caps(lw.encode_caps(caps)), caps)


class HandOverTest(unittest.TestCase):
    EMAIL = 'me@example.com'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.lines = []
        self.phone_pushes = []
        self.restored = [lw.Cap('five_hour', AT)]
        self.pending = [lw.Cap('weekly', AT + timedelta(days=3))]
        patch = mock.patch.object(lw, 'HANDOVER_POLL', 0.01)
        patch.start()
        self.addCleanup(patch.stop)

    def offer(self, at=AT, email=EMAIL):
        return lw.Offer(at, email, self.dir)

    def hand_over(self, at=AT, email=EMAIL, wait=10, sending_wait=10):
        return lw.hand_over(
            at,
            self.restored,
            self.pending,
            email,
            self.lines.append,
            on_goon=lambda: self.phone_pushes.append(1),
            directory=self.dir,
            wait=wait,
            sending_wait=sending_wait,
        )

    def find(self, suffix, at=AT, email=EMAIL):
        return lw.find_held(suffix, at, lw.email_key(email), self.dir)

    def in_thread(self, call):
        """Run `call` beside the test, like the other plugin's watcher; its result lands in the list."""
        result = []
        thread = threading.Thread(target=lambda: result.append(call()))
        thread.start()
        self.addCleanup(thread.join, 10)
        return thread, result

    def wait_for(self, condition):
        for _ in range(500):
            if condition():
                return
            threading.Event().wait(0.01)
        self.fail('timed out')

    def test_without_an_offer_nothing_is_handed_over_or_written(self):
        self.assertFalse(self.hand_over())
        self.assertEqual(list(self.dir.rglob('*')), [])

    def test_a_note_goon_sends_is_handed_over(self):
        with self.offer() as offer:
            thread, result = self.in_thread(self.hand_over)
            self.wait_for(offer.note.exists)
            self.assertTrue(offer.claim())
            self.assertEqual(offer.read_note(), (self.restored, self.pending))
            offer.delivered()
        thread.join(10)
        self.assertEqual(result, [True])
        self.assertEqual(self.phone_pushes, [1])

    def test_goon_claiming_before_the_note_arrives_still_covers_it(self):
        with self.offer() as offer:
            self.assertTrue(offer.claim())
            thread, result = self.in_thread(self.hand_over)
            self.wait_for(offer.note.exists)
            offer.delivered()
        thread.join(10)
        self.assertEqual(result, [True])

    def test_a_goon_that_claimed_but_could_not_send_leaves_the_email_to_the_caller(self):
        with self.offer() as offer:
            thread, result = self.in_thread(self.hand_over)
            self.wait_for(offer.note.exists)
            self.assertTrue(offer.claim())
        thread.join(10)
        self.assertEqual(result, [False])
        self.assertIn('Goon could not send the announcement', self.lines)
        self.assertEqual(
            self.phone_pushes, [1]
        )  # the phone was told when Goon claimed; the caller still owes the email

    def test_a_note_goon_leaves_unsent_is_taken_back_so_the_caller_sends_it(self):
        with self.offer() as offer:
            thread, result = self.in_thread(self.hand_over)
            self.wait_for(offer.note.exists)
        thread.join(10)
        self.assertEqual(result, [False])
        self.assertFalse(offer.note.exists())
        self.assertIn('Goon did not send the announcement', self.lines)
        self.assertEqual(self.phone_pushes, [])

    def test_a_goon_that_never_answers_loses_the_email_to_the_caller_for_good(self):
        with self.offer() as offer:
            self.assertFalse(self.hand_over(wait=0.05))
            self.assertFalse(offer.note.exists())
            self.assertFalse(offer.claim())

    def test_a_claim_made_while_the_caller_gives_up_wins(self):
        real = lw.write_new

        def claim_first(path, data):
            if data.get('by') == 'portavoce':
                real(path, {'v': 1, 'by': 'goon'})
            return real(path, data)

        with self.offer() as offer, mock.patch.object(lw, 'write_new', claim_first):
            self.assertTrue(self.hand_over(wait=0, sending_wait=0))
            self.assertEqual(json.loads(offer.verdict.read_text())['by'], 'goon')
        self.assertIn('Goon is still sending; leaving the email to it', self.lines)

    def test_a_caller_that_arrives_after_goon_sent_and_ended_leaves_the_email_to_it(self):
        with self.offer() as offer:
            self.assertTrue(offer.claim())
            offer.delivered()
        self.assertTrue(self.hand_over())
        self.assertNotIn('left the announcement with Goon', self.lines)
        self.assertEqual(self.phone_pushes, [1])

    def test_a_caller_that_arrives_after_goon_failed_to_send_sends_its_own(self):
        with self.offer() as offer:
            self.assertTrue(offer.claim())
        self.assertFalse(self.hand_over())

    def test_an_email_sent_to_another_address_or_at_another_time_is_not_the_callers(self):
        with self.offer(email='you@example.com') as other:
            other.claim()
            other.delivered()
        with self.offer(at=AT + lw.MERGE_WINDOW + timedelta(seconds=1)) as later:
            later.claim()
            later.delivered()
        self.assertFalse(self.hand_over())

    def test_of_many_simultaneous_writers_one_wins_and_its_data_is_what_is_stored(self):
        path = self.dir / 'verdict'
        wins = []
        threads = [
            threading.Thread(target=lambda who=who: lw.write_new(path, {'who': who}) and wins.append(who))
            for who in range(24)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(wins), 1)
        self.assertEqual(json.loads(path.read_text()), {'who': wins[0]})

    def test_goon_waits_for_the_note_of_a_portavoce_that_is_known_to_be_coming(self):
        with self.offer() as offer, lw.Intent(AT, self.EMAIL, self.dir):
            thread, result = self.in_thread(offer.wait_note)
            threading.Event().wait(0.1)
            self.assertEqual(result, [])
            lw.write_new(offer.note, {'v': 1, 'restored': lw.encode_caps(self.restored), 'pending': []})
            thread.join(10)
        self.assertEqual(result, [(self.restored, [])])

    def test_goon_does_not_wait_when_no_portavoce_is_coming(self):
        with self.offer() as offer:
            self.assertIsNone(offer.wait_note(patience=60))

    def test_goon_stops_waiting_when_the_portavoce_that_was_coming_ends_without_a_note(self):
        with self.offer() as offer:
            intent = lw.Intent(AT, self.EMAIL, self.dir).open()
            thread, result = self.in_thread(offer.wait_note)
            threading.Event().wait(0.1)
            intent.close()
            thread.join(10)
        self.assertEqual(result, [None])

    def test_goons_wait_for_a_portavoce_has_a_limit(self):
        with self.offer() as offer, lw.Intent(AT, self.EMAIL, self.dir):
            self.assertIsNone(offer.wait_note(patience=0.05))

    def test_an_intent_for_another_address_or_time_is_not_waited_for(self):
        with self.offer() as offer:
            with lw.Intent(AT, 'you@example.com', self.dir):
                self.assertIsNone(offer.wait_note(patience=60))
            with lw.Intent(AT + lw.MERGE_WINDOW + timedelta(seconds=1), self.EMAIL, self.dir):
                self.assertIsNone(offer.wait_note(patience=60))

    def test_the_intent_goes_when_its_watcher_ends(self):
        with lw.Intent(AT, self.EMAIL, self.dir) as intent:
            self.assertTrue(intent.path.exists())
        self.assertFalse(intent.path.exists())

    def test_an_unusable_shared_folder_gives_no_offer_instead_of_an_error(self):
        blocked = self.dir / 'blocked'
        blocked.write_text('a file where the folder should be')
        lines = []
        self.assertIsNone(lw.try_open(lw.Offer(AT, self.EMAIL, blocked), lines.append))
        self.assertEqual(lines, ['no hand-over: NotADirectoryError'])

    def test_a_shared_folder_that_fails_mid_way_leaves_the_caller_to_send_its_own_email(self):
        with self.offer(), mock.patch.object(lw, 'write_new', side_effect=PermissionError):
            self.assertFalse(self.hand_over())
        self.assertIn('hand-over failed: PermissionError', self.lines)
        self.assertEqual(self.phone_pushes, [])

    def test_a_goon_still_resuming_after_it_claimed_is_waited_for_up_to_the_sending_wait(self):
        with self.offer() as offer:
            offer.claim()
            self.assertTrue(self.hand_over(wait=0, sending_wait=0.05))
        self.assertIn('Goon is still sending; leaving the email to it', self.lines)

    def test_only_one_side_ever_gets_the_email(self):
        with self.offer() as offer:
            self.assertTrue(offer.claim())
            self.assertFalse(offer.claim())

    def test_files_left_by_a_dead_watcher_do_not_decide_for_the_next(self):
        with self.offer() as first:
            first.claim()
            first.delivered()
            lw.write_new(first.note, {'v': 1, 'restored': [], 'pending': []})
        with self.offer() as offer:
            self.assertEqual([offer.note.exists(), offer.sent.exists()], [False, False])
            self.assertTrue(offer.claim())

    def test_a_day_old_file_goes_unless_it_is_the_offer_of_a_live_watcher(self):
        folder = self.dir / 'handover'
        with self.offer() as live:
            old, fresh = folder / '1.sent', folder / '2.sent'
            old.write_text('{}')
            fresh.write_text('{}')
            for path in (old, live.path):
                os.utime(path, (1, 1))
            lw.sweep(folder)
            self.assertEqual([old.exists(), fresh.exists(), live.path.exists()], [False, True, True])

    def test_the_offer_goes_when_its_watcher_ends(self):
        with self.offer() as offer:
            self.assertTrue(offer.path.exists())
        self.assertFalse(offer.path.exists())

    def refused(self, **kwargs):
        """Hand over with a Goon that never answers, and say whether the note was ever left."""
        self.assertFalse(self.hand_over(wait=0.05, **kwargs))
        return 'left the announcement with Goon' not in self.lines

    def test_an_offer_whose_watcher_died_is_ignored(self):
        path = self.dir / 'handover' / f'{int(AT.timestamp())}.offer'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'v': 1, 'at': int(AT.timestamp()), 'email': lw.email_key(self.EMAIL)}))
        self.assertTrue(self.refused())

    def test_an_offer_to_another_address_is_ignored(self):
        with self.offer(email='you@example.com'):
            self.assertTrue(self.refused())

    def test_an_offer_for_a_different_time_is_ignored(self):
        with self.offer(at=AT + lw.MERGE_WINDOW + timedelta(seconds=1)):
            self.assertIsNone(self.find('.offer'))
        with self.offer(at=AT + lw.MERGE_WINDOW):
            self.assertIsNotNone(self.find('.offer'))

    def test_an_offer_of_another_version_is_ignored(self):
        with self.offer() as offer:
            offer_file = json.loads(offer.path.read_text())
            os.ftruncate(offer.fd, 0)
            os.lseek(offer.fd, 0, os.SEEK_SET)
            os.write(offer.fd, json.dumps({**offer_file, 'v': 2}).encode())
            self.assertIsNone(self.find('.offer'))

    def test_a_second_note_is_not_left_beside_one_waiting(self):
        with self.offer() as offer:
            lw.write_new(offer.note, {'v': 1, 'restored': [], 'pending': []})
            self.assertFalse(self.hand_over())
            self.assertTrue(offer.note.exists())

    def test_an_unreadable_note_is_none(self):
        with self.offer() as offer:
            self.assertIsNone(offer.read_note())
            offer.note.write_text('not json')
            self.assertIsNone(offer.read_note())
            offer.note.write_text(json.dumps({'v': 1, 'restored': [], 'pending': []}))
            self.assertIsNone(offer.read_note())
            offer.note.write_text(json.dumps({'v': 1, 'restored': [['five_hour', 'soon']], 'pending': []}))
            self.assertIsNone(offer.read_note())

    def test_a_note_left_by_a_dead_watcher_does_not_reach_the_next_offer(self):
        note = self.dir / 'handover' / f'{int(AT.timestamp())}.note'
        note.parent.mkdir(parents=True)
        lw.write_new(note, {'v': 1, 'restored': lw.encode_caps(self.restored), 'pending': []})
        with self.offer() as offer:
            self.assertIsNone(offer.read_note())


if __name__ == '__main__':
    unittest.main()
