# KAIZO Codex Windows Setup

Windows 原生 Codex / ChatGPT 桌面应用与 CC Switch 配置脚本（v2.4.0，只写配置）。默认 KAIZO、GPT-6 Astra、Medium、Fast OFF；保留个人账户，提供通用 AGENTS.md 和脱敏日志。

## 在 Windows 上运行

在已安装 Git 的 Windows 终端中执行：

```powershell
git clone https://github.com/Dylan-Nihilo/kaizo-codex-windows-setup.git
cd kaizo-codex-windows-setup
.\Setup.cmd
```

也可以 clone 后在文件夹里双击 **Setup.cmd**。无需另找 ZIP，无需提前安装 Python 或 Node.js。

1. 保存工作并退出 Codex 和 CC Switch，包含托盘中的 CC Switch。
2. 以当前日常账户运行，**不要以管理员身份运行**。
3. 首次自动下载并校验所需 Python 运行组件；随后提示输入 **KAIZO API Key**，输入不会显示在屏幕上。公开仓库不含可用密钥。
4. 按 Enter 开始，缺失的 Codex / CC Switch 从官方渠道安装。已有且兼容的应用会复用。
5. 等待显示“配置已写入并回读确认”，然后手动打开 Codex，新建 Windows 本地任务。

## 后续更新

在同一文件夹执行：

```powershell
git pull --ff-only
.\Repair.cmd
```

需要补装应用时运行 `Setup.cmd`。更新脚本本身不会切换账户；运行配置后默认启用 KAIZO，个人账户仍可从 CC Switch 切回。

Key 保存在 `%LOCALAPPDATA%\KAIZO-Setup\provider.json`，下载缓存保存在该目录下的 `Downloads`，均在 Git 仓库之外。后续更新或重新 clone 会复用本机 Key。需要更换 Key 时可编辑该文件；也支持 `KAIZO_API_KEY` 环境变量。旧版配置包中的 `provider.json` 可在首次运行时导入。`provider.example.json` 是空模板，不能直接当作可用凭据。

支持 Windows 10 / 11 的 x64、ARM64 原生环境，自动识别架构。系统必须满足官方桌面应用要求；旧版 Windows 10、企业安装策略或缺少 MSIX 依赖可能阻止安装，需要按系统提示处理。本包不配置 WSL 中的另一套 Codex。

桌面应用已安装时复用，缺失时从官方 Microsoft Store 安装，失败时尝试官方签名 MSIX。首次使用从官方来源下载 Windows Python 3.14.7 运行组件和需要的 CC Switch 3.20.2 安装包，按仓库清单校验 SHA-256 后才使用；下载完成后缓存，后续复用。安装组件下载需要联网。仅在 CC Switch 首次初始化或数据库升级时，由它自身生成数据库；配置完成后不自动打开应用。

## 默认配置

| 项目 | 内容 |
| --- | --- |
| API 地址 | `https://kaizo.top/v1` |
| 密钥 | 首次运行时隐藏输入，保存在当前 Windows 用户目录，界面和日志不显示 |
| 模型 | `gpt-6-astra` |
| 推理 | `medium` |
| Fast | 关闭 |
| 工作规范 | 同目录 `AGENTS.md`，通用内容，无定制人设 |

脚本将模型配置写入 Codex，并同步到 CC Switch 的 Codex 供应商及提示词。会备份并替换全局 `AGENTS.md`，移除优先级更高的全局 `AGENTS.override.md`，关闭其他 Codex 提示词；其他应用的供应商和提示词保留。项目内自己的工作规范仍然有效，已有任务也可能保留旧的模型选择，请新建任务验证。

已有 ChatGPT 登录会保留，登录文件或 Windows 凭据存储方式也会保留。KAIZO 的 Key 单独写入它的供应商配置，KAIZO 模型请求使用这个 Key。只有没有登录文件或其内容为空对象、使用 file 存储且未强制 ChatGPT 登录的新环境，脚本才写入 API Key 格式的 auth.json。已有凭据原文件保留，keyring / auto 存储不探测、不改写；登录是否有效需手动打开应用确认。

