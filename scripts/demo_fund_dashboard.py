"""Reproduce a historical synthetic demo, never fetch data or send email."""
import argparse
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from src.services.fund_dashboard import load_dashboard_config, build_dashboard
from src.core.trading_calendar import get_trading_dates
from src.reports.fund_charts import render_charts
from src.reports.fund_dashboard_html import render_dashboard, standalone_html
from src.notification_sender.email_sender import build_email_body


class SampleFundProvider:
    """Deterministic replay of existing NAV/dividend fixture shapes, not live NAV."""
    def __init__(self, degraded=False):
        self.errors={}
        self.degraded=degraded
        self.dates=get_trading_dates('cn',date(2025,5,5),date(2026,10,16)).date
        self.ex_date=self.dates[-60]
        self.sources={}

    def values(self,code):
        x=np.arange(len(self.dates),dtype=float)
        params={'022430':(.00017,.055),'020602':(.00025,.022),'006965':(.00006,.0015),
                '007364':(.00008,.003),'008701':(.00032,.04)}
        drift,amplitude=params.get(code,(.0001,.005))
        underlying=1+drift*x+amplitude*np.sin(x/17+list(params).index(code))
        paid=np.where(self.dates>=self.ex_date,.02,0.)
        return underlying-paid,underlying

    def fund(self,code,indicator):
        if indicator=='分红送配详情':
            if self.degraded and code=='007364':
                self.errors[(code,indicator)]='fixture only: provider timeout'
                return pd.DataFrame()
            return pd.DataFrame({'除息日':[self.ex_date],'每10份分红':['每10份派现金0.2000元']})
        if indicator=='拆分详情':return pd.DataFrame()
        nav,acc=self.values(code)
        column='单位净值' if indicator=='单位净值走势' else '累计净值'
        return pd.DataFrame({'净值日期':self.dates,column:nav if column=='单位净值' else acc})

    def manager(self,code):
        nav,acc=self.values(code)
        return pd.DataFrame({'净值日期':self.dates,'单位净值':nav,'累计净值':acc})

    def etf(self,code):
        mapping={'512050':'022430','563020':'020602','518850':'008701'}
        nav,_=self.values(mapping[code])
        return pd.DataFrame({'日期':self.dates,'收盘':nav*1.001,'成交量':10000.,'成交额':2000000.})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--config')
    parser.add_argument('--degraded',action='store_true')
    args=parser.parse_args()
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    settings=load_dashboard_config(args.config)
    data=build_dashboard(SampleFundProvider(args.degraded),date(2026,10,20),settings,
        sample_label='演示样本：净值及收益均为合成测试数据，最新样本日期2026-10-16，报告日期2026-10-20；目标配置及投入记录按所选配置。非实时行情，未发送邮件。')
    assets,failures=render_charts(data)
    if len(assets)!=8:
        raise RuntimeError('六类图表未完整生成：'+str(failures))
    full=render_dashboard(data,assets,failures,detail=True)
    compact=render_dashboard(data,assets,failures,detail=False)
    (output/'dashboard-demo.html').write_text(standalone_html(full),encoding='utf-8')
    (output/'daily-email-preview.html').write_text(standalone_html(compact),encoding='utf-8')
    (output/'dashboard-demo.txt').write_text(compact.text,encoding='utf-8')
    for image in assets.values(): (output/(image.cid+'.png')).write_bytes(image.png)
    message=build_email_body(compact.text,compact.html,compact.images)
    message['Subject']=compact.subject+'（样本）'
    (output/'dashboard-demo.eml').write_bytes(message.as_bytes())
    print(f'完成：六类图表、{len(assets)}幅PNG；精简邮件{len(compact.images)}幅；MIME {len(message.as_bytes()):,}字节；未发送')


if __name__=='__main__':main()
