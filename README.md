# astrbot_plugin_ddl_tracker

一个用于 AstrBot 群聊场景的 DDL 记录与提醒插件。

它的核心思路很直接：

- 管理员先在某个群里开启插件
- 插件开始静默记录该群的纯文本消息
- 定时把新消息交给 AI 提取 DDL 和提醒规则
- 把结果存入 SQLite
- 到达提醒时间后自动往原群发送提醒

当前代码结构已经按“少量文件、职责清晰、不过度抽象”的思路整理，不使用 `dataclass`，主要通过 `dict` 在模块之间传递数据。

## 功能概览

- 管理员可以在群内开启和关闭功能
- 开启后自动记录群内所有纯文本消息
- 记录内容写入 SQLite
- 自动提取 DDL
- 自动提取“某类 DDL 该怎么提醒”的规则
- 支持两类提醒策略
  - 固定提前时长提醒
  - 截止前若干天的固定时刻提醒
- 新规则会自动回刷历史同类 DDL
- 到达提醒时间后自动往群里发提醒
- 提供 LLM Tool
- 提供 Skill 给 AstrBot Agent 调用

## 当前代码结构

当前真正生效的核心文件如下：

```text
astrbot_plugin_ddl_tracker/
├─ main.py
├─ metadata.yaml
├─ _conf_schema.json
├─ README.md
├─ requirements.txt
├─ ddl_tracker/
│  ├─ __init__.py
│  ├─ plugin.py
│  ├─ sql_store.py
│  ├─ reminder_loop.py
│  ├─ data_ops.py
│  ├─ settings.py
│  └─ utils.py
└─ skills/
   └─ ddl_tracker/
      └─ SKILL.md
```

### 各文件职责

- `main.py`
  - AstrBot 插件最薄入口
  - 只负责导出 `DDLTrackerPlugin`

- `ddl_tracker/plugin.py`
  - 插件主入口
  - 负责命令注册、LLM Tool 注册、群消息监听、后台循环、调用 AI、调用 SQL 层和提醒层
  - 这是你以后最常改的文件

- `ddl_tracker/sql_store.py`
  - 纯 SQLite 层
  - 负责建表、查表、插入、更新、去重检查、读取待提醒项
  - 所有 SQL 都集中在这里

- `ddl_tracker/reminder_loop.py`
  - 提醒和后台扫描相关逻辑
  - 负责：
    - 计算 `remind_at_ts`
    - 保存抽取结果后的提醒数据
    - 回刷历史规则
    - 后台 tick
    - 扫描并发送提醒

- `ddl_tracker/data_ops.py`
  - 数据清理和整理
  - 负责：
    - 类别归一化
    - 提醒规则归一化
    - 提取结果清洗
    - 构造 DDL 指纹
    - 构造对话文本
    - 序列化输出结构

- `ddl_tracker/settings.py`
  - 配置读取和 setting 封装
  - 负责：
    - 读取 WebUI 配置
    - 处理默认值
    - 处理时区
    - 统一格式化时间
    - 生成默认提醒规则

- `ddl_tracker/utils.py`
  - 最基础的小工具函数
  - 目前主要包含：
    - `safe_int`
    - `safe_float`
    - `safe_json_loads`

- `skills/ddl_tracker/SKILL.md`
  - 给 AstrBot Agent 使用的 Skill
  - 告诉 AI 在什么场景调用哪些 Tool

## 工作流程

插件完整工作流如下：

1. 管理员在群里发送 `/ddl_on`
2. 插件开始记录该群的纯文本消息
3. 后台循环按 `extract_interval_minutes` 扫描该群是否有新消息
4. 如果有新消息，插件把这批消息拼成文本
5. 插件调用 LLM，提取：
   - `summary`
   - `deadlines`
   - `reminder_rules`
6. 插件把 DDL 和提醒规则写入 SQLite
7. 如果某类提醒规则发生变化，插件回刷该类历史 DDL 的提醒时间
8. 后台循环继续检查是否有 DDL 到达 `remind_at_ts`
9. 到时后插件向原群发送提醒

## 部署说明

### 1. 先部署 AstrBot

本插件运行在 AstrBot 中，不是独立程序。

官方文档：

- AstrBot 文档主页：https://docs.astrbot.app/
- 插件开发文档：https://docs.astrbot.app/dev/star/plugin-new.html
- 插件配置文档：https://docs.astrbot.app/dev/star/guides/plugin-config.html
- AI / Tool 文档：https://docs.astrbot.app/dev/star/guides/ai.html

### 2. 安装本插件

把整个插件目录放到 AstrBot 的插件目录中，典型路径如下：

