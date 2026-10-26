# moss-gateway

飞书、钉钉、微信、QQ、Telegram 等消息渠道接入，由 mosswork 仓库统一维护、构建和发布。Python 模块为 `moss_gateway`。

在仓库根目录运行：

```sh
uv sync --frozen
uv build --package moss-gateway --wheel
```

安装及升级使用 [mosswork Release](https://github.com/sudoprivacy/mosswork/releases) 随附的 wheel 或完整产品安装包。

接口源码位于 `src/moss_gateway/`。上游来源及归档摘要见 [SOURCE.json](SOURCE.json)，许可见 [LICENSE](LICENSE)。
