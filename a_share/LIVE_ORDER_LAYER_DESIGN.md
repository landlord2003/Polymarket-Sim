# 真实下单层设计文档（Live Order Layer）

> 版本：draft-2026-08-29 ｜ 范围：**架构 + stub 代码骨架，不接真钱**
> 前提：合规按"平台拿到国外用"处理，本设计不评估合规/地理限制，仅处理技术接入。
> 配套：A 报告《模拟盘成熟度报告》—— 接真实资金前须先满足其三项前置条件。

---

## 一、目标与边界

**目标**：在现有 `sim_trader`（虚拟记账）之外，新增一层"真实下单 + 真实资金"的执行器，使策略逻辑（MM / 纯套利扫描判定）可无缝切换到 Polymarket 真实 CLOB。

**边界（本设计明确不碰真钱）**：
- 所有 `stub` 默认 `DRY_RUN=True`，下单只构造/打印签名订单，不发送、不广播、不动用 USDC。
- 不创建真实 L2 钱包、不 approve 真实 USDC、不调用任何会动链的写操作。
- 真实接入需老吴显式翻转 `DRY_RUN=False` 并注入真实私钥（见第五节）。

---

## 二、整体架构

```
┌─────────────────────────────────────────────────────────────┐
│  sim_trader.run_once()  （策略调度，不变）                     │
│    └─ scan_poly() → 候选(opp)                                │
└───────────────────────────┬─────────────────────────────────┘
                            │  opp + size
                            ▼
              ┌──────────────────────────┐
              │   Executor (抽象接口)      │  ← 切换点
              │   .market_make(opp,size)  │
              │   .pure_arb(opp,size)     │
              └──────┬────────────┬───────┘
                     │            │
        ┌────────────▼───┐   ┌────▼──────────────┐
        │ RigorVirtualBook│   │ LiveExecutor (stub)│  ← 本设计新增
        │ (现有,虚拟)     │   │  DRY_RUN 默认 True │
        └────────────────┘   └───┬────────────────┘
                                 │
                     ┌───────────▼───────────┐
                     │ ClobClient (stub)      │  Polymarket CLOB API
                     │  sign_order / post     │  https://clob.polymarket.com
                     └───────────┬───────────┘
                                 │ EIP-712 签名订单
                     ┌───────────▼───────────┐
                     │  Wallet (stub)         │  L2 私钥 / USDC(Polygon)
                     │  签名 / 余额查询        │
                     └───────────┬───────────┘
                                 │
              ┌──────────────────▼──────────────────┐
              │  Reconcile (每日对账, stub)            │
              │  虚拟账本 vs 链上持仓/USDC 余额         │
              └──────────────────┬──────────────────┘
                                 │ 不一致 → 熔断/告警
              ┌──────────────────▼──────────────────┐
              │  CircuitBreaker (熔断/幂等, stub)      │
              │  断网重试 / 重复单 dedup / nonce / 拥堵 │
              └───────────────────────────────────────┘
```

切换点：把 `sim_trader` 里的 `book: RigorVirtualBook` 换成 `book: LiveExecutor`（实现同一 `market_make` / `pure_arb` 接口），策略代码零改动。

---

## 三、Executor 抽象接口（核心：让策略无感切换）

```python
# live_executor.py  (stub)
class OrderExecutor:
    """策略层只依赖此接口；虚拟/真实两种实现可互换。"""
    def market_make(self, opp, size_shares) -> dict: ...
    def pure_arb(self, opp, size_shares) -> dict: ...
    def view(self) -> dict: ...   # 返回 {cash, realized_pnl, positions}
    def _save(self): ...
```

`LiveExecutor` 实现该接口，内部委托 `ClobClient` + `Wallet` + `Reconcile` + `CircuitBreaker`。

---

## 四、ClobClient 封装（stub）

基于 Polymarket 公开 CLOB API（`polymarket-clob-client` 客户端语义）：

