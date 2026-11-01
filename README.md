# NIKKE 资讯助手

为 AstrBot 提供《胜利女神：NIKKE》官方资讯订阅、QQ 群图文推送、国际服翻译、活动提醒，以及无需登录的角色与技能查询。

## 功能

- **官方动态**：通过 RSSHub 订阅 bilibili 国服和 BlaBlaLink 繁中、英文、日文、韩文国际服动态。
- **图文推送**：正文、来源链接、逐张原图与视频封面；SQLite 去重、失败重试和图片续传。
- **国际服翻译**：英日韩动态翻译为简体中文，参考同帖繁中术语和配图，可选匹配 bilibili 配图。
- **内容筛选**：bilibili 可选大模型筛选抽奖和引流内容，支持 Jev 前置判断。
- **活动提醒**：国际服活动倒计时、提醒档位和夜间汇总。
- **聊天查询**：让 AstrBot 按需查询本群订阅来源的本地资讯存档。
- **角色与技能**：查询 BlaBlaLink 国际服公开角色资料及 1–10 级基础技能，繁中转简体。

## 效果展示

QQ 群内的活动到期提醒、国际服动态中文翻译与视频封面推送：

<img src="docs/images/qq-preview.jpg" alt="QQ 群推送效果：活动到期提醒、韩文官方动态中文翻译与视频封面" width="360">

<details>
<summary>角色技能图片回复示例</summary>

<img src="docs/images/character-skills-preview.png" alt="拉毗：小红帽的三个10级基础技能合并为一张资料卡，附来源和资料校验时间" width="360">

</details>

## 环境要求

| 项目 | 要求 |
| --- | --- |
| AstrBot | `>=4.10.4` |
| 消息平台 | OneBot v11（`aiocqhttp`）QQ 群 |
| RSSHub | 资讯订阅需要；国际服需要部署附带的私有路由 |
| 模型 | 翻译、筛选、配图匹配和聊天查询按需使用 AstrBot 已配置的模型 |
| 文转图服务 | 可选；角色图片回复复用 AstrBot 已配置的 t2i 服务 |

自动推送、AI 筛选、翻译和活动提醒初始关闭，按需配置。RSSHub 部署见 [部署指南](deploy/rsshub/README.md)。

## 安装

在 AstrBot 插件管理中选择「从链接安装」，填写：

```text
https://github.com/guxinghuahuo/astrbot_plugin_nikke
```

也可从 [v1.1.0 发布页](https://github.com/guxinghuahuo/astrbot_plugin_nikke/releases/tag/v1.1.0) 下载 `astrbot_plugin_nikke-v1.1.0.zip`，通过插件管理页上传。依赖由 AstrBot 安装。

## 首次使用

仅使用角色和技能查询，安装后可直接执行 `/nikke 角色 小红帽` 或 `/nikke 技能 小红帽`，无需配置账号、Cookie、RSSHub 或模型。
需要图片回复时，先在 AstrBot 的文转图配置中填写本地 t2i 地址，再开启插件「基础配置 → 角色与技能图片回复」。该开关默认关闭。

资讯订阅和自动推送按以下步骤配置：

1. 部署 RSSHub；订阅国际服时，先启用 [BlaBlaLink 路由](deploy/rsshub/README.md#国际服路由)。
2. 在「NIKKE 资讯助手 → 基础配置」填写 AstrBot 能访问的 RSSHub 地址，例如同一 Docker 网络中的 `http://rsshub:1200`。
3. 在「订阅及目标群」选择来源和语言，填写目标 QQ 群号。连接多个 OneBot 账号时，还需填写 QQ 平台 ID。
4. 管理员执行 `/nikke 检查 NIKKE官方` 确认读取正常；在目标群执行 `/nikke 最新 NIKKE官方` 预览一条真实图文。
5. 开启自动推送。首次成功获取建立基线，之后的新动态才会自动发送。

容器中的 `localhost` 指向容器自身，请根据网络填写服务名或宿主机地址。国际服订阅使用自己设置的名称，例如 `NIKKE国际服`。

## 指令

| 指令 | 用途 | 权限 |
| --- | --- | --- |
| `/nikke`、`/nikke 帮助` | 查看帮助 | 所有人 |
| `/nikke 状态` | 订阅和待投递状态 | AstrBot 管理员 |
| `/nikke 检查 订阅名称` | 检查来源和筛选结果 | AstrBot 管理员 |
| `/nikke 最新 订阅名称` | 向当前群发送最新图文 | 对应订阅目标群成员 |
| `/nikke 活动` | 查看活动 | 所有人 |
| `/nikke 角色 角色名` | 国际服角色基础属性、武器与技能名称 | 所有人 |
| `/nikke 技能 角色名 [等级]` | 三个基础技能，等级 1–10，默认 10 | 所有人 |

详细选项、翻译策略、筛选规则和投递行为见 [配置与使用](docs/configuration.md)。

角色查询使用 **BlaBlaLink 官方繁中 CDN**，本地转换为简体，保留国际服术语。
支持简繁输入和部分名称匹配；匹配到多个角色时列出候选，需改用完整名称。
例如 `/nikke 技能 拉毗：小红帽 7` 显示 7 级技能，省略等级时显示 10 级。
当前显示基础技能，不包含珍藏品强化。资料按需下载并缓存 24 小时，刷新失败时标注使用旧缓存。

开启「角色与技能图片回复」后，三个技能尽量合并为一张卡片，过长时自动分页，保留来源与资料校验时间。
渲染失败时只返回简短提示；关闭开关后沿用文字回复。名称歧义和参数错误始终用简短文字提示。

## 升级与数据

v1.1.0 增加角色、技能查询及可选图片回复，图片开关默认关闭，升级后按需开启。
内部标识仍为 `astrbot_plugin_nikke`，沿用已有配置和 `data/plugin_data/astrbot_plugin_nikke/` 数据。升级前备份配置和数据，升级后重载插件。

RSS 只提供近期内容，长时间停机可能遗漏动态。平台确认发送成功、尚未写入本地记录时进程退出，重启后可能重复发送。国际服目前支持 BlaBlaLink，X 尚未接入；视频以封面和链接呈现。

## 开发与发布

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q --basetemp=.pytest-tmp
python -m ruff check .
node --test tests/test_blablalink_route.mjs
python scripts/build_package.py
```

测试采用本地服务和 AstrBot 替身。实际部署后仍需检查插件加载、RSS 连通性和目标群图文展示。

打包脚本读取 `metadata.yaml` 的版本，生成安装包和 SHA-256 文件，检查市场的 16 MB 限制。自动检查见 [GitHub Actions](.github/workflows/ci.yml)，发布见 [发布指南](docs/releasing.md)，变更见 [CHANGELOG](CHANGELOG.md)。[配置样例](examples/nas-config.json)中的地址和群号请自行替换。

## 许可证

项目代码采用 [MIT](LICENSE) 许可证。内置 OpenCC 实现与词典遵循 [Apache-2.0](nikke/_vendor/opencc/LICENSE.txt) 和 [NOTICE](nikke/_vendor/opencc/NOTICE.txt)。NIKKE 商标、官方内容及相关图像归各自权利人所有。本项目为社区工具。
