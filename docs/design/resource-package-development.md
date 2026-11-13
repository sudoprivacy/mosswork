# mosswork 专家与技能资源包开发规范

本规范对应内置资源市场第一版。市场提供离线 ZIP 导入、文件校验、冻结草稿、提交审核、管理员发布、正式版本下载、推荐版本和软下架。发布和推荐操作均不修改现有 Agent、不自动触发员工升级。管理员发布的已上架专家，会在“专家 → 专家市场”展示推荐版本，用户可从该版本创建私有 Agent，并安装其固定技能依赖。

## 1. 交付对象

技能是具体任务的方法、脚本及参考资料；专家是角色与工作流程模板，可声明对已发布技能版本的依赖。完整的其他平台 Agent 服务或专有工作流需要适配，不能直接当作通用专家模板导入。

开发人员可以在任意编辑器或仓库开发。交付包不得依赖开发者电脑的绝对路径、虚拟环境、缓存、个人账号或会话。

## 2. 技能目录与内容

```text
equipment-query/
  market.json
  SKILL.md
  README.md
  scripts/query_equipment.py
  references/api-guide.md
  assets/report-template.xlsx
  examples/query-input.json
  tests/README.md
```

必需文件为 market.json 和 SKILL.md。其他文件按实际需要提供。根目录允许 README.md、CHANGELOG.md、LICENSE、LICENSE.txt、LICENSE.md；支持目录为 scripts、references、assets、examples、tests。第一版不隐式执行安装脚本。

SKILL.md 示例：

```markdown
---
name: equipment-query
description: 根据设备编号查询设备状态，在用户询问设备运行或巡检信息时使用。
---

# 设备状态查询

使用条件：已配置设备系统接口地址，并获得查询授权。
输入：设备编号；缺失时询问用户。
步骤：校验编号，按 references/api-guide.md 调用 scripts/query_equipment.py，整理接口真实返回结果。
输出：设备编号、状态、告警与查询时间。
异常：设备不存在时提示核对；无权限时提示管理员配置；接口失败时报告失败，不编造状态。
边界：本技能只查询，不修改设备参数。
```

第一版强制 name 与 market.json 的 slug 一致，description 和正文非空。说明应交代触发场景、输入输出、脚本参数、附件位置、异常处理以及业务写入边界。

## 3. 专家目录与内容

```text
equipment-inspector/
  market.json
  manifest.json
  SOUL.md
  IDENTITY.md
  AGENTS.md
  TOOLS.md
  README.md
```

必需文件为 market.json、manifest.json、非空 SOUL.md。IDENTITY.md、AGENTS.md、TOOLS.md、HEARTBEAT.md、PROACTIVE.md 按需提供；根说明和许可文件同技能，可附 references、assets、examples、tests。第一版要求依赖技能单独上架并显式引用，不接收内嵌 skills 或自动子专家目录。

manifest.json 示例：

```json
{
  "id": "equipment-inspector",
  "label": {"zh": "设备巡检专家", "en": "Equipment Inspector"},
  "description": {"zh": "设备查询、异常分析与巡检报告", "en": "Equipment inspection and reporting"},
  "prompt_files": ["SOUL.md", "IDENTITY.md"]
}
```

id 必须与 market.json 的 slug 一致。prompt_files 是模板展示信息，不授予工具、技能或连接器权限。SOUL.md 写角色职责、工作流程、技能选择、信息不足的处理和操作边界。专家市场创建支持通用服务器定义及其固定技能依赖；平台专用执行端、参数注入及能力适配按下方安装限制处理。

## 4. 市场声明

下面是符合第一版格式的技能声明。专家使用相同结构，将 kind 改为 expert 并填写自己的 slug/name。该规范中不接受未声明字段，避免误以为平台已处理其他平台的配置。

```json
{
  "format": "mosswork.market.v1",
  "kind": "skill",
  "slug": "equipment-query",
  "name": "设备状态查询",
  "description": "查询厂内设备实时状态",
  "version": "1.0.0",
  "changelog": "首次发布",
  "dependencies": [],
  "runtime": {
    "execution_targets": ["server"],
    "platforms": ["linux-amd64"],
    "python": ">=3.12",
    "runtime_profile": "factory-tools"
  },
  "configuration": {
    "business_api_url": {"type": "url", "required": true, "description": "现场设备系统接口根地址"},
    "credential_binding": {"type": "credential_ref", "required": true, "description": "设备系统凭据引用"}
  },
  "required_capabilities": ["business-system-query"],
  "network": {"scope": "intranet"},
  "license": "MIT",
  "author": "现场开发组"
}
```

