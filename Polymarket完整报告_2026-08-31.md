# Polymarket 完整研究报告（含自动交易技术方案）

> 📅 整理日期：2026-08-31 ｜ 整理：Claw 🦀 ｜ 适用：老吴（低空情报 + 量化交易工作流）
> ⚠️ 非投资建议；监管/地理封锁状态变动快，实盘前必以 Polymarket 官方实时公告与本地法律为准。

---

## 📋 今天问答总览（4 轮）

| # | 问题 | 核心结论 | 信号灯 |
|---|---|---|---|
| 1 | Polymarket 是干什么的？怎么参与？能否自动交易？资格？ | 全球最大去中心化预测市场；国际站无 KYC；**API 生态成熟可自动交易**；中国属国家级受限 | 🟢/🔴 |
| 2 | 加拿大人受限制吗？ | **非全国封锁，省级碎片化**：仅 ON/AB/BC/QC 四省 close-only，其余开放无 KYC | 🟡 |
| 3 | 什么是 KYC？给 VPS+CLOB 实盘方案；自己电脑不能做吗？ | KYC=身份验证；**北京 IP 直连不行**，实盘须跑境外 VPS；本地可做开发/回测/模拟盘 | 🔴/🟢 |
| 4 | **人在加拿大 NB 省，能用自己电脑跑自动交易吗？** | **✅ 可以**。NB 为完全开放省，按当前物理 IP 判定，本机直连合规、无需 VPS 绕行 | 🟢 |

> 一句话：**从北京 → 受限，需境外 VPS；从 NB → 开放，自己电脑直接干。** 下文展开。

---

## 1️⃣ 平台本质：Polymarket 是干什么的

### 1.1 核心定义
- **预测市场 = 用真金白银给「未来事件」定价**。不是和庄家对赌，而是用户间 P2P 交易。
- 每个市场是 **Yes/No 二元问题**（如「美联储 2026 年 6 月会降息吗？」）。
- **份额价格 ∈ [0,1] 美元，价格 = 概率**。Yes 份额 $0.65 ⇒ 市场认为发生概率 65%。
- 结算：获胜份额每股兑付 **$1.00**，失败份额归零。

### 1.2 技术架构（链上运行）
| 层 | 作用 |
|---|---|
| 区块链 | Polygon（以太坊 L2，gas <$0.01） |
| 结算货币 | **pUSD**（ERC-20，由 USDC 1:1 链上背书；充 USDC 到账即等值 pUSD，可随时提回 USDC/USDT 等 20+ 代币） |
| 资产表达 | Gnosis 条件代币框架（ERC-1155），每对 Yes/No 由 $1 抵押 |
| 撮合 | 中央限价订单簿（CLOB），P2P 撮合，非庄家对赌 |
| 裁决 | **UMA 乐观预言机**：提案结果押保证金 → 约 2 小时争议窗口 → 争议则由 UMA 持币者投票，公司无法操纵 |
| 钱包 | 智能钱包（Safe/Proxy）；邮箱登录后台自动建 Safe 钱包；**非托管，私钥自持** |

### 1.3 与传统博彩/交易所的区别
- ❌ 无「庄家」：不会因你赢太多而封号。
- ✅ 随时可卖：结算前随时卖回订单簿锁利润/止损。
- ✅ 非托管：资金在你的钱包，平台不触碰本金。
- ✅ 7×24、链上透明、可公开验证。

### 1.4 关键数据
- 2020 年由 Shayne Coplan 创立；累计交易量 **>40 亿美元**（2026 初）；2026-03 单月清量达 **$10.57 亿**。
- 股东含 Intercontinental Exchange、Founders Fund、1789 Capital。

---

## 2️⃣ 如何参与（实操流程 · 国际站）

