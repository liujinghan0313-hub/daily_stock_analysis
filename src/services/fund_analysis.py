"""Deterministic fund metrics with explicit price/NAV and adjustment contracts."""
from dataclasses import dataclass, field
from datetime import timedelta
import math
import re

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Fund:
    name: str
    otc: str
    code: str
    kind: str


FUNDS = (
    Fund('华夏中证A500ETF联接A', '022430', '512050', '股票ETF'),
    Fund('易方达中证红利低波动ETF联接发起式A', '020602', '563020', '股票ETF'),
    Fund('财通安瑞短债债券A', '006965', '006965', '主动债券基金'),
    Fund('易方达中债1—3年政策性金融债指数A', '007364', '007364', '债券指数基金'),
    Fund('华夏黄金ETF联接A', '008701', '518850', '黄金ETF'),
)
BY_CODE = {fund.code: fund for fund in FUNDS}
NAV_SOURCE = 'AkShare / 东方财富（已公布净值）'
PRICE_SOURCE = 'AkShare / 东方财富 ETF日线'


def selected_funds(stocks, bonds, gold):
    """Validate reserved identities before a bond/OTC code reaches stock logic."""
    groups = [(stocks, {'股票ETF'}, 'STOCK_LIST'),
              (bonds, {'主动债券基金', '债券指数基金'}, 'BOND_FUND_LIST'),
              (gold, {'黄金ETF'}, 'GOLD_ETF_LIST')]
    for codes, kinds, setting in groups:
        for code in codes:
            if code not in BY_CODE or BY_CODE[code].kind not in kinds:
                raise ValueError(f'{setting} 不支持 {code}；五基金模式必须使用表中对应的分析代码')
    chosen = set(stocks + bonds + gold)
    return [fund for fund in FUNDS if fund.code in chosen]


@dataclass
class Metric:
    name: str
    value: object = None
    source: str = ''
    dates: str = '未知'
    quality: str = '数据不足'
    adjustment: str = '不适用'
    reason: str = ''


@dataclass
class FundReport:
    fund: Fund
    metrics: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    existing_report: str = ''


def series(frame, date_col, value_col, today, positive=True):
    if frame is None or frame.empty or not {date_col, value_col}.issubset(frame.columns):
        reason = frame.attrs.get('error', '') if frame is not None else ''
        raise ValueError(reason or f'缺少 {date_col}/{value_col} 数据')
    result = frame[[date_col, value_col]].copy()
    # Daily exchange/provider timestamps retain their local session date.
    result['date'] = result[date_col].map(lambda x: pd.Timestamp(x).date() if pd.notna(x) else None)
    result['value'] = pd.to_numeric(result[value_col], errors='coerce')
    if result['date'].isna().any() or not np.isfinite(result['value']).all():
        raise ValueError('日期缺失或存在无效数值')
    if (result['date'] > today).any():
        raise ValueError('数据含未来日期，拒绝使用')
    if result.empty:
        raise ValueError('没有有效观测数据')
    if positive and (result['value'] <= 0).any():
        raise ValueError('价格/净值必须大于零')
    if (result.groupby('date')['value'].nunique() > 1).any():
        raise ValueError('同日数据互相冲突')
    return result.drop_duplicates('date').set_index('date')['value'].sort_index()


def quality(asof, today):
    return '当日已公布' if asof == today else f'历史已确认；距报告日 {(today - asof).days} 天'


def latest_metric(name, values, source, today, adjustment='未复权'):
    asof = values.index[-1]
    return Metric(name, float(values.iloc[-1]), source, str(asof), quality(asof, today), adjustment)


def require_sessions(values):
    from src.core.trading_calendar import get_trading_dates
    sessions = get_trading_dates('cn', values.index[0], values.index[-1])
    if sessions is None:
        raise ValueError('交易日历不可用，无法核验期间日期完整性')
    missing = set(sessions.date) - set(values.index)
    if missing:
        raise ValueError(f'缺少{len(missing)}个交易日数据（含 {min(missing)}），不填充净值')


