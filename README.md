# Cosplay 随机预览 Telegram 机器人

发送 `/start` 只显示成人内容与年龄确认提示，不显示菜单按钮。发送 `/confirm18` 后发送 `/menu`：私聊显示输入框键盘；群聊显示消息下方的内联菜单，均含 `Cosplay`、`NudeCosplay` 和 `EroCosplay` 三个分类。

机器人会从 [cosplaytele.com](https://cosplaytele.com/) 随机选择一篇公开作品，并按页面顺序每批发送最多 10 张预览图。若还有后续图片，私聊的“继续提取这一套”会显示在输入框键盘中，群聊则显示在批次提示下方；整套发完后按钮自动隐藏。如果续提提示未成功发送，可用 `/menu` 重显按钮或用 `/more` 直接继续。图片消息不显示原作品页链接。

- `Cosplay`、`NudeCosplay`、`EroCosplay`：按对应站点分类随机抽取一篇作品并发送预览图。
- `/hide`：隐藏输入框下方的键盘；`/menu`：重新显示分类按钮，以及尚未完成套图的续提按钮。

## 重要说明

- 网站含成人向内容。私聊仅供年满 18 岁用户使用；群聊必须确保所有成员均为成年人（18+），每位触发者都必须自行发送 `/confirm18`。群内发送的图片对全体成员可见；图片会带 Telegram spoiler 遮罩。
- 机器人仅抽取当前作品图库中的公开预览图，会排除页面的“Recommend For You”相关推荐图片；不访问登录后内容、不抓取付费/外部下载包，并遵守 `robots.txt` 的公开页面规则。
- 每批最多发送 10 张（Telegram 相册限制）。续提按钮只对抽取该套的用户有效，30 分钟后失效；机器人重启后未完成的续提会话也会失效。请先确认你有权保存或转发相关图片，并遵守网站条款、当地法律和作者权利。
- 目标网站页面结构或访问政策变化时，抓取可能失效。遇到 HTTP 403/429 时会停止这次请求，不做绕过。
- 机器人令牌仅保存在本地 `.env`，不要提交到 Git、发到群聊或贴到聊天中。

## 准备 Telegram Bot

1. 在 Telegram 中打开官方 `@BotFather`，使用 `/newbot` 创建机器人并取得 token。
2. 在本目录复制配置样例：

   ```bash
   cp .env.example .env
   ```

3. 编辑 `.env`，填写 `TELEGRAM_BOT_TOKEN`。不要把 token 发给任何人。
4. 安装依赖并启动：

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   python bot.py
   ```

5. 打开机器人私聊或将其添加到仅限成年人的群组。发送 `/start` 阅读提示；每位使用者确认年满 18 岁后发送 `/confirm18`，再发送 `/menu` 显示菜单。私聊用 `/hide` 收起键盘；群聊菜单使用消息下方的内联按钮，不需要关闭 Telegram 隐私模式。

## 配置项

| 变量 | 默认值 | 用途 |
|---|---:|---|
| `TELEGRAM_BOT_TOKEN` | 必填 | BotFather 创建的机器人令牌 |
| `MAX_IMAGES` | `10` | 每套最多发送图片数，硬上限 10 |
| `USER_COOLDOWN_SECONDS` | `45` | 同一用户的请求间隔秒数 |
| `BOT_DB_PATH` | `bot.sqlite3` | 保存 18+ 确认的 SQLite 文件路径 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 让机器人持续运行

这个项目使用 Telegram long polling。运行它的电脑/服务器需要保持开机和联网；关闭终端或休眠会停止机器人。测试可直接前台运行。长期运行建议部署到你已有的常开 Linux 主机或支持常驻进程的托管环境，并用 systemd/supervisor 管理进程；不要把机器人令牌写入公开仓库。

如果使用 CT8.PL，请按 [CT8 部署指南](DEPLOY_CT8.md) 配置 SSH、Python 虚拟环境、screen 和 `@reboot` Cron。

## 常见问题

- `请先设置 TELEGRAM_BOT_TOKEN`：检查 `.env` 是否在项目目录、变量名是否拼对。
- 图片获取失败：目标站点可能变更、暂时限流或不允许抓取；等待后重试，不要绕过访问限制。
- Telegram 拒绝图片：机器人会跳过单张异常图片；如果全部失败，稍后再试。
- Telegram 发送相册超时：Telegram 可能已经收到该批图片。机器人会停止这套图片的续提，不提供同批重发按钮；请先检查聊天记录，避免重复发送。
- “继续提取这一套”提示过期：重新发送 `cos` 或点击分类按钮抽取一套新图片。
