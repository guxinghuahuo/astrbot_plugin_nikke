# 发布指南

## GitHub

1. 检查 `metadata.yaml` 中的名称、版本、作者、仓库、平台和 AstrBot 兼容范围。
2. 更新 `CHANGELOG.md`、`docs/release-notes.md`、README 安装包名和加载日志版本。
3. 按 README 执行 Python 测试、Ruff 检查、Node 路由测试和打包。
4. 提交代码并推送到公开仓库，等待 GitHub Actions 检查通过。
5. 创建与 metadata 版本一致的标签，例如 `v1.0.0`，推送标签。Release 工作流再次检查并发布安装包及 SHA-256 附件。

`dist/` 是本地产物，不纳入新提交。`.gitattributes` 在 GitHub 源码归档中排除历史安装包和开发文件。首次发布到新仓库建议导入整理后的当前源码，避免公开私人开发历史中的旧配置及旧安装包；原 NAS 仓库可继续保留。

## AstrBot 插件市场

根据 [官方发布文档](https://docs.astrbot.app/dev/star/plugin-publish.html)，当前通过 [AstrBot Cloud](https://cloud.astrbot.app) 提交插件，需要登录 Cloud 账号。

GitHub 公开仓库和正式版本就绪后，在 Cloud 的插件发布页填写仓库地址，让系统读取根目录 `metadata.yaml`，确认展示名称、描述、作者、版本、平台、兼容范围和图标，再提交审核。仓库链接：

```text
https://github.com/guxinghuahuo/astrbot_plugin_nikke
```

安装包必须小于 16 MB。插件安装包和市场记录中的 name、author、version 应保持一致。展示名称可独立修改；沿用已有用户数据时应保留内部 name。

提交后检查审核状态，审核通过后再确认市场搜索和安装。GitHub Release 发布成功与插件市场审核通过是两个独立结果。
