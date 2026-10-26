# moss-server 镜像部署

服务端通过 Docker Compose 运行 moss-server、通信服务和 PostgreSQL。客户端名称为 **mosswork**，从[GitHub Releases](https://github.com/sudoprivacy/mosswork/releases)下载 macOS ARM64 DMG 或 Windows x64 EXE；安装后填写服务端地址。

## 一键安装

准备 Linux amd64/arm64 主机、Docker Engine、支持 `up --wait` 的 Compose v2、Python 3.11+、curl 和 tar。主机需要能够下载 GitHub Releases 附件；服务端、通信服务和 PostgreSQL 镜像均包含在发行包中。默认安装在 `/opt/moss-server`，监听 `0.0.0.0:8088`。

安装最新正式版：

```sh
curl -fsSL https://github.com/sudoprivacy/mosswork/releases/latest/download/install-moss-server.sh | sudo bash
```

预发布版本不进入 GitHub 的 `latest` 入口。从对应 Release 复制带 tag 的命令，例如：

```sh
curl -fsSL https://github.com/sudoprivacy/mosswork/releases/download/v0.0.1/install-moss-server.sh | sudo bash
```

自定义目录、监听地址和端口：

```sh
curl -fsSL https://github.com/sudoprivacy/mosswork/releases/latest/download/install-moss-server.sh |
  sudo bash -s -- --install-dir /srv/moss-server --bind 127.0.0.1 --port 8088
```

脚本按主机架构下载部署包与镜像包，校验两个文件的 SHA-256，通过 `docker load` 导入同一发行版本的镜像，生成数据库密码和通信密钥，执行迁移、创建首个管理员，并等待服务健康。初始凭据只写入权限为 `0600` 的文件：

```sh
sudo cat /opt/moss-server/initial-admin.json
```

打开 `http://服务器IP:8088` 登录，修改初始密码、配置模型并给员工授权。外网访问应配置受信任的 HTTPS 入口；使用反向代理时可绑定 `127.0.0.1`。数据库和通信端口不对宿主开放。应用会挂载本机 Docker socket，以创建隔离工作区。

## 文件与运维

| 路径 | 内容 |
| --- | --- |
| `/opt/moss-server/deployment.env` | 镜像版本、端口和私有配置 |
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

Release 提供 `moss-server-images-<tag>-linux-amd64.tar.gz` 和 `moss-server-images-<tag>-linux-arm64.tar.gz`，每个包包含对应架构的 `moss-server:<version>`、`moss-server-im:<version>` 和 PostgreSQL 镜像。`moss-server-linux-<arch>-images.json` 记录镜像 ID、标签及架构，`SHA256SUMS.txt` 记录附件摘要。Compose 仅使用本地已导入的镜像，不自动拉取镜像。源码部署与更细的资源规划见[企业部署与恢复](README.md)。

## 手动导入或离线安装

主机已具备上述 Docker、Compose 和 Python 环境时，可从同一 Release 下载部署包、对应架构的镜像包和 `SHA256SUMS.txt`，复制到服务器同一个目录。以下以 amd64 的 `v0.0.1` 为例；arm64 主机替换镜像包文件名中的架构：

```sh
sha256sum --check --ignore-missing SHA256SUMS.txt
sudo docker image load --input moss-server-images-v0.0.1-linux-amd64.tar.gz
tar -xzf moss-server-deploy-v0.0.1.tar.gz
sudo python3 moss-server/deploy.py --version v0.0.1
```

导入和启动阶段不需要镜像仓库访问权限。镜像包较大，下载和导入前应预留临时文件与 Docker 镜像的磁盘空间。
