"""Offline contracts: all three assets, adjustment, dates, and merged delivery."""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import pytest

from data_provider.fund_data import FundDataProvider, official_nav
from src.core.fund_pipeline import multi_asset_enabled, run_fund_analysis
from src.reports.fund_report import render_fund_report
from src.services.fund_analysis import (
    BY_CODE, FUNDS, FundReport, Metric, analyze_bond, analyze_etf, analyze_macro,
    performance, selected_funds, series, total_return_index,
)

TODAY = date(2026, 10, 16)


class SampleProvider:
    """Synthetic data only, with a cash dividend and varying ETF price."""
    def __init__(self):
        self.errors = {}
        self.dates = pd.bdate_range('2025-06-02', TODAY).date
        self.ex_date = self.dates[-50]
        self.paid = (self.dates >= self.ex_date) * 0.02
        self.unit = 1.08 + np.arange(len(self.dates)) * 0.00008 - self.paid
        self.calls = []

    def fund(self, code, indicator):
        self.calls.append((code, indicator))
        if indicator == '分红送配详情':
            return pd.DataFrame({'除息日': [self.ex_date], '每份分红': ['每份派现金0.0200元']})
        if indicator == '拆分详情':
            return pd.DataFrame()
        column = '单位净值' if indicator == '单位净值走势' else '累计净值'
        if code in ('512050', '563020', '518850'):
            values = self.etf(code)['收盘'].to_numpy() * 0.999
        else:
            values = self.unit if column == '单位净值' else self.unit + self.paid
        return pd.DataFrame({'净值日期': self.dates, column: values})

    def manager(self, code):
        return pd.DataFrame({'净值日期': self.dates, '单位净值': self.unit, '累计净值': self.unit + self.paid})

    def etf(self, code):
        values = 1.0 + np.arange(len(self.dates)) * 0.001 + 0.02 * np.sin(np.arange(len(self.dates)))
        return pd.DataFrame({'日期': self.dates, '收盘': values, '成交量': 50000 + np.arange(len(values)) * 10, '成交额': 6000000.0})

    def gold_spot(self):
        return pd.DataFrame({'date': self.dates[-60:], 'close': self.etf('518850')['收盘'].tail(60).to_numpy() * 500})

    def international_gold(self):
        return pd.DataFrame({'date': self.dates[-60:], 'close': 3000 + np.arange(60)*0.1})

    def yields(self):
        return pd.DataFrame([{'日期': day, '曲线名称': curve, '1年': 1.2 + i*.001, '3年': 1.5 + i*.002, '10年': 1.9 + i*.003}
                             for curve in ('中债国债收益率曲线', '中债国开债收益率曲线') for i, day in enumerate(self.dates[-10:])])

    def yahoo(self, symbol):
        return pd.DataFrame({'date': self.dates[-60:], 'Close': 100 + np.arange(60)*0.1})

    def real_yield(self):
        return pd.DataFrame({'observation_date': self.dates[-10:], 'DFII10': np.linspace(1.5, 1.6, 10)})


def metric(report, name):
    return next(item for item in report.metrics if item.name == name)


def test_code_mapping_and_order():
    assert [(f.otc, f.code) for f in FUNDS] == [('022430','512050'), ('020602','563020'), ('006965','006965'), ('007364','007364'), ('008701','518850')]
    assert selected_funds(['563020', '512050'], ['007364','006965'], ['518850']) == list(FUNDS)


@pytest.mark.parametrize('stocks,bonds,gold', [(['022430'],[],[]), (['006965'],[],[]), (['518850'],[],[]), ([],['512050'],[]), ([],[],['008701']), ([],['007365'],[])])
def test_wrong_asset_codes_fail_before_fetch(stocks, bonds, gold):
    with pytest.raises(ValueError): selected_funds(stocks, bonds, gold)


def test_opt_in_defaults_preserve_old_behavior():
    assert not multi_asset_enabled(SimpleNamespace())
    assert not multi_asset_enabled(SimpleNamespace(bond_fund_list=[], gold_etf_list=[]))
    assert multi_asset_enabled(SimpleNamespace(bond_fund_list=['006965']))