### 2.1 注册（3 选 1，**国际站无 KYC**）
1. **Google 一键登录**（最省事，后台自动建 Safe 钱包）
2. **邮箱 + 验证码**
3. **加密钱包直连**（MetaMask / Rabby / Coinbase Wallet / WalletConnect）
- 注册即登录，约 1–5 分钟；界面支持简体中文（右下角 Language 切换）。

### 2.2 充值（关键：选对链）
- **首选 Polygon 上的 USDC**：最低充值约 **$3**，gas 可忽略。
- 也支持 Ethereum（最低 $10）、Solana、Base、Arbitrum、BSC、Optimism、Tron、Bitcoin、Monad 等。
- 入金渠道：① 交易所提 Polygon-USDC 到充值地址（最便宜）；② 内置法币通道（卡/Apple Pay，1.5–3%）；③ Polygon 原生 DEX 兑换。
- ⚠️ **错链转账 = 资产永久丢失**，务必确认选 Polygon 网络。

### 2.3 交易
- 选市场 → 选 Yes/No → **市价单**（即时最优价，吃单方付费）或 **限价单**（自定价格挂单，**做市方 0 费**）。
- 最小下单：市价/1-Tap 最低 $1；限价单至少 5 份额。

### 2.4 结算
- 事件结束 → 提案结果 → **2 小时争议窗口** → 正式结算 → 获胜份额自动兑付 $1.00。
- 无资金费率、无保证金、无隔夜费。

### 2.5 费用结构（国际站）
| 市场类别 | 吃单方费率 | 做市方费率 |
|---|---|---|
| 地缘政治（geopolitics） | 0% | 0% |
| 政治（politics） | ~1% | 0% |
| 加密（crypto） | **峰值 1.80%** | 0% |
| 其他类别 | 0.75–1.8% | 0% |

---

## 3️⃣ 自动交易：能否做 & 怎么做（开发者视角 · 重点）

**✅ 完全可以，API 生态成熟。** 对你（已有 Quant-Trading 模拟做市项目）是最实用部分。

### 3.1 API 三层架构
| 服务 | Base URL | 认证 | 用途 |
|---|---|---|---|
| **Gamma API** | `https://gamma-api.polymarket.com` | 公开 | 市场发现、事件元数据、历史成交量、搜索 |
| **CLOB API** | `https://clob.polymarket.com` | **L2 HMAC**（必需） | 下单/撤单、订单簿深度、持仓、成交 |
| **Data API** | `https://data-api.polymarket.com` | 需认证 | 用户级持仓、PnL、交易历史 |

### 3.2 认证分级
- **L0 公开**：市场数据/订单簿/价格（~100 req/min）。
- **L1 Signer**：钱包私钥签 EIP-712 派生 API Key。
- **L2 认证**：派生 `api_key / api_secret / passphrase`，后续请求 HMAC-SHA256 签名。
- 认证端点限频 ~1000 req/min；**下单 ~10 次/秒**。

### 3.3 官方 Python SDK `py-clob-client`
```bash
pip install py-clob-client web3 requests
```

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, BUY

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon Mainnet

client = ClobClient(host=HOST, key="0xYOUR_BOT_PRIVATE_KEY", chain_id=CHAIN_ID)
creds: ApiCreds = client.create_or_derive_api_creds()  # 派生 L2 凭证
client.set_api_creds(creds)

# 拉盘口
book = client.get_order_book("TOKEN_ID_HERE")
print("ASKS:", [(a.price, a.size) for a in book.asks[:3]])
print("BIDS:", [(b.price, b.size) for b in book.bids[:3]])

# 下一张 GTC 限价买单（做市方，0 费）
order = client.create_order(OrderArgs(
    token_id="TOKEN_ID_HERE", price=0.45, size=50.0, side=BUY))
