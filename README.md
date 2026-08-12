# AstrBot YouTube 频道更新推送

通过 YouTube 官方公开 RSS（无需 API Key）订阅频道，检测新视频后推送到绑定会话。

## 功能

- 订阅 / 取消订阅频道（支持 `@handle`、频道 ID、频道链接）
- 后台定时轮询，有更新主动推送
- 手动查看最新视频
- 数据持久化（重启不丢订阅）

## 安装

1. 将本目录复制到 `AstrBot/data/plugins/astrbot_plugin_youtube_notify/`
2. 在 WebUI → 插件 中重载 / 启用
3. （可选）在插件配置中调整轮询间隔、是否附带封面等

若服务器访问 YouTube 困难，请在插件配置里填写 HTTP 代理。

## 指令

| 指令 | 说明 |
|------|------|
| `/yt 订阅 <频道>` | 订阅频道到当前会话 |
| `/yt 取消 <频道>` | 取消订阅 |
| `/yt 列表` | 查看本会话订阅列表 |
| `/yt 最新 <频道>` | 手动拉取该频道最新视频 |
| `/yt 检查` | 立即检查本会话所有订阅是否有更新 |
| `/yt 帮助` | 帮助 |

`<频道>` 示例：

- `@MrBeast`
- `UCX6OQ3DkcsbYNE6H8uQQuVA`
- `https://www.youtube.com/@MrBeast`
- `https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA`

## 原理

使用：

```text
https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID
```

每个会话独立订阅；首次订阅会记录当前最新视频 ID，之后只推送「之后」出现的新视频。

## 注意

- RSS 一般只包含最近约 15 条视频
- 部分平台可能不支持机器人主动发消息
- 请合理设置轮询间隔，避免请求过频
