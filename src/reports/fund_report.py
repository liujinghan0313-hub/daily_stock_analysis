"""Chinese consolidated Markdown, rendered by the existing email HTML formatter."""
from html import escape


def cell(value):
    return escape(str(value)).replace('|', '&#124;').replace('\n', ' ')


def metric_table(metrics):
    if not metrics:
        return ''
    lines = ['| 指标 | 数值 | 数据日期/区间 | 来源 | 数据质量 | 复权/分红调整 |',
             '| --- | --- | --- | --- | --- | --- |']
    for item in metrics:
        value = item.value if item.value is not None else '数据不足：' + item.reason
        if isinstance(value, float):
            value = f'{value:.6g}'
        lines.append('| ' + ' | '.join(cell(part) for part in (
            item.name, value, item.dates, item.source, item.quality, item.adjustment,
        )) + ' |')
    return '\n'.join(lines)


def render_fund_report(reports, macro, today, market_report=''):
    count = len(reports)
    lines = [f'# {count}只基金每日投资分析报告 · {today}（北京时间）',
             '收盘后复盘，辅助下一开放日观察。仅展示已公布数据；净值未发布时展示最近确认日期。',
             '## 1. 五基金整体表现摘要',
             '| 基金 | 支付宝代码 | 分析对象 | 类型 | 最近已确认价格/净值 | 近30日表现 | 状态 |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for report in reports:
        first = next((m for m in report.metrics if m.value is not None and ('收盘市价' in m.name or m.name.startswith('单位净值'))), None)
        summary = f'{first.name}：{first.value}（{first.dates}）' if first else '数据不足'
        period = next((m for m in report.metrics if m.name.startswith('近30日')), None)
        period_text = f'{period.value}（{period.dates}；{period.adjustment}）' if period and period.value is not None else '数据不足'
        gaps = sum(m.value is None or m.quality == '冲突' for m in report.metrics)
        status = f'{gaps}项数据缺口/冲突' if gaps else '见逐项来源与日期'
        lines.append('| ' + ' | '.join(cell(x) for x in (report.fund.name, report.fund.otc, report.fund.code, report.fund.kind, summary, period_text, status)) + ' |')
    lines += ['', '未提供持仓金额及权重，不计算组合收益、组合最大回撤或仓位比例。不同日期和不同口径的收益不直接相加。']
    bond_macro = [m for m in macro if '中国债券信息网' in m.source]
    gold_macro = [m for m in macro if m not in bond_macro and m.name != '大盘复盘']
    for index, report in enumerate(reports, 2):
        asset_macro = gold_macro if report.fund.kind == '黄金ETF' else (bond_macro if '债券' in report.fund.kind else [])
        lines += ['', f'## {index}. {report.fund.name}',
                  f'支付宝场外代码：**{report.fund.otc}**；实际分析对象：**{report.fund.code}**；类型：{report.fund.kind}。',
                  '', metric_table(report.metrics + asset_macro), '']
        lines += ['- ' + note for note in report.notes]
        if report.existing_report:
            lines += ['', '### 现有股票ETF技术、新闻与决策分析',
                      '以下保留原系统输出；短线信号仅对应场内ETF，不是场外联接基金的申购指令。实际报价以本节上方注明日期的未复权市价为准。',
                      '', '\n'.join('##' + line if line.startswith('#') else line for line in report.existing_report.splitlines())]
    lines += ['', f'## {len(reports)+2}. 组合层面风险与下一交易日观察事项',
              '', metric_table([m for m in macro if m.name == '大盘复盘']), '',
              '- 股票：检查两类指数成分与行业暴露重叠、20/60日趋势和放量回撤。',
              '- 债券：检查短端资金利率、国债/政金债收益率、信用事件和大额赎回公告。',
              '- 黄金：同时观察人民币现货、美元黄金、汇率和实际利率；不同市场收盘时间不同，相关性不代表因果。',
              '- 分批建仓遵循既定预算和目标配置；先确认数据日期、开放日、费用和流动性，不以缺失数据生成买入建议。',
              '- 下一交易日/开放日以交易所和基金公告为准，节假日不简单按明天处理。']
    if market_report:
        lines += ['', '### 原系统大盘复盘', '', market_report]
    return '\n'.join(lines) + '\n'
