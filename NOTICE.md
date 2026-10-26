# 来源与第三方许可

[English](NOTICE_EN.md)

## mosswork

mosswork 基于 [colorAi/Octop-Enterprise](https://github.com/colorAi/Octop-Enterprise) 的 `46fffbb7349b4099a543f359c3cef1194332e142` 提交开发，保留以下上游来源、版权及许可声明。

## Octop

Octop Enterprise 基于 [TencentCloud/Octop](https://github.com/TencentCloud/Octop) 二次开发，保留上游 `Copyright (c) 2026 Octop` 及 MIT 许可正文。企业桌面本机执行、企业服务适配、内部通信等改造由 Octop Enterprise 贡献者维护；项目不代表腾讯官方企业发行。

本项目基于上游 `eb280112`（1.0.2b6），并选择性移植了截至 `f79015b078c4d5d4c309a1bf6f3cd39ac06cc18f` 的部分变更。源码发行包中的 `PUBLIC_SOURCE.json` 记录导出时的源提交、工作树状态和逐文件摘要；上游提交用于标识来源范围，不表示完整合并。

## 自有通信服务

`services/communication/` 源自项目权利人开发的 Miaox 通信代码，采用目录内的 [MIT License](services/communication/LICENSE)。

[SOURCE.json](services/communication/SOURCE.json) 中的提交和摘要记录首次提取时的来源；后续修改文件的发行摘要以源码包中的 `PUBLIC_SOURCE.json` 为准。

## moss-harness

`packages/moss-harness/` 从 TencentCloud/octop-harness 1.0.3 源码派生，采用随附 MIT License。源归档地址及 SHA-256 记录于该目录的 SOURCE.json；Python 模块为 moss_harness，由本仓库构建维护。

## 第三方组件

根目录 MIT 许可不替代第三方许可证。以下是主要来源及随附许可的索引，不是完整依赖 SBOM：

| 内容 | 来源或许可位置 |
| --- | --- |
| Octop Gateway、Memory、Browser | 各依赖包的元数据、许可证及对应 TencentCloud 上游仓库 |
| 工程专家与设计专家 | 各专家目录内的 `LICENSE`（Apache-2.0）；保留随附来源声明 |
| SheetJS CE 0.20.3 | `dashboard/vendor/README.md`；tarball 内的 Apache-2.0 许可与第三方声明 |
| Python、Node.js、Go 与各语言依赖 | 各运行时和依赖包随附的许可；版本由 uv.lock、package-lock.json、go.sum 记录 |
| Wails、系统 WebView 及其他桌面依赖 | 各项目的许可与分发要求 |

制作二进制、Docker 镜像或客户端安装包时，还要附上实际携带组件的许可证。FFmpeg 的许可取决于具体构建选项；WebView2、Python 和其他运行时不能仅用本项目 MIT 标签替代各自分发要求。

## 未纳入公开导出的材料

`office-automation` 专家中的 docx、pptx、pdf、xlsx 技能受 Anthropic 许可的复制、衍生和再分发限制，未纳入本项目的公开源码发行。

本项目的文档工具基于 python-docx、python-pptx、pypdf、openpyxl 等独立依赖实现，各依赖遵循自身许可证。

Harness 上游附带的受限 `powerpoint` 技能（中英文）未纳入本仓库；文档工具仍使用 python-pptx。
