# NIKKE 资讯助手 v1.0.0

首个正式版本，支持 bilibili 国服与 BlaBlaLink 四种语言国际服官方动态、QQ 群图文推送、国际服翻译、AI 内容筛选、活动提醒及聊天资讯查询。

展示名由「NIKKE 官方动态」改为「NIKKE 资讯助手」，内部标识继续使用 `astrbot_plugin_nikke`，沿用原有配置及 SQLite 数据。升级前备份配置与数据，安装后重载插件；RSSHub 私有路由更新后需重启 RSSHub。

要求 AstrBot `>=4.10.4`、OneBot v11 和可访问的 RSSHub。国际服需部署附带的 BlaBlaLink 路由。新安装请填写自己的 RSSHub 地址及目标群。

下载 ZIP 后通过 AstrBot 插件管理页上传，或从仓库链接安装。校验值见随附的 `.sha256` 文件。详细安装步骤及效果截图见 README。