def window(values, days):
    end = values.index[-1]
    target = end - timedelta(days=days)
    before = values.loc[values.index <= target]
    if before.empty or (target - before.index[-1]).days > 14:
        raise ValueError(f'缺少 {days} 日前的有效基准点（允许节假日回溯最多14天）')
    sample = values.loc[before.index[-1]:end]
    if len(sample) < 2 or any((b-a).days > 14 for a, b in zip(sample.index, sample.index[1:])):
        raise ValueError('期间数据缺失超过14天，无法可靠计算')
    require_sessions(sample)
    return sample


def performance(values, days, source, today, adjustment, drawdown=False):
    label = f'近{days}日' if days != 365 else '近1年'
    label += '最大回撤' if drawdown else ('总回报' if '再投资' in adjustment else '价格收益率')
    try:
        sample = window(values, days)
        value = (sample / sample.cummax() - 1).min() if drawdown else sample.iloc[-1] / sample.iloc[0] - 1
        return Metric(label, f'{value:+.2%}', source, f'{sample.index[0]} 至 {sample.index[-1]}',
                      quality(sample.index[-1], today), adjustment)
    except ValueError as exc:
        return Metric(label, source=source, adjustment=adjustment, reason=str(exc))


def total_return_index(unit, accumulated, dividends, splits):
    """Reinvest cash per old unit at ex-date NAV; never use accumulated NAV returns.

    Reconcile cash distributions against accumulated minus unit NAV. Unknown
    splits, missing ex-date NAVs, or unexplained adjustments invalidate returns.
    """
    if splits is None or dividends is None:
        raise ValueError('分红/拆分接口失败，无法确认复权口径')
    if not splits.empty:
        raise ValueError('存在拆分/折算，尚无可靠折算因子；停止计算总回报')
    aligned = accumulated.reindex(unit.index)
    if aligned.isna().any():
        raise ValueError('累计净值日期不全，无法核验分红调整')
    cash = pd.Series(0.0, index=unit.index)
    if not dividends.empty:
        amount_col = next((col for col in ['每份分红', '每份分红（元）', '每份分红(元)', '每10份分红', '每10份分红（元）', '每10份分红(元)'] if col in dividends), None)
        if '除息日' not in dividends or amount_col is None:
            raise ValueError('分红数据缺少除息日或明确的每份/每10份分红单位')
        for _, event in dividends.iterrows():
            ex_date = pd.Timestamp(event['除息日']).date()
            if pd.isna(ex_date):
                raise ValueError('分红除息日无效')
            raw = str(event[amount_col]).strip()
            divisor = 10 if amount_col.startswith('每10份') else 1
            prefix = '每10份派现金' if divisor == 10 else '每份派现金'
            match = re.fullmatch(rf'(?:{prefix})?([0-9]+(?:\.[0-9]+)?)(?:元)?', raw)
            if not match:
                raise ValueError('分红列单位与内容不匹配，无法识别分红单位')
            amount = float(match.group(1)) / divisor
            if unit.index[0] < ex_date <= unit.index[-1]:
                if ex_date not in cash.index:
                    raise ValueError('除息日净值缺失')
                cash.loc[ex_date] += amount
    delta = (aligned - unit).diff().iloc[1:]
    if not np.allclose(delta, cash.iloc[1:], rtol=0, atol=0.00021):
        raise ValueError('累计净值与单位净值的差异变化无法由分红解释')
    growth = (unit + cash) / unit.shift(1)
    growth.iloc[0] = 1.0
    if (growth <= 0).any() or not np.isfinite(growth).all():
        raise ValueError('复权增长因子无效')
    return growth.cumprod()