def test_reinvestment_not_accumulated_nav_return():
    days = [date(2026,10,12), date(2026,10,13), date(2026,10,14)]
    unit = pd.Series([2.0,1.9,2.09], index=days)
    acc = pd.Series([2.,2.,2.19], index=days)
    div = pd.DataFrame({'除息日':[days[1]], '每份分红':[0.1]})
    total = total_return_index(unit, acc, div, pd.DataFrame())
    assert total.iloc[-1] == pytest.approx(1.1)
    assert total.iloc[-1]-1 != pytest.approx(acc.iloc[-1]/acc.iloc[0]-1)


@pytest.mark.parametrize('problem', ['missing_dividend','unknown_split','missing_ex_date','failed_api','per_ten_units'])
def test_unreliable_adjustments_rejected(problem):
    days = [date(2026,10,12), date(2026,10,13), date(2026,10,14)]
    unit = pd.Series([2.,1.9,2.09], index=days)
    acc = pd.Series([2.,2.,2.19], index=days)
    div = pd.DataFrame({'除息日':[days[1]], '每份分红':[0.1]})
    split = pd.DataFrame()
    if problem == 'missing_dividend': div = pd.DataFrame()
    elif problem == 'unknown_split': split = pd.DataFrame({'未知字段':[2]})
    elif problem == 'missing_ex_date':
        unit = unit.drop(days[1]); acc = acc.drop(days[1])
    elif problem == 'failed_api': div = None
    else: div = pd.DataFrame({'除息日':[days[1]], '每10份分红':['每份派现金0.1元']})
    with pytest.raises(ValueError): total_return_index(unit, acc, div, split)


def test_bond_nav_total_return_and_risks():
    provider = SampleProvider()
    for fund in FUNDS[2:4]:
        report = analyze_bond(fund, provider, TODAY)
        assert metric(report,'单位净值（元/份）').value == pytest.approx(provider.unit[-1])
        assert metric(report,'累计净值（元/份）').value == pytest.approx(provider.unit[-1]+0.02)
        assert metric(report,'近1年总回报').value is not None
        assert metric(report,'近90日最大回撤').value is not None
        assert metric(report,'管理人交叉核对').value == '一致'
        assert any('不使用股票买卖信号' in note for note in report.notes)


def test_dividend_failure_does_not_erase_latest_nav():
    provider = SampleProvider()
    provider.errors[('006965','分红送配详情')] = 'timeout'
    report = analyze_bond(BY_CODE['006965'], provider, TODAY)
    assert metric(report,'单位净值（元/份）').value is not None
    assert metric(report,'近30日总回报').value is None
    assert '接口异常' in metric(report,'近30日总回报').reason


def test_manager_conflict_invalidates_derived_metrics():
    provider = SampleProvider()
    original = provider.manager
    provider.manager = lambda code: original(code).assign(单位净值=2.0)
    report = analyze_bond(BY_CODE['006965'], provider, TODAY)
    assert metric(report,'近30日总回报').value is None
    assert metric(report,'单位净值（元/份）').quality == '冲突'


def test_missing_manager_is_explicit():
    provider = SampleProvider()
    provider.manager = lambda code: pd.DataFrame()
    report = analyze_bond(BY_CODE['007364'], provider, TODAY)
    assert metric(report,'管理人交叉核对').value is None
    assert metric(report,'单位净值（元/份）').value is not None


@pytest.mark.parametrize('problem', ['missing_date','future_date','nan','zero','conflict'])
def test_invalid_nav_rows_rejected(problem):
    frame = pd.DataFrame({'日期':[TODAY-timedelta(days=1), TODAY], '值':[1.0,1.1]})
    if problem == 'missing_date': frame.loc[0,'日期'] = None
    if problem == 'future_date': frame.loc[0,'日期'] = TODAY+timedelta(days=1)
    if problem == 'nan': frame.loc[0,'值'] = np.nan
    if problem == 'zero': frame.loc[0,'值'] = 0
    if problem == 'conflict': frame.loc[0,'日期'] = TODAY
    with pytest.raises(ValueError): series(frame, '日期', '值', TODAY)


