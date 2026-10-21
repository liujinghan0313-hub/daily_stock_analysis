"""Deterministic financial correctness, chart isolation and SMTP MIME contracts."""
from copy import deepcopy
from datetime import date, timedelta
from email import message_from_bytes
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import re
import warnings

import pandas as pd
import pytest

from scripts.demo_fund_dashboard import SampleFundProvider
from src.services.fund_dashboard import (
    load_dashboard_config, build_dashboard, actual_allocation, comparison_period,
    FundObservation, collect_official_events,
)
from src.reports.fund_charts import render_charts, percent, chinese_font, Chart
from src.reports.fund_dashboard_html import render_dashboard, standalone_html, contribution_summary
from src.notification_sender.email_sender import build_email_body, EmailSender

TODAY = date(2026, 10, 20)


@pytest.fixture(scope='module')
def settings():
    return load_dashboard_config()


@pytest.fixture(scope='module')
def data(settings):
    return build_dashboard(SampleFundProvider(), TODAY, deepcopy(settings), sample_label='合成历史测试样本，非实时数据')


@pytest.fixture(scope='module')
def assets(data):
    with warnings.catch_warnings(record=True) as captured:
        charts, errors = render_charts(data)
    assert not any('Glyph' in str(w.message) and 'missing' in str(w.message) for w in captured)
    assert not errors
    return charts


def test_default_mapping_and_weights(settings):
    assert [(f['otc'], f['code']) for f in settings['funds']] == [
        ('022430', '512050'), ('020602', '563020'), ('006965', '006965'),
        ('007364', '007364'), ('008701', '518850')]
    assert [f['target_pct'] for f in settings['funds']] == [25, 15, 35, 20, 5]
    assert settings['holdings'] == {} and settings['contributions'] == []


@pytest.mark.parametrize('value', [-1, 101, float('nan'), True])
def test_invalid_weight_rejected(settings, tmp_path, value):
    bad=deepcopy(settings);bad['funds'][0]['target_pct']=value
    p=tmp_path/'config.json';p.write_text(json.dumps(bad))
    with pytest.raises(ValueError): load_dashboard_config(p)


def test_private_json_config_precedence(settings, tmp_path, monkeypatch):
    private=deepcopy(settings);private['monthly_contribution_cny']=1000
    monkeypatch.setenv('FUND_DASHBOARD_CONFIG_JSON', json.dumps(private))
    assert load_dashboard_config()['monthly_contribution_cny']==1000
    p=tmp_path/'config.json';p.write_text(json.dumps(settings))
    assert load_dashboard_config(p)['monthly_contribution_cny'] is None


def test_same_dates_normalization_total_return(data):
    rows=data.periods['risk']
    assert len(rows)==5
    assert len({(r['start'],r['end']) for r in rows.values()})==1
    assert all(r['index'].iloc[0]==100 for r in data.trend.values())
    for row in rows.values():
        assert row['return']==pytest.approx(row['index'].iloc[-1]/100-1)
        assert row['drawdown']==pytest.approx((row['index']/row['index'].cummax()-1).min())
        assert row['drawdown']<=0
        assert '再投资' in row['basis']
    # A cash dividend is not a loss; use reinvested NAV rather than NAV difference.
    provider=SampleFundProvider();item=data.observations[0]
    unit,_=provider.values('022430')
    n=list(provider.dates).index(provider.ex_date)
    assert item.total.iloc[n]/item.total.iloc[n-1] == pytest.approx((unit[n]+.02)/unit[n-1])


def test_zero_drawdown_is_valid(data):
    original=data.observations[0]
    constant=FundObservation(original.definition, total=original.total*0+1)
    result,_=comparison_period([constant],90,TODAY)
    assert result['022430']['return']==0
    assert result['022430']['drawdown']==0
    assert percent(0)=='0.00%'
    assert percent(None)=='暂无可靠数据'


def test_nontrading_dates_no_fill(data):
    provider=SampleFundProvider()
    allowed=set(provider.dates)
    for row in data.trend.values():
        assert set(row['index'].index)<=allowed
        assert date(2026,10,18) not in row['index'].index
        assert row['end']==date(2026,10,16)
    assert all(i.nav_date!=data.report_date for i in data.observations)


def test_missing_trade_day_excluded(data):
    item=deepcopy(data.observations[0]);item.total=item.total.drop(item.total.index[-20])
    result,excluded=comparison_period([item],90,TODAY)
    assert not result and '缺少交易日' in excluded['022430']


def test_stale_data_excluded(data):
    item=deepcopy(data.observations[0]);item.total=item.total.iloc[:-20]
    result,excluded=comparison_period([item],90,TODAY)
    assert not result and '过旧' in excluded['022430']


