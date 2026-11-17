import fcntl
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_the_icon_url_travels_as_a_header_only_when_set(self):
        self.push(icon='https://example.com/icon.png')
        self.push()
        with_icon, without_icon = (headers for _, headers, _ in self.server.requests)
        self.assertEqual(with_icon['Icon'], 'https://example.com/icon.png')
        self.assertNotIn('Icon', without_icon)

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


if __name__ == '__main__':
    unittest.main()