```text
AstrBot/
└─ data/
   └─ plugins/
      └─ astrbot_plugin_ddl_tracker/
```

如果你使用 Git，可以这样放：

```bash
cd AstrBot/data/plugins
git clone <你的仓库地址> astrbot_plugin_ddl_tracker
```

如果不用 Git，直接复制整个项目目录也可以。

### 3. 重载或重启 AstrBot

安装完成后，使用以下任一方式生效：

- 重启 AstrBot
- 在 AstrBot WebUI 中重载插件

### 4. 在 WebUI 中配置插件

AstrBot 会读取仓库中的 `_conf_schema.json` 自动生成配置页面。

默认 WebUI 地址通常是：

```text
http://localhost:6185
```

## 配置项说明

当前插件支持以下配置：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `admin_ids` | list | `[]` | 额外管理员 ID 列表，用于补充平台无法正确识别群管理权限的情况 |
| `llm_provider_id` | string | `""` | DDL 抽取使用的模型 Provider ID。为空时尝试自动推断 |
| `timezone` | string | `Asia/Shanghai` | 时间解析和提醒计算使用的时区 |
| `extract_interval_minutes` | int | `30` | 自动抽取 DDL 的时间间隔，单位分钟 |
| `max_messages_per_extract` | int | `200` | 单次送入模型的最大消息条数 |
| `default_remind_before_minutes` | int | `60` | 没有单独分类规则时，默认提前多少分钟提醒 |
| `auto_remind_enabled` | bool | `true` | 是否自动发送提醒 |
| `send_extract_summary_back_to_group` | bool | `false` | 是否把每次抽取摘要自动发回群里 |
| `tick_interval_seconds` | int | `30` | 后台轮询间隔，单位秒 |

推荐起步配置：

| 场景 | 建议 |
| --- | --- |
| 一般班群 | `extract_interval_minutes = 30` |
| 讨论频繁的群 | `extract_interval_minutes = 10 ~ 15` |
| 成本敏感 | `max_messages_per_extract = 100` |
| 保守提醒 | `default_remind_before_minutes = 1440` |
| 不想频繁回群摘要 | `send_extract_summary_back_to_group = false` |

## 群内命令

当前插件提供以下命令：

| 命令 | 权限 | 作用 |
| --- | --- | --- |
| `/ddl_on` | 管理员 | 在当前群开启 DDL 跟踪 |
| `/ddl_off` | 管理员 | 在当前群关闭 DDL 跟踪 |
| `/ddl_status` | 全员可用 | 查看当前群插件状态 |
| `/ddl_now` | 管理员 | 立即抽取当前群尚未处理的新消息 |
| `/ddl_list` | 全员可用 | 查看当前群剩余 DDL |
| `/ddl_due 24` | 全员可用 | 查看未来 24 小时内截止的 DDL |
| `/ddl_rules` | 全员可用 | 查看当前群已生效的分类提醒规则 |

### 权限判断

以下身份可执行管理员命令：

- 平台识别出的群管理员/群主
- 配置在 `admin_ids` 里的账号

## 自动记录规则

插件的记录行为如下：

- 只在群聊中生效
- 只记录纯文本消息
- 不依赖 `@ Bot`
- 不依赖 `/` 唤醒
- 命令消息会记录，但抽取时会跳过
- 机器人自己发出的消息抽取时会跳过

## 自动抽取规则

抽取是按批次执行的，不是每条消息实时调用一次模型。

执行方式：

1. 读取某群上次抽取之后的新消息
2. 拼接成统一文本
3. 调用 LLM
4. 解析为结构化 JSON
5. 入库

如果你不想等定时抽取，可以手动执行：

```text
/ddl_now
```

## 提醒规则说明

### 默认提醒

如果某条 DDL 没有匹配到专属分类规则，则使用：

- `default_remind_before_minutes`

### 分类提醒规则

插件支持两类规则：

1. `offset`
   - 例子：作业提前 1 天提醒
   - 实际会换算成分钟，例如 `1440`

2. `fixed_day_before_time`
   - 例子：考试前一天 22:00 提醒
   - 实际存为：
     - `days_before = 1`
     - `fixed_hour = 22`
     - `fixed_minute = 0`

### 历史 DDL 回刷

当某个分类的新提醒规则生效后：

- 之前已入库的同类 DDL 会重新计算提醒时间
- 之后新增的同类 DDL 也会沿用该规则

## LLM Tool

当前插件注册了以下 Tool：

