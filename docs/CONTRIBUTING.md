# 贡献指南

感谢你帮助改进 QuantSieve。开始前请阅读根目录的[行为准则](../CODE_OF_CONDUCT.md)、[安全策略](../SECURITY.md)和[产品路线图](ROADMAP.md)。漏洞、凭据、账户信息、持仓、内部地址或非公开数据不要放进公开 Issue。

## 开始开发

需要 Python 3.12+、Node.js 24+ 和 pnpm 11.9.0：

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements-dev.txt
corepack enable
pnpm install --frozen-lockfile
```

启动 API 与 Web：

```bash
quantsieve-api
# 另开终端
pnpm dev
```

## 贡献流程

1. Fork 仓库并从最新 `main` 新建小范围分支，例如 `fix/...` 或 `feat/...`。
2. 先写清楚可复现问题、数据与时间语义、失败行为和验收条件。
3. 保持改动聚焦；不要顺手重构无关模块或提交缓存、数据库、构建产物。
4. 为新行为增加测试，并更新受影响的中英文文档。
5. 在 Pull Request 中列出真实运行过的验证命令；未运行的检查要明确说明。

## 不可破坏的完整性原则

- **数字可溯源**：所有面向用户的数字必须来自工具或确定性计算，LLM 只做推理和叙述。
- **无未来函数**：特征、信号、成交、标签和可用时间必须遵守对应研究合同。
- **基准诚实**：不要隐藏简单基准、成本、样本量、留出期失败或不确定性。
- **服务端拥有证据**：浏览器提交的指标、收益或风控结果不能成为权威事实。
- **失败关闭**：缺证据、授权或能力时拒绝运行，不静默降级成看似可信的结果。
- **无实盘路径**：公开版本不能连接交易账户、接收交易凭据或提交真实订单。

## 提交前检查

```bash
ruff check .
mypy
pytest
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

如果改动了公开文件清单、二进制演示素材或发布工具，还要重新生成发布候选并执行：

```bash
python scripts/public_release.py audit --root <candidate> --history --strict-policy --require-manifest
```

真实第三方接口测试标记为 `live`，不得把普通 CI 成功解释为第三方服务当前可用。
