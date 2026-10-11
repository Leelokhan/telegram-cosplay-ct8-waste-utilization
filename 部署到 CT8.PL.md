# 部署到 CT8.PL

以下按 **CT8.PL**（`ct8.pl`）说明。这个机器人使用 Telegram **long polling**，会主动连接 Telegram 获取消息，所以不需要为机器人开放入站端口，也不需要创建 Python 网站或配置 webhook。CT8.PL 当前提供 SSH、Python、Cron 和运行自有程序的能力；免费套餐页面列出的上限包括 20 个系统进程和 512 MB 内存。[1]

> CT8.PL 是共享主机，不是专用的 24/7 应用进程托管平台。CT8 论坛有用 `screen` 和 `@reboot` 启动 Python 脚本的示例，但这不能保证共享主机永不停止进程。建议先测试；如果服务商回收后台进程或无法安装依赖，就需要换用明确支持常驻 Worker 的 VPS/托管环境。[5]

## 1. 上传项目并登录

先从本对话下载最新的 `cosplay-telegram-bot.zip`。在本地电脑终端运行下面的命令，把 `LOGIN` 和 `SSH_HOST` 替换成 CT8 开通邮件/面板里的登录名和服务器地址：

```bash
scp -P 22 cosplay-telegram-bot.zip LOGIN@SSH_HOST:~/
ssh -p 22 LOGIN@SSH_HOST
```

CT8/MyDevil 的 SSH 文档说明，登录名和服务器地址应使用开通账户时邮件提供的信息。[4]

## 2. 启用自有程序权限

登录后运行：

```sh
devil binexec on
```

然后退出 SSH 并重新登录：

```sh
exit
ssh -p 22 LOGIN@SSH_HOST
```

也可以在 CT8 面板中开启 Binexec。官方说明要求开启后重新建立 SSH 会话，之后才能运行自有程序。[3]

## 3. 解压并安装依赖

```sh
mkdir -p "$HOME/apps"
cd "$HOME/apps"
unzip "$HOME/cosplay-telegram-bot.zip"
cd "$HOME/apps/cosplay-telegram-bot"

python3.11 --version
virtualenv -p "$(command -v python3.11)" .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

CT8 所依托的 MyDevil Python 文档列有 Python 3.11/3.12，并建议用 `virtualenv` 把项目依赖装在用户目录中。[2] 如果 `virtualenv` 命令不可用，可尝试：

```sh
python3.11 -m venv .venv
```

如果安装 Pillow 等依赖时因为进程配额导致编译失败，再单独尝试限制并行度后重装：

```sh
export MAX_CONCURRENCY=1 CPUCOUNT=1 MAKEFLAGS="-j1"
.venv/bin/pip install -r requirements.txt
```

这是 MyDevil Python 文档提供的进程配额问题处理建议。[2]

## 4. 安全设置 BotFather 令牌

在项目目录中：

```sh
cp .env.example .env
nano .env
```

在文件里只填入从 Telegram 官方 `@BotFather` 获取的 token：

```dotenv
TELEGRAM_BOT_TOKEN=你的BotFather令牌
```

保存后限制文件权限：

```sh
chmod 600 .env
```

不要把 token 放进公开 Git 仓库，也不要把它写在 Cron 命令里。

## 5. 前台测试

```sh
cd "$HOME/apps/cosplay-telegram-bot"
.venv/bin/python bot.py
```

看到机器人启动日志后，在 Telegram 私聊或仅限成年人的群组中发送 `/start` 测试。群组内每位使用者都必须自行发送 `/confirm18`；发送 `/menu` 后通过消息下方的内联按钮选择分类。完成后按 `Ctrl+C` 停止前台进程。

## 6. 用 screen 后台运行

先检查 CT8 是否提供 `screen`：

```sh
command -v screen
```

如果命令有输出，在 SSH 中运行：

```sh
cd "$HOME/apps/cosplay-telegram-bot"
screen -dmS cosplaybot sh -c 'cd "$HOME/apps/cosplay-telegram-bot" && exec .venv/bin/python -u bot.py >> bot.log 2>&1'
screen -ls
tail -n 50 "$HOME/apps/cosplay-telegram-bot/bot.log"
```

查看日志可用 `tail -f "$HOME/apps/cosplay-telegram-bot/bot.log"`，按 `Ctrl+C` 退出日志查看，不会停止机器人。停止机器人：

```sh
screen -S cosplaybot -X quit
```

CT8 论坛的 Python/Cron 讨论中给出了 `screen -dmS ...` 配合 Python 虚拟环境的启动示例。[5]

## 7. 设置主机重启后启动

CT8 官方 Cron 文档支持 `@reboot`，它会在系统启动时运行一次命令。[6] 先查出实际主目录：

```sh
echo "$HOME"
command -v screen
```

然后用 `crontab -e` 编辑定时任务，新增一行。请将路径替换为上一步显示的实际主目录；若 `screen` 不在 `/usr/local/bin/screen`，也要替换成 `command -v screen` 的结果：

```cron
@reboot /usr/local/bin/screen -dmS cosplaybot sh -c 'cd /usr/home/LOGIN/apps/cosplay-telegram-bot && exec .venv/bin/python -u bot.py >> bot.log 2>&1'
```

查看 Cron 是否保存成功：

```sh
crontab -l
```

不要用 `* * * * *` 这种每分钟启动一次的规则；这个机器人本身是长时间运行的轮询进程，重复启动会造成多个实例互相冲突。CT8 官方 Cron 文档也列有 `@reboot` 写法；论坛示例同样使用 `@reboot` 启动 `screen`。[5] [6]

## 常用维护命令

```sh
# 查看 screen 会话
screen -ls

# 查看最近日志
tail -n 100 "$HOME/apps/cosplay-telegram-bot/bot.log"

# 重启：先停止旧会话，再执行前面的 screen 启动命令
screen -S cosplaybot -X quit
```

首次部署最重要的是先完成前台测试。如果依赖安装、`screen` 或 `@reboot` 在你的 CT8 账户上报错，把报错内容贴出来即可继续排查；不要把 BotFather token 一起贴出来。

## 参考资料

[1]: https://www.ct8.pl/offer "CT8.PL 官方套餐说明"
[2]: https://pomoc.mydevil.net/Python/ "MyDevil 官方 Python 文档"
[3]: https://pomoc.mydevil.net/Binexec/ "MyDevil 官方 Binexec 文档"
[4]: https://pomoc.mydevil.net/Logowanie/ "MyDevil 官方 SSH 登录文档"
[5]: https://forum.ct8.pl/showthread.php?tid=6670 "CT8 官方论坛：通过 Cron 运行 Python 脚本"
[6]: https://pomoc.mydevil.net/Cron/ "MyDevil 官方 Cron 文档"
