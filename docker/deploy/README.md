# mosswork Docker 部署目录

这个目录可以直接复制到另一台 Docker 主机或复制成另一个环境。主机只需要 Docker Engine 和 `docker compose`；脚本不调用 Python、curl、Node 或其他运行时。

先把应用镜像导入 Docker（或在有源码的机器上构建后导出），然后启动：

```sh
docker load --input mosswork-image.tar
./start.sh
```

停止服务但保留数据：

```sh
./stop.sh
```

复制环境时修改 `.env` 中的 `MOSSWORK_PROJECT_NAME`、`MOSSWORK_PORT` 和 `MOSSWORK_DATA`。项目名避免 Compose 网络和容器相互冲突，数据目录隔离每个环境；不要让两个环境共用同一个数据目录。

首次启动未设置 `MOSSWORK_DEFAULT_PASSWORD` 时，容器会生成随机管理员密码并写入 `MOSSWORK_DATA/credential.txt`。登录后立即修改密码。

脚本默认使用目录内的 `.env` 和 `docker-compose.yml`，也可以通过 `MOSSWORK_ENV_FILE`、`MOSSWORK_COMPOSE_FILE` 指定它们。`stop.sh --volumes` 只会移除 Compose 管理的匿名卷；数据目录是宿主机目录，不会被脚本删除。
