"""Headless static PNG charts; no client scripts or network assets."""
from dataclasses import dataclass
from io import BytesIO
import logging
import math
from pathlib import Path

from src.services.fund_dashboard import actual_allocation

logger = logging.getLogger(__name__)
GAIN, LOSS, GRAY, BLUE, ORANGE = '#d74848', '#168568', '#94a3b8', '#2563eb', '#d97706'
STATUS_COLORS = {'正常':'#2563eb', '部分缺失':'#94a3b8', '需修复':'#d97706', '暂不可用':'#64748b'}


@dataclass
class Chart:
    cid: str
    title: str
    png: bytes
    alt: str
    explanation: str
    note: str = ''


def percent(value):
    return '暂无可靠数据' if value is None else ('0.00%' if abs(value) < .00000001 else f'{value:+.2%}')


def chinese_font(path=None):
    from matplotlib import font_manager, ft2font
    if path:
        candidates = [str(Path(path))]
    else:
        candidates = []
        for name in ('Noto Sans CJK SC', 'Source Han Sans SC', 'PingFang SC', 'Heiti SC', 'STHeiti', 'Microsoft YaHei', 'SimHei', 'WenQuanYi Micro Hei'):
            try:
                candidates.append(font_manager.findfont(name, fallback_to_default=False))
            except ValueError:
                pass
    required = '基金净值收益率日期配置回撤暂无可靠数据股票债券黄金风险观察质量'
    for candidate in candidates:
        try:
            chars = ft2font.FT2Font(candidate).get_charmap()
            if all(ord(char) in chars for char in required):
                return candidate
        except Exception:
            continue
    raise ValueError('中文图表字体不可用；安装fonts-noto-cjk或配置FUND_CHART_FONT')


def _png(fig):
    from PIL import Image
    raw = BytesIO()
    fig.savefig(raw, format='png', dpi=180, facecolor='white', bbox_inches='tight')
    optimized = BytesIO()
    Image.open(BytesIO(raw.getvalue())).convert('RGB').quantize(colors=192).save(optimized, format='PNG', optimize=True)
    return optimized.getvalue()


def _style(ax):
    ax.set_facecolor('white')
    ax.spines[['top','right','left']].set_visible(False)
    ax.spines['bottom'].set_color('#dbe3ed')
    ax.tick_params(colors='#526176', length=0, pad=8)
    ax.grid(axis='x', color='#e8edf4', linewidth=.7)
    ax.set_axisbelow(True)


