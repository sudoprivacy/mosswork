# moss-server 镜像部署

服务端通过 Docker Compose 运行 moss-server、通信服务和 PostgreSQL。客户端名称为 **mosswork**，从[GitHub Releases](https://github.com/sudoprivacy/mosswork/releases)下载 macOS ARM64 DMG 或 Windows x64 EXE；安装后填写服务端地址。

## 一键安装

准备 Linux amd64 主机、Docker Engine、支持 `up --wait` 的 Compose v2、Bash、tar 和 sha256sum。在线安装还需要 curl 和 GitHub Releases 下载访问；离线安装使用提前复制的附件，无需 curl。服务端、通信服务和 PostgreSQL 镜像均包含在发行包中。默认安装在 `/opt/moss-server`，监听 `0.0.0.0:8088`。

安装最新正式版：

```sh
curl -fsSL https://github.com/sudoprivacy/mosswork/releases/latest/download/install-moss-server.sh | sudo bash
```

预发布版本不进入 GitHub 的 `latest` 入口。从对应 Release 复制带 tag 的命令，例如：

```sh
curl -fsSL https://github.com/sudoprivacy/mosswork/releases/download/v0.0.6/install-moss-server.sh | sudo bash
```

自定义目录、监听地址和端口：

```sh
curl -fsSL https://github.com/sudoprivacy/mosswork/releases/latest/download/install-moss-server.sh |
  sudo bash -s -- --install-dir /srv/moss-server --bind 127.0.0.1 --port 8088
```

在线安装按主机架构下载部署包与镜像包，校验两个文件的 SHA-256，通过 `docker load` 导入同一发行版本的镜像，生成数据库密码和通信密钥，执行迁移、创建首个管理员，并等待服务健康。脚本不需要 Python、Node 或包管理器。初始凭据只写入权限为 `0600` 的文件：

```sh
sudo cat /opt/moss-server/initial-admin.json
```

打开 `http://服务器IP:8088` 登录，修改初始密码、配置模型并给员工授权。外网访问应配置受信任的 HTTPS 入口；使用反向代理时可绑定 `127.0.0.1`。数据库和通信端口不对宿主开放。应用会挂载本机 Docker socket，以创建隔离工作区。

## 文件与运维

| 路径 | 内容 |
| --- | --- |
| `/opt/moss-server/.env` | 镜像版本、端口和私有配置；`deployment.env` 是兼容链接 |
| `/opt/moss-server/docker-compose.yml` | 可直接使用的 Compose 文件 |
| `/opt/moss-server/start.sh`、`stop.sh` | 一键启动、停止；停止保留数据 |
| `/opt/moss-server/data/` | 数据库、应用数据、媒体和通信密钥 |
| `/opt/moss-server/current/` | 最近一次成功启动版本的 Compose 和部署工具 |
| `/opt/moss-server/releases/<tag>/` | 各版本部署文件 |

检查状态、查看日志：

```sh
sudo docker compose -p moss-server --env-file /opt/moss-server/deployment.env \
  -f /opt/moss-server/current/compose.release.yml ps
sudo docker compose -p moss-server --env-file /opt/moss-server/deployment.env \
  -f /opt/moss-server/current/compose.release.yml logs --tail 100 moss-server
curl -fsS http://127.0.0.1:8088/api/health/ready
```

首次安装中断时，可用同一版本重新执行安装命令。已生成的密码、密钥和数据会保留；数据库已有用户时不会再次创建管理员。初始化文件记录的是初始密码，用户修改密码后不会更新此文件。

日常启动和停止：

```sh
sudo bash /opt/moss-server/start.sh
sudo bash /opt/moss-server/stop.sh
```

## 复制为另一个环境

将同一 Release 的部署包解压到新目录，编辑其中的 `.env`，将 `MOSSWORK_PROJECT_NAME` 和 `MOSSWORK_PORT` 改成未占用的项目名和端口。`MOSSWORK_DATA_ROOT` 留空时自动创建该目录内的 `data/`；也可指定独立的绝对路径。镜像已导入后，在新目录执行 `sudo bash start.sh` 即可生成新密码、密钥和数据库并启动。

已有环境也可在停止服务后复制整个目录，再修改副本 `.env` 的项目名、端口和 `MOSSWORK_DATA_ROOT`（改为副本的数据目录绝对路径）。副本会保留原账号和资料；两个环境不能共用同一数据目录。

## 升级与备份

先停止接收任务，等待本机与隔离工作区任务结束，再停止整套服务并备份。以下命令使用默认目录；自定义安装时替换对应路径。

```sh
sudo install -d -m 700 /srv/moss-server-backups
sudo docker compose -p moss-server --env-file /opt/moss-server/deployment.env \
  -f /opt/moss-server/current/compose.release.yml stop
sudo tar -C /opt -czf "/srv/moss-server-backups/moss-server-$(date +%Y%m%d-%H%M%S).tar.gz" moss-server
sudo docker compose -p moss-server --env-file /opt/moss-server/deployment.env \
  -f /opt/moss-server/current/compose.release.yml up -d --wait
```

从目标 Release 下载脚本并传入 `--upgrade`。例如安装最新正式版：

```sh
curl -fsSL https://github.com/sudoprivacy/mosswork/releases/latest/download/install-moss-server.sh |
  sudo bash -s -- --upgrade
```

升级保留数据和密钥，先下载、校验并导入镜像，再停止应用和通信服务、执行迁移并启动新版本。只有健康检查通过后才更新 `deployment.env` 和 `current`。失败时保留现场检查日志；数据库迁移可能已经执行，不能只切换旧镜像来回退。需要恢复时，在隔离主机上恢复完整备份并使用对应旧镜像验证，再切换入口。

Release 提供 `moss-server-images-<tag>-linux-amd64.tar.gz`，包含 `moss-server:<version>`、`moss-server-im:<version>` 和 PostgreSQL 镜像。服务端仅支持 Linux amd64；`moss-server-linux-amd64-images.json` 记录镜像 ID、标签及架构，`SHA256SUMS.txt` 记录附件摘要。Compose 仅使用本地已导入的镜像，不自动拉取镜像。源码部署与更细的资源规划见[企业部署与恢复](README.md)。

## 离线安装与本地镜像

将同一 Release 的 `install-moss-server.sh`、部署包、amd64 镜像包及 `SHA256SUMS.txt` 提前复制到 Linux amd64 服务器，例如 `/srv/moss-server-release`。进入该目录执行离线安装：

```sh
cd /srv/moss-server-release
sudo bash install-moss-server.sh --offline
```

安装器会自动启动服务。附件目录只存放下载文件，实际安装目录默认为 `/opt/moss-server`；日常启动、停止使用安装目录中的 `start.sh` 和 `stop.sh`。也可以从其他目录指定附件路径：

```sh
sudo bash /srv/moss-server-release/install-moss-server.sh \
  --offline-dir /srv/moss-server-release --version v0.0.6
```

`--offline` 表示使用当前工作目录，`--local-dir PATH` 与 `--offline-dir PATH` 等价。安装器直接读取本地文件，校验后导入镜像，不下载附件、不查询 GitHub 最新版本，也不会将大镜像包复制到临时目录。源文件会保留，缺文件或校验失败直接报错，不转为在线下载。源码脚本未指定版本时，只在目录内有唯一部署包时推断版本；有多个版本时必须传 `--version`。

如果 Docker 中已经加载了对应版本的 moss-server、moss-server-im 和 PostgreSQL 镜像，离线目录只需放部署包和 `SHA256SUMS.txt`。安装器会跳过镜像导入，由部署脚本检查本地三个镜像；缺少镜像时会报错，Compose 不会自动拉取。目录中仍有镜像包时会校验并重新导入。

离线安装同样支持 `--install-dir`、`--port`、`--bind`、`--socket` 和 `--upgrade`，例如：

```sh
sudo bash install-moss-server.sh --offline-dir /srv/moss-server-release \
  --version v0.0.6 --install-dir /srv/moss-server --port 18088
```

### 手动导入

也可以直接执行以下手动步骤。镜像已加载时省略 `docker image load`：

```sh
sha256sum --check --ignore-missing SHA256SUMS.txt
sudo docker image load --input moss-server-images-v0.0.6-linux-amd64.tar.gz
tar -xzf moss-server-deploy-v0.0.6.tar.gz
sudo bash moss-server/deploy.sh --version v0.0.6
```

导入和启动阶段不需要镜像仓库访问权限。镜像包较大，下载和导入前应预留临时文件与 Docker 镜像的磁盘空间。
