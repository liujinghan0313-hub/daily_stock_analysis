# 五基金每日分析（可选）

在原股票分析流程上增加债券基金、黄金ETF模块，并通过现有 NotificationService / EmailSender 发送一份中文合并报告。新增变量为空时，原有股票、大盘及邮件行为保持不变。仅扩展 CLI / 定时任务主流程；不改变 Web/API 分析接口及载荷。

## 配置

在 GitHub 仓库 Settings → Secrets and variables → Actions → Variables 设置：

```dotenv
STOCK_LIST=512050,563020
BOND_FUND_LIST=006965,007364
GOLD_ETF_LIST=518850
```

保留现有邮件和模型 Secrets。工作流 `.github/workflows/00-daily-analysis.yml` 显式传入新变量。无需增加依赖。工作日北京时间16:10首次尝试，17:10和18:10备用；已成功发送邮件的当天由 Artifact 标记跳过备用运行。交易日过滤继续生效，交易所节假日跳过。GitHub 的定时任务仍可能延迟或漏触发，实际收件时间无法保证；手动 `--force-run` 可以生成带真实数据日期的历史复盘。

| 支付宝场外基金 | 支付宝代码 | 实际分析代码 | 模块 |
| --- | --- | --- | --- |
| 华夏中证A500ETF联接A | 022430 | 512050 | 原股票ETF分析 + 确定性指标 |
| 易方达中证红利低波动ETF联接发起式A | 020602 | 563020 | 原股票ETF分析 + 确定性指标 |
| 财通安瑞短债债券A | 006965 | 006965 | 正式基金净值与债券风险 |
| 易方达中债1—3年政策性金融债指数A | 007364 | 007364 | 正式基金净值与指数跟踪风险 |
| 华夏黄金ETF联接A | 008701 | 518850 | 黄金ETF与黄金市场 |

五基金模式对上述代码进行严格类别校验并去重，顺序固定为表格顺序。错把场外联接代码放进 STOCK_LIST 或把债券放入股票列表会明确报错，不会触发错误资产分析。可以配置上述清单的子集；五基金模式中不支持额外证券。移除两项新增变量即恢复原有任意股票列表行为。

本地和 Docker 通过环境变量或 `.env` 配置；新增列表在进程启动时读取，修改后重启。Web 设置页没有新增输入控件。`--market-review` 仍仅运行原大盘复盘。正常执行 `python main.py` 或 `python main.py --no-market-review` 使用新链路。`--no-notify` 不发送；`--dry-run` 只获取数据并保存合并诊断报告，不调用股票LLM，不发送。

## 计算与数据约定

每项指标都有来源、数据日期/实际区间、数据质量和复权/分红口径。所有接口失败记录日志并以“数据不足”进入报告，禁止估算正式净值或使用旧值冒充当日值。

- ETF报价：AkShare `fund_etf_hist_em(adjust='')`；失败时备选YFinance对应上海ETF的原始价格（`auto_adjust=False`，成交量股转手；成交额不可得则留空），展示未复权收盘价、成交量（手）、成交额（元）、20/60交易样本均线和量能变化。7/30/90自然日价格收益、90日/1年价格回撤不含分红，不声称为投资者总回报。原技术、新闻及决策分析继续使用既有 StockAnalysisPipeline、多数据源与原报告生成器。
- ETF折溢价：只用该ETF同日未复权收盘价和正式单位净值 `close/nav-1`；不是盘中IOPV，也不能代替场外联接基金净值。当天ETF净值未发布则留空；保留最近已确认净值的日期。指数估值没有可核验源时显示“数据不足”，不用ETF自身PE代替指数PE。
- 债券净值：AkShare `fund_open_fund_info_em` 的单位净值、累计净值、分红送配详情、拆分详情。单位/累计净值分别展示自己的日期；不做累计净值差额收益。
- 债券总回报：假定每份现金分红在**除息日按该日单位净值再投资**，每10份分红明确除以10换算成每份；连乘 `(NAV_t + 每份现金分红_t) / NAV_(t-1)`。累计净值只参与核验：其与单位净值差额的变化必须能被分红记录解释，容忍净值展示精度差异0.00021元。实际账户再投资确认日期、申赎费用及平台费率可能导致不同收益。
- 分红接口异常不能当成“无分红”。除息日净值缺失、分红单位不明确或字段与内容冲突、分红不一致、拆分因子不可靠时，停止总回报和回撤计算并说明原因。目前不猜测拆分/折算比例，含拆分历史的总回报显示数据不足。
- 7/30/90/365为自然日，以最近已确认数据日为终点；起点取目标日期及之前最近记录，节假日最多回溯14日，报告展示真实起止日。按既有中国交易日历核验区间观测完整性，缺少交易日不前向填充。最大回撤为区间序列相对区间内历史峰值的最小变动率，不将区间外峰值混入。
- 管理人核对：尝试读取财通/易方达官方产品页面的带日期、单位净值、累计净值静态表，按同日核对。页面动态加载、访问受限或表结构改变则明确“未完成交叉核对”；同日冲突会抑制相关净值和收益指标。未完成官方交叉核对的第三方数据不能视为已双源验证。
- 债券市场：AkShare `bond_china_yield`，展示国债/国开债1、3、10年收益率及相对前次变化（bp），不把收益率水平当作基金回报。信用、流动性、杠杆、久期、指数跟踪讨论为风险观察框架；没有最新持仓/久期/基准财富指数时，信用暴露、久期及跟踪误差不输出虚构数值。
- 黄金：SGE `spot_hist_sge('Au99.99')` 人民币元/克；AkShare新浪 `futures_foreign_hist(symbol="XAU")` 伦敦黄金现货（接口名称含期货但XAU品种为现货）；YFinance `DX-Y.NYB` 美元指数、`CNY=X` 汇率；FRED `DFII10` 美国10年TIPS实际收益率。某个接口不可用时明确缺口，不以COMEX黄金期货冒充现货。各市场日期单独保留，收盘时点并不相同。
- 黄金ETF与现货关系：配对最近31个同日价格，计算30个日收益的相关性；20个收益样本波动率按252日年化。数据不全或方差为零时不计算。这不是跟踪误差或因果判断。
- 未提供实际持仓权重，不计算虚构的组合收益、组合回撤或推荐买入金额。

