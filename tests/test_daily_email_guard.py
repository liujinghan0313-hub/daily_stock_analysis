"""Offline checks for the scheduled email retry guard."""

from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from scripts import daily_email_guard
from src.notification import NotificationChannel, NotificationService


ROOT = Path(__file__).resolve().parents[1]


def _at_utc(hour):
    return datetime(2026, 10, 21, hour, 11, tzinfo=timezone.utc)


def test_early_scheduled_run_is_skipped_without_api_request():
    with mock.patch.object(daily_email_guard, 'delivered_today') as lookup:
        day, skip, reason = daily_email_guard.decision(
            _at_utc(6), 'schedule', 'owner/repo', 'token'
        )
    assert (day, skip) == ('2026-10-21', True)
    assert '报告时段' in reason
    lookup.assert_not_called()


def test_delayed_run_after_china_midnight_is_skipped():
    with mock.patch.object(daily_email_guard, 'delivered_today') as lookup:
        day, skip, _ = daily_email_guard.decision(
            _at_utc(16), 'schedule', 'owner/repo', 'token'
        )
    assert (day, skip) == ('2026-10-22', True)
    lookup.assert_not_called()


def test_manual_run_can_repeat_without_lookup():
    with mock.patch.object(daily_email_guard, 'delivered_today') as lookup:
        day, skip, _ = daily_email_guard.decision(
            _at_utc(10), 'workflow_dispatch', '', ''
        )
    assert (day, skip) == ('2026-10-21', False)
    lookup.assert_not_called()


def test_scheduled_run_skips_only_after_success_marker():
    with mock.patch.object(daily_email_guard, 'delivered_today', side_effect=[False, True]):
        assert daily_email_guard.decision(
            _at_utc(8), 'schedule', 'owner/repo', 'token'
        )[1] is False
        assert daily_email_guard.decision(
            _at_utc(9), 'schedule', 'owner/repo', 'token'
        )[1] is True


def test_artifact_lookup_ignores_expired_markers_and_uses_exact_day():
    payload = {'artifacts': [
        {'name': 'daily-email-sent-2026-10-20', 'expired': False},
        {'name': 'daily-email-sent-2026-10-21', 'expired': True},
    ]}
    response = mock.MagicMock()
    response.__enter__.return_value = BytesIO(json.dumps(payload).encode())
    with mock.patch.object(daily_email_guard, 'urlopen', return_value=response) as request:
        assert daily_email_guard.delivered_today('owner/repo', 'token', '2026-10-21') is False
    assert 'daily-email-sent-2026-10-21' in request.call_args.args[0].full_url
    assert request.call_args.kwargs['timeout'] == 15


def test_artifact_lookup_accepts_current_unexpired_marker():
    payload = {'artifacts': [{'name': 'daily-email-sent-2026-10-21', 'expired': False}]}
    response = mock.MagicMock()
    response.__enter__.return_value = BytesIO(json.dumps(payload).encode())
    with mock.patch.object(daily_email_guard, 'urlopen', return_value=response):
        assert daily_email_guard.delivered_today('owner/repo', 'token', '2026-10-21') is True


def test_lookup_failure_stops_scheduled_run():
    with mock.patch.object(daily_email_guard, 'delivered_today', side_effect=OSError('API down')):
        try:
            daily_email_guard.decision(_at_utc(8), 'schedule', 'owner/repo', 'token')
        except OSError:
            pass
        else:
            raise AssertionError('API failure must prevent sending a duplicate report')


def test_report_email_writes_marker_only_after_success(tmp_path, monkeypatch):
    marker = tmp_path / 'sent.txt'
    monkeypatch.setenv('DAILY_EMAIL_DELIVERY_MARKER', str(marker))
    notifier = NotificationService.__new__(NotificationService)
    notifier._stock_email_groups = []
    notifier._should_use_image_for_channel = lambda *_: False
    notifier.send_to_email = mock.Mock(side_effect=[False, True])
    send = lambda route: notifier._send_to_static_channel(
        NotificationChannel.EMAIL, 'report', image_bytes=None,
        email_stock_codes=None, email_send_to_all=False, route_type=route,
    )
    assert send('report') is False
    assert not marker.exists()
    assert send('alert') is True
    assert not marker.exists()
    notifier.send_to_email = mock.Mock(return_value=True)
    assert send('report') is True
    assert marker.read_text(encoding='utf-8')


def test_report_image_email_also_writes_marker(tmp_path, monkeypatch):
    marker = tmp_path / 'sent.txt'
    monkeypatch.setenv('DAILY_EMAIL_DELIVERY_MARKER', str(marker))
    notifier = NotificationService.__new__(NotificationService)
    notifier._stock_email_groups = []
    notifier._should_use_image_for_channel = lambda *_: True
    notifier._send_email_with_inline_image = mock.Mock(return_value=True)
    assert notifier._send_to_static_channel(
        NotificationChannel.EMAIL, 'report', image_bytes=b'image',
        email_stock_codes=None, email_send_to_all=False, route_type='report',
    ) is True
    assert marker.read_text(encoding='utf-8')


def test_dashboard_email_writes_marker_only_after_success(tmp_path, monkeypatch):
    marker = tmp_path / 'sent.txt'
    monkeypatch.setenv('DAILY_EMAIL_DELIVERY_MARKER', str(marker))
    notifier = NotificationService.__new__(NotificationService)
    notifier._stock_email_groups = []
    notifier._should_use_image_for_channel = lambda *_: False
    notifier.send_to_email = mock.Mock(side_effect=[False, True])
    report = SimpleNamespace(subject='五基金日报', html='<p>报告</p>', images=[])

    def send():
        return notifier._send_to_static_channel(
            NotificationChannel.EMAIL, 'report', image_bytes=None,
            email_stock_codes=None, email_send_to_all=False,
            route_type='report', email_report=report,
        )

    assert send() is False
    assert not marker.exists()
    assert send() is True
    assert marker.read_text(encoding='utf-8')
    assert notifier.send_to_email.call_args.kwargs['html_content'] == report.html


def test_workflow_has_three_postclose_attempts_and_one_delivery_guard():
    data = yaml.safe_load(
        (ROOT / '.github/workflows/00-daily-analysis.yml').read_text(encoding='utf-8')
    )
    workflow_on = data.get('on', data.get(True))
    assert [item['cron'] for item in workflow_on['schedule']] == [
        '10 8 * * 1-5', '10 9 * * 1-5', '10 10 * * 1-5',
    ]
    assert data['permissions']['actions'] == 'read'
    steps = data['jobs']['analyze']['steps']
    assert sum(step.get('id') == 'delivery_guard' for step in steps) == 1
    analyze = next(step for step in steps if step['name'] == '执行股票分析')
    assert 'DAILY_EMAIL_DELIVERY_MARKER' in analyze['env']
    marker_upload = next(step for step in steps if step['name'] == '记录已发送的当日邮件')
    assert marker_upload['with']['if-no-files-found'] == 'ignore'
    assert marker_upload['with']['retention-days'] == 30
    assert any(step['name'] == '上传分析报告' for step in steps)
