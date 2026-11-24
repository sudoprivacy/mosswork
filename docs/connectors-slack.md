# Slack 连接器

在「连接器 → 内置连接器 → Slack」中，可以把个人 Slack 账号授权给 mosswork，通过远程 MCP 使用工作区工具。

1. 使用 HTTPS 地址打开 mosswork。在桌面客户端中，将服务器地址设置为同一个 HTTPS 入口。授权浏览器须能访问该地址，服务端须能通过 HTTPS 访问 Slack。
2. 打开 Slack 连接器，将页面显示的完整 OAuth 回调地址复制到 Slack App 的 **OAuth & Permissions → Redirect URLs**。使用工作区允许的内部应用或已在 Slack Marketplace 发布的应用。
3. 在连接器页面填写名称、Client ID 和 Client Secret，并至少填写一个用户权限。例如，测试读取表情可使用 `emoji:read`。在 Slack App 的 **OAuth & Permissions → User Token Scopes** 中添加相同权限，然后保存连接器。
4. 点击「前往授权」，在浏览器中登录 Slack 并确认授权。如果浏览器拦截了弹窗，点击「打开授权页」。回到 mosswork，页面显示「已连接 Slack」后，点击「探测」获取可用工具数量。
5. 在会话中选择该连接器，或在连接器配置中打开默认启用。每个用户分别授权自己的账号。

编辑时，Client Secret 留空会保留原值。变更 Client ID 时需要同时填写对应的 Secret。变更凭据或权限后请重新授权。服务端加密保存应用密钥和用户令牌，在 Slack 返回刷新令牌时按需续期。