| 字段 | 规则 |
| --- | --- |
| format | mosswork.market.v1；兼容接收 octop.market.v1 声明 |
| kind | skill 或 expert |
| slug | 小写英文字母开头，使用小写字母、数字、单个连字符，最长 80 字符 |
| name / description | 非空，分别最多 120 / 4000 字符 |
| version | 严格三段非负数字，如 1.0.0；不允许前导零、预发布后缀或覆盖正式版本 |
| changelog | 非空，最多 8000 字符 |
| dependencies | 精确的 resource_id/version；发布时要求对应已发布、上架中的技能；不接受自依赖或重复依赖 |
| runtime | execution_targets 为 server/desktop；platforms 非空，如 linux-amd64、windows-amd64、darwin-arm64；无平台要求可填 any |
| configuration | 声明字段类型、必填与说明；类型为 string/url/integer/boolean/credential_ref；不包含真实值 |
| required_capabilities | 所需能力声明，不授予权限；当前安装器拒绝非空能力要求，待对应运行适配完成后开放 |
| network.scope | none 或 intranet；第一版不接受公网依赖范围 |
| license / author | license 必填，保留代码与附件的实际来源及分发许可；不要把第三方限制材料直接标成 MIT |
| resource_id | 可省略；首次导入由服务器建立身份。提供时必须与显式选中的目标资源一致 |

runtime、configuration 等为发布契约，不代表平台自动准备环境、注入参数或安装依赖。安装不会执行初始化脚本或联网下载依赖。新建 Agent 的实际对话仍需要当前账号可用的模型及运行环境。

## 5. 身份与新版本

首次导入使用“导入资源”。后续选择原资源的“导入新版本”，保持 kind 和 slug 一致，修改 version、changelog 和文件内容。不能按名称相同覆盖他人的资源；维护者归属由服务器核对。

每次导入生成独立、不可编辑的内容草稿。需要修正时重新导入，不覆盖审核中的字节；被退回的旧记录与意见保留。提交后管理员针对当前摘要审核；发布创建正式版本。同资源同版本只能成功发布一次。

首个正式版本自动成为推荐版本；以后发布不会悄悄切换推荐。管理员显式调整推荐版本，但第一版不会因此升级任何已安装实例。下架保留全部版本与审核记录，不等同于执行停用。

## 6. 现场配置与运行环境

业务系统地址是技能访问的现场 API 根地址，与市场地址、模型地址无关；纯提示词技能不需要强制声明。地址和非敏感参数在安装阶段由现场提供；密码、Token 和实际账号通过平台凭据授权，不能打入包中。

脚本使用相对路径；Python/Node/命令行工具依赖列明版本，并准备离线运行环境，不能假定运行时能从公网安装。桌面脚本需要验证员工实际操作系统、架构、网络和可用依赖。当前企业桌面不加载外部 MCP，且禁用自动子专家加载，需要这些能力的资源应声明服务器执行或等待适配。

不要把 requirements.txt 放在包根目录：第一版的根文件白名单不接收它。可放在 scripts/ 中作为依赖说明，但系统不会自动安装。

## 7. 文件与内容校验

- 压缩包及展开文件总量均不超过 64 MiB，ZIP 条目不超过 2000；支持直接根文件或一层外包装目录。
- 文本 Markdown 和 JSON 使用 UTF-8。路径为规范相对路径，不含反斜杠、绝对路径、父目录跳转、盘符、重复路径或大小写冲突。
- 不接收符号链接、设备文件、加密 ZIP、隐藏目录及文件；macOS、Windows 和 Linux 文件管理器生成的已知压缩元数据会在导入时自动忽略（例如 `__MACOSX`、`._*`、`.DS_Store`、`Thumbs.db`、`desktop.ini`、`.directory` 和回收站目录）。
- 排除 USER.md、MEMORY.md、credentials.json、node_modules、__pycache__、.env、Git、个人数据、会话、检查点、日志和虚拟环境；文件白名单不能替代人工内容检查。
- 服务器自行计算每文件摘要与完整资源包摘要；导入与预览不运行脚本、不联网解析依赖。
- 文本预览上限 256 KiB；二进制文件不在线渲染。正式包下载前重新校验摘要。

开发者应自查源码和文档中是否仍有硬编码密码、生产数据、内网账号等；文件名校验无法识别任意正文中的秘密。

## 8. 推荐验收材料

在 README.md 或 tests/ 中列出脱敏输入与预期输出，至少覆盖正常、缺参、无数据、无权限和接口异常。写操作说明确认条件、幂等标识与重复提交处理。新版本列出与旧配置和旧会话的兼容性；不兼容变更提升主版本并说明处理方式。

## 9. 管理员体验步骤

1. 管理员登录，打开“资源市场”，点击“导入资源”。
2. 下载技能或专家模板；模板无需公网、模型或业务凭据。
3. 上传 ZIP，检查草稿的声明、文件清单和文本内容。
4. 点击“提交审核”，再由实际管理员“审核并发布”；需要退回时填写意见。
5. 在正式版本中查看、下载固定包；修改本地版本号和内容后，通过原资源导入新版本。
6. 确认旧版本摘要和下载内容仍保留，再按需设置推荐或软下架。

