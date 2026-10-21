"""Table/inline-CSS email dashboard, also exportable as a single offline HTML."""
from dataclasses import dataclass
import base64
import pandas as pd
from html import escape

from src.reports.fund_charts import percent, GAIN, LOSS, GRAY, STATUS_COLORS
from src.services.fund_dashboard import actual_allocation


@dataclass
class FundEmailReport:
    text: str
    html: str
    images: list
    subject: str


def signed_html(value):
    color = GRAY if value is None or value == 0 else GAIN if value > 0 else LOSS
    return f'<span style="color:{color};font-weight:700">{escape(percent(value))}</span>'


def highlights(data):
    values = [(i,r) for i in data.observations if (r:=data.periods[30].get(i.definition['otc'])) and not r['substitute']]
    result=[]
    if len(values) >= 2:
        hi=max(values,key=lambda p:p[1]['return']); lo=min(values,key=lambda p:p[1]['return'])
        result.append(f"同一30日区间，{hi[0].definition['short_name']}收益{percent(hi[1]['return'])}，{lo[0].definition['short_name']}为{percent(lo[1]['return'])}。差异值得结合目标配置观察，不直接改变长期定投计划。")
    risk=data.periods['risk']
    if risk:
        code=min(risk,key=lambda c:risk[c]['drawdown'])
        name=next(i.definition['short_name'] for i in data.observations if i.definition['otc']==code)
        result.append(f"{name}在共同90日区间的最大回撤为{percent(risk[code]['drawdown'])}。这反映历史路径，不能据此推断未来最大亏损或机械止损。")
    bad=[i.definition['short_name'] for i in data.observations if i.status in ('需修复','暂不可用')]
    if bad:
        result.append('、'.join(bad)+'存在核心数据缺口；相关收益或图点已停用，先核验净值与分红，再作长期计划观察。')
    if not result:
        result.append('可比数据不足，无法形成可靠的变化判断。维持资料核验与既定预算观察，不以缺失信息生成投资结论。')
    return result[:3]


def contribution_summary(data):
    batches=sorted((b for b in data.config.get('contributions',[]) if pd.Timestamp(b['date']).date() <= data.report_date), key=lambda b:b['date'])
    if not batches:
        return '定投执行记录待接入；每月新增资金未配置。'
    total=sum(float(b.get('actual_total_cny') or 0) for b in batches)
    latest=batches[-1]
    message=f"已记录实际申购合计{total:,.2f}元；最近一批日期{latest.get('date') or '待确认'}，实际{float(latest.get('actual_total_cny') or 0):,.2f}元。"
    detail=latest.get('actual_by_fund_cny')
    if detail:
        message+='本批：'+'、'.join(f"{f['short_name']} {float(detail[f['otc']]):,.0f}元" for f in data.config['funds'] if f['otc'] in detail)+'。'
    return message+'申购额不是当前持仓市值；确认份额、市值及持有收益待接入。'


def render_plain(data):
    lines=[f'五基金长期投资观察 | 报告日期 {data.report_date}（北京时间）']
    if data.sample_label: lines.append(data.sample_label)
    lines += ['重要变化：',*highlights(data),'基金数据：']
    for item in data.observations:
        f=item.definition
        lines.append(f"{f['name']}（支付宝{f['otc']}）：单位净值{item.nav:.4f}，确认日期{item.nav_date}" if item.nav is not None else f"{f['name']}（{f['otc']}）：暂无可靠正式净值")
        for days in (7,30,90):
            row=data.periods[days].get(f['otc'])
            lines.append(f"近{days}日：{percent(row['return']) if row else '暂无可靠数据'}"+(f"；{row['start']}—{row['end']}；{row['basis']}" if row else ''))
        risk=data.periods['risk'].get(f['otc'])
        lines.append('90日最大回撤：'+percent(risk['drawdown'] if risk else None))
        lines.append('数据状态：'+item.status+'；'+'；'.join(item.issues))
    lines += ['目标配置：'+'、'.join(f"{f['short_name']} {f['target_pct']:g}%" if f.get('target_pct') is not None else f"{f['short_name']}未配置" for f in data.config['funds']),
              actual_allocation(data.config,data.report_date)[1], contribution_summary(data),
              '长期观察：股票关注指数暴露与长期估值；债券关注信用、流动性与利率；黄金关注人民币现货、汇率与实际利率。',
              '不设机械买入点、止损位或目标价；缺少持仓市值时不计算组合收益。', '重要事件：'+data.news_status]
    lines += [f"{e['date']} {e['title']} | {e['source']} | {e['url']}" for e in data.events]
    return '\n'.join(lines)+'\n'


