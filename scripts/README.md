# mosswork 脚本

开发依赖用 `uv sync --locked` 和 `npm --prefix dashboard ci` 准备。

`install-moss-server.sh` 从本仓库 Release 下载校验过的部署包，拉取 moss-server 镜像并初始化服务，使用方法见[一键部署](../docker/enterprise/INSTALL.md)。

上游 `install.sh`、`install-mosswork.sh`、`install.ps1` 和 `install.bat` 仍然禁用。

服务端部署见[企业部署指南](../docker/enterprise/README.md)，桌面客户端构建见[桌面说明](../desktop/README.md)。

其余脚本的用途以各文件说明为准。维护者导出和校验源码发行包时，参阅[源码发行说明](../docs/open-source-release.md)。