```python
# clob_client.py  (stub)
import os

CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")

class ClobClient:
    def __init__(self, host=CLOB_HOST, api_key=None, wallet=None, dry_run=True):
        self.host = host
        self.api_key = api_key      # L2 API key (链下鉴权)
        self.wallet = wallet        # Wallet stub
        self.dry_run = dry_run      # True=只构造不发送

    def build_order(self, opp, side, size, price) -> dict:
        """构造 EIP-712 订单结构（不签名）。"""
        return {
            "owner": self.wallet.address,
            "tokenId": opp["token_id"],       # CTF 仓位 id（来自 Gamma）
            "side": side,                     # "BUY"/"SELL"
            "price": price,
            "size": size,
            "feeRateBps": 0,                  # CLOB 当前 0 手续费（占位）
            "nonce": self._next_nonce(),      # 见第七节
            "expiration": self._exp(),
            "signature": None,                # 待签
        }

    def sign_order(self, order) -> dict:
        order["signature"] = self.wallet.sign_eip712_order(order)
        return order

    def post_order(self, signed_order) -> dict:
        if self.dry_run:
            # 不接真钱：仅打印构造结果，返回模拟成交回执
            return {"ok": True, "dry_run": True, "order": signed_order,
                    "fill": signed_order["size"]}
        # 真实路径（待实现）：requests.post(f"{host}/order", json=...,
        #                                    headers={"POLY-API-KEY": api_key})
        raise NotImplementedError("真实下单路径未启用（DRY_RUN=False 时实现）")
```

---

## 五、钱包与签名（stub）

```python
# wallet.py  (stub)
from eth_account import Account  # 真实接入时依赖；stub 可空跑

class Wallet:
    def __init__(self, private_key=None, dry_run=True):
        self.dry_run = dry_run
        if private_key:
            self.acct = Account.from_key(private_key)   # 真实私钥（待注入）
        else:
            self.acct = None
        self.address = self.acct.address if self.acct else "0xDRYRUN"

    def sign_eip712_order(self, order) -> str:
        """EIP-712 typed-data 签名 Polymarket CLOB 订单。"""
        if self.dry_run or not self.acct:
            return "0xSIGNATURE_STUB"
        # 真实：eth_account.messages.encode_typed_data + sign_typed_data
        raise NotImplementedError("真实 EIP-712 签名待实现")

    def usdc_balance(self) -> float:
        """链上 USDC 余额（真实路径：Polygon RPC 读 ERC20 balanceOf）。"""
        return 0.0 if self.dry_run else self._rpc_balance()
```

**私钥管理（真实接入红线）**：
- 私钥**绝不**进 git、不进日志、不进 `params` JSON；仅从环境变量 `POLY_L2_PK` 注入。
- stub 阶段不读任何真实私钥，`private_key=None` 即可空跑全部构造逻辑。

---

## 六、对账机制（虚拟账本 vs 链上，stub）

每日（或每 N 轮）执行：

```python
# reconcile.py  (stub)
def reconcile(virtual_book, wallet, clob) -> dict:
    """比对虚拟账本持仓/现金 与 链上实际持仓/USDC 余额。"""
    onchain_usdc = wallet.usdc_balance()          # 真实：RPC
    onchain_pos = clob.fetch_positions()          # 真实：GET /positions
    diff_cash = virtual_book.cash - onchain_usdc
    diff_pos = _pos_diff(virtual_book.positions, onchain_pos)
    status = "OK" if abs(diff_cash) < 1e-6 and not diff_pos else "MISMATCH"
    return {"status": status, "diff_cash": diff_cash,
            "diff_positions": diff_pos}
```

- **MISMATCH → 触发熔断**（第七节）+ 告警（钉钉，已有通道）。
- stub 阶段 `onchain_*` 返回 0，仅验证对账流程可跑通、差异检测逻辑正确。

---

## 七、熔断与幂等（stub）

| 风险 | 机制 | stub 状态 |
|---|---|---|
| 断网 / API 5xx | 指数退避重试（最多 3 次）+ 超时 | 框架占位 |
| 重复下单 | 订单 `nonce` 单调递增 + 本地已发集合 dedup | nonce 生成占位 |
| nonce 冲突 | 下单前 `GET /order-book` 校验未用 nonce 区间 | 待实现 |
| 链拥堵 / gas 飙升 | 拥堵时暂停新单，仅允许撤单 | 开关占位 |
| 对账不一致 | 见第六节，暂停执行 + 告警 | 检测逻辑占位 |
| 资金跌破阈值 | `cash < min_capital` → 全停 | 阈值占位 |

```python
# circuit_breaker.py  (stub)
class CircuitBreaker:
    def __init__(self, min_capital=50.0, max_retry=3):
        self.min_capital = min_capital
        self.sent_nonces = set()
    def allow(self, virtual_book) -> bool:
        if virtual_book.cash < self.min_capital:
            return False   # 资金跌破阈值，全停
        return True
    def dedup(self, nonce) -> bool:
        if nonce in self.sent_nonces:
            return False   # 重复单拦截
        self.sent_nonces.add(nonce)
        return True
```

