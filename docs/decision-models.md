# 统一判断模型

在 AI 内容筛选中同时开启总开关与判断模型预筛选。下拉选择 jev、gemini、astrbot 或 custom。Jev、Gemini、custom 共用 API Key、API Base、模型名称；切换服务时更新这三个值。旧字段隐藏保留，首次按原服务迁移，清空新 Token 后不会从旧配置恢复。

- jev：地址留空使用 TypeSafe，模型留空使用 jev-latest；支持 /v1 Base 或完整接口地址。
- gemini：地址留空使用 Google v1beta/decisions，模型留空使用 gemini-decision-1。
- astrbot：要求 **AstrBot v4.29.4 及以上**。先在 AstrBot 中配置判断模型，再填写其提供商 ID（留空为默认判断模型），不需要插件凭据。Jev、Gemini、custom 直连方式仍兼容 AstrBot v4.10.4 及以上。
- custom：API Base 必填，填写实际 Key 和模型名称，选择 systemone 或 gemini 兼容协议。不支持任意 JSON 请求及响应格式。

## 筛选流程

bilibili 沿用抽奖规则，高置信度正常动态直接放行，其他交给大模型。国际服使用独立线上/线下规则：高置信度 normal 保留、高置信度 offline 过滤，低置信度、uncertain 或请求失败交给大模型。默认阈值0.8，保留用户旧阈值。大模型仍无法确认时按 other 保留，避免误删。

线下包括 cosplay 见面会、漫展、秋叶原门店联动、快闪店、实体演出及回顾。线上游戏更新、技能、维护、兑换码、游戏内联动、线上比赛及在线观看的直播保留；混合帖子存在独立的线上价值时保留，不按关键词机械拦截。网上发布线下活动照片不因此变成线上服务。

筛选只提交文字，在翻译、图片下载及发送前执行；手动检查、预览与自动投递共用入口。已开始发送的图片续传不重新筛选。存档和活动日历不受影响。「提示词模板」按 bilibili 和国际服各提供大模型提示词、分类动作、判断模型提示词及判断条件。四种接入方式共用对应来源的判断模板；旧 Jev 自定义内容保留。修改某个来源的大模型提示词或分类动作后，该来源暂停本地捷径及判断模型预筛选，直接交给大模型，避免绕过自定义规则。国际服默认判断标识为 normal/offline/uncertain，bilibili 为 normal/lottery/uncertain；可编辑说明，改变过滤动作请编辑大模型分类条件。

关闭判断模型开关时直接用大模型，关闭 AI 总开关时跳过全部筛选。直连缓存按后端、协议、地址、模型、规则和动态隔离，框架模式不用插件判断缓存。保留超时、失败冷却、凭据脱敏、不跟随重定向和取消语义。

版本与接口依据：[AstrBot v4.29.4 发布说明](https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.29.4)（首次正式加入判断模型支持）、[v4.29.4 DecisionService](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/provider/decision.py)、[Google Decisions API](https://aistudio.google.com/docs/decisions-api)。
