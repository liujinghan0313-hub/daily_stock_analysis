"""Build visual artifacts first, then use the existing routed notifier exactly once."""
from pathlib import Path

from src.services.fund_dashboard import load_dashboard_config, build_dashboard, collect_official_events
from src.services.fund_analysis import analyze_macro, analyze_bond, analyze_etf, FUNDS
from src.reports.fund_charts import render_charts
from src.reports.fund_dashboard_html import render_dashboard, standalone_html
from src.notification_sender.email_sender import build_email_body


def prepare_fund_dashboard(config, provider, today, stock_codes, args, search_service=None):
    settings = load_dashboard_config(getattr(config,'fund_dashboard_config',None))
    groups = [(stock_codes, '股票'), (config.bond_fund_list,'债券'), (config.gold_etf_list,'黄金')]
    definitions={f['code']:f for f in settings['funds']}
    for codes,asset in groups:
        if any(code not in definitions or definitions[code]['asset']!=asset for code in codes):
            raise ValueError('资产列表与基金配置不一致，不能混用场外和ETF代码')
    selected=stock_codes+config.bond_fund_list+config.gold_etf_list
    data=build_dashboard(provider,today,settings,selected=selected,period=getattr(config,'fund_report_period','daily'))
    for item in data.observations:
        fund=next((f for f in FUNDS if f.otc==item.definition['otc']),None)
        if fund:
            try:
                data.auxiliary_reports[fund.otc]=(analyze_bond if fund.code==fund.otc else analyze_etf)(fund,provider,today)
            except Exception:
                item.issues.append('辅助市场明细暂不可用，正式净值观察继续。')
    data.market=analyze_macro(provider,today,bool(config.gold_etf_list),bool(config.bond_fund_list))
    if not args.dry_run:
        collect_official_events(search_service,data)
    assets,failures=render_charts(data,getattr(config,'fund_chart_font',None))
    report=render_dashboard(data,assets,failures,detail=settings.get('email_detail',False))
    full=render_dashboard(data,assets,failures,detail=True)
    root=Path(__file__).resolve().parents[2]/'reports'
    root.mkdir(exist_ok=True)
    stem=f'fund_dashboard_{today:%Y%m%d}_{data.period}'
    image_dir=root/stem
    image_dir.mkdir(exist_ok=True)
    for image in assets.values():
        (image_dir/(image.cid+'.png')).write_bytes(image.png)
    (root/(stem+'.md')).write_text(report.text,encoding='utf-8')
    (root/(stem+'.html')).write_text(standalone_html(report),encoding='utf-8')
    (root/(stem+'_details.html')).write_text(standalone_html(full),encoding='utf-8')
    message=build_email_body(report.text,report.html,report.images)
    message['Subject']=report.subject
    (root/(stem+'.eml')).write_bytes(message.as_bytes())
    return report