def analyze_bond(fund, provider, today):
    report = FundReport(fund)
    unit = accumulated = None
    for indicator, column in [('单位净值走势', '单位净值'), ('累计净值走势', '累计净值')]:
        try:
            values = series(provider.fund(fund.code, indicator), '净值日期', column, today)
            report.metrics.append(latest_metric(column + '（元/份）', values, NAV_SOURCE, today))
            if column == '单位净值':
                unit = values
            else:
                accumulated = values
        except Exception as exc:
            report.metrics.append(Metric(column, source=NAV_SOURCE, reason=str(exc)))
    adjustment = '分红在除息日按单位净值再投资；不含申赎费'
    try:
        if unit is None or accumulated is None:
            raise ValueError('净值历史不完整')
        div = provider.fund(fund.code, '分红送配详情')
        split = provider.fund(fund.code, '拆分详情')
        errors = provider.errors
        if (fund.code, '分红送配详情') in errors or (fund.code, '拆分详情') in errors:
            raise ValueError('分红/拆分接口异常，未将失败当作无分红')
        total = total_return_index(unit, accumulated, div, split)
        report.metrics += [performance(total, days, NAV_SOURCE, today, adjustment) for days in (7, 30, 90, 365)]
        report.metrics += [performance(total, days, NAV_SOURCE, today, adjustment, True) for days in (90, 365)]
        report.notes.append(f'分红记录 {len(div)} 条；拆分记录 {len(split)} 条（未来尚未除息的公告不计入收益）。累计净值仅用于展示和核验分红。')
    except Exception as exc:
        for name in ('近7日总回报', '近30日总回报', '近90日总回报', '近1年总回报', '近90日最大回撤', '近1年最大回撤'):
            report.metrics.append(Metric(name, source=NAV_SOURCE, adjustment=adjustment, reason=str(exc)))
    try:
        official = provider.manager(fund.code)
        confirmed = series(official, '净值日期', '单位净值', today)
        official_acc = series(official, '净值日期', '累计净值', today)
        if unit is None or accumulated is None:
            raise ValueError('缺少第三方净值，无法交叉核对')
        common = unit.index.intersection(confirmed.index).intersection(accumulated.index).intersection(official_acc.index)
        if common.empty:
            raise ValueError('没有同日正式净值可核对')
        check_date = common[-1]
        matches = all(abs(a.loc[check_date] - b.loc[check_date]) <= 0.0001
                      for a, b in ((unit, confirmed), (accumulated, official_acc)))
        report.metrics.append(Metric('管理人交叉核对', '一致' if matches else '冲突，相关指标不可用于决策',
                                     '基金管理人官网 + 东方财富', str(check_date), '已核对' if matches else '冲突', '未复权'))
        if not matches:
            for metric in report.metrics:
                if metric.source == NAV_SOURCE:
                    metric.value = None
                    metric.quality = '冲突'
                    metric.reason = '同日管理人正式净值与第三方不一致'
    except Exception as exc:
        report.metrics.append(Metric('管理人交叉核对', source='基金管理人官网', reason=str(exc)))
    if fund.code == '006965':
        report.notes.append('信用风险：需核查最新季报的信用等级与发行人集中度；流动性风险：关注大额赎回、回购杠杆和低流动性券；短端利率上行可能引发净值回撤。未获取最新持仓，无法量化信用暴露。')
    else:
        report.notes.append('指数跟踪：关注抽样复制、费用及现金拖累；1—3年政金债仍有利率敏感性，收益率上行会压低债券价格。未获得组合久期与同口径基准财富指数，久期及跟踪误差均为数据不足，不用基金波动率冒充跟踪误差。')
    report.notes.append('债券基金不使用股票买卖信号；申赎以基金正式净值和开放日规则为准。')
    return report


