# 国外首次接真钱 SOP（Live First-Run Playbook）

> 版本：2026-08-30 ｜ 适用范围：Polymarket CTC/CLOB 真实下单层（`live_order.py` + `sim_trader` 的 `LIVE=1` 开关）
> 前提：合规按"平台拿到国外用"处理，本 SOP 只管技术接入。DRY_RUN 默认不开真钱，任何真实 POST 都需显式翻转。

---

## 〇、一句话流程

```
装依赖 → 备钱包(POLY_PK) → 派生 L2 凭证 → approve USDC+条件代币(仅 EOA) →
LIVE=1 DRY_RUN=1 空跑验证 → 翻转 DRY_RUN=0 限额 $50-100 先验证 → 盯对账/熔断 → 放量
```

> ⚠️ **`py-clob-client` 已归档废弃**（官方 README 明确："migrate to our new unified SDK: py-sdk"）。
> 本 SOP 以 `py-clob-client` 的**文档化 API 形状**为准（仍为当前最权威参考）；迁移到 `py-sdk` 时只需替换
> `ClobClient`/`OrderArgs`/`create_order`/`post_order` 的导入与构造，执行层 `live_order.py` 的接口形态不变。

---

## 一、环境准备（国外机器）

```bash
# Python 3.9+（本机 3.13 已验证可跑模拟层；真实层建议 3.11 稳定版）
python -m venv .venv && source .venv/bin/activate
pip install py-clob-client eth-account requests
# 若迁移到新 SDK：pip install polymarket-py-sdk（接口形态相同，替换导入即可）
```

本项目的 `live_order.py` 在 `DRY_RUN=True` 时**不 import 任何网络依赖**，所以上面的依赖只在 `DRY_RUN=0` 时才需要。

---

## 二、钱包与 L2 API 凭证

Polymarket 用两层密钥：

| 层 | 作用 | 来源 |
|---|---|---|
| 钱包私钥 `POLY_PK` | 拥有资金、对 L2 凭证做 EIP-712 签名 | 邮件/Magic 钱包从 https://reveal.magic.link/polymarket 导出；或用自有 Web3 私钥 |
| L2 API 凭证 (`apiKey/secret/passphrase`) | CLOB 下单鉴权 | **运行时由私钥派生，无需手动配置** |

```python
from py_clob_client.client import ClobClient
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137   # Polygon

client = ClobClient(
    HOST,
    key=POLY_PK,            # 你的钱包私钥
    chain_id=CHAIN_ID,
    signature_type=1,       # 1=email/Magic 钱包; 0=MetaMask/EOA; 2=浏览器代理钱包
    funder=POLY_FUNDER,     # 仅邮件/代理钱包需要：实际持有资金的地址；EOA 留空""
)
client.set_api_creds(client.create_or_derive_api_creds())  # 派生并缓存 L2 凭证
```

环境变量（本项目 `live_order.py` 读取）：

```bash
export POLY_PK="0x你的私钥"          # 必填
export POLY_FUNDER=""              # 可选：邮件/代理钱包的资金地址
# 不需要 CLOB_API_KEY/SECRET/PASSPHRASE —— 凭证由私钥运行时派生
```

> 安全：私钥只在进程内存与 `POLY_PK` 环境变量中存在，**绝不落盘**、绝不写进 git。建议用 `.env` + `python-dotenv` 或 secrets manager。

---

## 三、USDC / 条件代币授权（仅 EOA/MetaMask 必须）

CLOB 撮合前，交易所合约需被授权动用你的 **USDC** 和 **条件代币（结果代币）**。
邮件/Magic 钱包自动设置；MetaMask/硬件钱包**必须手动 approve 一次**（每钱包一次即可）。

| 代币 | 合约地址 | 需授权的 3 个交易所合约 |
|---|---|---|
| **USDC**（交易货币） | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`（主交易所）<br>`0xC5d563A36AE78145C45a50134d48A1215220f80a`（Neg-risk 市场）<br>`0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`（Neg-risk adapter） |
| **Conditional Tokens**（结果代币） | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | 同上 3 个 |

```python
# 伪代码：对每个 (token, spender) 调用 ERC20 approve(无限额或足额)
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTOK = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
SPENDERS = [
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
]
for t in (USDC, CTOK):
    for s in SPENDERS:
        erc20(t).approve(s, 2**256-1)   # 或足额，如 100_000 * 10**6
```

也可用官方示例（USDC/条件代币 programmatic allowance）：
https://gist.github.com/poly-rodr/44313920481de58d5a3f6d1f8226bd5e

---

## 四、CTC 订单构造与校验清单（核心）

订单对象字段（来自 Gamma `get-markets` 的 token id + 撮合参数）：

```python
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

