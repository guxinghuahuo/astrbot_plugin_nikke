# NIKKE 资讯助手 v1.4.0

判断模型预筛选支持统一下拉选择：旧版 Jev 直连、AstrBot 已配置判断模型、Google Gemini Decision 及 custom 兼容接口。

旧配置无需迁移，默认保持 Jev；API Base 可自定义。框架模式复用 AstrBot 模型凭据，可指定提供商 ID 或使用默认判断模型。Jev/Gemini/custom 共用 API Key、地址和模型名称；旧字段自动迁移。

只提交文字。正常且高置信度直接放行，其余交给原有大模型；国际服高置信度线下活动过滤，低置信度交给大模型，保留有实质线上内容的混合帖子。Jev、Gemini、custom 直连方式兼容 AstrBot v4.10.4 及以上；astrbot 框架模式要求 AstrBot v4.29.4 及以上，并预先配置判断模型。

提示词模板按 bilibili 和国际服分别提供大模型及判断模型配置，适用于全部判断模型接入方式。旧自定义内容保留；修改大模型规则后，对应来源直接交给大模型，避免预筛选绕过自定义条件。

安装后重载插件，详见 [配置说明](decision-models.md)。安装包为 `astrbot_plugin_nikke-v1.4.0.zip`，并提供 SHA-256 校验文件。