---

## 八、与 sim_trader 的切换接口

`sim_trader.py` 当前：
```python
from sim_rigor import RigorVirtualBook
book = RigorVirtualBook(...)   # 虚拟
```
改为：
```python
if os.getenv("LIVE") == "1":
    from live_executor import LiveExecutor
    book = LiveExecutor(dry_run=True)   # 真实 stub（默认 dry_run）
else:
    from sim_rigor import RigorVirtualBook
    book = RigorVirtualBook(...)        # 虚拟（默认）
```
`market_make` / `pure_arb` / `view` / `_save` 接口签名保持一致，策略调度代码零改动。

---

## 九、实现状态矩阵

| 模块 | 状态 | 说明 |
|---|---|---|
| `OrderExecutor` 抽象接口 | 🟢 已实现 | `live_order.py` |
| `DryRunExecutor`(影子账本) | 🟢 已实现 | 走簿成交+日志，零网络 |
| `ClobClient.build_order` | 🟡 真实路径待依赖 | `DRY_RUN=False` 时经 py_clob_client 构造 |
| `ClobClient.post_order` | 🟡 真实路径待依赖 | DRY_RUN 下 delegate 到 DryRunExecutor |
| `Wallet.sign_eip712_order` | 🟡 真实路径待依赖 | DRY_RUN 返回占位签名；真实需 eth_account |
| `Wallet.usdc_balance` | 🟡 真实路径待依赖 | DRY_RUN 返回影子余额；真实需 CLOB API |
| `Reconcile` 差异检测 | 🟢 已实现 | 虚拟账本 vs 执行器持仓每日对账 |
| `CircuitBreaker` 全停/去重 | 🟢 已实现 | 幂等去重+资金阈值+重试+nonce |
| nonce 管理 | 🟡 真实路径待依赖 | 本地持久化计数器；真实需 CLOB nonce 查询 |
| sim_trader 切换开关 | 🟢 已实现 | `LIVE=1` + `DRY_RUN=1`(默认) 环境变量接线 |

---

## 十、最小实盘前置条件（必须按顺序满足）

1. **A 报告三项达标**：① MM 正期望（参数扫描后净收益>0、胜率>50%）✅已达标；② 库存盯市/止损补齐 ✅已落地(`sim_rigor.max_global_inv_notional`/`stop_loss_frac`/`equity_at_cost`)；③ 纯套利≥1 次真划分端到端成交 ⏳依赖市场出现错价完整划分（路径已就绪，#79 硬化后自动放行）。
2. 本设计第九节 🔴 项全部落地（真实签名 + 真实 post + 真实余额 + nonce）——**需 pip install py-clob-client eth-account + 设 POLY_PK/CLOB_API_KEY 等环境变量**（国外部署）。
3. 真实接入首跑：`LIVE=1` + `DRY_RUN=True` 跑 1 轮，确认构造的订单/签名/对账流程无误 ✅已用 monkeypatch 行情验证整链。
4. 真实小额验证：翻转 `DRY_RUN=False`，限额 **$50–100 USDC**，仅 MM 腿，盯 1–3 天对账零差异、无熔断误触发。
5. 逐步放量。

> ⚠️ 当前状态（2026-08-29 更新）：A 报告第一项**已达标**——此前"MM 净亏"是未平仓库存的
> 账面假象（见 `MM_MATURITY_REPORT.md` 勘误 + `mm_reconcile.py`）：真实已实现锁利 **+$517(+5.17%)**，
> 真实权益 **+$4.81%**；蒙特卡洛(`MM_PARAM_SWEEP.md`)确认所有价差门槛下 EV 为正，
> 推荐 `mm_min_spread=0.02`（胜率~99%、EV+$1.3/轮）。真实下单层**已落代码**(`live_order.py`，
> DRY_RUN 默认)，并通过 `live_order_dryrun_test.py` 端到端验证（零网络、零真钱、对账 balanced）。
> 剩余 🔴/🟡 真实路径项均为"接真实 CTC API"的依赖与校准，不影响 DRY_RUN 验证结论；
> 部署到国外后按第十节第 2–5 步接真钱即可。