def render_dashboard(data, assets, failures=None, detail=False):
    failures=failures or {}
    chosen_days=7 if data.period=='weekly' else 30
    selected=[f'returns-{chosen_days}','trend','allocation']
    if detail:
        selected += ['risk','scatter','quality']+[f'returns-{d}' for d in (7,30,90) if d!=chosen_days]
    selected=[cid for cid in selected if cid in assets]
    # 2MB raw PNG budget leaves ample room for MIME/base64 overhead at QQ SMTP.
    included=[]; size=0
    for cid in selected:
        if size+len(assets[cid].png) <= 2_000_000:
            included.append(cid); size+=len(assets[cid].png)
        else:
            failures={**failures,cid:'图表超出邮件体积预算，已保留文字说明。'}
    parts=[]
    def card(title,body,tag=''):
        parts.append('<tr><td style="padding:10px 18px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #e3e9f1;border-radius:12px;background:#ffffff"><tr><td style="padding:20px"><div style="font-size:10px;letter-spacing:1px;color:#2563eb;margin-bottom:5px">'+escape(tag)+'</div><h2 style="font-size:18px;margin:0 0 14px;color:#18263c">'+escape(title)+'</h2>'+body+'</td></tr></table></td></tr>')
    def chart(cid):
        if cid not in included:
            if cid in failures or 'all' in failures:
                card('图表暂不可用','<p style="color:#64748b">'+escape(failures.get(cid,failures.get('all','')))+ '</p>')
            return
        image=assets[cid]
        card(image.title,f'<img src="cid:{cid}" alt="{escape(image.alt,quote=True)}" width="640" style="width:100%;max-width:640px;height:auto;display:block;border:0"><p style="font-size:13px;line-height:1.8;color:#334155">{escape(image.explanation)}</p><p style="font-size:11px;line-height:1.7;color:#64748b">{escape(image.note)}</p>','可视化观察')
    latest=max((i.nav_date for i in data.observations if i.nav_date),default=None)
    market_dates=sorted({str(i.substitute.index[-1]) for i in data.observations if i.substitute is not None})
    period_label={'daily':'每日','weekly':'每周（近7自然日观察）','monthly':'每月（近30自然日观察）'}[data.period]
    header=f'<p style="font-size:11px;letter-spacing:2px;color:#2563eb">五基金 · 长期投资观察</p><h1 style="font-size:28px;line-height:1.3;margin:8px 0;color:#14243d">让数据服务长期计划</h1><p style="font-size:13px;line-height:1.8;color:#526176">{escape(period_label)}报告 {data.report_date}（北京时间）<br>最近净值确认：{latest or "待核验"} · ETF替代行情：{escape("、".join(market_dates) or "本期未用于替代")}</p>'
    if data.sample_label:
        header+='<p style="padding:10px;background:#fff4df;color:#9a5700;font-size:12px">'+escape(data.sample_label)+'</p>'
    card('今日观察导航',header,'报告日期 ≠ 净值确认日期 ≠ 行情交易日期')
    market=[]
    for item in getattr(data,'market',[]):
        if item.name in ('国内人民币黄金 Au99.99（元/克）','国际美元黄金现货（美元/盎司）','美国10年实际利率（%）','美元兑人民币（人民币/美元）'):
            value=('数据不足' if item.value is None else f'{item.value:.2f}' if isinstance(item.value,(int,float)) else str(item.value))
            market.append(f'<p style="font-size:12px;margin:6px 0">{escape(item.name)}：<b>{escape(value)}</b> <span style="color:#64748b">截至{escape(item.dates)}；{escape(item.source)}</span></p>')
    card('市场概况',''.join(market) or '<p style="font-size:13px;color:#64748b">以已确认基金净值观察本期表现；未取得的宏观指标不作趋势推断。</p>','01 / 市场与组合')
    valid=sum(i.total is not None for i in data.observations)
    card('五基金总体表现',f'<p style="font-size:14px;line-height:1.9">{len(data.observations)}只基金 · {valid}只取得可核验含分红序列。<br>只比较各基金自身历史，不将不同日期或口径相加成组合收益。</p>')
    chart(f'returns-{chosen_days}')
    card('最值得关注的变化',''.join('<p style="font-size:14px;line-height:1.9;border-left:3px solid #2563eb;padding-left:12px">'+escape(text)+'</p>' for text in highlights(data)),'最多3条 / 不预测明日涨跌')
    chart('trend')
    risk=data.periods['risk']
    if risk:
        card('风险与长期计划','<p style="font-size:14px;line-height:1.9">'+escape(highlights(data)[1] if len(highlights(data))>1 else '历史回撤不等于未来最大亏损。')+'</p><p style="font-size:13px;color:#64748b">收益与回撤明细、散点图保存在详细版HTML，采用相同90日区间及场外含分红口径。</p>','02 / 风险与趋势')
    chart('allocation')
    weights,holding_note=actual_allocation(data.config,data.report_date)
    allocation='<p style="font-size:13px;color:#64748b">'+escape(holding_note)+'</p>'
    if weights:
        for f in data.config['funds']:
            target=f.get('target_pct')
            if target is not None:
                delta=weights[f['otc']]-target
                warning=abs(delta)>data.config.get('deviation_threshold_pp',5)
                allocation+=f'<p style="font-size:13px;color:{"#d97706" if warning else "#334155"}">{escape(f["short_name"])}：目标{target:.1f}% / 实际{weights[f["otc"]]:.1f}% / 偏离{delta:+.2f}个百分点'+('（超过观察阈值）' if warning else '')+'</p>'
    allocation+='<p style="font-size:13px;line-height:1.8">'+escape(contribution_summary(data))+'</p>'
    allocation+='<p style="font-size:13px;line-height:1.8;color:#64748b">每月新增投资资金：'+(f'{data.config["monthly_contribution_cny"]:,.2f}元' if data.config.get('monthly_contribution_cny') is not None else '未配置')+'。偏离仅用于再平衡观察，不自动交易。</p>'
    card('定投执行与再平衡观察',allocation,'03 / 组合管理')
    for item in data.observations:
        f=item.definition; own=f['otc']; nav=f'{item.nav:.4f}元' if item.nav is not None else '暂无可靠数据'
        body=f'<p style="font-size:12px;color:#64748b">支付宝 {escape(own)} · {escape(f["asset"])} · 净值确认 {item.nav_date or "待核验"}</p><p style="font-size:14px">单位净值 <b>{nav}</b></p><table role="presentation" width="100%"><tr>'
        for days in (7,30,90):
            row=data.periods[days].get(own)
            body+=f'<td style="padding:6px 4px;font-size:12px;color:#64748b">近{days}日<br>{signed_html(row["return"] if row else None)}</td>'
        body+='</tr></table>'
        for days in (7,30,90):
            metric=data.periods[days].get(own)
            if metric:
                body+=f'<p style="font-size:11px;line-height:1.7;color:#64748b">{days}日区间 {metric["start"]}—{metric["end"]} · {escape(metric["basis"])}<br>{escape(metric["source"])}</p>'
        risk_row=risk.get(own)
        body+='<p style="font-size:12px">90日最大回撤 '+signed_html(risk_row['drawdown'] if risk_row else None)+'</p>'
        if item.accumulated_nav is not None:
            body+=f'<p style="font-size:12px;color:#64748b">累计净值 {item.accumulated_nav:.4f}元；仅核验分红，不用累计净值差额计算收益。</p>'
        advice = ('关注短债底层信用、赎回流动性与短端利率变化，历史波动小不等于保本。' if own=='006965' else
                  '关注1—3年政策性金融债指数跟踪与利率敏感性；缺少指数总回报时不估算跟踪误差。' if own=='007364' else
                  '关注人民币黄金、美元金价、汇率与实际利率，按目标权重分批观察。' if f['asset']=='黄金' else
                  '关注指数暴露、行业重叠与可靠估值，按预算分批观察，不以短期排名追涨。')
        body+='<p style="font-size:13px;line-height:1.8">'+escape(advice)+'</p>'
        auxiliary=data.auxiliary_reports.get(own)
        if detail and auxiliary:
            for metric in auxiliary.metrics:
                value=(f'{metric.value:.4f}' if isinstance(metric.value,(int,float)) else str(metric.value)) if metric.value is not None else '数据不足'
                body+='<p style="font-size:12px;line-height:1.7">'+escape(metric.name+'：'+value)+'<br><span style="color:#64748b">'+escape(f'{metric.source}；{metric.dates}；{metric.quality}；{metric.adjustment}')+'</span></p>'
            body+='<p style="font-size:12px;line-height:1.7">'+escape('；'.join(auxiliary.notes))+'</p>'
        if detail:
            body+='<p style="font-size:11px;line-height:1.7;color:#64748b">'+escape(item.source)+'；复权口径：除息日现金分红按净值再投资，累计净值仅核验，不包含申赎费用。</p>'
        card(f['short_name']+' · 基金详情',body)
    events='<p style="font-size:13px;color:#64748b">'+escape(data.news_status)+'</p>'
    for event in data.events:
        events+=f'<p style="font-size:13px;line-height:1.8"><a href="{escape(event["url"],quote=True)}" style="color:#2563eb">{escape(event["title"])}</a><br>{event["date"]} · {escape(event["source"])}<br>关注：核对官方条款、开放日与长期配置影响，不据此推断涨跌。</p>'
    card('重要事件与可靠来源',events)
    quality=''
    for item in data.observations:
        quality+=f'<p style="font-size:13px;line-height:1.8"><b>{escape(item.definition["short_name"])}</b> <span style="color:{STATUS_COLORS[item.status]}">{item.status}</span><br><span style="font-size:12px;color:#64748b">'+escape('；'.join(item.issues+item.auxiliary) or '关键净值、分红及收益计算检查通过。')+'</span></p>'
    card('数据质量与异常',quality,'核心异常优先 / 缺失不填0')
    if detail and getattr(data,'market',[]):
        market_detail=''
        for metric in data.market:
            value='数据不足' if metric.value is None else str(metric.value)
            market_detail+='<p style="font-size:12px;line-height:1.8">'+escape(metric.name+'：'+value)+'<br><span style="color:#64748b">'+escape(f'{metric.source}；{metric.dates}；{metric.quality}；{metric.adjustment}')+'</span></p>'
        card('债券与黄金市场明细',market_detail)
    if detail:
        for cid in ['risk','scatter','quality']+[f'returns-{d}' for d in (7,30,90) if d!=chosen_days]:
            chart(cid)
    card('长期投资观察','<p style="font-size:13px;line-height:1.9">股票：关注指数成分、行业重叠和可靠估值。债券：关注信用、流动性及利率敏感性。黄金：结合人民币现货、汇率与实际利率观察。<br>没有机械买点、止损位、目标价或短线交易评分。缺少真实持仓时，组合收益与实际配置保持待接入。</p>')
    document='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>五基金长期投资观察</title></head><body style="margin:0;background:#f3f6fa;font-family:Arial,Microsoft YaHei,PingFang SC,sans-serif;color:#26344a"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center"><table role="presentation" width="720" cellspacing="0" cellpadding="0" style="width:100%;max-width:720px;padding:14px 0">'+''.join(parts)+'</table></td></tr></table></body></html>'
    label={'daily':'每日','weekly':'每周观察','monthly':'每月观察'}[data.period]
    return FundEmailReport(render_plain(data),document,[assets[c] for c in included],f'五基金{label} · {data.report_date}')


def standalone_html(report):
    html=report.html
    for image in report.images:
        html=html.replace('cid:'+image.cid,'data:image/png;base64,'+base64.b64encode(image.png).decode('ascii'))
    return html