resp = client.post_order(order, OrderType.GTC)
print("Order:", resp)
```

### 3.4 WebSocket 实时流（毫秒级，套利/做市必需）
```
wss://ws-subscriptions-clob.polymarket.com/ws/market
```
订阅指定 `token_id` 接收订单簿/成交/市场变动推送；REST 轮询有秒级延迟，低延迟策略必须用 WS。

### 3.5 可构建策略类型
| 策略 | 说明 | 关键注意 |
|---|---|---|
| **做市（Market Making）** | 双边挂单吃价差，0 挂单费 | 需库存管理，防单边方向风险 |
| **套利（Arbitrage）** | 利用相关市场概率数学关系的偏离 | 速度敏感，机会瞬逝 |
| **统计套利** | 历史价格模式预期回归 | 警惕过拟合，需样本外验证 |
| **事件驱动** | 监听新闻/价格触发自动下单 | 设过滤避免假信号 |

### 3.6 Bot 风控铁律
- 🔴 **始终设 kill switch**（异常即停）、单市场/总仓位上限、日亏损限额。
- 🟡 失败安全：无法判定时减仓而非加仓。
- 🟡 断线处理：指数退避 + WS 自动重连 + 全量订单日志。
- 🟡 渐进路径：回测 → 模拟盘（paper trading）→ 实盘。

> 你的 Quant-Trading 项目已在 `E:\WorkBuddy\Quant-Trading` 跑 v3（真实盘口轮动 + 模拟做市 + 三重合规过滤），接的正是这套 CLOB API。下文代码骨架可直接对齐你现有 `mm_leg` / `caifu` pipeline。

---

## 4️⃣ 参与资格与合规红线（重点）

### 4.1 双平台结构（2026 现状）
| | 国际站 polymarket.com | 美国站（DCM）polymarketexchange.com |
|---|---|---|
| 监管 | 离岸、crypto-native、未受 CFTC 直接监管 | CFTC 注册 DCM（2025 以 $1.12 亿收购 QCEX） |
| 市场 | 全类别（政治/体育/加密/经济/文化/地缘） | 目前仅体育，逐步扩展 |
| 入金 | USDC on Polygon | 美元（借记卡/银行转账） |
| KYC | **无** | 完整（SSN、政府 ID、地址证明） |
| 费用 | 0–1.8% | 0.01% flat |
| 准入 | 非受限地区开放 | 邀请制候补（排队 6–12 周） |

### 4.2 什么是 KYC（第 3 轮问答补充）
**KYC = "Know Your Customer"（了解你的客户）**。金融机构/交易平台为反洗钱(AML)、反恐融资、监管合规，强制验证用户真实身份：
- 政府签发证件（护照/驾照）、生物识别自拍、地址证明（水电账单/银行对账单）、税号（如美国 SSN）等。
- **Polymarket 语境**：国际站**无 KYC**（钱包/邮箱即注册）；仅美国 DCM(QCEX) 与加拿大 CIRO 受监管路径需完整 KYC。平台用 KYC 确认你非受限地区居民、非受制裁对象、满足税务申报。

### 4.3 受限司法管辖区（官方 Help Center 2026-08-02 复核）
- **国家级（约 39 个，完全封锁/仅浏览）**：Australia、Belarus、Belgium、Brazil、Burundi、中非、古巴、刚果(金)、Ethiopia、France、Germany、Iran、Iraq、Ireland、Italy、Japan、Lebanon、Libya、Malta、Myanmar、Netherlands、Nicaragua、朝鲜、Poland、Russia、Singapore、Slovakia、Somalia、南苏丹、Sudan、Syria、Taiwan、Thailand、UK、US、Venezuela、Yemen、Zimbabwe 等。
- **省级次级地区（close-only，仅平仓提现）**：Canada 的 **Alberta / British Columbia / Ontario / Quebec**；Ukraine 的 Crimea / Donetsk / Luhansk。
- 平台三层封锁识别受限用户：① IP 地理围栏；② 钱包司法筛查；③ 高交易量 KYC 要求非受限证件。
- **美国**：2022-01 CFTC 和解（$140 万罚款 + 停止向美国人提供事件合约 + 地理封锁）。国际站对美 IP 封锁率约 99%。美国用户仅能走 QCEX 合规通道。

### 4.4 🔴 中国大陆（老吴北京 IP · 务必看清）
- 从**北京 IP 直连国际站极可能被地理封锁**，无法注册/入金/交易。
- ⚠️ **强烈不建议 VPN 绕过**：违反 ToS；平台多手段识别 VPN/数据中心 IP、设备指纹、支付轨迹；一旦判定受限地区用户，**账户可终止、余额可冻结/没收、无法律救济**；盈利还涉未申报境外收入的税务/合规风险。
- ✅ 合规替代：仅做**数据/研究用途**（浏览市场、读 API 行情、做分析，不涉及真实资金下注）——只读 API 无合规风险，是极好的另类数据源。

### 4.5 🟡 加拿大省份细分（第 2 轮问答 · 与中国的关键区别）
加拿大**非全国封锁，而是省级碎片化**。开放省份允许交易、无 KYC；仅 4 省 close-only（网站可开、余额可见、可平仓提现，但拒绝所有新订单）：

| 省份/地区 | 状态 | 说明 |
|---|---|---|
| Ontario（安省） | 🔴 Close-only | 2025-04 起永久（OSC 执法，二元期权违规） |
| Alberta（阿省） | 🟡 Close-only | 2026-07-06 起（平台主动 geo-fence） |
| British Columbia（BC） | 🟡 Close-only | 2026-07-06 起 |
| Quebec（魁省） | 🟡 Close-only | 2026-07-06 起 |
| **其余省份+3领地（MB/SK/NS/NB/NL/PEI/Yukon/NWT/Nunavut）** | 🟢 **Open** | **完全开放，邮箱/钱包即注册，无 KYC** |

- 法律背景：加拿大无联邦级预测市场禁令；2017 年 CSA 禁 30 天内短期二元期权，预测市场结构近似——但**实际执法仅针对运营商（OSC），不抓个人交易者**。
- 受监管替代（CIRO）：IBKR Canada / Wealthsimple 等可跨境参与，但**仅限经济/金融/气候类、最少 30 天期限，体育选举除外**，不适合短期做市 bot。
- 税务：CRA 视加密交易为应税；偶发=资本利得（50% 计入），频繁系统化=营业收入（100% 应税），跑实盘需留交易记录报 T1。

---

## 5️⃣ 技术方案：自动交易落地

### 5.0 两种部署场景对比
| 维度 | 场景 A：北京 IP（你当前所在地） | 场景 B：人在加拿大 NB 省（第 4 轮问答） |
|---|---|---|
| 出口 IP | 中国（国家级受限） | 加拿大 NB（完全开放省） |
| 自己电脑实盘 | 🔴 不行（地理封锁+违反 ToS） | 🟢 **可以，合规直连** |
| 是否需要 VPS | ✅ 必须（境外开放省节点） | 🟡 可选（仅为 7×24 不掉线） |
| 是否需 VPN | 🔴 不建议（违规没收风险） | ❌ 不需要（且勿挂，避免误落受限区） |
| KYC | 无（国际站） | 无（国际站） |
| 资金/税务 | 母国资本管制风险 | CRA 税务申报义务 |

---

### 5.1 场景 A：北京 → VPS + CLOB 实盘（第 3 轮方案）

> 适用：你北京本地开发/回测/模拟盘照常；**实盘进程必须跑在境外 VPS**（出口 IP 落非受限区），通过 SSH 远程管理。

#### 架构 5 层
| 层 | 职责 | 关键组件 |
|---|---|---|
| 数据层 | 市场发现 + 实时盘口 | Gamma API、CLOB WebSocket、Data API |
| 策略层 | 信号生成 | 复用 Quant-Trading 的 `mm_leg` 做市/套利逻辑 |
| 执行层 | 下单撤单 | CLOB REST（L2-HMAC），做市用 GTC 挂单（0 费） |
| 风控层 | 熔断/限额 | kill switch、单市场/总仓位上限、日亏损限额、断线重连 |
| 监控层 | 告警/日志 | 钉钉+微信双通道（复用 `D:\WorkBuddy\output\dingtalk_notify.py`） |

#### 地域与 VPS 选型（避开受限名单）
| 地域 | 状态 | 说明 |
|---|---|---|
| 🟢 **加拿大开放省**（MB/SK/NS/NB/NL/PEI/Yukon/NWT/Nunavut） | 完全开放、无 KYC | 避开 ON/AB/BC/QC 四省（close-only） |
| 🟢 韩国 / 印度 | 非受限、有活跃零售 | 无本地许可框架但无执法 |
| 🟢 瑞士 / 瑞典 / 挪威 / 葡萄牙 | 不在官方 39 国列表 | 欧洲非受限稳定地区 |

- 推荐：**加拿大 MB/SK 或韩国首尔节点**；规格 2–4 vCPU / 4–8 GB RAM / SSD；Ubuntu 22.04/24.04 LTS；月成本 **$5–15**（Vultr / DigitalOcean / Linode / 阿里云国际版 等非受限区节点）。
- ⚠️ 西班牙、比利时、爱尔兰、意大利、荷兰、波兰、斯洛伐克等均在受限列表，**勿选**。

#### 运行环境（Ubuntu）
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3.11 python3.11-venv git nginx
mkdir -p /opt/pm-bot && cd /opt/pm-bot
python3.11 -m venv venv && source venv/bin/activate
pip install py-clob-client web3 websockets redis python-dotenv
```

