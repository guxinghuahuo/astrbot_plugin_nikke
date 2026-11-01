# RSSHub 部署指南

本目录提供独立 Docker Compose 项目 `nikke-rsshub`，包含 RSSHub、Redis 和 Chromium。Redis、Chromium 仅供项目内部使用。RSSHub 镜像固定到支持私有路由的 digest，镜像可通过环境变量替换。

## 准备配置

将本目录复制到服务器的持久化目录，进入该目录，执行：

```sh
cp -n .env.example .env
cp -n rsshub.env.example rsshub.env
chmod 600 .env rsshub.env
openssl rand -hex 32
```

将生成的随机值填入 `.env` 的 `BROWSERLESS_TOKEN`。`.env` 控制 Compose 插值，`rsshub.env` 保存来源网站 Cookie。已有配置请保留并按需补充。

默认 `NAS_BIND_IP=127.0.0.1`，仅宿主机可访问。AstrBot 位于其他容器或设备时，改为服务器的局域网接口地址，并在防火墙中允许 AstrBot 访问 1200 端口。也可将两个容器接入同一 Docker 网络，通过服务名访问。插件中的 RSSHub 地址需要与实际网络一致。

## 启动与检查

```sh
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
curl --fail --max-time 15 http://127.0.0.1:1200/healthz
```

镜像使用 DaoCloud 镜像地址，可在 `.env` 替换为可访问的上游地址。依据：[RSSHub 官方 Compose](https://github.com/DIYgod/RSSHub/blob/master/docker-compose.yml)。

## bilibili 动态

在 `rsshub.env` 按模板填写自己的 bilibili Cookie，然后重建 RSSHub：

```sh
docker compose up -d --no-deps --force-recreate rsshub
curl --fail --max-time 120 'http://127.0.0.1:1200/bilibili/user/dynamic/3546734206650905/showEmoji=0&embed=0'
```

Cookie 过期或平台风控会导致读取失败。Cookie 留在 RSSHub 服务端，插件无需填写它。参见 [RSSHub bilibili 路由](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/bilibili/dynamic.ts)。

## 国际服路由

Compose 已挂载 `routes-user/nikke-blablalink.mjs` 并设置 `USER_ROUTES_PATH`。路由固定读取 BlaBlaLink 国际服官方账号，支持 `zh-TW`、`en`、`ja`、`ko`，无需 Cookie。

```sh
docker compose exec -T rsshub node /app/nikke-checks/check_blablalink.mjs --capabilities
docker compose exec -T rsshub node /app/nikke-checks/check_blablalink.mjs
curl --fail --max-time 120 http://127.0.0.1:1200/nikke-blablalink/official/zh-TW
```

将路径结尾换为 `en`、`ja`、`ko` 可检查其他语言。路由目录只放路由模块，自检脚本位于单独目录。固定镜像信息见 [.env.example](.env.example)。路由读取近期一页，保留官方原文、长帖子 ID、原图参数、视频封面和详情链接。更新路由文件后执行 `docker compose restart rsshub`，缓存最多约 300 秒后更新。

## AstrBot 连通性与图片

在 AstrBot 容器内执行，将 `http://rsshub:1200` 替换为实际地址：

```sh
python /AstrBot/data/plugins/astrbot_plugin_nikke/scripts/check_international.py --rsshub http://rsshub:1200
```

成功输出包含每种语言的条数和完整图片字节数，脚本不发送群消息。`--skip-images` 只检查 RSS。

RSS 成功但图片失败时，在插件的「国际服图片代理」填写容器可访问的 HTTP/Clash mixed 代理。支持 URL 内账号密码认证；官方图片、公开角色资料和 YouTube 缩略图使用它，RSSHub、bilibili 和日历直连。可读取已保存配置检查：

```sh
python /AstrBot/data/plugins/astrbot_plugin_nikke/scripts/check_international.py --rsshub http://rsshub:1200 --config /AstrBot/data/config/astrbot_plugin_nikke_config.json
python /AstrBot/data/plugins/astrbot_plugin_nikke/scripts/diagnose_image_proxy.py --rsshub http://rsshub:1200 --config /AstrBot/data/config/astrbot_plugin_nikke_config.json
```

第二个脚本报告 RSS、代理连接、CONNECT、TLS 和完整下载阶段。代理主机与端口会显示在摘要中，凭据会隐藏。

## 故障与更新

- RSS 404：检查镜像、挂载目录、`USER_ROUTES_PATH` 和启动日志。
- bilibili 请求失败：检查 Cookie、登录状态、风控和 RSSHub 出站网络。
- 国际服 RSS 失败：执行容器内官方 API 自检，检查到 BlaBlaLink 的网络。
- `failed_stage=original_image`：检查 AstrBot 到图片 CDN 的网络和图片代理。

```sh
docker compose logs --tail 100 rsshub browserless redis
```

更新前备份配置并记录镜像 digest，更新后重新检查健康状态、RSS 和 AstrBot 内连通性。Redis 保存缓存，投递记录位于 AstrBot 数据目录。分享日志前移除 Cookie、Token、群号等信息。
