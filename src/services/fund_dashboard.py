"""Fund observation data, shared-period total returns and explicit quality states."""
from dataclasses import dataclass, field
from datetime import timedelta
import json
import logging
import math
import os
from pathlib import Path
import re
from urllib.parse import urlparse

import pandas as pd

from src.services.fund_analysis import series, total_return_index, require_sessions, FUNDS

logger = logging.getLogger(__name__)
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / 'config' / 'fund_dashboard.json'


def load_dashboard_config(path=None):
    data = (json.loads(os.environ['FUND_DASHBOARD_CONFIG_JSON'])
            if not path and os.environ.get('FUND_DASHBOARD_CONFIG_JSON')
            else json.loads(Path(path or DEFAULT_CONFIG).read_text(encoding='utf-8')))
    funds = data.get('funds', [])
    if len(funds) != 5 or len({f['otc'] for f in funds}) != 5 or len({f['code'] for f in funds}) != 5:
        raise ValueError('基金配置必须包含五只不同的场外基金和分析对象')
    canonical = {f.otc:f for f in FUNDS}
    for f in funds:
        known = canonical.get(f['otc'])
        asset = ('债券' if known and known.code==known.otc else '黄金' if known and known.kind=='黄金ETF' else '股票')
        if not known or f['code'] != known.code or f.get('kind') != known.kind or f['asset'] != asset:
            raise ValueError('场外基金、场内ETF映射或资产类型与核验清单不一致')
        if not all(re.fullmatch(r'\d{6}', f[key]) for key in ('otc', 'code')):
            raise ValueError('基金代码必须为六位字符串，保留前导零')
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', f['color']) or f['asset'] not in ('股票', '债券', '黄金'):
            raise ValueError('基金类别或颜色配置无效')
        value = f.get('target_pct')
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100):
            raise ValueError('目标占比必须为0至100的有限数值或null')
    if sum(f.get('target_pct') or 0 for f in funds) > 100 + 1e-9:
        raise ValueError('已配置目标占比之和不能超过100%')
    if data.get('history_days', 90) not in (90, 180, 365):
        raise ValueError('历史图范围仅支持90、180或365自然日')
    threshold = data.get('deviation_threshold_pp', 5)
    if not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or threshold < 0:
        raise ValueError('偏离提醒阈值必须是非负百分点')
    budget = data.get('monthly_contribution_cny')
    if budget is not None and (not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget < 0):
        raise ValueError('每月新增资金必须为非负金额或null')
    for batch in data.get('contributions', []):
        if pd.isna(pd.Timestamp(batch['date'])):
            raise ValueError('申购记录缺少日期')
        actual = batch.get('actual_by_fund_cny', {})
        if set(actual) - {f['otc'] for f in funds}:
            raise ValueError('申购记录基金代码未知')
        values = list(actual.values()) + [batch.get('actual_total_cny', 0)]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
            raise ValueError('申购金额须为非负有限数值')
        if actual and abs(sum(actual.values()) - batch.get('actual_total_cny', 0)) > .01:
            raise ValueError('分基金实际申购额与实际合计不一致')
    return data


@dataclass
class FundObservation:
    definition: dict
    nav: object = None
    nav_date: object = None
    accumulated_nav: object = None
    total: object = None
    substitute: object = None
    source: str = 'AkShare / 东方财富正式净值、分红及拆分记录'
    substitute_source: str = ''
    status: str = '暂不可用'
    issues: list = field(default_factory=list)
    auxiliary: list = field(default_factory=list)


@dataclass
class DashboardData:
    report_date: object
    observations: list
    config: dict
    periods: dict = field(default_factory=dict)
    trend: dict = field(default_factory=dict)
    exclusions: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    news_status: str = '未取得可核验新闻，无法判断是否存在重大事件。'
    sample_label: str = ''
    period: str = 'daily'
    auxiliary_reports: dict = field(default_factory=dict)


def _reason(exc, field_name):
    """Do not expose technical tracebacks, endpoints or provider error payloads."""
    message = str(exc)
    if '冲突' in message:
        return '同日净值来源冲突，相关收益暂不用于分析。'
    if '缺少' in message or '缺失' in message:
        return f'{field_name}缺失或日期不完整，暂无可靠数据。'
    if '未来' in message:
        return '日期出现未来记录，已停止使用该序列。'
    if field_name == '含分红收益':
        return '分红或折算记录异常，无法计算可靠的含分红总回报。'
    return f'{field_name}暂不可用，保留其他已确认数据。'


