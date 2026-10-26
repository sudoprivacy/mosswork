# 产品命名与构建

| 用途 | 名称 |
| --- | --- |
| 项目仓库、桌面客户端、原生窗口与安装包 | `mosswork` |
| 浏览器中的服务端界面、API 标题、系统服务 | `moss-server` |
| Python 包、导入路径 | `mosswork`、`src/octop/` |
| 服务端命令 | `moss-server`，同时保留本项目的 `mosswork` 命令入口 |
| 环境变量、默认数据目录 | `MOSSWORK_*`、`~/.mosswork` |

普通网页构建显示 `moss-server`。`desktop/build-dashboard.py` 设置
`VITE_DESKTOP_BUNDLE=1`，生成显示 `mosswork` 的客户端界面。标题、翻译文案
和横向标志使用同一个构建模式；两端均提供亮色和暗色标志。

服务端名称常量位于 `src/octop/branding.py`，网页名称和标志选择位于
`dashboard/src/utils/branding.ts`。修改展示名称不应修改用户文件路径、
翻译插值参数、仓库地址或第三方依赖标识。

`moss-harness` 是仓库内维护的引擎，源码位于 `packages/moss-harness`，
Python 模块为 `moss_harness`，命令为 `moss-harness`。通过 uv workspace
与主项目一起锁定和构建，服务端镜像及桌面运行时均包含本地构建的版本。
技能元数据使用 `metadata.mosswork`，同时读取历史格式。
`moss-gateway`、`moss-memory`、`moss-browser` 仍是第三方依赖。
原始版权、来源声明和历史更新记录保留在 LICENSE、NOTICE 和 CHANGELOG 中。

图标使用 sudowork 的几何标志；源文件为 `docs/assets/brand-mark.svg`。
执行 `uv run tools/generate_brand_assets.py` 可重新生成网页、PWA、桌面及
托盘资源。桌面构建再从 PNG 生成各平台的 ICNS 和 ICO。

本源码使用新的模块名、环境变量和客户端桥接标识。部署时应从同一份源码
构建服务端和客户端，不能直接混用原版客户端。现有服务升级前需要映射原
`OCTOP_*` 配置，并显式指定原数据目录；重命名源码不会自动迁移已有数据库、
容器卷、浏览器登录状态或桌面客户端缓存。