def analyze_etf(fund, provider, today):
    report = FundReport(fund)
    source = PRICE_SOURCE
    close = None
    try:
        frame = provider.etf(fund.code)
        source = frame.attrs.get('source', PRICE_SOURCE)
        close = series(frame, '日期', '收盘', today)
        report.metrics.append(latest_metric('ETF收盘市价（元/份）', close, source, today))
        for days in (7, 30, 90):
            report.metrics.append(performance(close, days, source, today, '未复权；非含息总回报'))
        for days in (90, 365):
            report.metrics.append(performance(close, days, source, today, '未复权；价格回撤', True))
        try:
            if len(close) < 2:
                raise ValueError('缺少前一交易日收盘价')
            require_sessions(close.iloc[-2:])
            report.metrics.append(Metric('日涨跌幅', f'{close.iloc[-1]/close.iloc[-2]-1:+.2%}', source,
                                         f'{close.index[-2]} 至 {close.index[-1]}', quality(close.index[-1], today), '未复权'))
        except ValueError as exc:
            report.metrics.append(Metric('日涨跌幅', source=source, reason=str(exc)))
        for days in (20, 60):
            if len(close) < days + 1 or any((b-a).days > 14 for a,b in zip(close.index[-days-1:], close.index[-days:])):
                report.metrics.append(Metric(f'MA{days}趋势', source=source, reason='交易样本不足或日期缺失'))
                continue
            try:
                require_sessions(close.iloc[-days-1:])
            except ValueError as exc:
                report.metrics.append(Metric(f'MA{days}趋势', source=source, reason=str(exc)))
                continue
            ma = close.rolling(days).mean()
            direction = '向上' if ma.iloc[-1] > ma.iloc[-2] else ('向下' if ma.iloc[-1] < ma.iloc[-2] else '持平')
            report.metrics.append(Metric(f'MA{days}趋势', f'{ma.iloc[-1]:.4f}，{direction}；市价在均线' + ('上方' if close.iloc[-1] > ma.iloc[-1] else '下方'),
                                         source, f'{close.index[-days]} 至 {close.index[-1]}', quality(close.index[-1], today), '未复权价格均线；除息可能影响趋势'))
        for column, label in [('成交量', '成交量（手）'), ('成交额', '成交额（元）')]:
            try:
                values = series(frame, '日期', column, today, positive=False)
                if (values < 0).any():
                    raise ValueError('成交数据为负数')
                report.metrics.append(latest_metric(label, values, source, today, '原始成交数据'))
                if column == '成交量' and len(values) >= 21 and values.iloc[-21:-1].mean() > 0:
                    require_sessions(values.iloc[-21:])
                    report.metrics.append(Metric('成交量/前20日均量', f'{values.iloc[-1]/values.iloc[-21:-1].mean():.2f}倍', source,
                                                 f'{values.index[-21]} 至 {values.index[-1]}', quality(values.index[-1], today), '原始成交数据'))
            except Exception as exc:
                report.metrics.append(Metric(label, source=source, reason=str(exc)))
    except Exception as exc:
        report.metrics.append(Metric('ETF行情数据缺口', source=source, reason=str(exc)))
    try:
        # The ETF's own NAV, never the linked OTC fund's NAV.
        nav = series(provider.fund(fund.code, '单位净值走势'), '净值日期', '单位净值', today)
        report.metrics.append(latest_metric('ETF正式单位净值（元/份）', nav, NAV_SOURCE, today))
        if close is None:
            raise ValueError('ETF原始市价不可用，保留正式净值但不计算折溢价')
        asof = close.index[-1]
        if asof not in nav.index:
            raise ValueError('ETF最新收盘日正式净值尚未公布，不以旧净值计算折溢价')
        report.metrics.append(Metric('ETF收盘折溢价', f'{close.loc[asof]/nav.loc[asof]-1:+.2%}', source + ' + ' + NAV_SOURCE,
                                     str(asof), quality(asof, today), '同日未复权市价 / ETF正式净值 - 1；非盘中IOPV'))
    except Exception as exc:
        report.metrics.append(Metric('ETF行情/折溢价缺口', source=source + ' + ' + NAV_SOURCE, reason=str(exc)))
    if fund.kind == '股票ETF':
        report.metrics.append(Metric('相关指数估值', source='指数公司', reason='未接入可核验的同日指数PE/PB，不使用ETF自身PE代替指数估值'))
        report.notes.append('长期持有观察：核对指数暴露及持仓重叠；结合20/60日趋势、回撤与成交量，按预设预算分批观察，避免追涨或单凭短期信号满仓。')
    else:
        report.notes.append('黄金ETF跟踪人民币黄金现货，ETF单位价格与金价每克报价不同，不比较绝对数值。美元金价与美元兑人民币同时影响人民币黄金；人民币贬值可能放大人民币金价涨幅，升值反之。')
        report.notes.append('中长期配置观察：按目标权重分批，关注实际利率、美元、地缘风险与流动性；黄金无票息，短期可能剧烈波动。折溢价异常时先核对正式净值，不把市价当成场外净值。')
    report.notes.append(f'支付宝申购代码 {fund.otc}；分析对象 {fund.code} 为场内ETF。上列市价和ETF净值均不是 {fund.otc} 的申购净值，联接基金另有费用及跟踪偏离。')
    return report