`auth.json` 是 Codex 的本地登录状态文件，不代表请求一定走 OpenAI 官方 API。脚本开启 CC Switch 的 `preserveCodexOfficialAuthOnSwitch`，使切换 KAIZO 时保留个人登录。相关行为已核对 [CC Switch 3.20.2 的官方认证保留与切换源码](https://github.com/farion1231/cc-switch/blob/v3.20.2/src-tauri/src/codex_config.rs)。

`/v1` 是 API 路径，不能因为 CC Switch 的端点测速失败就删除。本包只检查保存的配置，不发送模型请求。CC Switch 对地址本身的 GET 测速与 `/v1/responses` 的模型调用不是同一项检查。

## 随时切换个人账户与 KAIZO

两种方式共同保存在 CC Switch 的 **Codex** 页面：

| 条目 | 用途 |
| --- | --- |
| **我的 ChatGPT 账户** | 使用个人账户和官方服务；原来正在使用官方配置时，保留其模型设置 |
| **KAIZO · Astra / Medium** | 使用 KAIZO 的 Key 和额度，Astra / Medium / Fast OFF |

切换步骤：**保存工作并完全退出 Codex → 在 CC Switch 的 Codex 页面启用目标条目 → 重新打开 Codex，新建本地任务**。不需要为了切换而点击“退出登录”，也不需要每次重跑安装脚本。两种方式可保留并切换使用，同一任务仍按其选定的供应商发请求，不会同时合并两边额度。

尚未在 Codex 登录个人账户时，首次启用“我的 ChatGPT 账户”需要完成官方登录。登录后再切回 KAIZO，个人登录会保留。登录失效、主动退出登录或服务器撤销授权时，仍可能需要重新登录；个人账户的网络和订阅权限按官方服务要求。脚本不尝试登录、刷新或退出个人账户；应用要求个人登录时，需要在应用中完成。

若此前运行过旧版包，个人登录已被旧版替换，新版不会猜测或自动恢复旧账户；切到“我的 ChatGPT 账户”登录一次即可开始两边切换。已有的其他官方供应商或托管账户条目保留，也可以继续使用。

运行新版 `Setup.cmd` 可安装或更新配置；应用已经安装且兼容时，也可直接运行新版 `Repair.cmd`。在 KAIZO 状态重跑脚本不会用 KAIZO 设置覆盖已有的个人账户条目。

## 代理处理

检查当前进程、用户/系统环境变量、当前用户 PowerShell 启动文件、Codex `.env` 和 Windows 显式系统代理。

只有本地代理端口明确拒绝连接，且 KAIZO HTTPS 直连通过，才会备份并清理对应的用户环境变量和 `.env` 赋值。对于用户 PowerShell 启动文件，追加只匹配该旧代理完整值的清理段，防止下次启动再次注入。正常代理、远程代理和其他值会保留。

系统级环境变量或 Windows 系统代理失效时，脚本指出来源并停止，由你在系统设置中修正。不会全局关闭代理、改防火墙或改长期 PowerShell 执行策略。企业 PAC、VPN、第三方终端自动注入及 WSL 代理需要在各自来源处理。

## 其他入口

| 文件 | 用途 |
| --- | --- |
| `Setup.cmd` | 检查/安装应用，写入并回读配置，完成后手动打开应用 |
| `Repair.cmd` | 复用已安装且兼容的应用，重新配置、检查代理并核对文件 |
| `Restore.cmd` | 粘贴此前显示的备份目录，恢复配置与被清理的代理 |
| `Preview.cmd` | 仅预览进度界面，不配置应用 |
| `Check.cmd` | 在临时目录运行离线回归检查，不调用真实应用或网络 |
| `Open-Logs.cmd` | 打开日志文件夹，按修改时间查找最近一次运行记录 |

## 出错后查看日志

每次运行独立保存一份 UTF-8 文本日志，成功和失败都会保留：

```text
%LOCALAPPDATA%\KAIZO-Setup\Logs\setup-日期-时间-进程号.log
```

双击 **Open-Logs.cmd** 即可打开日志文件夹。如果该系统目录不可写，会尝试在配置包旁的 `logs` 文件夹保存，窗口会显示实际路径。失败摘要 `Last-error.txt` 放在本次备份目录；备份尚未建立时尝试放在配置包目录。回滚不会删掉运行日志。

日志包含版本、Windows / PowerShell / Python 信息、每步开始与结束时间、耗时、执行程序路径与进程号、退出码、文件或注册表键路径、WinError / HRESULT、配置文件回读结果及回滚结果。失败步骤和最后一次操作会保留在文件中，不会随进度界面关闭而消失。

不记录命令参数、配置正文、环境变量值、个人账户资料、接口请求/响应正文或子进程原始输出；记录的文本另做 Key、Bearer、账户令牌和代理密码脱敏。进度动画不会逐帧写入日志。

**仍出错时，把最新的 `.log` 发来即可**，也可以附 `Last-error.txt`。日志含本地文件路径（可能含 Windows 用户名）；不要发送 `provider.json`、`auth.json`、备份清单或整个备份目录。

备份位于当前用户目录 `.kaizo-setup-backups`。写入或回读失败时，在确认相关应用退出后自动恢复本次配置及代理修改；原本已有的登录凭据不会被安装逻辑覆盖，自动恢复也会保留它们可能发生的正常令牌刷新。若应用仍运行，保留备份并提示使用 `Restore.cmd`。手动恢复会按备份恢复登录文件。恢复不卸载已安装的软件，不撤回系统安装器或 CC Switch 自身产生的所有文件。

源码不含可用 Key。本机配置和备份含凭据，不要提交到仓库或公开 Issues；敏感文件已加入 `.gitignore`。持久凭据文件与备份目录会限制访问权限。

## 验证范围与来源

本版完全取消 Codex 配置服务、模型请求和自动打开应用，直接解析并更新用户目录中的 `config.toml`，同步 CC Switch 数据库。Codex 的安装检测只查询 Windows 注册记录，不访问或运行 `WindowsApps` 中的 `codex.exe`。

回归检查将所有应用启动设为拒绝访问，再执行完整配置流程，确认仍能完成；同时覆盖个人账户和 API Key 凭据保留、file / keyring / auto 存储、两张配置卡切换、TOML 字段值保留、重复执行、备份回滚、代理处理、隐藏输入与日志脱敏。

TOML 写入保留其他字段的值，包括 MCP 配置、数组、带点的键名及日期时间；注释和排版会重新整理，原文保存在本次备份中。脚本核对的是用户配置文件，不能离线证明组织策略、额外启动参数或已有任务的最终生效结果。

检查不调用制作端 Mac 的 Codex 或 CC Switch。GitHub Actions 在 Windows runner 上验证运行组件引导和离线配置流程，不代表真实桌面登录、网络或模型调用已经验证。**“配置已写入”仅表示文件写入和回读通过**；运行 Setup / Repair 也不会发送模型请求。

- [官方 Windows 桌面应用说明](https://learn.chatgpt.com/docs/windows/windows-app)
- [官方 Windows 部署与签名安装包](https://learn.chatgpt.com/docs/enterprise/windows-deployment)
- [官方 Codex 登录说明](https://learn.chatgpt.com/docs/auth)
- [官方 config.toml 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)
- [CC Switch 3.20.2 官方发布](https://github.com/farion1231/cc-switch/releases/tag/v3.20.2)
- [Python 3.14.7 官方 Windows 文件与校验值](https://www.python.org/ftp/python/3.14.7/windows-3.14.7.json)

`assets/manifest.json` 记录原始下载地址和 SHA-256。运行组件和 CC Switch 安装包每次使用前都会校验。当前写入逻辑已核对 CC Switch 3.20.2 的 schema 18；发现其他数据库版本会停止，不猜测新结构。


## 开发与发布

`python -X utf8 check_setup.py` 在临时目录使用虚构凭据和禁止应用启动的测试回归检查；不调用真实 Codex / CC Switch 或模型 API。`scripts/check_powershell.ps1` 从当前源码提取并检查 PowerShell 语法与日志脱敏。推送会触发 Windows CI。

[版本记录](https://github.com/Dylan-Nihilo/kaizo-codex-windows-setup/releases)提供每版变更；日常使用 `git pull --ff-only` 更新。Python 与 CC Switch 按固定版本和校验值下载，升级时需更新 `assets/manifest.json` 并核对兼容性，不能只替换下载链接。

第三方组件通过上面的官方链接下载，遵循各自许可证：[Python](https://docs.python.org/3/license.html)、[CC Switch MIT](https://github.com/farion1231/cc-switch/blob/v3.20.2/LICENSE)。本项目不重新打包第三方安装器。