def test_dividend_interface_failure_stops_only_one_fund(settings):
    degraded=build_dashboard(SampleFundProvider(True),TODAY,settings)
    item=next(i for i in degraded.observations if i.definition['otc']=='007364')
    assert item.status=='需修复' and item.nav is not None and item.total is None
    assert '007364' not in degraded.trend and '007364' not in degraded.periods['risk']
    assert len(degraded.trend)==4
    text=render_dashboard(degraded,{}).text
    assert '分红' in text and '含分红总回报' in text
    assert 'Traceback' not in text and 'fixture only' not in text


def test_manager_conflict_disables_nav_and_returns(settings):
    provider=SampleFundProvider();manager=provider.manager
    def conflict(code):
        frame=manager(code)
        if code=='007364': frame['单位净值']+=.1
        return frame
    provider.manager=conflict
    result=build_dashboard(provider,TODAY,settings)
    item=next(i for i in result.observations if i.definition['otc']=='007364')
    assert item.nav is None and item.total is None and item.status=='需修复'
    assert len(result.trend)==4


def test_etf_fallback_label_only_bars(settings):
    provider=SampleFundProvider();fund=provider.fund
    def missing(code,indicator):
        if code=='022430': return pd.DataFrame()
        return fund(code,indicator)
    provider.fund=missing
    data=build_dashboard(provider,TODAY,settings)
    assert data.periods[30]['022430']['substitute']
    assert 'ETF替代' in data.periods[30]['022430']['basis']
    assert '022430' not in data.trend and '022430' not in data.periods['risk']
    report=render_dashboard(data,{})
    assert '不是场外基金实际收益' in report.html


def test_contribution_is_not_position(settings,data):
    data=deepcopy(data)
    amounts=dict(zip([f['otc'] for f in settings['funds']],[100,200,300,400,500]))
    data.config['contributions']=[{'date':'2026-10-16','actual_total_cny':1500,'actual_by_fund_cny':amounts}]
    assert actual_allocation(data.config,TODAY)==({},'待接入持仓数据')
    summary=contribution_summary(data)
    assert '1,500.00' in summary and '2026-10-16' in summary and '不是当前持仓市值' in summary
    data.report_date=date(2026,10,15)
    assert '1,500' not in contribution_summary(data)


@pytest.mark.parametrize('problem', ['missing','negative','future','different_dates','currency','nan','zero'])
def test_invalid_holdings_never_fabricate_weights(settings,problem):
    cfg=deepcopy(settings)
    cfg['holdings']={f['otc']:{'as_of':'2026-10-19','market_value_cny':1000} for f in cfg['funds']}
    first=cfg['holdings']['022430']
    if problem=='missing':cfg['holdings'].pop('008701')
    if problem=='negative':first['market_value_cny']=-1
    if problem=='future':first['as_of']='2026-10-21'
    if problem=='different_dates':first['as_of']='2026-10-16'
    if problem=='currency':first['currency']='USD'
    if problem=='nan':first['market_value_cny']=float('nan')
    if problem=='zero':
        for h in cfg['holdings'].values():h['market_value_cny']=0
    weights,note=actual_allocation(cfg,TODAY)
    assert weights=={} and '待接入' in note


def test_actual_weights_deviation_and_no_auto_trade(settings,data):
    data=deepcopy(data)
    data.config['holdings']={f['otc']:{'as_of':'2026-10-19','market_value_cny':1000} for f in settings['funds']}
    weights,_=actual_allocation(data.config,TODAY)
    assert weights=={f['otc']:20 for f in settings['funds']}
    report=render_dashboard(data,{})
    assert '偏离-15.00个百分点' in report.html and '超过观察阈值' in report.html


def test_font_and_six_chart_types(assets):
    assert chinese_font()
    assert set(assets)=={'returns-7','returns-30','returns-90','trend','risk','scatter','allocation','quality'}
    assert all(c.png.startswith(b'\x89PNG\r\n\x1a\n') and c.alt and c.explanation for c in assets.values())
    assert sum(len(c.png) for c in assets.values())<2_000_000


def test_chart_failure_isolated(data):
    from src.reports import fund_charts
    real=fund_charts._png
    calls=0
    def broken(fig):
        nonlocal calls
        calls+=1
        if calls==1:raise RuntimeError('chart fixture')
        return real(fig)
    with patch.object(fund_charts,'_png',side_effect=broken):
        charts,errors=render_charts(data)
    assert len(charts)==7 and set(errors)=={'returns-7'}