def test_calendar_missing_session_not_forward_filled():
    provider = SampleProvider()
    values = pd.Series(provider.unit, index=provider.dates).drop(date(2026,10,14))
    result = performance(values, 30, 'fixture', TODAY, '再投资')
    assert result.value is None
    assert '缺少1个交易日' in result.reason


def test_short_history_insufficient_not_zero_return():
    values = pd.Series([1.,1.01], index=[TODAY-timedelta(days=1), TODAY])
    assert performance(values, 365, 'fixture', TODAY, '再投资').value is None


def test_stock_etf_raw_price_mapping_and_ma():
    provider = SampleProvider()
    report = analyze_etf(FUNDS[0], provider, TODAY)
    assert metric(report,'ETF收盘市价（元/份）').value == provider.etf('512050')['收盘'].iloc[-1]
    assert metric(report,'MA20趋势').value is not None
    assert metric(report,'MA60趋势').value is not None
    assert metric(report,'ETF收盘折溢价').value is not None
    assert all(code != '022430' for code,_ in provider.calls)
    assert any('不是 022430' in note for note in report.notes)


def test_premium_never_uses_previous_nav():
    provider = SampleProvider()
    original = provider.fund
    provider.fund = lambda code, indicator: original(code, indicator).iloc[:-1]
    report = analyze_etf(FUNDS[0], provider, TODAY)
    assert not any(item.name == 'ETF收盘折溢价' for item in report.metrics)
    assert '尚未公布' in metric(report,'ETF行情/折溢价缺口').reason
    assert metric(report,'ETF收盘市价（元/份）').value is not None


def test_gold_has_own_macro():
    provider = SampleProvider()
    report = analyze_etf(FUNDS[-1], provider, TODAY)
    names = [item.name for item in analyze_macro(provider, TODAY) if item.value is not None]
    assert '黄金ETF与现货日收益相关性（30样本）' in names
    assert '美国10年实际利率（%）' in names
    assert '国际美元黄金现货（美元/盎司）' in names
    assert any('人民币贬值' in note for note in report.notes)


def test_macro_one_endpoint_failure_isolated():
    provider = SampleProvider()
    provider.gold_spot = Mock(side_effect=TimeoutError('fixture timeout'))
    macro = analyze_macro(provider, TODAY)
    assert any('国内人民币' in m.name and m.value is None for m in macro)
    assert any('美元兑人民币' in m.name and m.value is not None for m in macro)


def test_adapter_failure_cached_and_logged():
    provider = FundDataProvider(TODAY)
    loader = Mock(side_effect=TimeoutError('fixture'))
    assert provider.get('test', loader).empty
    assert provider.get('test', loader).empty
    loader.assert_called_once()
    assert 'TimeoutError' in provider.errors['test']


def test_official_page_rejects_unrelated_table():
    response = Mock(text='<table><tr><th>单位净值</th></tr><tr><td>99</td></tr></table>')
    with patch('data_provider.fund_data.requests.get', return_value=response):
        with pytest.raises(ValueError): official_nav('006965')


def test_consolidated_send_once_order_and_asset_failure():
    provider = SampleProvider()
    original = provider.fund
    provider.fund = lambda code, indicator: pd.DataFrame() if code == '006965' else original(code, indicator)
    config = SimpleNamespace(bond_fund_list=['006965','007364'], gold_etf_list=['518850'], single_stock_notify=True)
    args = SimpleNamespace(dry_run=False, no_notify=False)
    pipeline = Mock()
    pipeline.run.return_value = [object()]
    pipeline.notifier.generate_single_stock_report.return_value = '模拟原系统技术/新闻/决策分析'
    pipeline.notifier.send.return_value = True
    pipeline.notifier.save_report_to_file.return_value = 'fixture.md'
    assert run_fund_analysis(config, args, pipeline, ['512050','563020'], provider=provider, today=TODAY)
    pipeline.notifier.send.assert_called_once()
    content = pipeline.notifier.send.call_args.args[0]
    for i, fund in enumerate(FUNDS, 2): assert f'## {i}. {fund.name}' in content
    assert '## 7. 组合层面风险' in content
    assert '数据不足' in content
    assert [call.kwargs['stock_codes'] for call in pipeline.run.call_args_list] == [['512050'],['563020']]
    assert all(call.kwargs['send_notification'] is False for call in pipeline.run.call_args_list)
    pipeline.notifier.save_report_to_file.assert_called_once()


