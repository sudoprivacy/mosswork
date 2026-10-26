# moss-browser

浏览器自动化、登录状态、录制与回放，由 mosswork 仓库统一维护、构建和发布。Python 模块为 `moss_browser`。

在仓库根目录运行：

```sh
uv sync --frozen
uv build --package moss-browser --wheel
```

安装及升级使用 [mosswork Release](https://github.com/sudoprivacy/mosswork/releases) 随附的 wheel 或完整产品安装包。

接口源码位于 `src/moss_browser/`。上游来源及归档摘要见 [SOURCE.json](SOURCE.json)，许可见 [LICENSE](LICENSE)。
