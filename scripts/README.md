# mosswork 脚本

开发依赖用 `uv sync --locked` 和 `npm --prefix dashboard ci` 准备。

`install-moss-server.sh` 默认从本仓库 Release 下载校验过的部署包，导入 moss-server 镜像并初始化服务。也可以用 `--offline-dir PATH` 从本地目录安装；目录放置部署包和 `SHA256SUMS.txt`，镜像包可省略并直接使用已经加载到 Docker 的镜像。部署主机只需 shell 工具和 Docker，不需要 Python；使用方法见[一键部署](../docker/enterprise/INSTALL.md)。

上游 `install.sh`、`install-mosswork.sh`、`install.ps1` 和 `install.bat` 仍然禁用。

服务端部署见[企业部署指南](../docker/enterprise/README.md)，桌面客户端构建见[桌面说明](../desktop/README.md)。

其余脚本的用途以各文件说明为准。维护者导出和校验源码发行包时，参阅[源码发行说明](../docs/open-source-release.md)。