@pytest.mark.parametrize('dry_run,no_notify', [(True,False),(False,True)])
def test_dry_run_no_notify_never_send(dry_run, no_notify):
    pipeline = Mock()
    pipeline.run.return_value = []
    cfg = SimpleNamespace(bond_fund_list=['006965'], gold_etf_list=[])
    assert run_fund_analysis(cfg, SimpleNamespace(dry_run=dry_run,no_notify=no_notify), pipeline, [], provider=SampleProvider(), today=TODAY)
    pipeline.notifier.send.assert_not_called()
    pipeline.run.assert_not_called()


def test_notification_failure_keeps_report():
    pipeline = Mock()
    pipeline.notifier.send.return_value = False
    cfg = SimpleNamespace(bond_fund_list=['006965'], gold_etf_list=[])
    assert not run_fund_analysis(cfg, SimpleNamespace(dry_run=False,no_notify=False), pipeline, [], provider=SampleProvider(), today=TODAY)
    pipeline.notifier.save_report_to_file.assert_called_once()


def test_render_escapes_untrusted_table_content():
    content = render_fund_report([FundReport(FUNDS[0],[Metric('<script>',None,'a|b',reason='<img>')])], [], TODAY)
    assert '<script>' not in content
    assert '&lt;script&gt;' in content and 'a&#124;b' in content


def test_config_optional_lists_preserve_leading_zeroes(monkeypatch):
    from src.config import Config
    monkeypatch.setenv('BOND_FUND_LIST','006965,007364,006965')
    monkeypatch.setenv('GOLD_ETF_LIST','518850')
    cfg = Config._load_from_env()
    assert cfg.bond_fund_list == ['006965','007364']
    assert cfg.gold_etf_list == ['518850']


def test_per_ten_cash_dividend_converted_and_future_not_used():
    days = [date(2026,10,12), date(2026,10,13), date(2026,10,14)]
    unit = pd.Series([2.0,1.9,2.09], index=days)
    acc = pd.Series([2.,2.,2.19], index=days)
    div = pd.DataFrame({'除息日':[days[1],date(2026,10,19)], '每10份分红':['每10份派现金1.0000元','每10份派现金0.0450元']})
    assert total_return_index(unit,acc,div,pd.DataFrame()).iloc[-1] == pytest.approx(1.1)


def test_stale_data_keeps_confirmed_date():
    report = analyze_bond(FUNDS[2],SampleProvider(),TODAY+timedelta(days=2))
    nav = metric(report,'单位净值（元/份）')
    assert nav.dates == str(TODAY)
    assert nav.quality == '历史已确认；距报告日 2 天'


def test_official_labelled_static_table_and_utf8_encoding():
    response = Mock(text='<p>007364</p><table><tr><th>日期</th><th>单位净值(元)</th><th>累计净值(元)</th></tr><tr><td>2026-10-16</td><td>1.0127</td><td>1.1891</td></tr></table>', apparent_encoding='utf-8')
    with patch('data_provider.fund_data.requests.get',return_value=response):
        actual = official_nav('007364')
    assert actual.iloc[0]['单位净值'] == pytest.approx(1.0127)
    assert response.encoding == 'utf-8'


