# mosswork Docker 部署

企业模式从本仓库构建，使用 [enterprise/README.md](enterprise/README.md) 的首次安装、升级和恢复步骤。它包含 PostgreSQL、独立通信服务、设备通道和员工工作区隔离。

若已有导入 Docker 的应用镜像，直接复制 [`deploy/`](deploy/) 目录即可部署基础单机模式。编辑 `.env` 中的项目名、端口和数据目录后执行 `./start.sh`，停止执行 `./stop.sh`；目录内脚本只调用 Docker Compose，不需要 Python。

`Dockerfile` 构建基础应用镜像，`enterprise/compose.yml` 组合企业服务。其他 Compose 文件适用于基础模式和开发环境。

[English](README.md)