接口调用复用现有 AkShare 可终止子进程超时（25秒）；管理人/FRED/YFinance请求设12秒超时。所有缓存仅在当次报告内复用，包含明确的失败记录，不跨日冒充新数据。

## 报告、邮件与历史

报告按摘要、两只股票ETF、两只债券基金、黄金ETF、组合风险顺序生成。债券与黄金不进入股票买卖信号链路。一只基金或原股票LLM失败会保留该节缺口，并继续处理其他基金。

股票分析和可选大盘复盘始终以 `send_notification=False` 运行；末尾仅调用一次现有 `NotificationService.send(..., email_send_to_all=True, route_type='report')`，收件人按既有 EmailSender 规则合并。即使 SINGLE_STOCK_NOTIFY=true，也不发送单只基金邮件。通知路由、静默时段和已有渠道策略仍然适用；要每天收到邮件仍需正确配置邮箱及通知策略。

合并报告保存在 `reports/fund_report_YYYYMMDD.md`。原股票数据库历史、各ETF本地报告、日志与Actions Artifacts机制继续保留。原报告生成器可能按日覆盖单ETF汇总文件，完整五基金内容以合并文件为准。无通知和dry-run也保存合并报告；通知失败返回失败状态，文件仍保留。

## 验证及回滚

```bash
python -m pytest tests/test_fund_analysis.py
python -m pytest tests/test_main_portfolio.py tests/test_config_env_compat.py tests/test_notification_sender.py tests/test_trading_calendar.py tests/test_daily_analysis_workflow_notification_env.py
./scripts/ci_gate.sh
python -m py_compile main.py src/config.py data_provider/fund_data.py src/services/fund_analysis.py src/core/fund_pipeline.py src/reports/fund_report.py
```

测试使用人工构造的净值、分红、价格及异常，覆盖分红再投资与累计净值误用反例、资产类型、日期缺失、拆分拒绝、官方冲突、单基金失败和单次合并发送。在线数据可用性和真实SMTP送达属于独立验收；离线通过不代表所有免费接口当日可用。

紧急回滚：清空 BOND_FUND_LIST 和 GOLD_ETF_LIST 并重新运行，即走原有代码路径；需要彻底撤销时还原本功能涉及文件。无需数据库迁移。未修改英文README：本能力面向人民币基金中文报告，详细约定集中在本专题文档，首页定位及英文原能力不变。

数据接口依据：[AkShare基金接口源码](https://github.com/akfamily/akshare/blob/main/akshare/fund/fund_em.py)、[SGE接口说明](https://github.com/akfamily/akshare/blob/main/docs/data/spot/spot.md)、[中债收益率曲线](https://yield.chinabond.com.cn/cbweb-cbrc-web/cbrc/showCbrc)、[易方达官方007364](https://www.efunds.com.cn/Mobile/fund/007364.shtml)、[华夏008701](https://www.chinaamc.com.cn/fund/008701/index.shtml)。