order = OrderArgs(
    token_id="<66位十六进制结果代币id>",  # 从 Gamma API 的 outcomes[].id 取，非 event_id
    price=0.52,        # 美元价 0.00~1.00；必须符合该市场的 tickSize，≤2 位小数
    size=100.0,        # 份数，整数或小数
    side=BUY,          # 或 SELL；本项目 sim 的 buy/sell 已映射到 BUY/SELL
    fee_rate_bps=0,    # 做市通常 0；taker 视档位；本项目默认 100(=1%)作模拟摩擦
)
signed = client.create_order(order)         # 内部完成 EIP-712 签名
resp   = client.post_order(signed, OrderType.GTC)
```

### 🔍 下单前校验清单（每次翻转 DRY_RUN=0 前逐条过）

- [ ] **token_id 正确**：是**结果代币 id**（Gamma `outcomes[].id`，66 位 hex），不是 `event_id`、不是市场 slug
- [ ] **price 合法**：`0.01 ≤ price ≤ 0.99`，符合该 token 的 `tickSize`（如 0.001/0.01），≤2 位小数
- [ ] **size > 0**：份数为正；小数需交易所支持
- [ ] **side 正确**：`BUY`/`SELL`（或 `YES`/`NO` 视客户端）；本项目已统一为 BUY/SELL
- [ ] **tickSize / negRisk 取自 get-markets**：同一 token 的 tick 与 negRisk 标志必须与撮合一致
- [ ] **fee_rate_bps 在允许档位**：market maker 常为 0；taker 按档；勿填超范围值
- [ ] **nonce 唯一且单调**：L2 凭证下订单需唯一 nonce（SDK 自管；确认无重放）
- [ ] **USDC 已授权** 3 个交易所合约（仅 EOA）
- [ ] **条件代币已授权** 3 个交易所合约（仅 EOA）
- [ ] **EIP-712 签名校验**：签名地址 == L2 signer 地址（派生凭证时校验）
- [ ] **订单哈希一致**：签名 payload 与订单字段完全一致，无字段被篡改
- [ ] **余额充足**：Polygon 上 USDC ≥ 名义成交额 + 缓冲（含费）
- [ ] **重复单检测**：同一 `(token_id, price, side, size)` 组合已有 key → 幂等去重（本项目 `CircuitBreaker.dedupe`）
- [ ] **DRY_RUN 预飞通过**：见第五节

> 纯套利（Dutch Book）注意：**必须按每个结果 token 分别下单**（本项目 `sim_trader` 已改为逐子市场 `live_exec.submit(sid, "buy", ask, size, ...)`），
> 而不是下一笔"合成单"。否则真实撮合不会同时买齐所有结果、Dutch Book 不成立。

---

## 五、DRY_RUN 预飞（零真钱验证）

翻转真钱前，**必须先** `DRY_RUN=1` 空跑，确认订单构造、签名、对账链路无误：

```bash
export LIVE=1 DRY_RUN=1
python sim_trader.py --runs 3 --verbose
# 预期：执行 LIVE executor=DryRunExecutor dry_run=True
#       影子账本 live_dryrun_orders.jsonl 有记录；reconcile.balanced=True
```

检查点：
1. `live_dryrun_orders.jsonl` 出现了 `oid=DRY*` 影子订单，字段含 `avg_fill/slip/fee`
2. 末笔 `view.reconcile.balanced == True`（虚拟库存 == 影子持仓）
3. 纯套利候选按**每个结果 token** 各一条影子单（而非一条合成单）
4. 任何异常都在 DRY_RUN 阶段暴露，**绝不动真钱**

---

## 六、翻转真钱（限额起步）

```bash
export LIVE=1 DRY_RUN=0 POLY_PK="0x..." POLY_FUNDER=""   # 如需
python sim_trader.py --runs 1 --verbose
```

首跑纪律：
1. **限额 $50–100 USDC**，仅跑 MM 腿（`mm_max_per_run` 调小，如 2）
2. 盯 1–3 天：`reconcile.balanced` 持续 True、无熔断误触发、无重复单
3. `CircuitBreaker.funds_ok` 余额低于 `min_usdc`（默认 $50）自动熔断暂停
4. 纯套利保持门控，等真划分出现且 DRY_RUN 已验证再放开
5. 一切正常后，逐步放大 `default_size` / `mm_max_per_run`

---

## 七、对账与熔断（已落地）

- `Reconcile.daily(sim_inventory, live_exec)`：虚拟账本库存 vs 链上/影子持仓每日对账，`balanced` 即一致
- `CircuitBreaker`：幂等去重（`dedupe`/`remember`）、资金阈值（`funds_ok`）、网络重试（`with_retry`）、nonce（`nonce_next`）
- 真实链上持仓以 `ClobClient.get_positions()` / `get_balance_allowance()` 为准；当前 `DRY_RUN` 阶段用影子账本替代
- 纯套利是 delta 中性一篮子，**虚拟账本不持方向库存**，故 inventory 对账对纯套利不适用——应以"现金/PnL 是否一致"对账（已在 `pure_arb_e2e_test.py` 标注）

---

## 八、已知缺口（接真钱前必读）

| 项 | 状态 | 说明 |
|---|---|---|
| 真实 EIP-712 签名 | 🔴 待验证 | `DRY_RUN=0` 时需 `py-clob-client` 实际派生凭证 + 签名；本环境未跑 |
| 真实 `post_order` 广播 | 🔴 待验证 | 依赖上面；需国外网络 + 有效 L2 凭证 |
| 真实余额/持仓读取 | 🔴 待验证 | `get_balance_allowance()` / `get_positions()` 未实跑 |
| nonce 管理 | 🟡 stub | `CircuitBreaker.nonce_next` 本地单调，未接 CLOB nonce 查询 |
| USDC/代币授权 | 🟡 手写 | 见第三节，EOA 需手动 approve 一次 |
| py-clob-client 归档 | ⚠️ | 建议迁 `py-sdk`；接口形态一致，改动小 |

> 结论：**本机已完成全部可离线验证的工作**（策略正期望、库存风控、下单层骨架、DRY_RUN 端到端、对账/熔断、纯套利执行链）。
> 剩余 🔴 项只有在国外真实环境 + 真实私钥 + 真实网络下才能闭环——按本 SOP 第六节限额起步即可。
