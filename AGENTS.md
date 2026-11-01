# AstrBot 插件开发约定

本项目遵循 AstrBot 官方插件规范。涉及框架接口时，先核对官方文档或源码，并确认兼容 metadata.yaml 声明的最低版本。

- 日志统一使用 `from astrbot.api import logger`。独立业务模块通过构造参数接收入口传入的 logger，不导入 Python 内置 logging；测试替身除外。Ruff 的 TID251 规则会阻止违规导入。
- 网络请求使用异步客户端；SQLite 和其他可能阻塞事件循环的操作按需交给工作线程。
- 持久化数据通过 `StarTools.get_data_dir` 定位，不向插件源码目录写入运行数据，不直接操作 AstrBot 的内部数据库。
- 使用官方指令、权限、消息发送、模型和工具注册接口；不直接改写系统提示词、会话历史或共享工具集合。
- 在 `initialize` / `terminate` 中正确启动和释放任务、网络会话和存储资源；保留取消语义，避免重载后残留任务或句柄。
- 修复故障时补充能覆盖实际失败场景的回归测试；测试中的 AstrBot 替身应体现受影响接口的真实行为。

发布前执行 README 列出的 Python 测试、Ruff 检查、Node 路由测试和打包检查。确认 metadata、加载日志、README、发布说明、安装包名称及 Git 标签的版本一致。推送后检查 GitHub Actions 和 Release 附件，不将发布成功表述为插件市场审核通过。
