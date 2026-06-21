# Security Policy / 安全策略

## Supported Versions / 支持版本

English: Security fixes target the current `main` branch until the project starts
publishing versioned releases.

中文：在项目开始发布版本化 release 之前，安全修复面向当前 `main` 分支。

## Reporting a Vulnerability / 报告漏洞

English: Please do not open a public issue for suspected vulnerabilities. Report
them privately through GitHub private vulnerability reporting when it is enabled
for the repository, or contact the repository owner directly.

中文：请不要通过公开 issue 报告疑似漏洞。当仓库启用 GitHub private vulnerability reporting
时，请通过该渠道私密报告；否则请直接联系仓库所有者。

Include / 请包含：

- affected commit or version / 受影响的 commit 或版本
- reproduction steps / 复现步骤
- expected impact / 预期影响
- any logs or MCP responses with secrets and proprietary data removed / 已移除密钥和专有数据的日志或 MCP 响应

## Sensitive Data / 敏感数据

English: Debugging embedded targets can expose private firmware details. Never
include credentials, signing keys, customer data, full memory dumps, proprietary
source snippets, or private hardware identifiers in public reports.

中文：调试嵌入式目标可能暴露私有固件细节。请勿在公开报告中包含凭据、签名密钥、客户数据、完整内存转储、专有源码片段或私有硬件标识。
