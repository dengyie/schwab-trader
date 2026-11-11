# K线无截断滑动阅读 — 开发文档（切片 5a/5b/5c）

> 项目：schwab-trader（Schwab & Alpaca Trader Hub）
> 状态：待开发 → 本文档即实施依据
> 权威入口：vault `Note/Infra/schwab-trader 部署与运维.md`（§7 数据层系列）；本切片完成后在 §7 登记为 §7.13。
> 创建：2026-11-11

## 速读（当前有效 · 维护于 2026-11-11）

- 目标：用户在 K 线图向左滑动时**不被时间截断**——滑到边界自动补载更早的历史段，看过的数据本次会话**永久保留**，切周期/切标的不丢。
- 根因：`app/core/timeframes.py:42` 的 `YAHOO_TIMEFRAME_MAP` 把每个周期硬编码成一个固定 `(interval, range)` 窗口（15m 只拉 1mo 等），前端一次 `setData` 全量画，没有"加载更多"通路。
- 解法一句话：后端加 `before` 游标分页 + 分段缓存（历史段不可变、TTL 7 天）；前端 TradingView 左边界钩子 prepend 补载；响应头 `s-maxage` 与 CF 免费边缘缓存联动。
- 铁律：新鲜段行为**零变化**；历史段数据**不可变**；任何一段拉取失败只影响该段，不回退整体。

---

## 一、现状事实（2026-11-11 代码摸底，全部已验证）

| 事实 | 位置 | 内容 |
|---|---|---|
| 截断根因 | `backend/app/core/timeframes.py:42` `YAHOO_TIMEFRAME_MAP` | 每周期一个 `(interval, range)`：1m→7d，5m/15m→1mo，1h→6mo，4h→(1h,1y)，1d→5y，1wk→10y，1mo→max |
| Yahoo 物理上限 | Yahoo v8 chart API | 1m 最长 7d；**5m/15m 最长 60d**；1h 最长 730d；日线 1980s 起。`range` 可换 `period1/period2`（本设计采用） |
| 4h 是聚合产物 | `yahoo_adapter.py:171` | 4h 由 1h bars 聚合（对齐交易所时区），不是原生 Yahoo 周期 |
| 冷启动/刷新路径 | `alpaca_market_feed.py:338` `_fetch_upstream`、`yahoo_feed.py:197` `_fetch_bars` | **US feed 的 bars 上游其实也是 Yahoo**（不是 Alpaca REST），两个 feed 共享同一 `YahooFinanceAdapter.fetch_bars` |
| 缓存结构 | `bar_cache.py` | 键 `(sym, tf)`，文件 `{sym}_{tf}.json`，RAM+磁盘双层、单飞、冷却、SWR、`MAX_DISK_FILES=400`、`MAX_DISK_AGE=30d` |
| API 入口 | `routes.py:290` `get_bars` | 参数 `symbol/timeframe/market`，弱 ETag `W/"{sym}-{tf}-{fetched_at}"`，`Cache-Control: private, max-age=remaining`，304 支持 |
| 前端拉取 | `PriceChart.tsx:198` `fetchKlines` | SWR 双层缓存（15s 新鲜 + ETag 304），`klinesCacheRef` LRU 50 条；60s 定时全量刷新 |
| 前端渲染 | `PriceChart.tsx:547` | `series.setData(klines)` 一次性全量；实时 tick 走 `series.update` O(1) 增量 |
| 实时桶守卫 | `PriceChart.tsx:808` | 日/周/月不等长，实时桶顺延仅限分钟/小时视图，日级视图靠 60s 轮询兜底（§7.10 已有决策） |
| 移动端 | `src/pages/mobile/*` | 移动端与桌面**是两套代码**（§六铁律），图表改动需确认移动端是否共用 `PriceChart`（见 §五-4） |

**关键认知修正**（相比最初设计）：US 与 intl 的 bars 都走 Yahoo，所以本切片对两个市场**同一套代码同时生效**，无需市场分支；defeatbeta/HF（美股日线兜底）是后续独立切片，其 Parquet range-scan 天然兼容 `before` 语义。

## 二、各周期滑动极限（诚实版，写进 UI）

