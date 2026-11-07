<div align="center">

<img src="logo.png" alt="NIKKE 资讯助手" width="104">

# NIKKE 资讯助手

**把妮姬资讯、角色技能与珍藏品查询，带进你的群聊。**

[![Version](https://img.shields.io/badge/版本-v1.3.3-17675f?style=for-the-badge)](https://github.com/guxinghuahuo/astrbot_plugin_nikke/releases/tag/v1.3.3)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.10.4-182e48?style=for-the-badge)](https://github.com/AstrBotDevs/AstrBot)
[![OneBot](https://img.shields.io/badge/OneBot-v11-52b7a5?style=for-the-badge)](#环境与配置)
[![License](https://img.shields.io/badge/License-MIT-758394?style=for-the-badge)](LICENSE)

[效果预览](#效果预览) · [快速开始](#快速开始) · [查询指令](#查询指令) · [模型工具](#模型工具) · [配置文档](docs/configuration.md)

</div>

> 角色、技能和珍藏品查询使用国际服中文公开资料，无需游戏账号或 Cookie。基地与红球查询使用内置数据，无需联网。

## 效果预览

<table>
  <tr>
    <td align="center" width="50%" valign="top">
      <p><b>群聊资讯推送</b></p>
      <a href="docs/images/qq-preview.jpg"><img src="docs/images/qq-preview.jpg" alt="QQ 群活动提醒、国际服翻译与视频封面推送" width="360"></a>
      <p>活动倒计时 · 国际服翻译 · 图文动态</p>
    </td>
    <td align="center" width="50%" valign="top">
      <p><b>角色技能图片回复</b></p>
      <a href="docs/images/character-skills-preview.png"><img src="docs/images/character-skills-preview.png" alt="拉普拉斯7级技能、珍藏品阶段3的资料卡，页脚含作者及项目地址" width="360"></a>
      <p><code>/nikke 技能 拉普拉斯 7 珍藏品3</code></p>
    </td>
  </tr>
</table>

## 功能一览

| 功能 | 可以做什么 |
| --- | --- |
| 角色与技能 | 查询国际服角色属性、武器和1–10级技能，繁中转简体 |
| 珍藏品 | 按1–3阶段查询强化技能，区分已强化和仍为基础版本的技能 |
| 基地与红球 | 按基地等级或普通、困难通关进度，查询每小时资源产出 |
| 官方资讯 | 订阅 bilibili 国服及 BlaBlaLink 繁中、英、日、韩国际服动态 |
| 群聊图文 | 发送正文、原链接、逐张原图和视频封面，支持去重、重试及图片续传 |
| 翻译与筛选 | 可选国际服翻译、bilibili 大模型筛选及 Jev 前置判断 |
| 活动提醒 | 活动截止倒计时、分档提醒和夜间汇总 |
| 模型工具 | 让聊天模型按需查询角色技能、珍藏品及本群官方资讯存档 |

## 快速开始

### 1. 安装插件

在 AstrBot 插件市场搜索 **NIKKE 资讯助手**；也可在插件管理中选择「从链接安装」，填写：

```text
https://github.com/guxinghuahuo/astrbot_plugin_nikke
```

离线安装可从 [v1.3.3 发布页](https://github.com/guxinghuahuo/astrbot_plugin_nikke/releases/tag/v1.3.3) 下载 `astrbot_plugin_nikke-v1.3.3.zip`，在插件管理页上传。依赖由 AstrBot 安装。

### 2. 直接查询

安装后即可使用，无需配置 RSSHub 或模型：

```text
/nikke 角色 小红帽
/nikke 技能 拉毗：小红帽 7
/nikke 技能 拉普拉斯 珍藏品1
/nikke 技能 拉普拉斯 7 珍藏品3
```

技能默认10级、基础版本；末尾加 `珍藏品1`、`珍藏品2` 或 `珍藏品3` 可指定阶段。技能等级和珍藏品阶段分别计算，未解锁的技能沿用基础版本。名称有歧义时会列出候选，需补全名称。

### 3. 开启图片回复（可选）

先在 AstrBot 的文转图配置中填写本地 **t2i 服务地址**，再开启插件「基础配置 → 角色与技能图片回复」。

三个技能尽量合并为一张卡片，过长时自动分页；保留技能限制、珍藏品阶段和旧缓存提示，页脚显示作者及项目地址。该开关默认关闭，关闭时发送文字。渲染失败会返回简短提示。

<details>
<summary><b>还需要订阅资讯？展开配置步骤</b></summary>

1. 仅订阅国际服时，保持「国际服直连官方接口」开启，无需部署 RSSHub。
2. 订阅 bilibili 时，在「基础配置」填写可用的「RSSHub 地址」。关闭国际服直连后，国际服也使用此地址，需要部署对应路由，见 [RSSHub 部署指南](deploy/rsshub/README.md)。
3. 在「订阅及目标群」选择来源和语言，填写目标 QQ 群号。连接多个 OneBot 账号时，还需指定 QQ 平台 ID。
4. 管理员执行 `/nikke 检查 订阅名称` 确认读取正常，再在目标群执行 `/nikke 最新 订阅名称` 预览图文。
5. 开启自动推送。首次成功获取只建立基线，后续新动态才会自动发送。

也可由管理员在目标群或私聊中执行 `/nikke 订阅`，查看已配置的来源，再指定名称订阅。退订使用 `/nikke 退订 订阅名称`，修改会持久保存并即时生效。

容器中的 `localhost` 指向容器自身，访问其他服务时请填写容器服务名或宿主机地址。

</details>

## 查询指令

| 指令 | 用途 |
| --- | --- |
| `/nikke`、`/nikke 帮助` | 查看完整帮助 |
| `/nikke 角色 角色名` | 角色基础属性、武器与技能名称 |
| `/nikke 技能 角色名 [等级] [珍藏品1–3]` | 查询三个技能，等级1–10，默认10级基础版本 |
| `/nikke 基地等级 533` | 该等级满学院的信用点、战斗数据、芯尘每小时产出 |
| `/nikke 红球 h48-32` | 普通默认全通，困难已通关48-32时的红球产出 |
| `/nikke 红球 33-23 h18-32` | 指定普通与困难进度，参数顺序不限 |
| `/nikke 活动` | 查看国际服活动及截止时间 |
| `/nikke 最新 订阅名称` | 向当前群发送最新图文，仅对应订阅目标群成员可用 |

基地与红球固定回复简短文字，不使用 t2i。困难关卡须带 `h`（也接受 `H`），省略普通进度时按内置最后一关 **50-36** 计算。非标准写法和错误参数只提示正确指令。

<details>
<summary><b>AstrBot 管理员指令</b></summary>

| 指令 | 用途 |
| --- | --- |
| `/nikke 状态` | 查看订阅、投递队列与工具状态 |
| `/nikke 检查 订阅名称` | 检查来源及筛选结果 |
| `/nikke 订阅 [订阅名称]` | 列出来源，或订阅到当前群、私聊账号 |
| `/nikke 退订 [订阅名称]` | 列出当前订阅，或取消当前会话的订阅 |

订阅、退订只影响当前会话，保留其他目标的订阅。自动推送需另行开启总开关，活动提醒按其独立配置生效。

</details>

## 模型工具

在「聊天查询工具」中开启对应开关，并在 AstrBot 中允许使用工具。聊天模型及 Agent 执行器需支持工具调用。

| 工具 | 参数 | 可用范围 |
| --- | --- | --- |
| `nikke_query_character_skills` | 角色中文名、技能等级、珍藏品阶段 | 群聊和私聊均可，无需资讯订阅 |
| `nikke_query_official_posts` | 开始日期、结束日期 | 开启工具的订阅目标 QQ 群，只查询本群来源的本地存档 |

技能工具可回答“拉普拉斯的7级技能是什么？按珍藏品阶段3查询”，对应调用：

```python
nikke_query_character_skills(
    character_name="拉普拉斯", skill_level=7, treasure_phase=3
)
```

`treasure_phase=0`（默认）表示基础技能，`1–3` 表示珍藏品阶段；技能等级默认10。工具与指令共用国际服资料及缓存，将文字结果交给模型组织回答，不直接发送图片。

资讯工具支持明确日期以及“今天”“昨天”“N天前”，可用来回答“昨天国际服发布了什么公告？”。结果仅覆盖已抓取的存档。普通聊天不会自动附加动态或技能正文。

## 环境与配置

| 项目 | 要求 |
| --- | --- |
| AstrBot | `>=4.10.4` |
| 消息平台 | OneBot v11（`aiocqhttp`），动态订阅支持 QQ 群及私聊 |
| RSSHub | bilibili 动态及其配图回退需要；国际服默认直连 |
| 模型 | 翻译、筛选、配图匹配和聊天工具按需使用 AstrBot 配置的模型 |
| t2i | 可选，角色与技能图片回复复用 AstrBot 配置的服务 |

自动推送、AI 筛选、翻译、活动提醒和聊天查询工具默认关闭，按需开启。

| 文档 | 内容 |
| --- | --- |
| [配置与使用](docs/configuration.md) | 配置项、翻译策略、权限、查询规则和排错说明 |
| [RSSHub 部署](deploy/rsshub/README.md) | bilibili 与可选国际服路由的自建部署 |
| [配置样例](examples/nas-config.json) | 可参考的配置结构，使用前替换地址和群号 |
| [更新日志](CHANGELOG.md) | 各版本功能与修复记录 |

## 数据与升级

角色、技能和珍藏品使用 **BlaBlaLink 官方繁中 CDN**，本地转换为简体，保留国际服术语。资料按需下载并缓存24小时；刷新失败时标注使用旧缓存。文字回复和工具结果保留来源与校验时间，图片页脚显示项目署名。

基地与红球使用内置国际服静态表，覆盖基地1–800级、普通2-12至50-36、困难h0-1至h50-36，精度和来源见 [数据说明](nikke/data/README.md)。

升级沿用已有配置及 `data/plugin_data/astrbot_plugin_nikke/` 数据目录，建议先备份，更新后重载插件。RSS 只提供近期条目，长期停机可能遗漏动态；平台发送成功但投递记录尚未保存时退出，重启后可能重复发送。

## 开发与发布

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q --basetemp=.pytest-tmp
python -m ruff check .
node --test tests/test_blablalink_route.mjs
python scripts/build_package.py
```

测试使用本地服务和 AstrBot 替身。打包生成 ZIP 与 SHA-256 文件，检查16 MB安装包限制。发布流程见 [发布指南](docs/releasing.md)，自动检查见 [GitHub Actions](https://github.com/guxinghuahuo/astrbot_plugin_nikke/actions)。

## 支持与致谢

**项目作者：@孤行花火**

喜欢本插件的话，请为本项目添加一个Star吧OwO

[提交问题](https://github.com/guxinghuahuo/astrbot_plugin_nikke/issues) · [参与贡献](https://github.com/guxinghuahuo/astrbot_plugin_nikke/pulls) · [GitHub 项目](https://github.com/guxinghuahuo/astrbot_plugin_nikke)

感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot)、[RSSHub](https://github.com/DIYgod/RSSHub) 和 [OpenCC](https://github.com/BYVoid/OpenCC)。README 排版参考了 [群聊日常分析插件](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)。

代码采用 [MIT](LICENSE) 许可证；内置 OpenCC 实现与词典遵循其 [许可证](nikke/_vendor/opencc/LICENSE.txt) 和 [NOTICE](nikke/_vendor/opencc/NOTICE.txt)。NIKKE 商标、官方内容及图像归各自权利人所有，本项目为社区工具。