| Tool 名称 | 作用 |
| --- | --- |
| `ddl_extract_messages` | 从一段纯文本中提取摘要、DDL 和提醒规则 |
| `ddl_sync_group_deadlines` | 立即同步当前群最近的新消息并更新 DDL |
| `ddl_list_remaining_deadlines` | 查询当前群剩余 DDL |
| `ddl_list_due_within` | 查询指定时间范围内截止的 DDL |
| `ddl_set_category_reminder` | 手动设置某类 DDL 的提醒规则 |

## Skill

仓库中已经提供：

```text
skills/ddl_tracker/SKILL.md
```

这个 Skill 的作用是告诉 AstrBot Agent：

- 什么时候该先同步群消息
- 什么时候该查剩余 DDL
- 什么时候该查最近截止 DDL
- 什么时候该设置分类提醒规则

如果你只想使用自动记录、自动抽取、自动提醒，那么 Skill 不是必须的。

## 使用示例

### 1. 开启插件

群里发送：

```text
/ddl_on
```

机器人返回：

```text
当前群已开启 DDL 跟踪。
接下来会自动记录纯文本消息、周期抽取 DDL，并在到达提醒时间时自动提醒。
```

### 2. 群里自然出现 DDL

群消息：

```text
班长：高数作业 4 月 28 日晚上 23:00 前提交到雨课堂
助教：离散数学小测 4 月 29 日下午 14:00 开始
学委：讲座报告本周五 18:00 前交到邮箱
```

插件在抽取后会尝试得到类似结构：

```json
{
  "summary": "最近群里新增了作业、小测和讲座报告相关截止事项。",
  "deadlines": [
    {
      "description": "提交高数作业到雨课堂",
      "category": "作业",
      "deadline_text": "4 月 28 日晚上 23:00 前",
      "deadline_ts": 1777398000,
      "confidence": 0.97
    },
    {
      "description": "参加离散数学小测",
      "category": "考试",
      "deadline_text": "4 月 29 日下午 14:00",
      "deadline_ts": 1777442400,
      "confidence": 0.95
    }
  ],
  "reminder_rules": []
}
```

### 3. 群里自然设定提醒规则

群里有人说：

```text
以后作业统一提前 1 天提醒，考试统一前一天晚上 22 点提醒。
```

插件在抽取后会尝试识别为：

```json
{
  "reminder_rules": [
    {
      "category": "作业",
      "remind_type": "offset",
      "offset_minutes": 1440
    },
    {
      "category": "考试",
      "remind_type": "fixed_day_before_time",
      "days_before": 1,
      "fixed_hour": 22,
      "fixed_minute": 0
    }
  ]
}
```

### 4. 查看剩余 DDL

群里发送：

```text
/ddl_list
```

示例输出：

```json
{
  "group_id": "123456",
  "deadlines": [
    {
      "id": 3,
      "description": "提交高数作业到雨课堂",
      "category": "作业",
      "deadline_ts": 1777398000,
      "deadline_at": "2026-04-28 23:00:00",
      "remind_at_ts": 1777311600,
      "remind_at": "2026-04-27 23:00:00",
      "reminded": false,
      "confidence": 0.97
    }
  ]
}
```

### 5. 查看 24 小时内截止的 DDL

群里发送：

```text
/ddl_due 24
```

示例输出：

```json
{
  "group_id": "123456",
  "hours": 24,
  "deadlines": [
    {
      "id": 5,
      "description": "提交讲座报告到邮箱",
      "category": "讲座报告",
      "deadline_ts": 1777600800,
      "deadline_at": "2026-05-01 18:00:00",
      "remind_at_ts": 1777514400,
      "remind_at": "2026-04-30 18:00:00",
      "reminded": false,
      "confidence": 0.93
    }
  ]
}
```

### 6. 查看分类提醒规则

群里发送：

```text
/ddl_rules
```

示例输出：

```text
作业: 提前 1440 分钟提醒
考试: 截止前 1 天 22:00 提醒
```

## 对话示例

### 示例 1：管理员启用插件

```text
管理员：/ddl_on
Bot：当前群已开启 DDL 跟踪。
Bot：接下来会自动记录纯文本消息、周期抽取 DDL，并在到达提醒时间时自动提醒。
```

### 示例 2：自然讨论 DDL

```text
学委：这周三晚 11 点前把高数作业交了
班长：周四下午 2 点离散小测，别迟到
同学 A：收到
同学 B：以后作业统一提前 1 天提醒吧
```

这时插件不会立即回复，但会：

- 记录这些消息
- 在下次抽取时识别作业和考试 DDL
- 同时识别“作业提前 1 天提醒”的分类规则

### 示例 3：管理员手动抽取

```text
管理员：/ddl_now
Bot：群 123456 抽取完成：新增 DDL 2 条，更新提醒规则 1 条，重算历史 DDL 3 条。
```