#### 钱包与密钥安全（🔴 最高优先级）
- 自托管热钱包：bot 用独立热钱包，私钥**仅存环境变量 / systemd EnvironmentFile / secret manager**，绝不停代码或 git。
- 资金分层：热钱包（VPS）只放小额（$200–500）；冷存主资金（硬件钱包/离线），周期补给。
- VPS 安全：禁用密码 SSH（仅密钥）、防火墙只开必要端口、系统自动更新。

#### 代码骨架（接现有 Quant-Trading）
```python
# /opt/pm-bot/bot.py —— 骨架，接 mm_leg 逻辑
import os, json, asyncio
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, BUY, SELL

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

PK = os.environ["PM_BOT_PK"]          # 私钥仅从环境变量读，绝不硬编码
client = ClobClient(host=HOST, key=PK, chain_id=CHAIN_ID)
creds: ApiCreds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

MAX_POS_PER_MARKET = float(os.environ.get("MAX_POS", 200))    # 单市场最大仓位 USDC
DAILY_LOSS_LIMIT   = float(os.environ.get("DAILY_LOSS", 100)) # 日亏损限额 USDC
kill_switch = {"on": False}  # 异常置 True，停止一切新单

def place_maker_order(token_id: str, side, price: float, size: float):
    """做市挂单 GTC —— 挂单成交为做市方，0 费"""
    if kill_switch["on"]:
        return None
    order = client.create_order(OrderArgs(token_id=token_id, price=price, size=size, side=side))
    return client.post_order(order, OrderType.GTC)

async def ws_loop(token_ids: list[str]):
    """WebSocket 实时盘口 —— 毫秒级，套利/做市必需"""
    import websockets
    uri = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"type": "subscribe", "markets": token_ids}))
                async for msg in ws:
                    data = json.loads(msg)
                    # TODO: 接入你的 mm_leg 信号 -> place_maker_order(...)
        except Exception as e:
            await asyncio.sleep(5)  # 断线指数退避重连

if __name__ == "__main__":
    asyncio.run(ws_loop(["TOKEN_ID_1", "TOKEN_ID_2"]))
```
> 你的 `mm_leg` / `caifu` pipeline 可直接平移到 VPS：把策略核心作为模块 import，替换上面 `TODO` 处。

