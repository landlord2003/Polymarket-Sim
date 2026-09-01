# NB 真实成交率校准 SOP（calibrate_fill.py）

> 用途：北京模拟盘是零真钱影子账本，**没有链上真实成交率**。要拿 ground truth，必须在一台
> 有外网、物理 IP 落加拿大 NB 等开放省（非中国、无 KYC）的机器上真挂 maker 单、轮询成交，
> 把观测到的成交率回填到 `FILL_BASE`。本 SOP 给 NB 伙伴一步步照做即可。
>
> 安全铁律：单笔极小（默认 $3）、未成交自动撤单、私钥只放 `.env`（已 gitignore、永不入库）。

---

## 0. 前置条件

- 一台有外网、IP 落开放省的机器（北京/中国机器**做不了**，无外网直连 + 合规限制）
- 已 `git clone` 本仓库并 `cd` 到 `a_share/`
- Python 3.13（与北京同版本，避免语法差异）
- 一个 Polygon Mainnet 钱包私钥 + 少量 USDC（仅用于 L2 凭证派生与 $3/单 的微量挂单）

---

## 1. 准备 .env（从 NB 模板复制）

```bash
cd a_share
cp .env.nb .env
```

编辑 `.env`，先保持**预览模式**（不发单）：

```ini
COMPLIANCE_FILTER=0        # NB 无合规风险，放开全部市场
LIVE_MODE=0                # 第一步先 0，只跑预览
PM_BOT_PK=                 # 先留空
FILL_BASE=0.30             # 当前假设值，校准后会被覆盖
```

---

## 2. 跑预览（零风险，验证"拉盘口 + 定价"两条路径）

```bash
python calibrate_fill.py --preview --markets 30
```

预期输出：打印 30 个将挂 maker 单的 `side / price / mid / gross% / liquidity / question`，
末尾提示「（共预览 30 个市场；--live 才会真挂）」。

- 若报 `[error] 拉盘口失败` → 检查外网/代理（Gamma API 需直连 `gamma-api.polymarket.com`）
- 若正常打印 → 说明 `polymarket.py`（stdlib urllib）拉盘口 OK、定价逻辑 OK，**可进入实盘**

> 说明：`--preview` 只依赖 `polymarket.py`（标准库 urllib），**无需 pip 安装任何额外包**。

---

## 3. 装实盘依赖（仅 --live 需要）

```bash
pip install py-clob-client web3
```

> `calibrate_fill.py --live` 会通过 `clob_exec.ClobExec` 惰性导入 `py_clob_client`，
> 未安装时 `--preview` 不受影响，但 `--live` 会失败。

---

## 4. 填私钥 + 开实盘开关

编辑 `.env`：

```ini
LIVE_MODE=1
PM_BOT_PK=<你的 Polygon Mainnet 私钥>
```

> ⚠️ `PM_BOT_PK` 只放 `.env`，**绝不写进代码、绝不 git 提交、绝不打日志**。
> `ClobExec` 强制校验：`LIVE_MODE=1` 且 `PM_BOT_PK` 为空会直接 `RuntimeError` 拒绝发单。

---

## 5. 真校准（每单仅 $3，两轮共约 60 单）

```bash
python calibrate_fill.py --live --markets 30 --size 3 --window 600 --rounds 2
```

- 每轮对 30 个市场双边挂 maker 单（BUY/SELL 交替），单笔名义 `--size 3` USD
- 每轮观察 `--window 600` 秒后轮询成交状态；**未成交的单自动撤单**，无残留库存
- 总暴露受 `.env` 的 `MAX_TOTAL_POS` / `DAILY_LOSS_LIMIT` 风控双重限制
- 结果写入 **`a_share/data/fill_calibration_live.json`**，并打印 `=== 校准结果 ===`

---

## 6. 回填 FILL_BASE（让模拟盘用上真实成交率）

打开 `a_share/data/fill_calibration_live.json`，取字段 `recommended_base`（已 clamp 到 0.05–0.95）：

```json
{
  "observed_fill_rate_pct": 42.5,
  "recommended_base": 0.425,
  "...": "..."
}
```

写回 `.env`：

```ini
FILL_BASE=0.425          # 用 recommended_base 的真实值替换
```

重启 `sim_server.py` 生效（看板「成交率」徽章与统计中心即反映真实校准值）。

---

## 7. 回滚 / 中止

- 不想实盘：`.env` 里 `LIVE_MODE` 改回 `0`，`FILL_BASE` 留 `0.30`（原假设）即可，无任何残留。
- 中途想停：`Ctrl+C` 终止；脚本在观察窗口结束时会自动撤未成交单，不会留库存。

---

## 8. 给北京的回执

校准完成后，把 `fill_calibration_live.json` 里的这两行发回即可，北京无需私钥/外网：

```
observed_fill_rate_pct = XX.X
recommended_base       = 0.XXX
```

北京据此更新 `.env` 的 `FILL_BASE` 并重启，模拟盘的成交率假设即升级为 NB 真观测。

---

## 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `[error] 拉盘口失败` | NB 无外网 / 代理拦截 Gamma | 检查出口 IP 与直连；勿走中国合规代理 |
| `RuntimeError: LIVE_MODE=1 但未设置 PM_BOT_PK` | 私钥未填 | 填 `PM_BOT_PK` 后重试 |
| `ModuleNotFoundError: py_clob_client` | 没装实盘依赖 | `pip install py-clob-client web3` |
| 成交率偏低（如 <10%） | 盘口薄 / 观察窗口太短 | 调大 `--window`（如 900）或选流动性更高市场 |
| `risk_control` 拒单 | 触及 `MAX_TOTAL_POS` / `DAILY_LOSS_LIMIT` | 调大限额或减小 `--size` / `--markets` |
