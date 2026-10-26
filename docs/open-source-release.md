# mosswork 源码发行说明

[English](open-source-release_EN.md)

本文面向维护者，说明 [mosswork](https://github.com/sudoprivacy/mosswork) 的源码导出、校验和发行要求。当前源码版本为 `0.0.1`，主项目、自有引擎及通信模块采用 MIT。

本仓库提供源码，服务端镜像与 Windows/macOS 企业客户端按对应构建文档生成。发行材料通过[本仓库 Releases](https://github.com/sudoprivacy/mosswork/releases)分发；二进制安装包需单独完成目标平台验证，并记录签名与公证状态。

## 源码范围

- 以中文作为默认首页，保留英文说明、中英文员工指南、贡献说明、安全政策及部署和构建说明。
- 根目录及通信服务保留原始 MIT 许可和贡献者声明。第三方来源与分发范围见 [NOTICE](../NOTICE.md)。
- 本发行版禁用在线更新 CLI/API、上游一键安装脚本及 `make publish` / `publish-test`；安装和升级使用本仓库的 Release 镜像、客户端或源码。
- 公开副本排除 Git 历史、本地工具配置、运行环境文件、浏览器会话、部署记录和构建产物。
- `office-automation` 专家及其头像不纳入源码发行，因为其随附文档技能限制再分发。测试和示例使用公开包含的专家；独立文档处理库和本机工具不受此排除影响。
- 员工快速上手使用 [Markdown 文档](user-guide.md)；源码发行不包含生成的 `docs/user-guide.html` 和上游个人版介绍 `docs/mosswork-introduction.html`。
- 源码发行不包含 FnOS 打包工具及上游发行资产下载表生成工具。
- 上游工作流保存在 `.github/upstream-workflows/`，扩展名为 `.disabled`。本项目的 `mosswork-ci.yml`、`mosswork-release.yml` 在公开导出中保持启用；其他工作流仍归档。

## Tag 自动发布

推送与 `pyproject.toml` 版本完全一致的 `v<version>` tag 后，`mosswork-release.yml` 自动构建。当前版本对应 `v0.0.1`；工作流不自动修改版本、提交代码或创建 tag。手工重跑须选择同一个 tag，选择分支会被拒绝。

发布前先按下文导出并验证完整公开源码，将确定的源码提交到发行仓库，再由维护者推送 tag。流程从该 tag 再次导出完整公开范围，并从同一份导出物构建客户端和镜像。

| 产物 | 平台与名称 |
| --- | --- |
| mosswork 客户端 | `mosswork-company-macos-arm64-<version>.dmg`，macOS 13+ ARM64 |
| mosswork 客户端 | `mosswork-company-windows-amd64-<version>.exe`，Windows x64 |
| 服务端镜像包 | `moss-server-images-<tag>-linux-<arch>.tar.gz`，分别提供 amd64、arm64，含 moss-server、通信服务和 PostgreSQL |
| moss-harness 引擎 | `moss_harness-<version>-py3-none-any.whl`，源码位于 `packages/moss-harness` |
| 下载附件 | 公开源码、部署包、`install-moss-server.sh`、构建记录、镜像 ID 与 SHA-256 |

CI 不发布 macOS Intel、Windows ARM、Linux 客户端、PyPI 包或独立 portable 下载包。两个客户端和两个架构的镜像都成功后才发布 GitHub Release，任一失败不会发布部分版本。预发布 tag（如 `v0.0.2b1`）标记为 prerelease，不更新正式版 `latest`。已公开的 Release 不允许覆盖。

仓库需允许 Actions 的 `GITHUB_TOKEN` 写入 Releases。所有产物均作为 Release 附件分发，无需 Packages 权限或镜像仓库凭据。两个原生 Linux runner 分别构建并导出对应架构的 Docker 镜像包，重新导入归档后验证首次部署、健康检查和重复安装。发布前校验每个附件小于 GitHub 的 2 GiB 上限，并生成 SHA-256 清单。

客户端运行时包含目标架构 Python、锁定的第三方引擎、文档库和 FFmpeg，并在各自原生 runner 上执行工具冒烟测试。FFmpeg 的下载源与 SHA-256 固定在 `desktop/company-tools.json`，使用 Sudowork 的公开镜像；维护时同步来源、许可材料和对应源码。Windows 内置微软完整 WebView2 安装器并验证微软签名。当前 CI 的 macOS 应用使用 ad-hoc 签名，未公证；Windows 客户端未做 Authenticode 签名。构建记录不代表已经完成真实终端安装验收。

部署命令和数据目录见[镜像一键部署](../docker/enterprise/INSTALL.md)。服务端 CLI 使用 `moss-server`；底层 Python 包和配置变量继续使用 `mosswork` / `MOSSWORK_`。

## 导出与验证

同一次正式发布前，所有修改在企业版仓库中统一累积，并共用一个当前待发布入口。候选源码从完整工作区导出，包括先前批次的未提交修改和新增测试；验证状态单独记录，不按修复批次拆成并列的待发布版本。历史快照仅用于追溯。

在开发仓库执行，输出必须是仓库外尚不存在的新目录：

```sh
python3 tools/export_public_source.py --output /absolute/new-directory/mosswork-Enterprise
python3 tools/check_public_source.py --root /absolute/new-directory/mosswork-Enterprise
```

导出工具以 Git 跟踪和未忽略文件为候选，复制当前工作区内容，包括未提交改动。它不会修改 Git 索引、分支或远端。每个源码快照使用新的输出目录。

刷新待发布入口时，先导出到新的临时目录并验证，再更新该入口中的源码、压缩包、变更清单和校验值；上一快照转为历史记录。后续修改仍归入同一待发布版本。正式构建前再次比对完整工作区与候选清单，确认没有遗漏；安装包和运行时工具包从这份最终源码统一构建。

`PUBLIC_SOURCE.json` 记录源 HEAD、`source_worktree_dirty`、导出范围和逐文件 SHA-256。`source_worktree_dirty=true` 表示快照包含未提交改动，需用逐文件清单和源码包 SHA-256 标识完整内容。正式发行从指定提交的干净工作树导出，并保留对应标签、清单与校验值。

校验工具检查清单摘要、主要文档的本地链接、锚点与图片、专家提示文件和命令示例指向的专家，并拦截上游镜像、下载入口和打包身份回流。归档为 inactive 的上游工作流、来源致谢与带有效入口阻断的旧安装器不作为可用分发入口。符号链接只能指向发行范围内的相对路径，摘要针对链接文本。内置凭据模式筛查覆盖有限，不等同于完整安全或许可审计。

## 构建前提

服务端支持目标是 Linux + Docker Engine + Compose，命令见[企业部署与恢复](../docker/enterprise/README.md)。构建网络需要能获取基础镜像与依赖；镜像包包含应用、通信服务和 PostgreSQL；安装主机仍需预先具备 Docker Engine、Compose v2 和 Python 3.11+。将镜像包、部署包和摘要清单复制到主机后可以离线导入并启动。

客户端构建见[桌面说明](../desktop/README.md)（English）。正式 CI 产物限定 Windows amd64 和 macOS 13+ ARM64。`desktop/package-company.py` 优先从 `PUBLIC_SOURCE.json` 读取来源身份；普通检出使用 Git HEAD 和工作树状态。导出源码无需重新创建 Git 提交。

登录 bundle、`company-dashboard.zip` 和生成清单不随源码包分发。桌面完整打包器会从源码构建这些资源；单独构建桌面壳时，先按桌面文档生成资源。

## 仓库与安全入口

- 问题与建议：[本仓库 Issues](https://github.com/sudoprivacy/mosswork/issues)。
- 安全报告：按 [SECURITY](../SECURITY.md) 获取私密渠道，不在 Issue 中提交漏洞细节。
- 贡献分支：工作分支向本仓库 `main` 提交 PR，见[贡献说明](../CONTRIBUTING.md)。
- 新建分发仓库时，使用校验通过的源码快照；通过 NOTICE 与 PUBLIC_SOURCE 保留来源信息。

## 发行清单

- 对应提交或候选快照身份、源码包与 SHA-256，目录与压缩包逐文件一致。
- 版本、Harness 版本、主库和通信库 schema、设备与公司运行时协议要求。
- 构建平台和架构、依赖版本、构建命令、服务端部署和客户端安装说明。
- 实际验证的平台与架构，以及未验证的安装、签名、公证、容量、升级和恢复项目。
- 已知限制、数据边界、升级前备份和回退步骤；计划中的群机器人等能力不列为已交付。
- LICENSE、NOTICE、通信服务及第三方许可。后续分发安装包时，按实际组件附上 FFmpeg、WebView2、Python 等许可，源码包检查不能替代二进制分发检查。
- 若提供镜像或安装包，一并公布唯一标签/digest、架构、SHA-256、签名和公证状态，不复用同一正式标签覆盖不同源码。

桌面 bundle 与 Windows assembly 使用 `io.github.sudoprivacy.mosswork`，Docker 镜像元数据指向本仓库。二进制发行需指定镜像地址、签名主体和升级步骤；升级前保留员工工作目录、账号配置与任务回执。Python 导入路径、CLI 命令、环境变量和数据库名沿用 `mosswork` 约定。