#### 进程管理与监控
- 进程看护：`systemd` service / `supervisor` / Docker（容器内跑 venv）。
- 监控告警：复用 `dingtalk_notify.py` 双通道 —— 启动/异常/kill switch 触发/日亏损触限均推手机。
- 健康检查：定时拉 `/positions` 与 WS 心跳，失联即告警。

#### 上线前合规检查清单（场景 A）
- [ ] VPS 出口 IP 在**非受限地区**（避开 ON/AB/BC/QC 及 39 国列表）
- [ ] **未使用** VPN 伪装地域（违反 ToS 高风险）
- [ ] 私钥仅在环境变量/secret manager，无硬编码、无 git
- [ ] 热钱包仅小额，主资金冷存
- [ ] kill switch + 仓位/日亏限额已生效
- [ ] 钉钉/微信告警已联通
- [ ] **先模拟盘跑 ≥1 周**验证执行逻辑，再小资金实盘
- [ ] 保留全部交易记录（税务/审计）

---

### 5.2 场景 B：人在加拿大 NB 省 → 本机直连（第 4 轮问答 · 新结论）

> **核心结论：✅ 可以，自己电脑直接跑，无需 VPS 绕行、无需 VPN。**

#### 为什么本机直连合规
- Polymarket 地理封锁**按「当前物理位置（IP）」判定，不看国籍/户籍/账号注册地**（官方 Help Center 原话：*Nationality, a home address, or the country where an account was created does not override the location check*）。
- **NB 属完全开放省**：邮箱/钱包即注册、**无 KYC**、**完整 CLOB API 可用**、可正常开仓平仓。
- 即便持中国护照，只要物理在 NB、用当地 ISP 出口 IP，平台按 NB 处理 = 放行。

