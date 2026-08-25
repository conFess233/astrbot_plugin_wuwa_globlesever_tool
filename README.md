<div align="center">

# 鸣潮国际服数据工具

面向 AstrBot 的鸣潮国际服账号绑定、数据刷新、本地档案与统一图片卡插件

[![Version](https://img.shields.io/badge/version-0.6.0-c8a96a)](CHANGELOG.md)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.24.2%20%3C5-6f42c1)](https://github.com/AstrBotDevs/AstrBot)
[![Platform](https://img.shields.io/badge/OneBot_11-QQ-12b7f5)](https://github.com/botuniverse/onebot-11)
[![License](https://img.shields.io/badge/license-AGPL--3.0-green)](LICENSE)

[功能](#功能概览) · [安装](#安装与部署) · [命令](#命令) · [管理](#webui) · [数据](#本地数据与隐私) · [更新日志](CHANGELOG.md)

</div>

> [!IMPORTANT]
> 邮箱和密码只用于当次登录请求，不会写入数据库，但会经过你自行部署的 AstrBot 进程。请只在可信 Bot 上登录，并为登录服务配置你自己控制的 HTTPS 域名。

## 功能概览

| 功能           | 说明                                                                                          |
| -------------- | --------------------------------------------------------------------------------------------- |
| 国际服邮箱登录 | Bot 发送一次性临时链接，在独立网页中登录、选择区服账号并设置默认账号；成功后 Bot 在原会话提示 |
| 多区服账号     | 一个 QQ 可绑定多个账号；账号以 `(区服, UID)` 唯一标识，不同区服中的相同 UID 互不冲突          |
| 数据刷新       | 账号信息、日常和探索可按管理员全局配置在查询前刷新；角色数据由用户主动刷新或后台定时同步      |
| 角色数据       | 从国际服攻略站获取拥有角色、共鸣链及武器；接口身份优先，允许对等级、链数和武器字段做手动覆盖  |
| 本地档案       | 未登录、未拥有角色时也可创建本地记录；接口以后返回该角色时会按稳定角色 ID 合并                |
| 统一卡片       | 角色总览、角色详情、账号信息、日常、探索和帮助均返回统一风格图片；角色总览为单张五列长图      |
| 资源与字体     | 武器和属性使用图片/图标，不再暴露内部 ID；支持受限资源缓存、字体 URL/ZIP 下载、默认字体切换   |
| WebUI          | 管理配置、账号、字体、资源、卡片预览、缓存、审计和脱敏导出；沿用 AstrBot Dashboard 登录态     |

声骸评分暂未启用。数据模型保留评分字段，当前展示值为 `---`，便于后续扩展。

## 安装与部署

### 环境要求

- AstrBot `>=4.24.2,<5`
- QQ / OneBot 11（`aiocqhttp`）
- Python 环境可安装 `aiohttp>=3.9,<4`
- 邮箱登录需要一个反向代理至插件独立登录端口的公网 HTTPS 域名

推荐从 AstrBot 插件市场安装。手动安装时必须复制完整插件目录，不能只复制 `main.py`；随后让 AstrBot 安装 [requirements.txt](requirements.txt) 并重载插件。

本插件不指定 `cryptography` 版本，使用 AstrBot 核心提供且受保护的兼容版本，避免插件安装时触发核心依赖冲突。

### 登录端口

默认配置：

```text
login_server_host = 127.0.0.1
login_server_port = 6199
public_https_base_url = https://你的登录域名
```

登录服务和 AstrBot Dashboard 使用不同端口。不要把公网域名反向代理到 Dashboard 的 `6185` 端口，否则匿名请求会得到 `Missing API key`。

### Cloudflare Tunnel

AstrBot 与 `cloudflared` 在同一服务器时，保持监听地址为 `127.0.0.1`，并在 Cloudflare Tunnel 的 Public Hostname 中设置：

```text
Hostname: 你的登录域名
Service:  http://127.0.0.1:6199
```

建议同时保持：

```text
login_trust_proxy_headers = true
login_trusted_proxy_cidrs = []
```

保存后重载插件，再发送 `/kh 登录` 获取新链接。可在 Windows PowerShell 中验证独立服务：

```powershell
curl.exe -i "https://你的登录域名/health"
```

应返回登录服务状态 JSON。请直接填写 URL，不要把 Markdown 的 `[文字](链接)` 语法复制进命令。

如果代理和 AstrBot 不在同一主机，应将 `login_server_host`、防火墙与 `login_trusted_proxy_cidrs` 配成仅允许代理主机访问；不建议直接向公网开放登录端口。

## 登录流程

1. 在群内发送 `/kh 登录`。
2. 打开 Bot 返回的临时 HTTPS 链接，输入国际服邮箱和密码。
3. 勾选要绑定的区服账号，并选择默认账号。
4. 网页完成后等待 Bot 在原会话提示登录成功。
5. 发送 `/kh 刷新` 获取角色数据；账号信息、日常和探索会按全局配置刷新或读取缓存。

登录消息仅发送一条合并转发，内容包含简短提示、过期时间和链接。链接短时有效且仅可使用一次；可通过 `/kh 取消登录` 立即作废。

登录成功后，插件会加密保存 SDK 返回的 `autoToken` 和设备 ID，仅在游戏 OAuth 或攻略站访问令牌失效时用于免密续期；不会保存邮箱密码。`autoToken` 本身失效后仍需重新执行 `/kh 登录`。

不登录也能使用纯本地档案：

```text
/kh 修改 今汐 等级 90
/kh 修改 今汐 共鸣链 2
/kh 角色 今汐
```

## 命令

`/kh` 是永久兼容入口，不能关闭。管理员可通过 `extra_command_roots` 增加 `/ww` 等入口。正式命令和关键词均兼容消息中的 `@Bot`。

### 查询

| 命令                      | 说明                                                         |
| ------------------------- | ------------------------------------------------------------ |
| `/kh`、`/kh 帮助`         | 返回图片帮助卡                                               |
| `/kh 账号信息 [@用户]`    | 头像、昵称、区服、UID、等级、角色数、活跃天数等              |
| `/kh 日常`                | 结晶波片、储备结晶单质、活跃度、战歌重奏和先约电台；仅限本人 |
| `/kh 探索 [@用户]`        | 声匣、奇藏箱及潮汐之遗等接口确认计数                         |
| `/kh 角色 [@用户]`        | 当前档案的完整角色总览长图，不分页                           |
| `/kh 角色 <角色> [@用户]` | 角色、共鸣链、元素及武器详情                                 |

管理员开启 `allow_query_others` 后，账号信息、探索、角色总览和角色详情可在命令前后放置一个查询对象，例如 `@用户 kh角色`、`kh角色 今汐 @用户`。他人查询只读缓存，不触发对方账号刷新；日常始终只能查询本人。

旧的 `/kh 练度`、`/kh 面板` 已删除并提示改用 `/kh 角色`。`kh角色 2页`、`kh角色 x页` 等分页写法会提示直接获取完整长图。

### 登录、账号与刷新

| 命令                             | 说明                                            |
| -------------------------------- | ----------------------------------------------- |
| `/kh 登录`                       | 创建一次性网页登录链接                          |
| `/kh 取消登录`                   | 作废当前 QQ 的活动登录会话                      |
| `/kh 账号`                       | 以编号列出已绑定的区服和完整 UID                |
| `/kh 切换 <编号\|完整UID\|本地>` | 切换当前活动档案；相同 UID 跨区服时必须使用编号 |
| `/kh 刷新 [完整UID]`             | 刷新当前或指定已绑定账号的角色数据              |
| `/kh 同步 [完整UID]`             | `/kh 刷新` 的兼容别名                           |
| `/kh 解绑 <完整UID>`             | 发起解绑当前 QQ 的账号，需二次确认              |
| `/kh 确认`                       | 确认当前会话中 60 秒内的待执行操作              |
| `/kh 取消`                       | 取消待执行操作                                  |

账号信息、日常和探索共享玩家数据快照与刷新冷却。`query_refresh_enabled=true` 时，冷却外先刷新再出图；关闭后优先读缓存，但从未获取过快照时仍会尝试获取一次。角色刷新冷却按 `(区服, UID)` 分开计算。

### 手动角色维护

| 命令                              | 范围                                         |
| --------------------------------- | -------------------------------------------- |
| `/kh 修改 <角色> 等级 <1-90>`     | 覆盖角色等级                                 |
| `/kh 修改 <角色> 共鸣链 <0-6>`    | 覆盖共鸣链                                   |
| `/kh 修改 <角色> 武器 <名称>`     | 接口未返回武器时设置本地武器名称             |
| `/kh 修改 <角色> 武器等级 <1-90>` | 覆盖武器等级                                 |
| `/kh 修改 <角色> 武器精炼 <1-5>`  | 覆盖武器精炼                                 |
| `/kh 重置 <角色> <字段\|全部>`    | 清除手动覆盖；重置全部需确认                 |
| `/kh 删除角色 <角色>`             | 删除纯本地角色；接口拥有角色不可删除，需确认 |

字段优先级为：有效手动覆盖 → 最新接口值 → 允许的本地回退 → `—`。角色和武器身份始终优先采用接口数据。漂泊者只识别接口实际返回的稳定记录，不推断性别或属性形态。

### 默认关键词

| 功能            | 默认关键词                                          |
| --------------- | --------------------------------------------------- |
| 帮助            | `kh帮助`、`鸣潮帮助`                                |
| 登录 / 取消登录 | `kh登录`、`鸣潮登录` / `kh取消登录`、`鸣潮取消登录` |
| 账号 / 切换     | `kh账号`、`鸣潮账号` / `kh切换`、`鸣潮切换`         |
| 账号信息        | `kh账号信息`、`鸣潮账号信息`                        |
| 角色            | `kh角色`、`鸣潮角色`                                |
| 日常            | `kh日常`、`鸣潮日常`                                |
| 探索            | `kh探索`、`鸣潮探索`                                |
| 刷新            | `kh刷新`、`kh同步`、`鸣潮刷新`                      |

所有关键词列表都可在插件设置中修改。解析采用完整短语和最长匹配；配置中存在关键词冲突时会拒绝整次保存。

## WebUI

插件页面复用 AstrBot Dashboard 登录态，不设置插件 API Key。当前页面提供：

- 运行概览、账号列表、区服/UID 复合键解绑与 QQ 数据删除；
- 登录监听、查询刷新、关键词、后台同步、网络超时和缓存配置；
- 角色静态目录检查、更新、回滚及资源缓存清理；
- 字体 URL 安装、TTF/OTF/ZIP 校验、默认字体选择和删除保护；
- 账号信息、日常、探索、角色总览、角色详情和帮助卡预览；
- 管理操作审计、脱敏备份导出、备份预检和恢复。

字体下载会阻止 loopback、私网、链路本地和云元数据地址，并限制重定向、文件大小、ZIP 路径、文件数和解压体积。插件预设 HarmonyOS Sans 的 ZIP 镜像，也允许管理员添加自定义 URL；字体许可和官方资源说明请查看 [Huawei Design Resources](https://developer.huawei.com/consumer/en/design/resource/)。

## 配置

完整字段、默认值和提示以 [\_conf_schema.json](_conf_schema.json) 及 Dashboard 表单为准。

| 配置                                                              | 默认值               | 说明                                   |
| ----------------------------------------------------------------- | -------------------- | -------------------------------------- |
| `public_https_base_url`                                           | 空                   | 登录页 HTTPS 根地址；留空禁用网页登录  |
| `login_server_host` / `login_server_port`                         | `127.0.0.1` / `6199` | 独立登录监听地址和端口                 |
| `login_link_ttl_minutes`                                          | `3`                  | 登录链接有效期，允许 1–60 分钟         |
| `allow_query_others`                                              | `false`              | 全局控制是否允许查询其他 QQ 的公开缓存 |
| `query_refresh_enabled`                                           | `true`               | 账号信息、日常、探索查询前自动刷新     |
| `player_refresh_cooldown_seconds`                                 | `60`                 | 玩家快照共用冷却                       |
| `role_refresh_cooldown_minutes`                                   | `5`                  | 用户主动角色刷新冷却                   |
| `auto_sync_enabled` / `auto_sync_interval_minutes`                | `false` / `360`      | 后台角色同步开关与周期                 |
| `request_timeout_seconds` / `request_retry_count`                 | `20` / `2`           | 单次请求超时与幂等查询重试次数         |
| `player_refresh_timeout_seconds` / `role_refresh_timeout_seconds` | `45` / `180`         | 两类整体刷新超时                       |
| `resource_cache_max_mb`                                           | `512`                | 仅回收未被当前数据引用的远程资源       |
| `resource_download_timeout_seconds`                               | `60`                 | 图片与字体下载总超时                   |
| `admin_audit_retention_days`                                      | `30`                 | 管理审计保留天数；`0` 表示关闭         |

旧配置 `auto_sync_interval_hours` 会在读取时兼容换算为分钟，不需要手工删除旧配置。

## 本地数据与隐私

所有可变数据只写入：

```text
AstrBot/data/plugin/astrbot_plugin_wuwa_globlesever_tool/
├─ wuwa.sqlite3
├─ secrets/
├─ settings/
├─ snapshots/raw/
├─ cache/
│  ├─ resources/
│  ├─ character/
│  ├─ weapon/
│  ├─ static_data/
│  └─ manifests/
├─ media/cards/
├─ media/temp/
├─ fonts/
├─ migrations/
├─ backups/
├─ exports/
└─ logs/
```

- 每个账号只保留最新玩家快照、角色快照、脱敏原始响应和最终卡片，不保存历史。
- 登录凭据使用实例主密钥加密；密码和验证码不持久化。
- 原始响应落盘前递归移除邮箱、Token、Cookie、认证头等敏感字段，再加密保存。
- Dashboard 默认导出不包含可复用凭据、`master.key`、Cookie 或登录会话。
- 若插件数据目录和主密钥同时泄露，静态加密不能阻止攻击者解密凭据，因此必须限制目录权限并做好整目录备份。

## 项目结构

```text
application/       账号、登录、刷新、命令、卡片和后台管理用例
domain/            稳定模型、目录和业务语义
integrations/      Kuro、国际服攻略站与 AstrBot 适配器
infrastructure/    数据库迁移、仓储、HTTP、安全下载、加密与存储
presentation/      命令解析、卡片渲染、资源和字体管理
web/               独立登录服务与 Dashboard 路由
pages/manage/      Dashboard 前端
static/            只读角色数据、资源 URL、卡片样式、占位图与字体预设
scripts/           静态清单生成工具
test/              仅本地测试，已忽略且不会发布
```

内部导入均使用完整包内相对路径，避免 AstrBot 加载时把 `clients`、`services` 等目录误当作顶层包。

## 已知限制

- 当前仅适配 QQ OneBot 11 / `aiocqhttp`。
- 声骸评分尚未实现，评分占位为 `---`。

## 鸣谢

实现过程中参考了以下项目的结构、接口与数据处理思路：

- [XutheringWavesUID](https://github.com/Loping151/XutheringWavesUID)
- [WwTool](https://github.com/conFess233/WwTool)
- [astrbot_plugin_iconic_quotes](https://github.com/conFess233/astrbot_plugin_iconic_quotes)

## 许可证

[AGPL-3.0](LICENSE)