普通开发者需管理员显式授予“资源市场与提交”模块权限，仅维护自己资源的草稿。正式发布、推荐和上下架必须实际管理员操作。员工既有专家授权不受市场上架操作影响。


## 10. 存储与备份

版本元数据位于现有主数据库，ZIP 对象位于 MOSSWORK_HOME/resource-market/objects，以服务器计算的 SHA-256 命名。应用备份会包含该目录的已完成对象，不包含临时写入文件；恢复前核对每个归档对象摘要。使用数据库外部备份时必须协调保存该对象目录，不能仅备份 SQL 后期望下载文件自动恢复。

本版替换常规专家/技能界面的外部市场入口，未删除历史远程导入和 CLI 能力。整个部署完全断公网仍需单独验收模型、依赖、图标、登录验证和其他资源访问路径。

现有 /api/docs 使用 Scalar 公共 CDN 加载文档界面，受限网络下可能空白；市场 API 的 /api/openapi.json 类型定义可用。正式断网交付前需将文档静态依赖内置或保持该可选页面关闭。

## 专家市场创建流程与当前限制

1. 开发人员导入专家包并提交，管理员审核发布。资源需保持上架；首个发布版本自动推荐，后续版本由管理员选择推荐。
2. 在“专家 → 专家市场”查看已发布推荐版本，刷新可获取最新上架结果。使用者需要专家模块权限，无需资源发布管理权限。
3. 点击创建，在现有创建抽屉中配置名称、模型、存储和账号已授权的知识库/连接器。定义文件只读，显示版本与依赖；不会读取腾讯市场。
4. 创建按选中的 version_id 读取经过摘要验证的归档；递归安装精确技能依赖，推荐版本变化不替换已经选中的版本。依赖下架、内容损坏、版本冲突、循环或文件冲突均阻止安装。
5. 创建完成在“我的专家”使用。数据库记录 Agent 对应专家版本、依赖版本及摘要；写工作区失败时撤销创建，不留半成品 Agent。

当前创建器接受 execution_targets 包含 server、platforms 包含 any，且未声明 Python 版本、runtime_profile、required_capabilities 或必填 configuration 的通用资源。需要这些声明的资源可以提交和发布，但创建时会明确拒绝，不能把声明当成已准备好的环境。暂不在固定依赖之外叠加宿主技能包或编辑源定义；如需新定义应重新发布版本。

采用“仅使用分配专家”的员工不能自行创建；管理员先安装为来源专家，再通过现有部门/员工分配流程授权。技能既可随专家固定依赖安装，也可按以下独立流程安装。现有 Agent 自动升级及旧会话自动跟随新版仍未实现。


## 独立技能安装与挂载流程

1. 开发人员导入技能包并提交审核，管理员发布并保持资源上架。
2. 用户打开“技能包 → 资源市场”，或专家的“技能 → 资源市场”。使用者需要 `skill_packages` 权限，无需 `resource_market` 发布权限。
3. 点击“查看详情”，查看说明、文件预览、完整固定依赖和版本；可切换同一资源的历史正式版本。目录默认展示管理员推荐版本。管理市场的技能正式版本也可直接点击“安装到技能包”。
4. 输入新技能包名称并安装。服务器只从本地对象库读取指定 `version_id`，校验摘要及全部精确依赖，整体暂存成功后才公开技能包。失败清理暂存内容；重复名称返回冲突，不覆盖已有技能包。
5. 安装成功后，技能包页面自动选中新包；从专家技能页安装时自动切到“技能包”页签，开启对应包的挂载开关即可使用。挂载沿用现有 Agent 所有权与存储后端校验；不支持宿主技能包的后端需使用既有复制能力或更换支持的后端。
6. 每次安装生成独立版本快照，可以并存多个市场版本。升级时安装新版本，再通过现有挂载设置替换旧包；不要同时挂载含同名技能的多个版本。正式市场版本保持不可变，发布或推荐变化不会自动更新已安装包。

包根目录 `market-source.json` 保存最初安装的 resource_id、version_id、版本、对象摘要和完整依赖锁；技能包详情返回 `market_source` 并显示来源版本。技能包仍可按原有权限本地编辑，来源记录不代表编辑后内容仍等于原发布版本。备份技能包时保留整个包目录及该来源文件。

消费 API：`GET /api/skill-packages/market`、`GET /api/skill-packages/market/{version_id}`、`POST /api/skill-packages/market/{version_id}/install`，安装请求体为 `{ "name": "设备巡检 v1.1.0" }`。这些接口与安装过程不请求腾讯市场或公网、不执行包内脚本，不自动准备 Python/Node 环境或现场凭据。运行要求沿用专家固定版本安装器的通用服务器限制。

验证覆盖见 `tests/integration/test_local_skill_market.py` 与 `dashboard/src/components/ResourceMarketEntry.test.tsx`：普通用户消费权限、历史版本锁定、完整依赖、安装后挂载、名称冲突、下架、损坏内容、运行要求、失败清理及前端操作回调。
