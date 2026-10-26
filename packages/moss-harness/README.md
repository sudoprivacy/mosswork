# moss-harness

mosswork 的 Agent 执行引擎，与服务端和桌面客户端在同一仓库中构建发布。

- Python 包：`moss_harness`
- 安装包与命令：`moss-harness`
- 源码：`packages/moss-harness/src/moss_harness`
- 功能：模型调用、工具执行、工作区、Docker 沙箱、上下文压缩、MCP、技能与插件。

在仓库根目录使用 `uv sync` 安装本地引擎；`uv build --package moss-harness --wheel` 构建 wheel。
服务端 Docker 和客户端 portable 打包均使用此目录源码，不下载同名上游包。

来源版本与归档校验值记录在 `SOURCE.json`，许可证见 `LICENSE`。