### 示例 4：AI 使用 Tool 查询

```text
用户：帮我看下这个群今天内有哪些 DDL 要截止
AI：我先同步一下这个群最近的新消息，再查询 24 小时内截止的 DDL。
AI：今天内即将截止的 DDL 有 2 项：
AI：1. 高数作业，今晚 23:00 截止
AI：2. 讲座报告，今晚 18:00 截止
```

## 数据存储

SQLite 数据库默认位于：

```text
data/plugin_data/ddl_tracker/ddl_tracker.sqlite3
```

如果 AstrBot 自己配置了数据目录，则以 AstrBot 的实际数据目录为准。

### 数据表

当前主要有 4 张表：

| 表名 | 作用 |
| --- | --- |
| `groups` | 记录哪些群启用了插件，以及该群的抽取进度 |
| `messages` | 记录群纯文本消息 |
| `reminder_rules` | 记录每个群、每个类别的提醒规则 |
| `deadlines` | 记录 DDL、提醒时间、状态和来源 |

### 去重规则

DDL 入库时按以下字段生成指纹去重：

- `category`
- `description`
- `deadline_ts`

完全相同的组合不会重复写入。

## 开发说明

这版代码刻意不做过多抽象。

### 当前设计原则

- 不使用 `dataclass`
- 主要用 `dict` 在模块之间传递数据
- SQL 集中在 `sql_store.py`
- 提醒和后台循环集中在 `reminder_loop.py`
- 清洗和整理逻辑集中在 `data_ops.py`
- 配置读取集中在 `settings.py`
- 插件主流程集中在 `plugin.py`

### 推荐修改入口

- 改命令、Tool、群消息监听：改 `ddl_tracker/plugin.py`
- 改数据库结构和 SQL：改 `ddl_tracker/sql_store.py`
- 改提醒策略和后台循环：改 `ddl_tracker/reminder_loop.py`
- 改类别归一化、清洗逻辑、输出结构：改 `ddl_tracker/data_ops.py`
- 改配置项读取逻辑：改 `ddl_tracker/settings.py`

## 注意事项

### 1. 只记录纯文本

插件当前依赖 `message_str` 记录消息，因此图片、文件、语音、复杂富文本不会按原始结构保存。

### 2. 抽取效果依赖模型

DDL 抽取是否准确，和模型能力高度相关。

建议：

- 使用较稳定的聊天模型
- 生产环境明确填写 `llm_provider_id`
- 对关键群先人工抽查效果

### 3. 自动抽取不是逐条实时触发

插件是“按间隔批量抽取”，不是“每条消息都立即调模型”。

如果刚发完通知就想立刻更新，请用：

```text
/ddl_now
```

### 4. Skill 不是必须

如果你不需要 AI 主动查询数据库，只想让插件自动记录、自动提取、自动提醒，那么不上传 Skill 也可以正常工作。

## 故障排查

### 群里发消息没有被记录

优先检查：

1. 当前群是否已执行 `/ddl_on`
2. 是否确实是群聊消息
3. 消息是否包含纯文本
4. 插件是否在 AstrBot 中加载成功

### 一直没有抽取出 DDL

优先检查：

1. `llm_provider_id` 是否可用
2. AstrBot 是否已经配置聊天模型
3. `extract_interval_minutes` 是否过大
4. 先手动执行 `/ddl_now`

### 没有自动提醒

优先检查：

1. `auto_remind_enabled` 是否为 `true`
2. 是否已经成功生成 `remind_at_ts`
3. `tick_interval_seconds` 是否合理
4. 目标 DDL 是否已经被标记为 `expired`

### Skill 没生效

优先检查：

1. AstrBot 版本是否支持 Skills
2. Skill 是否放在正确目录或正确上传
3. 当前会话是否支持 Tool/Skill
4. 当前 Agent 运行环境是否允许调用工具

## 参考文档

- AstrBot 插件开发指南：https://docs.astrbot.app/dev/star/plugin-new.html
- AstrBot 插件配置：https://docs.astrbot.app/dev/star/guides/plugin-config.html
- AstrBot 调用 AI / Tool：https://docs.astrbot.app/dev/star/guides/ai.html
- AstrBot 插件使用说明：https://docs.astrbot.app/use/plugin.html
- AstrBot 源码部署：https://docs.astrbot.app/deploy/astrbot/cli.html
- AstrBot Docker 部署：https://docs.astrbot.app/deploy/astrbot/docker.html
- AstrBot Skills 说明：https://docs.astrbot.app/en/use/skills.html
- AstrBot Computer Use / Skills 目录说明：https://docs.astrbot.app/use/computer.html