def test_font_failure_preserves_text(data):
    charts,errors=render_charts(data,'/missing/fixture-font.ttf')
    assert charts=={} and 'all' in errors
    result=render_dashboard(data,charts,errors)
    assert not result.images and '图表环境或中文字体不可用' in result.html
    assert all(i.definition['otc'] in result.text for i in data.observations)


def test_mime_related_plain_html_and_cid(data,assets):
    report=render_dashboard(data,assets)
    assert len(report.images)==3
    msg=message_from_bytes(build_email_body(report.text,report.html,report.images).as_bytes())
    assert msg.get_content_type()=='multipart/related'
    assert msg.get_payload()[0].get_content_type()=='multipart/alternative'
    html=next(p for p in msg.walk() if p.get_content_type()=='text/html').get_payload(decode=True).decode()
    refs=set(re.findall('cid:([A-Za-z0-9_-]+)',html))
    ids={p['Content-ID'].strip('<>') for p in msg.walk() if p.get_content_type()=='image/png'}
    assert refs==ids=={'returns-30','trend','allocation'}
    assert next(p for p in msg.walk() if p.get_content_type()=='text/plain').get_payload(decode=True).decode()==report.text.strip()
    assert '<script' not in html and not re.search(r'<img[^>]+src="https?://',html)
    assert 'cid:' not in standalone_html(report) and 'data:image/png;base64,' in standalone_html(report)
    assert len(render_dashboard(data,assets,detail=True).images)==8


def test_invalid_duplicate_images_removed_without_broken_reference(assets):
    chart=assets['trend'];bad=Chart('broken','坏图',b'bad','坏图','')
    message=build_email_body('text','<img src="cid:trend"><img src="cid:broken">',[chart,chart,bad])
    images=[p for p in message.walk() if p.get_content_type()=='image/png']
    html=next(p for p in message.walk() if p.get_content_type()=='text/html').get_payload(decode=True).decode()
    assert len(images)==1 and 'cid:trend' in html and 'cid:broken' not in html


def test_qq_smtp_ssl_exactly_one_message(data,assets):
    report=render_dashboard(data,assets)
    cfg=SimpleNamespace(email_sender='fixture@qq.com',email_password='test-only-not-a-secret',email_receivers=['receiver@example.test'])
    sender=EmailSender(cfg)
    with patch('src.notification_sender.email_sender.smtplib.SMTP_SSL') as smtp:
        assert sender.send_to_email(report.text,report.subject,html_content=report.html,inline_images=report.images)
    smtp.assert_called_once_with('smtp.qq.com',465,timeout=30)
    smtp.return_value.send_message.assert_called_once()
    smtp.return_value.quit.assert_called_once()


def test_pipeline_html_once_without_stock_signal(data,assets):
    from src.core.fund_pipeline import run_fund_analysis
    cfg=SimpleNamespace(bond_fund_list=['006965','007364'],gold_etf_list=['518850'],fund_report_format='html')
    args=SimpleNamespace(no_notify=False,dry_run=False)
    pipeline=SimpleNamespace(notifier=Mock(),run=Mock())
    report=render_dashboard(data,assets)
    with patch('src.core.fund_dashboard_pipeline.prepare_fund_dashboard',return_value=report):
        run_fund_analysis(cfg,args,pipeline,['512050','563020'],provider=SampleFundProvider(),today=TODAY)
    pipeline.run.assert_not_called()
    pipeline.notifier.send.assert_called_once_with(report.text,email_send_to_all=True,route_type='report',email_report=report)


def test_failed_delivery_never_resends_legacy(data,assets):
    from src.core.fund_pipeline import run_fund_analysis
    cfg=SimpleNamespace(bond_fund_list=['006965','007364'],gold_etf_list=['518850'],fund_report_format='html')
    pipeline=SimpleNamespace(notifier=Mock(),run=Mock())
    pipeline.notifier.send.return_value=False
    with patch('src.core.fund_dashboard_pipeline.prepare_fund_dashboard',return_value=render_dashboard(data,assets)):
        assert not run_fund_analysis(cfg,SimpleNamespace(no_notify=False,dry_run=False),pipeline,['512050','563020'],provider=SampleFundProvider(),today=TODAY)
    pipeline.notifier.send.assert_called_once();pipeline.run.assert_not_called()


def test_only_dated_official_news(data):
    data=deepcopy(data)
    def event(url,day):return SimpleNamespace(url=url,published_date=day,title='公告',source='管理人')
    service=SimpleNamespace(is_available=True,search_stock_news=Mock(return_value=SimpleNamespace(success=True,results=[
        event('https://www.efunds.com.cn/notice','2026-10-19'),
        event('https://efunds.com.cn.attacker.test/notice','2026-10-19'),
        event('javascript://efunds.com.cn/notice','2026-10-19'),
        event('https://www.efunds.com.cn/future','2026-10-21'),
        event('https://www.efunds.com.cn/no-date',None)])))
    collect_official_events(service,data)
    assert len(data.events)==1 and data.events[0]['url']=='https://www.efunds.com.cn/notice'


