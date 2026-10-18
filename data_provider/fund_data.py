"""Multi-asset public data adapters. No estimates, no undated quote fallbacks."""
from datetime import date, timedelta
from io import StringIO
import logging

import pandas as pd
import requests

logger = logging.getLogger(__name__)

MANAGER_URLS = {
    '006965': 'https://www.ctfund.com/funds/006965/overview/index.html',
    '007364': 'https://www.efunds.com.cn/Mobile/fund/007364.shtml',
}


def bounded_call(func, **kwargs):
    # Reuse the existing killable subprocess guard: several AkShare endpoints
    # have no HTTP timeout and a timed-out thread would keep the runner alive.
    from data_provider.akshare_fetcher import _akshare_call_with_timeout
    return _akshare_call_with_timeout(func, timeout=25, call_name=func.__name__, **kwargs)


def official_nav(code):
    """Only accept dated, explicitly labelled official NAV tables for this code.

    JS-only pages or changed schemas are deliberately unavailable. A homepage
    containing several funds must never be guessed into a NAV for one fund.
    """
    url = MANAGER_URLS[code]
    response = requests.get(url, timeout=12)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or 'utf-8'
    if code not in response.text or code not in url:
        raise ValueError('管理人页面未提供可按基金代码核验的静态净值表')
    for frame in pd.read_html(StringIO(response.text)):
        frame.columns = [str(col).replace('(元)', '').replace('（元）', '').strip() for col in frame.columns]
        frame = frame.rename(columns={'日期': '净值日期', '净值时间': '净值日期'})
        if {'净值日期', '单位净值', '累计净值'}.issubset(frame.columns):
            return frame[['净值日期', '单位净值', '累计净值']]
    raise ValueError('管理人净值表不可解析（可能需要动态接口），未完成交叉核对')


class FundDataProvider:
    """Cache successes and explicit failures only for the lifetime of one report."""

    def __init__(self, today=None):
        self.today = today or date.today()
        self._cache = {}
        self.errors = {}

    def get(self, key, loader):
        if key not in self._cache:
            try:
                self._cache[key] = loader()
                if self._cache[key] is None:
                    raise ValueError('接口返回 None')
            except Exception as exc:
                logger.warning('基金数据 %s 不可用: %s', key, exc)
                self.errors[key] = f'{type(exc).__name__}: {exc}'
                self._cache[key] = pd.DataFrame()
                self._cache[key].attrs['error'] = self.errors[key]
        return self._cache[key]

    def fund(self, code, indicator):
        def load():
            import akshare as ak
            return bounded_call(ak.fund_open_fund_info_em, symbol=code, indicator=indicator)
        return self.get((code, indicator), load)

    def manager(self, code):
        return self.get((code, '管理人核对'), lambda: official_nav(code))

    def etf(self, code):
        def load():
            import akshare as ak
            return bounded_call(
                ak.fund_etf_hist_em, symbol=code, period='daily', adjust='',
                start_date=(self.today - timedelta(days=550)).strftime('%Y%m%d'),
                end_date=self.today.strftime('%Y%m%d'),
            )
        frame = self.get((code, '行情'), load)
        if not frame.empty:
            frame.attrs['source'] = 'AkShare / 东方财富 ETF日线'
            return frame
        # Only unadjusted Yahoo OHLC may replace the raw exchange price.
        fallback = self.yahoo(code + '.SS')
        if not fallback.empty and {'date', 'Close', 'Volume'}.issubset(fallback.columns):
            frame = fallback.rename(columns={'date': '日期', 'Close': '收盘'}).copy()
            frame['成交量'] = pd.to_numeric(frame['Volume'], errors='coerce') / 100
            frame.attrs['source'] = 'YFinance / Yahoo（auto_adjust=False，成交量股转手）'
        return frame

    def gold_spot(self):
        def load():
            import akshare as ak
            return bounded_call(ak.spot_hist_sge, symbol='Au99.99')
        return self.get('SGE', load)

    def international_gold(self):
        def load():
            import akshare as ak
            # The API name includes futures, but XAU is Sina's London SPOT
            # gold instrument, not COMEX GC or a rolling futures contract.
            return bounded_call(ak.futures_foreign_hist, symbol='XAU')
        return self.get('伦敦金XAU', load)

    def yields(self):
        def load():
            import akshare as ak
            return bounded_call(
                ak.bond_china_yield,
                start_date=(self.today - timedelta(days=30)).strftime('%Y%m%d'),
                end_date=self.today.strftime('%Y%m%d'),
            )
        return self.get('中债收益率曲线', load)

    def yahoo(self, symbol):
        def load():
            import yfinance as yf
            return yf.Ticker(symbol).history(
                start=(self.today - timedelta(days=550)).isoformat(),
                end=(self.today + timedelta(days=1)).isoformat(),
                auto_adjust=False, timeout=12,
            ).rename_axis('date').reset_index()
        return self.get(symbol, load)

    def real_yield(self):
        def load():
            response = requests.get(
                'https://fred.stlouisfed.org/graph/fredgraph.csv',
                params={'id': 'DFII10', 'cosd': (self.today - timedelta(days=45)).isoformat()},
                timeout=12,
            )
            response.raise_for_status()
            return pd.read_csv(StringIO(response.text))
        return self.get('DFII10', load)