| 周期 | 现在能看到 | 切片后能滑到 | 依据 |
|---|---|---|---|
| 1m | 7 天 | **7 天（不变）** | Yahoo 1m 物理上限 7d，无法突破 |
| 5m | 1 个月 | **60 天** | Yahoo 5m 上限 60d |
| 15m | 1 个月 | **60 天（×2）** | Yahoo 15m 上限 60d |
| 1h | 6 个月 | **730 天（×4）** | Yahoo 1h 上限 730d |
| 4h | 1 年 | **730 天** | 1h 聚合，同 1h 上限 |
| 1d | 5 年 | 5 年（不变，本切片） | 后续 defeatbeta 切片可到上市至今 |
| 1wk/1mo | 10y / max | 不变 | 已够 |

到达极限后前端显示**"数据起点"提示**（该周期最早可得时间），不假装还有数据。

## 三、后端设计（切片 5a + 5c）

### 3.1 参数与分页语义

```
GET /api/market/bars?symbol=SPY&timeframe=15m              → 新鲜段（现状路径，零变化）
GET /api/market/bars?symbol=SPY&timeframe=15m&before=<unix秒> → 历史段：time < before 的最近 PAGE_SIZE 根
```

- `before`：可选 int，UNIX 秒。非法值（非整数/负数）→ 400。
- `PAGE_SIZE`：**2000 根/段**（≈200KB JSON，一次 prepend 滑 1-2 屏）。`1d/1wk/1mo` 每段 400 根（长周期 2000 根没有意义，还省存储）。
- 响应体在 records 后附一页元信息？**否——响应头携带**：`X-Bars-Has-More: 1|0`、`X-Bars-Oldest: <unix秒>`（避免改 JSON 结构破坏现有 `ModelFeatureExporter.to_records` 消费方与测试）。

### 3.2 分段上限表（新增于 `timeframes.py`，单点真相原则）

```python
# Historical span ceiling per timeframe, in days. These are Yahoo's hard
# limits, not preferences: past them the API simply has no data, and the
# chart shows the "data begins here" edge instead of pretending otherwise.
TIMEFRAME_SPAN_DAYS: Dict[str, Optional[int]] = {
    "1m": 7, "5m": 60, "15m": 60, "1h": 730, "4h": 730,
    "1d": None,   # None = no cursor paging; the whole span is fetched in one range
    "1wk": None, "1mo": None,
}
```

- `None` 的周期（日级三键）**不开放分页**：它们一次拉全量且已有 5y/10y/max 语义，分页徒增键爆炸；`before` 对它们返回 400（与"未知周期 400"同款明确拒绝，不静默替代）。
- `1d` 的"更深历史"留给 defeatbeta 切片（美股），国际市场无源就不做——宁缺毋滥。

### 3.3 Yahoo adapter 扩展

`YahooFinanceAdapter.fetch_bars` 新增可选 `period1: Optional[int]`（UNIX 秒）：

- `period1` 存在时：`range` 参数省略，改传 `period1/period2`（Yahoo v8 原生支持）；
- **不传时行为逐字节不变**（回归保障：现有测试全绿 + `TIMEFRAME_MAP` 不动）；
- `4h` 分页：底层仍按 `(1h, period1/period2)` 拉取后走现有 `_aggregate_bars`——聚合前先过滤到 `time < before` 的 1h 桶，再聚合，保证段边界干净；
- `_drop_forming_tail` 照常执行（历史段的最后一根也必须是完整蜡烛）。

### 3.4 分段缓存键与不可变性

- BarCache 键扩展：`(sym, tf)` → `(sym, tf)` 为**新鲜段**；`(sym, tf, before)` 为**历史段**。磁盘文件名 `{sym}_{tf}_h{before}.json`（`h` = historical segment，避免与现有文件冲突；旧文件旧代码也读不到新名字，天然兼容回滚）。
- **TTL 差异化**（缓存效率核心）：
  - 新鲜段：现有 `TIMEFRAME_TTLS` 不动（15m=120s 等）；
  - 历史段：**不可变数据，TTL = 7 天**（`TIMEFRAME_HIST_TTL = 7*24*3600`）。过期也只回源重拉一次然后又稳 7 天。