#### 上线前 IP 自检（关键一步）
NB 本地 ISP 的 IP 段**绝大多数**会被 GeoIP 正确标注为 NB/加拿大开放省，但少数可能被误标成 ON/AB/BC/QC → 触发 close-only。上线前用下面命令确认：
```bash
# 查看出口 IP 及其地理标注
curl -s ipinfo.io | python -m json.tool
# 关注返回中的 "country": "CA" 与 "region": "New Brunswick"
# 若 region 落在 ON/AB/BC/QC 或 country 非 CA，则会被限，需排查网络
```
仅在确认 IP 解析到 **CA / New Brunswick（或任一开放省）** 后再接实盘。

#### 本机实操步骤
1. **网络**：直连 NB 本地宽带/WiFi，**不要挂 VPN**（尤其勿挂到 ON/AB/BC/QC 或海外受限国，否则直接 close-only/封锁）。
2. **钱包 + 资金**：本地装非托管钱包（如 MetaMask），往 Polymarket 充值地址打 **Polygon 上的 USDC**（最低 $3）。
3. **API 接入**：本机用 `py_clob_client` 生成 `api_key/secret/passphrase`（L2 认证），bot 直接跑在你电脑上，出口 IP 即 NB。
4. **策略**：直接 import 你 Quant-Trading 的 `mm_leg` / `caifu` pipeline，做市挂单 0 费、吃单方峰值 1.8%。
5. **运行看护**：用本机任务计划程序 / systemd（Linux 子系统）/ `supervisor` 守护进程；笔记本注意合盖休眠会断线。

#### 与场景 A（VPS）的差异
- VPS 方案是为「北京 IP」准备的绕行手段；**NB 场景下地域合规问题不存在，VPS 从「必须」降级为「可选（只为 7×24 不掉线）」**。
- 若只要持续运行且不愿本机常开，仍可在**加拿大开放省节点**（如 MB/SK）加一台廉价 VPS 只做运行托管——但 IP 必须落在开放省，不能误落四省 close-only。