def test_etf_fallback_keeps_raw_price_and_converts_shares_to_lots():
    provider = FundDataProvider(TODAY)
    provider._cache[('518850','行情')] = pd.DataFrame()
    provider.yahoo = Mock(return_value=pd.DataFrame({'date':[TODAY], 'Close':[8.], 'Volume':[10000.]}))
    result = provider.etf('518850')
    assert result.iloc[0]['收盘'] == 8
    assert result.iloc[0]['成交量'] == 100
    assert 'YFinance' in result.attrs['source']
    assert '成交额' not in result


def test_real_email_sender_has_one_mime_with_five_funds():
    from src.config import Config
    from src.notification_sender.email_sender import EmailSender
    provider = SampleProvider()
    reports = [analyze_bond(f,provider,TODAY) if '债券' in f.kind else analyze_etf(f,provider,TODAY) for f in FUNDS]
    content = render_fund_report(reports,[],TODAY)
    sender = EmailSender(Config(email_sender='fixture@qq.com',email_password='fixture-only',email_receivers=['fixture@example.com']))
    with patch('src.notification_sender.email_sender.smtplib.SMTP_SSL') as smtp:
        assert sender.send_to_email(content)
    smtp.return_value.send_message.assert_called_once()
    message = smtp.return_value.send_message.call_args.args[0]
    assert message.is_multipart()
    plain,html = [part.get_payload(decode=True).decode('utf-8') for part in message.get_payload()]
    assert all(f.code in plain and f.otc in html for f in FUNDS)


def test_main_dispatches_multi_asset_before_legacy_notifications():
    import main
    config = SimpleNamespace(stock_list=['512050','563020'],bond_fund_list=['006965','007364'],gold_etf_list=['518850'],
                             single_stock_notify=True,merge_email_notification=False,market_review_enabled=False,
                             market_review_region='cn',daily_market_context_enabled=False,backtest_enabled=False)
    args = SimpleNamespace(portfolio=None,single_notify=True,no_context_snapshot=True,no_market_review=True,
                           workers=1,dry_run=False,no_notify=False,schedule=False)
    pipeline = Mock()
    with patch.object(main,'_refresh_stock_index_cache_for_analysis'), patch.object(main,'_compute_trading_day_filter',return_value=(['512050','563020'],'cn',False)), patch('src.core.pipeline.StockAnalysisPipeline',return_value=pipeline), patch('src.core.fund_pipeline.run_fund_analysis',return_value=True) as multi, patch.object(main,'_run_auto_backtest'):
        assert main.run_full_analysis(config,args,['512050','563020'])
    multi.assert_called_once()
    pipeline.run.assert_not_called()
    pipeline.notifier.send.assert_not_called()


def test_etf_price_failure_still_displays_own_nav():
    provider = SampleProvider()
    provider.etf = Mock(side_effect=TimeoutError('fixture'))
    provider.fund = lambda code,indicator: pd.DataFrame({'净值日期':[TODAY], '单位净值':[2.]})
    report = analyze_etf(FUNDS[-1],provider,TODAY)
    assert metric(report,'ETF正式单位净值（元/份）').value == 2
    assert metric(report,'ETF行情/折溢价缺口').value is None


def test_daily_change_requires_previous_session():
    provider = SampleProvider()
    original = provider.etf
    provider.etf = lambda code: original(code).loc[lambda frame: frame['日期'] != date(2026,10,15)]
    provider.fund = lambda code,indicator: pd.DataFrame({'净值日期':[TODAY], '单位净值':[2.]})
    report = analyze_etf(FUNDS[-1],provider,TODAY)
    assert metric(report,'日涨跌幅').value is None
    assert '缺少1个交易日' in metric(report,'日涨跌幅').reason


def test_gold_market_data_is_in_gold_section():
    provider=SampleProvider()
    reports=[analyze_etf(f,provider,TODAY) if 'ETF' in f.kind else analyze_bond(f,provider,TODAY) for f in FUNDS]
    content=render_fund_report(reports,analyze_macro(provider,TODAY),TODAY)
    gold_section=content.split('## 6. ')[1].split('## 7. ')[0]
    assert '国际美元黄金现货' in gold_section
    assert '美国10年实际利率' in gold_section