def observe_fund(definition, provider, today):
    item = FundObservation(definition)
    code = definition['otc']
    try:
        unit = series(provider.fund(code, '单位净值走势'), '净值日期', '单位净值', today)
        item.nav, item.nav_date = float(unit.iloc[-1]), unit.index[-1]
    except Exception as exc:
        logger.warning('基金%s净值不可用: %s', code, exc)
        item.issues.append(_reason(exc, '正式净值'))
        unit = None
    if unit is not None:
        acc = None
        try:
            acc = series(provider.fund(code, '累计净值走势'), '净值日期', '累计净值', today)
            if item.nav_date in acc.index:
                item.accumulated_nav = float(acc.loc[item.nav_date])
            dividends = provider.fund(code, '分红送配详情')
            splits = provider.fund(code, '拆分详情')
            if any((code, key) in provider.errors for key in ('分红送配详情', '拆分详情')):
                raise ValueError('分红接口异常')
            item.total = total_return_index(unit, acc, dividends, splits)
            item.status = '正常'
        except Exception as exc:
            logger.warning('基金%s复权异常: %s', code, exc)
            item.status = '需修复'
            item.issues.append(_reason(exc, '含分红收益'))
        if code in ('006965', '007364'):
            try:
                official = provider.manager(code)
                official_unit = series(official, '净值日期', '单位净值', today)
                official_acc = series(official, '净值日期', '累计净值', today)
                if acc is None:
                    raise ValueError('缺少累计净值')
                common = unit.index.intersection(official_unit.index).intersection(acc.index).intersection(official_acc.index)
                if common.empty:
                    raise ValueError('缺少同日记录')
                check = common[-1]
                if abs(unit.loc[check]-official_unit.loc[check]) > .0001 or abs(acc.loc[check]-official_acc.loc[check]) > .0001:
                    item.total = None
                    item.nav = item.accumulated_nav = None
                    item.status = '需修复'
                    item.issues.append('同日管理人净值与第三方冲突，相关净值和收益已停用。')
                else:
                    item.source += f'；管理人同日核对 {check}'
            except Exception:
                if item.status == '正常':
                    item.status = '部分缺失'
                item.issues.append('管理人同日净值暂未完成交叉核对。')
    if item.total is None and code != definition['code']:
        try:
            frame = provider.etf(definition['code'])
            item.substitute = series(frame, '日期', '收盘', today)
            item.substitute_source = frame.attrs.get('source', 'AkShare / 东方财富ETF日线')
            item.auxiliary.append('场内ETF替代观察数据：未复权价格收益，不是场外基金实际收益；不纳入含分红风险比较。')
        except Exception:
            item.issues.append('对应ETF辅助行情也暂不可用。')
    return item


def comparison_period(observations, days, today, allow_substitute=False):
    """One common start/end; no interpolation. Keep provenance on every point."""
    candidates, excluded = {}, {}
    from src.core.trading_calendar import get_trading_dates
    sessions = get_trading_dates('cn', today-timedelta(days=45), today)
    recent = list(sessions.date) if sessions is not None else []
    for item in observations:
        code = item.definition['otc']
        values = item.total
        substitute = values is None and allow_substitute and item.substitute is not None
        if substitute:
            values = item.substitute
        if values is None:
            excluded[code] = '缺少可靠的场外含分红收益历史' if not allow_substitute else '暂无可靠数据'
            continue
        if not recent or len([day for day in recent if day > values.index[-1]]) > 2:
            excluded[code] = '最近确认日期过旧或交易日历不可用，不用于本期比较'
            continue
        target = values.index[-1] - timedelta(days=days)
        if values.loc[values.index <= target].empty:
            excluded[code] = '完整历史长度不足'
            continue
        candidates[code] = (item, values, substitute)
    if not candidates:
        return {}, excluded
    end = min(values.index[-1] for _, values, _ in candidates.values())
    target = end-timedelta(days=days)
    common = None
    for _, values, _ in candidates.values():
        eligible = set(values.index[values.index <= target])
        common = eligible if common is None else common & eligible
    if not common or (target-max(common)).days > 14:
        return {}, {**excluded, **{c:'共同起点缺失，无法可靠对齐' for c in candidates}}
    start = max(common)
    result = {}
    for code, (item, values, substitute) in candidates.items():
        try:
            if end not in values.index:
                raise ValueError('缺少共同终点')
            sample = values.loc[start:end]
            require_sessions(sample)
            if any((b-a).days > 14 for a,b in zip(sample.index, sample.index[1:])):
                raise ValueError('日期缺失')
            normalized = sample/sample.iloc[0]*100
            result[code] = {'return': float(sample.iloc[-1]/sample.iloc[0]-1),
                            'drawdown': float((sample/sample.cummax()-1).min()),
                            'index': normalized, 'start': start, 'end': end,
                            'basis': '场内ETF替代观察数据（未复权价格）' if substitute else '场外基金含分红再投资总回报',
                            'substitute': substitute, 'source': item.source if not substitute else item.substitute_source+'；未复权日线，辅助观察'}
        except Exception:
            excluded[code] = '统计期间缺少交易日或日期异常，未填充净值'
    return result, excluded


