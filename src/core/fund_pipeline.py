"""Opt-in multi-asset orchestration, one saved report and one notification call."""
from datetime import datetime
import logging
from zoneinfo import ZoneInfo

from data_provider.fund_data import FundDataProvider
from src.reports.fund_report import render_fund_report
from src.services.fund_analysis import (
    FundReport, Metric, analyze_bond, analyze_etf, analyze_macro, selected_funds,
)

logger = logging.getLogger(__name__)


def multi_asset_enabled(config):
    return bool(getattr(config, 'bond_fund_list', []) or getattr(config, 'gold_etf_list', []))


def run_fund_analysis(config, args, pipeline, stock_codes, *, provider=None, today=None, market_loader=None):
    today = today or datetime.now(ZoneInfo('Asia/Shanghai')).date()
    funds = selected_funds(stock_codes, config.bond_fund_list, config.gold_etf_list)
    provider = provider or FundDataProvider(today)
    if getattr(config, 'fund_report_format', 'legacy') == 'html':
        from src.core.fund_dashboard_pipeline import prepare_fund_dashboard
        # Preparation is separate from delivery: never retry sending via legacy
        # after an SMTP/notification failure (which could duplicate an email).
        try:
            report = prepare_fund_dashboard(
                config, provider, today, stock_codes, args,
                search_service=getattr(pipeline, 'search_service', None),
            )
        except Exception:
            logger.exception('可视化报告准备失败，恢复原版日报')
        else:
            if args.no_notify or args.dry_run:
                return True
            return pipeline.notifier.send(
                report.text, email_send_to_all=True, route_type='report', email_report=report,
            )
    reports = []
    for fund in funds:
        try:
            if fund.kind in ('主动债券基金', '债券指数基金'):
                report = analyze_bond(fund, provider, today)
            else:
                report = analyze_etf(fund, provider, today)
        except Exception as exc:
            logger.exception('基金 %s 分析失败，继续其他基金', fund.code)
            report = FundReport(fund, [Metric('模块状态', reason=f'{type(exc).__name__}: {exc}')])
        if fund.kind == '股票ETF':
            try:
                results = pipeline.run(stock_codes=[fund.code], dry_run=args.dry_run, send_notification=False)
                if results:
                    report.existing_report = pipeline.notifier.generate_single_stock_report(results[0])
                elif not args.dry_run:
                    report.notes.append('原系统技术/新闻/决策分析未返回结果，保留上述确定性指标。')
            except Exception as exc:
                logger.exception('ETF %s 原分析链路失败', fund.code)
                report.notes.append(f'原系统技术/新闻/决策分析数据不足：{type(exc).__name__}')
        reports.append(report)
    macro = analyze_macro(provider, today, bool(config.gold_etf_list), bool(config.bond_fund_list))
    market_report = ''
    if market_loader is not None and not args.dry_run:
        try:
            market_report = market_loader()
        except Exception as exc:
            logger.exception('大盘复盘失败，保留基金报告')
            macro.append(Metric('大盘复盘', reason=f'{type(exc).__name__}: {exc}'))
    content = render_fund_report(reports, macro, today, market_report)
    # The stock pipeline retains DB history and local ETF reports, but never sends.
    # This combined artifact is saved even when --no-notify or --dry-run is used.
    path = pipeline.notifier.save_report_to_file(content, filename=f'fund_report_{today:%Y%m%d}.md')
    logger.info('多资产合并报告已保存: %s', path)
    if not args.no_notify and not args.dry_run:
        if not pipeline.notifier.send(content, email_send_to_all=True, route_type='report'):
            logger.error('基金合并报告通知发送失败，已保留本地报告')
            return False
    return True