#### ⚠️ NB 场景仍须盯住的坑
- 🟡 **IP 地理库误判**：极少数 ISP IP 段被错标成受限省 → close-only。上线前务必 `curl ipinfo.io` 自检。
- 🟡 **24/7 在线**：笔记本合盖休眠 = bot 断线。要持续运行需台式机常开或加开放省 VPS。
- 🟡 **税务（CRA）**：若构成加拿大税务居民，加密交易收益应税——偶发算资本利得（50% 计入），频繁系统化算营业收入（100% 应税）。跑实盘务必留交易记录报 T1。
- 🟡 **本地法律**：从 NB 接入 Polymarket 合规，但你仍需自行承担**母国（中国）资本管制/税务**等属人义务——平台不管这层。

---

## 6️⃣ 与你（老吴）的相关性 & 行动建议

- 🦀 **你已是量化开发者**：Quant-Trading v3 已接入 Polymarket CLOB 做模拟做市 + 合规过滤（政治/地缘/军事/中东航运咽喉）。本报告 API 与代码骨架可直接用于对齐/升级你的 `py_clob_client` 接入、限频、WS 订阅。
- 🔴 **北京 IP 合规风险**：你从北京访问国际站属受限情形。当前 Quant-Trading 的 **DRY_RUN 模拟盘** 思路正确——**实盘真钱接入前先解决部署地域**（你笔记已记「接真钱第三步按边界搁置，需境外部署 + $50-100」，建议维持该边界，除非人到 NB 等开放省）。
- 🟡 **费用优化**：做市策略天然吃「挂单 0 费」红利；crypto 类吃单方峰值 1.8%，大单应偏挂单做市方降成本。
- 🟢 **数据价值**：即便不真钱下注，Polymarket 实时概率（利率/宏观/地缘类）是极好另类数据源，可纳入低空情报 + 宏观监测工作流（只读 API，无合规风险）。
- 🟢 **NB 新路径**：若你物理抵达 NB（或任一开放省），可**本机直连实盘**，省去 VPS 绕行，合规且最简。

---

## 7️⃣ 信号一览（结论浓缩）

| 议题 | 结论 | 信号灯 |
|---|---|---|
| 平台本质 | 去中心化预测市场，价格=概率，Polygon+pUSD+UMA | 🟢 |
| 参与门槛 | 国际站无 KYC，Polygon-USDC 最低 $3 | 🟢 |
| 自动交易 | API+SDK+WebSocket 成熟，可做市/套利/事件驱动 | 🟢 |
| 中国（北京）资格 | 国家级受限，直连封锁，VPN 绕行高危 | 🔴 |
| 加拿大整体 | 非全国封锁，省级碎片化 | 🟡 |
| NB 省资格 | 完全开放，无 KYC，本机可实盘 | 🟢 |
| 自己电脑实盘（北京） | 不行（地域受限） | 🔴 |
| 自己电脑实盘（NB） | 可以（合规直连） | 🟢 |
| KYC | 国际站无；仅美 DCM/加 CIRO 路径需 | 🟢 |

---

## 8️⃣ 参考来源（数据截至 2026-08-31）
1. Polymarket 官方文档 — Polymarket 101 / FAQ：learn.polymarket.com
2. Polymarket API 开发者指南（CLOB/Gamma/Data + py-clob-client）
3. Polymarket 官方 Help Center — Geographic Availability（2026-08-02 复核）
4. money.wiki — Polymarket Review: Decentralized Prediction Markets
5. polymarketblog.com — Sign Up Guide / US Legal Guide (2026)
6. polymarkets.co.il — Country Guide: US, UK, EU & Asia (2026-04)
7. tradetheoutcome.com — How to Connect to the Polymarket API
8. predictengine.ai — Polymarket Bot API Guide (2026)
9. startpolymarket.com — 中文新手指南 / 加密交易者指南 (2026)
10. polymart.app / swrnn.com / stakesim.com / predmarket.io / datawallet.com — 加拿大省份细分与受限清单（2026-08）
11. 国泰海通证券 —《Polymarket：一种"用市场定价未来"的新型信息基础设施》(2026-06-28)

> ⚠️ 监管状态每月变化，涉及真钱前请复核官方最新公告与本地法律。