@pytest.mark.parametrize('period', ['weekly','monthly'])
def test_week_month_extension_no_fabricated_holdings(settings,period):
    data=build_dashboard(SampleFundProvider(),TODAY,settings,period=period)
    report=render_dashboard(data,{})
    assert ('近7自然日' if period=='weekly' else '近30自然日') in report.html
    assert '待接入持仓数据' in report.html


def test_swapped_etf_mapping_rejected(settings,tmp_path):
    cfg=deepcopy(settings);cfg['funds'][0]['code']='518850';cfg['funds'][-1]['code']='512050'
    path=tmp_path/'swapped.json';path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError,match='映射'):load_dashboard_config(path)


def test_unconfigured_targets_not_imputed(data):
    data=deepcopy(data);data.config['funds'][1]['target_pct']=None
    data.config['funds'][-1]['target_pct']=None
    assert actual_allocation(data.config,TODAY)[0]=={}
    assert '红利低波未配置' in render_dashboard(data,{}).text
    assert sum(f.get('target_pct') or 0 for f in data.config['funds'])==80


def test_artifacts_preparation_real_computation(settings,tmp_path,monkeypatch):
    from src.core import fund_dashboard_pipeline as module
    monkeypatch.setattr(module,'__file__',str(tmp_path/'src/core/fund_dashboard_pipeline.py'))
    monkeypatch.delenv('FUND_DASHBOARD_CONFIG_JSON',raising=False)
    path=tmp_path/'settings.json';path.write_text(json.dumps(settings))
    cfg=SimpleNamespace(fund_dashboard_config=str(path),fund_chart_font='/no-font.ttf',fund_report_period='daily',bond_fund_list=['006965','007364'],gold_etf_list=['518850'])
    with patch.object(module,'analyze_macro',return_value=[]):
        report=module.prepare_fund_dashboard(cfg,SampleFundProvider(),TODAY,['512050','563020'],SimpleNamespace(dry_run=True))
    assert all(f['otc'] in report.text for f in settings['funds'])
    assert len(list((tmp_path/'reports').glob('*.html')))==2
    assert len(list((tmp_path/'reports').glob('*.eml')))==1
    assert '20' in report.html and '买卖评分' not in report.html


def test_workflow_preserves_schedule_and_supports_assets():
    from pathlib import Path
    import yaml
    path=Path(__file__).resolve().parents[1]/'.github/workflows/00-daily-analysis.yml'
    workflow=yaml.safe_load(path.read_text())
    trigger=workflow.get('on',workflow.get(True))
    assert [slot['cron'] for slot in trigger['schedule']]==[
        '10 8 * * 1-5', '10 9 * * 1-5', '10 10 * * 1-5',
    ]
    text=path.read_text()
    for name in ['BOND_FUND_LIST','GOLD_ETF_LIST','FUND_REPORT_FORMAT','FUND_DASHBOARD_CONFIG_JSON']:
        assert name in text
    assert 'fonts-noto-cjk' in text and 'reports/' in text
    font_step=next(step for step in workflow['jobs']['analyze']['steps']
                   if step['name']=='安装图表中文字体')
    assert font_step['if']=="steps.delivery_guard.outputs.skip != 'true'"


def test_real_notification_routing_preserves_html_and_group_receivers(data,assets):
    from src.config import Config
    from src.notification import NotificationService
    from src.notification_noise import reset_notification_noise_state
    reset_notification_noise_state()
    config=Config(stock_list=[],email_sender='fixture@qq.com',email_password='test-only',email_receivers=['default@example.test'],stock_email_groups=[(['512050'],['group@example.test'])],markdown_to_image_channels=['email'])
    report=render_dashboard(data,assets)
    with patch('src.notification.get_config',return_value=config), patch('src.notification_sender.email_sender.smtplib.SMTP_SSL') as smtp, patch('src.md2img.markdown_to_image') as markdown:
        notifier=NotificationService()
        assert notifier.send(report.text,email_send_to_all=True,route_type='report',email_report=report)
    markdown.assert_not_called()
    smtp.return_value.send_message.assert_called_once()
    message=smtp.return_value.send_message.call_args.args[0]
    assert message.get_content_type()=='multipart/related'
    assert set(message['To'].split(', '))=={'default@example.test','group@example.test'}
    reset_notification_noise_state()