def render_charts(data, font_path=None):
    assets, failures = {}, {}
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        chosen = chinese_font(font_path)
        font_manager.fontManager.addfont(chosen)
        family = font_manager.FontProperties(fname=chosen).get_name()
    except Exception:
        logger.exception('中文图表运行环境不可用')
        return {}, {'all':'图表环境或中文字体不可用，保留全部文字与数据。'}
    by_code = {i.definition['otc']:i.definition for i in data.observations}

    def make(cid, title, painter, explanation, note=''):
        fig = None
        try:
            with plt.rc_context({'font.family':family,'font.size':11, 'axes.unicode_minus':False,
                                 'axes.titlesize':14, 'axes.labelsize':10, 'figure.autolayout':True}):
                fig = plt.figure(figsize=(8.0,4.4))
                painter(fig)
                image = _png(fig)
                assets[cid] = Chart(cid,title,image,title+'；'+explanation,explanation,note)
        except Exception:
            logger.exception('基金图表%s失败，保留其他图表',cid)
            failures[cid] = title+'暂未生成，仍可阅读文字数据。'
        finally:
            if fig is not None:
                plt.close(fig)

    for days in (7,30,90):
        rows = data.periods[days]
        ordered = sorted(by_code,key=lambda c: rows[c]['return'] if c in rows else -math.inf,reverse=True)
        comparable = [(c,m) for c,m in rows.items() if not m['substitute']]
        if comparable:
            highest=max(comparable,key=lambda x:x[1]['return']); lowest=min(comparable,key=lambda x:x[1]['return'])
            observation=f"{by_code[highest[0]]['short_name']}为{percent(highest[1]['return'])}，{by_code[lowest[0]]['short_name']}为{percent(lowest[1]['return'])}。"
        else:
            observation='场外含分红数据不足，暂不能可靠比较基金表现。'
        explanation=f'展示近{days}自然日收益，按展示值排序。'+observation+'长期定投应结合目标配置，不将短期领先当作买入依据。'
        def draw_returns(fig, rows=rows, ordered=ordered, days=days):
            ax=fig.subplots(); _style(ax)
            for y,code in enumerate(ordered):
                value=rows[code]['return']*100 if code in rows else None
                if value is None:
                    ax.text(0,y,'暂无可靠数据',color=GRAY,va='center',fontsize=10)
                else:
                    ax.barh(y,value,height=.48,color=GAIN if value>0 else LOSS if value<0 else GRAY)
                    ax.annotate(f'{value:+.2f}%' if value else '0.00%',(value,y),xytext=(5 if value>=0 else -5,0),textcoords='offset points',va='center',ha='left' if value>=0 else 'right',fontsize=10,color='#26344a')
            labels=[by_code[c]['short_name']+('（ETF替代）' if c in rows and rows[c]['substitute'] else '') for c in ordered]
            ax.set_yticks(range(len(labels)),labels); ax.invert_yaxis(); ax.axvline(0,color='#9caabd',lw=.8)
            ax.set_xlabel('区间收益率（%）；红涨绿跌'); ax.set_title(f'五基金近{days}日收益观察',loc='left',pad=20)
            bounds=[r['return']*100 for r in rows.values()]+[0]
            spread=max(max(bounds)-min(bounds),1)
            ax.set_xlim(min(bounds)-spread*.35,max(bounds)+spread*.55)
        first=next(iter(rows.values()),None)
        note=(f"共同区间：{first['start']}—{first['end']}。" if first else '暂无共同可靠区间。')+'场外采用分红再投资；ETF替代仅为未复权价格观察，不是场外实际收益。'
        make(f'returns-{days}',f'近{days}日收益对比',draw_returns,explanation,note)

    def draw_trend(fig):
        import matplotlib.dates as mdates
        ax=fig.subplots(); _style(ax); ax.grid(axis='y',color='#e8edf4')
        for code,row in data.trend.items():
            ax.plot(list(row['index'].index),row['index'].values,color=by_code[code]['color'],lw=1.8,label=by_code[code]['short_name'])
        ax.axhline(100,color=GRAY,ls='--',lw=.9,label='起点基准100')
        ax.set_title('历史含分红净值趋势',loc='left',pad=20); ax.set_ylabel('归一化投资价值（起点=100）'); ax.set_xlabel('真实净值确认日期')
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3,maxticks=5)); ax.xaxis.set_major_formatter(mdates.DateFormatter('%m月%d日'))
        ax.legend(loc='upper left',ncol=3,fontsize=9,frameon=False)
        if not data.trend: ax.text(.5,.5,'暂无可靠历史数据',ha='center',transform=ax.transAxes,color=GRAY)
    first=next(iter(data.trend.values()),None)
    trend_note=(f"共同区间 {first['start']}—{first['end']}；" if first else '')+'来源：AkShare / 东方财富正式净值、分红及拆分记录；非交易日不插值。排除：'+('；'.join(by_code[c]['short_name']+'：'+reason for c,reason in data.exclusions['trend'].items()) or '无')
    trend_observation=('期末最高的'+by_code[max(data.trend,key=lambda c:data.trend[c]['return'])]['short_name']+'为'+f"{max(r['index'].iloc[-1] for r in data.trend.values()):.2f}。" if data.trend else '可靠历史不足，无法比较。')
    make('trend','归一化净值趋势',draw_trend,trend_observation+'展示同日起点为100的含分红投资价值。曲线高低反映共同区间内的累计变化，不能代表未来收益；长期定投应观察过程与承受能力，缺失序列已排除。',trend_note)

    risk=data.periods['risk']
    def draw_risk(fig):
        ax=fig.subplots(); _style(ax)
        for y,code in enumerate(by_code):
            if code not in risk:
                ax.text(0,y,'暂无可靠数据',color=GRAY,va='center'); continue
            r=risk[code]
            for offset,key,hatch in ((-.15,'return',None),(.15,'drawdown','///')):
                value=r[key]*100
                ax.barh(y+offset,value,height=.25,color=GAIN if value>0 else LOSS if value<0 else GRAY,hatch=hatch,edgecolor='#526176',linewidth=.3)
                ax.annotate(f'{value:+.2f}%' if value else '0.00%',(value,y+offset),xytext=(4 if value>=0 else -4,0),textcoords='offset points',ha='left' if value>=0 else 'right',va='center',fontsize=8)
        ax.set_yticks(range(len(by_code)),[f['short_name'] for f in by_code.values()]); ax.invert_yaxis(); ax.axvline(0,color=GRAY,lw=.6)
        ax.set_xlabel('收益率 / 最大回撤（%）'); ax.set_title('近90日收益与最大回撤',loc='left',pad=20)
        from matplotlib.patches import Patch
        ax.legend(handles=[Patch(facecolor=GAIN,label='累计收益：实色（红涨绿跌）'),Patch(facecolor=LOSS,hatch='///',label='最大回撤：斜线')],fontsize=9,frameon=False,loc='lower right')
        vals=[r[k]*100 for r in risk.values() for k in ('return','drawdown')]+[0]; gap=max(max(vals)-min(vals),1)
        ax.set_xlim(min(vals)-gap*.35,max(vals)+gap*.4)
    deepest=min(risk,key=lambda c:risk[c]['drawdown']) if risk else None
    text=(f"{by_code[deepest]['short_name']}的区间最大回撤为{percent(risk[deepest]['drawdown'])}。" if deepest else '可靠样本不足，无法比较。')
    make('risk','收益与最大回撤',draw_risk,'展示相同90日区间的含分红收益和峰值至后续低点的跌幅。'+text+'回撤是历史风险观察，不代表未来最大亏损，也不直接触发止损。','仅纳入场外含分红再投资序列；ETF替代不参与。')

    def draw_scatter(fig):
        ax=fig.subplots(); _style(ax); ax.grid(axis='y',color='#e8edf4')
        for code,row in risk.items():
            x=abs(row['drawdown'])*100; y=row['return']*100
            ax.scatter(x,y,color=by_code[code]['color'],s=70,edgecolor='white',linewidth=1)
            ax.annotate(by_code[code]['short_name'],(x,y),xytext=(10,18 if list(risk).index(code)%2 else -18),textcoords='offset points',fontsize=9,arrowprops={'arrowstyle':'-','color':GRAY,'lw':.6})
        ax.set_title('历史风险与收益位置',loc='left',pad=20); ax.set_xlabel('最大回撤绝对值（%，越左历史回撤越小）'); ax.set_ylabel('90日累计收益（%，越上历史收益越高）')
        ax.margins(y=.18)
        xs=[abs(r['drawdown'])*100 for r in risk.values()]; ax.set_xlim(-.1,max(xs+[1])*1.4)
        if not risk: ax.text(.5,.5,'暂无可靠数据，未绘制虚构点',ha='center',transform=ax.transAxes,color=GRAY)
    scatter_observation=(by_code[min(risk,key=lambda c:abs(risk[c]['drawdown']))]['short_name']+'在本期历史回撤最小。' if risk else '无可靠图点，不能作比较。')
    make('scatter','风险收益散点',draw_scatter,scatter_observation+'每个点对应同一历史区间的收益与回撤，左上方在本区间表现较均衡。不同资产目标不同，不应只凭位置决定优劣；该图不预测未来，也不生成买卖指令。','缺少可靠场外总回报的基金不绘制。')

    weights,holding_note=actual_allocation(data.config,data.report_date)
    def draw_allocation(fig):
        left,right=fig.subplots(1,2,gridspec_kw={'width_ratios':[1,1.3]})
        assets={a:sum(f.get('target_pct') or 0 for f in data.config['funds'] if f['asset']==a) for a in ('股票','债券','黄金')}
        rest=max(0,100-sum(assets.values()))
        if rest>1e-8: assets['未配置']=rest
        chosen=[(k,v) for k,v in assets.items() if v>0]
        if chosen:
            left.pie([v for _,v in chosen],colors=[{'股票':BLUE,'债券':'#0d9488','黄金':'#d29b18','未配置':GRAY}[k] for k,_ in chosen],startangle=90,wedgeprops={'width':.3,'edgecolor':'white'},autopct=lambda x:f'{x:.0f}%' if x>0 else '',pctdistance=.83,textprops={'fontsize':10})
            left.text(0,0,'目标\n资产配置',ha='center',va='center',fontsize=12,color='#26344a')
            left.legend([f'{k} 已配置{v:g}%' for k,v in chosen],loc='lower center',bbox_to_anchor=(.5,-.17),fontsize=9,frameon=False)
        _style(right)
        funds=data.config['funds']
        for y,f in enumerate(funds):
            target=f.get('target_pct')
            if target is None: right.text(0,y,'目标未配置',color=GRAY,va='center')
            else:
                right.barh(y-.13 if weights else y,target,height=.25 if weights else .42,color=f['color'])
                right.text(target+.7,y-.13 if weights else y,f'{target:.1f}%',va='center',fontsize=9)
            if weights:
                right.barh(y+.13,weights[f['otc']],height=.22,color=f['color'],alpha=.4,hatch='//')
        right.set_yticks(range(5),[f['short_name'] for f in funds]); right.invert_yaxis(); right.set_xlim(0,max([f.get('target_pct') or 0 for f in funds]+list(weights.values())+[40])*1.3)
        right.set_xlabel('基金占比（%）；斜线为实际市值占比' if weights else '基金目标占比（%）')
        fig.suptitle('长期目标配置与持仓观察',x=.02,ha='left',fontsize=14)
    allocation_observation='目标：'+'、'.join(a+f"{sum(f.get('target_pct') or 0 for f in data.config['funds'] if f['asset']==a):g}%" for a in ('股票','债券','黄金'))+'。'
    make('allocation','目标资产配置',draw_allocation,allocation_observation+'展示已明确的基金与资产类别目标，而不是收益预测。未配置比例保持空缺；只有完整、同日的真实持仓市值才计算实际占比与偏离，用于再平衡观察，不自动交易。',holding_note)

    def draw_quality(fig):
        ax=fig.subplots(); ax.set_xlim(0,1.6)
        for y,item in enumerate(data.observations):
            ax.barh(y,1,height=.5,color=STATUS_COLORS[item.status],alpha=.16)
            ax.text(.04,y,item.status,color=STATUS_COLORS[item.status],va='center',weight='bold')
        ax.set_yticks(range(len(data.observations)),[i.definition['short_name'] for i in data.observations]); ax.invert_yaxis(); ax.set_xticks([])
        for spine in ax.spines.values(): spine.set_visible(False)
        ax.tick_params(length=0); ax.set_title('数据可靠性：按核心问题严重程度分类',loc='left',pad=20)
    quality_observation='本期'+str(sum(i.status=='正常' for i in data.observations))+'只正常，'+str(sum(i.status in ('需修复','暂不可用') for i in data.observations))+'只存在核心缺口。'
    make('quality','数据可靠性',draw_quality,quality_observation+'展示正式净值、分红、日期及收益验证的严重程度。核心异常标为需修复，辅助缺口单独披露；状态不由报错数量决定，无法核验的数据不支持明确投资结论。','007364分红异常时暂停含分红收益；技术细节仅留日志。')
    return assets,failures