def actual_allocation(config, today):
    holdings = config.get('holdings', {})
    codes = {f['otc'] for f in config['funds']}
    if not holdings:
        return {}, '待接入持仓数据'
    try:
        if set(holdings) != codes:
            raise ValueError('尚未录入全部基金市值')
        dates = {pd.Timestamp(h['as_of']).date() for h in holdings.values()}
        if len(dates) != 1 or next(iter(dates)) > today:
            raise ValueError('持仓日期不一致或晚于报告日')
        values = {code: float(h['market_value_cny']) for code,h in holdings.items()}
        if any(h.get('currency', 'CNY') != 'CNY' for h in holdings.values()):
            raise ValueError('持仓必须先转换为人民币同日市值')
        if any(not math.isfinite(v) or v < 0 for v in values.values()) or sum(values.values()) <= 0:
            raise ValueError('市值无效或合计为零')
        weights = {c:v/sum(values.values())*100 for c,v in values.items()}
        return weights, '已录入市值日期：'+str(next(iter(dates)))
    except Exception as exc:
        return {}, '待接入完整持仓数据：'+str(exc)


def build_dashboard(provider, today, settings, selected=None, period='daily', sample_label=''):
    if period not in ('daily','weekly','monthly'):
        raise ValueError('报告周期仅支持daily、weekly、monthly')
    definitions = [f for f in settings['funds'] if selected is None or f['code'] in selected]
    observations = []
    for definition in definitions:
        try:
            observations.append(observe_fund(definition, provider, today))
        except Exception:
            logger.exception('基金%s独立分析失败', definition['otc'])
            observations.append(FundObservation(definition, issues=['本基金数据暂不可用，其他基金继续分析。']))
    data = DashboardData(today, observations, settings, sample_label=sample_label, period=period)
    for days in (7,30,90):
        data.periods[days], data.exclusions[days] = comparison_period(observations, days, today, True)
    data.trend, data.exclusions['trend'] = comparison_period(observations, settings.get('history_days',90), today)
    data.periods['risk'], data.exclusions['risk'] = comparison_period(observations,90,today)
    for item in observations:
        if item.definition['otc'] not in data.periods['risk'] and item.status == '正常':
            item.status = '部分缺失'
            item.issues.append('90日同口径风险样本不足，不绘制虚构点。')
    return data


def collect_official_events(service, data):
    """Reuse existing search; publish only dated, linkable primary-source titles."""
    hosts = ('chinaamc.com', 'chinaamc.com.cn', 'efunds.com.cn', 'ctfund.com',
             'sge.com.cn', 'pbc.gov.cn', 'csindex.com.cn', 'sse.com.cn', 'szse.cn')
    if service is None or getattr(service, 'is_available', False) is not True:
        return
    any_success = False
    for item in data.observations:
        try:
            response = service.search_stock_news(item.definition['otc'], item.definition['name'], max_results=2)
            any_success |= response.success
            for event in response.results if response.success else []:
                parsed = urlparse(event.url)
                if parsed.scheme not in ('http', 'https'):
                    continue
                host = parsed.hostname or ''
                if not any(host == h or host.endswith('.'+h) for h in hosts):
                    continue
                published = pd.Timestamp(event.published_date).date() if event.published_date else None
                if published is None or pd.isna(published) or not data.report_date-timedelta(days=7) <= published <= data.report_date:
                    continue
                if event.url not in {e['url'] for e in data.events}:
                    data.events.append({'title':event.title, 'url':event.url, 'date':published, 'source':event.source})
            if len(data.events) >= 3:
                break
        except Exception:
            logger.warning('基金公告检索暂不可用')
    data.events = sorted(data.events,key=lambda e:e['date'],reverse=True)[:3]
    if data.events:
        data.news_status = '以下为带日期的官方来源检索结果，需点击核对原文；不推断行情因果。'
    elif any_success:
        data.news_status = '已检索，未找到满足日期与官方来源条件的重大变化；不等同于市场没有事件。'