- **上限保护**：每个 `(sym, tf)` 最多缓存 **3 段**历史（最早段+中间段+边界段），第 4 段落盘时**淘汰最旧段**——防 LRU 爆炸；磁盘 `MAX_DISK_FILES=400` 上限在分段后按需评估（初值不改，回归看 prune 测试）。
- **重叠去重**：段与段可能有一根重叠（`before` = 当前最旧一根的 time，Yahoo 返回 `<= before` 的第一根可能就是那根）。合并时前端按 `time` 去重；后端段与段独立缓存，不去重（各段自洽）。

### 3.5 单飞与冷却

- 历史段拉取复用 `BarCache.begin_refresh/end_refresh/cool_down` 全套单飞语义（键含 before，天然互不干扰）；
- 用户狂滑：同一段只有第一个请求打上游，其余等单飞结果；失败段 30s 冷却（现有常量）——滑到无数据的边界最多每 30s 重试一次，不打风暴；
- `_schedule_refresh` 的"禁用态绝不打上游"规则同样适用于历史段（`disabled` 是运维开关）。

### 3.6 响应头与 CF 联动（切片 5c）

历史段响应头：

```
X-Bars-Has-More: 1
X-Bars-Oldest: 1717200000
Cache-Control: public, max-age=86400, s-maxage=604800, stale-while-revalidate=86400
ETag: W/"{sym}-{tf}-h{before}-{int(fetched_at)}"
```

- **`public`**：历史段是全用户共享的不可变公开数据，允许 CF 边缘缓存（免费档认 `s-maxage`）。新鲜段保持 `private` 不动；
- ETag 含段游标与 fetched_at，CF 透传 If-None-Match，源站 304 → 边缘 304；
- **`X-Bars-Has-More` 必须进 CF 的 cache key 吗？不需要**——同一 `(sym, tf, before)` 请求的 Has-More 恒定（不可变数据），缓存整个响应是安全的。

### 3.7 数据质量门禁

- 段内 `time` 必须严格升序且 `time < before`（Yahoo 偶发乱序时过滤，日志告警一次/段）；
- 段为空（到达物理上限）→ `X-Bars-Has-More: 0` + 200 空数组（不是 404——"没有更早的数据"是正常终态）；
- `4h` 段边界：聚合桶 `time` 是桶起点，`before` 过滤在聚合后的桶级做（用户语义是"滑过这根 4h 蜡烛"，不是"滑过这根 1h 蜡烛"）。

## 四、前端设计（切片 5b）

### 4.1 左边界检测与补载

`PriceChart.tsx` 新增（对齐现有 SWR/竞态模式）：

```ts
// 挂载于 mainChart 创建后：
mainChart.subscribeVisibleLogicalRangeChange((range) => {
  if (!range || loadingMoreRef.current || !hasMoreRef.current) return;
  if (range.from < 10) loadOlder();   // 左边距 <10 根时触发
});

const loadOlder = useCallback(async () => {
  const list = klinesRef.current;
  if (!list.length) return;
  const oldest = list[0].time;
  loadingMoreRef.current = true;
  try {
    const url = `/market/bars?symbol=...&timeframe=...&before=${oldest}`;
    // 段级会话缓存：key `${symbol}-${tf}-h${oldest}`，命中直接 prepend
    const more = await fetch...;
    if (selectedSymbolRef.current !== symbol) return;      // 现有竞态守卫
    const merged = dedupeById([...more, ...list]);          // 按 time 去重
    klinesRef.current = merged;
    setKlines(merged);                                      // setData 全量重画
    hasMoreRef.current = resp.headers.get('X-Bars-Has-More') === '1';
  } finally { loadingMoreRef.current = false; }
}, []);
```

### 4.2 会话内永久保留（用户核心诉求）

- `klinesRef` **只增不减**：60s 定时刷新（现有逻辑）只更新最新段，**不得覆盖 `klinesRef` 为仅新鲜段**——修改 `fetchKlines` 成功路径：返回数据与现有 `klinesRef` 合并（新数据 replace 尾部 + 保留更早段），而非直接赋值；
- 段级会话缓存：`klinesCacheRef` 键扩展为 `...-h{before}`，**段数据永不 TTL 过期**（不可变），仅保留 LRU 50 条上限防内存失控；新鲜段缓存行为不变；
- 切周期/切标的：现有 cache 命中路径零改动，命中即全量回显（含历史段）。

