# mosswork Docker deployment

For enterprise mode, build from this repository and follow the [enterprise deployment guide](enterprise/README_EN.md) for installation, upgrades, backups and recovery. The stack includes PostgreSQL, a separate communication service, the device channel and employee workspace isolation.

For a standalone deployment with an already imported application image, copy the [`deploy/`](deploy/) directory. Edit the project name, port and data directory in `.env`, then run `./start.sh` or `./stop.sh`. These scripts call Docker Compose only and do not require Python.

`Dockerfile` builds the base application image; `enterprise/compose.yml` assembles the enterprise services. Other Compose files cover base mode and development environments.

[中文](README_CN.md)