def analyze_macro(provider, today, include_gold=True, include_bonds=True):
    """Each series fails independently; differing global closes remain labelled."""
    metrics = []
    if include_bonds:
        try:
            curves = provider.yields()
            if not {'曲线名称', '日期'}.issubset(curves.columns):
                raise ValueError('中债收益率曲线接口缺少名称/日期')
            for curve in ('中债国债收益率曲线', '中债国开债收益率曲线'):
                for term in ('1年', '3年', '10年'):
                    name = f'{curve} {term}'
                    try:
                        values = series(curves.loc[curves['曲线名称'] == curve], '日期', term, today, positive=False)
                        metrics.append(latest_metric(name + '（%）', values, 'AkShare / 中国债券信息网', today, '到期收益率；非基金收益'))
                        if len(values) >= 2:
                            metrics.append(Metric(name + '较前次变化', f'{(values.iloc[-1]-values.iloc[-2])*100:+.2f}bp',
                                                  'AkShare / 中国债券信息网', f'{values.index[-2]} 至 {values.index[-1]}',
                                                  quality(values.index[-1], today), '到期收益率差；1%=100bp'))
                    except Exception as exc:
                        metrics.append(Metric(name, source='AkShare / 中国债券信息网', reason=str(exc)))
        except Exception as exc:
            metrics.append(Metric('国债/政策性金融债收益率', source='AkShare / 中国债券信息网', reason=str(exc)))
    if not include_gold:
        return metrics
    series_specs = [
        ('国内人民币黄金 Au99.99（元/克）', 'AkShare / 上海黄金交易所', provider.gold_spot, 'date', 'close', True),
        ('国际美元黄金现货（美元/盎司）', 'AkShare / 新浪伦敦金XAU（现货）', provider.international_gold, 'date', 'close', True),
        ('美元指数 DXY', 'YFinance / Yahoo DX-Y.NYB', lambda: provider.yahoo('DX-Y.NYB'), 'date', 'Close', True),
        ('美元兑人民币（人民币/美元）', 'YFinance / Yahoo CNY=X', lambda: provider.yahoo('CNY=X'), 'date', 'Close', True),
        ('美国10年实际利率（%）', 'FRED / DFII10（美国财政部TIPS）', provider.real_yield, 'observation_date', 'DFII10', False),
    ]
    for name, source, loader, day_col, value_col, positive in series_specs:
        try:
            frame = loader()
            # FRED missing observations are explicitly '.', not valid quotes.
            if value_col == 'DFII10':
                frame = frame.loc[pd.to_numeric(frame[value_col], errors='coerce').notna()]
            values = series(frame, day_col, value_col, today, positive)
            metrics.append(latest_metric(name, values, source, today, '原始报价；不同市场收盘时区不同'))
            if len(values) >= 2:
                is_rate = value_col == 'DFII10'
                change = (values.iloc[-1]-values.iloc[-2])*100 if is_rate else values.iloc[-1]/values.iloc[-2]-1
                metrics.append(Metric(name + '较前次变化', f'{change:+.2f}bp' if is_rate else f'{change:+.2%}',
                                      source, f'{values.index[-2]} 至 {values.index[-1]}', quality(values.index[-1], today), '原始报价'))
            if name.startswith('国内人民币'):
                etf_frame = provider.etf('518850')
                etf_source = etf_frame.attrs.get('source', PRICE_SOURCE)
                etf = series(etf_frame, '日期', '收盘', today)
                paired = pd.concat([etf.rename('etf'), values.rename('spot')], axis=1, join='inner').tail(31)
                if len(paired) < 31 or any((b-a).days > 14 for a,b in zip(paired.index, paired.index[1:])):
                    raise ValueError('黄金ETF与现货同日样本不足31个或日期缺失')
                returns = paired.pct_change(fill_method=None).dropna()
                corr = returns['etf'].corr(returns['spot'])
                if not np.isfinite(corr):
                    raise ValueError('收益率方差不足，无法计算相关性')
                dates = f'{paired.index[0]} 至 {paired.index[-1]}'
                metrics.append(Metric('黄金ETF与现货日收益相关性（30样本）', f'{corr:.3f}', etf_source + ' + ' + source,
                                      dates, quality(paired.index[-1], today), '同日未复权收盘价格收益；收盘时点不同，非跟踪误差'))
                vol = returns['etf'].tail(20).std(ddof=1) * math.sqrt(252)
                metrics.append(Metric('黄金ETF近期20样本年化波动率', f'{vol:.2%}', etf_source, dates,
                                      quality(paired.index[-1], today), '未复权日收益；252日年化假设'))
        except Exception as exc:
            metrics.append(Metric(name + '数据缺口', source=source, reason=str(exc)))
    return metrics