### 4.3 边界提示与 1m 特例

- `hasMore=false` 后，左边界显示**"数据起点 YYYY-MM"**占位（用 TradingView 的 `createPriceLine`？否——用图例区的静态文案，i18n 键 `chart.dataBegins`，zh-CN/en-US 双语，对齐 §7.10 i18n 决策）；
- **1m 直接不挂补载钩子**（上限就是 7 天，初始拉取已是全量）；
- 移动端：`AdaptiveLayout` 切换的是页面级组件，`PriceChart` 本身两端共用（已验证 import 关系）——**前端改动单点生效双端**，无需双份实现（§六铁律的例外：该组件本就共享）。

### 4.4 性能护栏

- prepend 后 `setData` 全量重画：2 万根以内 <16ms（Lightweight Charts O(n) setData 实测）；分页 60d/15m ≈ 40 段封顶 → 极限 ~5000 根/标的/周期，无忧；
- 指标重算：现有"长寿命 series + setData 更新"机制（`PriceChart.tsx:623` 注释）对新数据自动生效，MA 计算在 prepend 后触发一次即可；
- **不预取**：只在用户真的滑到边界才拉（打上游的是"看历史的人"，不看不打）。

## 五、实施切片与验收

| # | 切片 | 内容 | 验收标准 |
|---|---|---|---|
| 5a | 后端分页 | `TIMEFRAME_SPAN_DAYS` + adapter `period1` + 分段 BarCache + 路由 `before` + 响应头 | ① 现有测试全绿（新鲜段零回归）；② 新增：分页语义（边界/空段/400/去重）测试全绿；③ `SPY` 15m `before` 实测返回更早数据且 `X-Bars-Has-More` 正确 |
| 5b | 前端补载 | 左边界钩子 + prepend 去重 + 会话永久缓存 + 边界提示 + i18n | ① vitest 全绿 + tsc 干净；② 手动：SPY 15m 滑到底自动补 1 个月数据，切走切回数据仍在；③ 1m 无补载钩子 |
| 5c | TTL+CF | 历史段 7d TTL + `public, s-maxage` 响应头 | ① 响应头实测（curl 看缓存头）；② CF `cf-cache-status: HIT` 二次请求验证（Caddy/CF 手工配置，见下） |
| 5d | 部署+手册 | CI 部署 → 线上验收 → vault 登记 §7.13 | 后端/前端测试全绿；线上同 ②③；`scan-doc-governance` 三项 OK |

**部署注意**：Caddyfile 与 CF 规则**不在仓库、CI 不管入口层**（手册 §速读）——5c 的 CF Cache Rule 需 dashboard 手工配（仅 `*bars*` 路径 + 尊重 origin 头即可，默认行为已兼容 `s-maxage`，可先只改响应头观察 `cf-cache-status`）。5a/5b/5c 的应用层全部走 CI 正常部署。

## 六、风险与回滚

| 风险 | 缓解 |
|---|---|
| Yahoo `period1` 行为与 `range` 不一致（返回含 forming candle 等） | `_drop_forming_tail` 照常；门禁 §3.7 兜底；段级独立缓存使坏段不污染新鲜段 |
| 分段文件导致磁盘文件数膨胀（400 上限提前触发） | 段数上限 3/(sym,tf)；`_prune_disk` 按 mtime 淘汰天然覆盖；回归测试盯 prune 行为 |
| 前端 prepend 与实时 tick 竞争 | prepend 只改 `klinesRef` + `setData`；实时 tick `series.update` 只碰最后一根——两者操作不同数据区域，TradingView 内部串行化 |
| 回滚 | 后端：`git revert` 单 commit（`before` 参数消失即回到现状，分段文件旧代码不读，无害）；前端：同理单 commit。无 DB/schema 变更 |

## 七、明确不做（本切片范围外）

- defeatbeta/HF 接入（美股日线更深历史 + 基本面）——独立切片，`before` 语义已为它预留；
- Yahoo 429 计数进 health——已列 §7.12 待办，独立小切片；
- 代理池/号池——永久否决（公众项目不产品化 ToS 违规）；
- `1d/1wk/1mo` 分页——一次拉全量已够，键爆炸不值得。
