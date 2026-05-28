# DDL Tracker — AstrBot 群聊 DDL 跟踪插件

基于 LLM 的群聊 DDL（截止日期）自动提取、分类提醒与 `future_task` 集成插件。

---

## 功能

- **消息录制** — 开启后自动记录群聊文本消息（上限 500 条/群）
- **AI 提取 DDL** — 定时/手动将最近消息发给 LLM，提取作业、考试、实验、报告等 DDL
- **分类提醒规则** — 支持自然语言设置规则，如"作业提前1天提醒""考试前一天晚上22点提醒"
- **官方提醒集成** — 配合主 Agent 的 `future_task` 工具创建定时提醒任务
- **查询命令** — 查看群状态（`/ddl_status`）和最近的 DDL（`/ddl_nearest`）

### 支持的命令

| 命令 | 说明 |
|------|------|
| `/ddl_on` | 开启当前群的 DDL 跟踪 |
| `/ddl_off` | 关闭当前群的 DDL 跟踪 |
| `/ddl_status` | 显示当前群状态（消息数、DDL 数、规则数、配置等） |
| `/ddl_extract [分钟]` | 手动整理最近一段时间消息中的 DDL |
| `/ddl_nearest [数量]` | 显示距离截止最近的 K 个 DDL |

### 提供的 LLM Tool（供 Agent 调用）

| Tool | 说明 |
|------|------|
| `ddl_extract_recent_messages` | 整理最近消息并提取 DDL |
| `ddl_get_remaining` | 查看未截止的 DDL |
| `ddl_get_due_within` | 查看指定时间范围内即将截止的 DDL |
| `ddl_set_type_reminder` | 为某一类 DDL 设置提醒规则 |
| `ddl_get_reminder_rules` | 查看已生效的分类提醒规则 |
| `ddl_get_pending_future_tasks` | 获取待创建的 FutureTask 提醒计划 |
| `ddl_mark_future_task_created` | 记录 DDL 已绑定官方提醒任务 |

---

## 架构

```
ddl_tracker/
├── main.py              # 插件入口，继承所有 Mixin + Star
├── constants.py         # 模块级常量
├── utils.py             # 纯工具函数
├── config.py            # ConfigMixin：配置读取
├── state_store.py       # StateMixin：状态加载/保存/过期清理
├── ddl_item.py          # DDLItemMixin：DDL 归一化、指纹、合并去重
├── reminder_rules.py    # ReminderRulesMixin：规则提取/构建/匹配
├── future_task.py       # FutureTaskMixin：待创建任务计算
├── extraction.py        # ExtractionMixin：LLM 提取循环
├── commands.py          # CommandsMixin：命令处理器
├── llm_tools.py         # LLMToolsMixin：7 个 LLM Tool
├── _conf_schema.json    # 配置项 schema
├── ddl_groups.json      # 持久化状态（自动生成）
├── metadata.yaml        # 插件元数据
└── skills/ddl-tracker/
    └── SKILL.md         # Agent Skill 指引
```

### 数据流

```
群消息 ──► on_group_message ──► 状态存储 (ddl_groups.json)
                                      │
                   ┌──────────────────┘
                   ▼
    auto_extract_loop (定时) / /ddl_extract (手动)
                   │
                   ▼
          筛选消息 ──► 构建 Prompt ──► 调用 LLM
                   │
                   ▼
          解析 JSON ──► 合并去重 ──► 持久化 DDL 列表
                   │
                   ▼
    计算提醒时间（匹配提醒规则）──► 主 Agent 创建 future_task
```

---

## 部署

### 环境要求

- **AstrBot** 已安装并正常运行
- AstrBot 已配置至少一个 LLM Provider（用于 DDL 提取）
- Python >= 3.10

### 安装步骤

1. 将整个 `astrbot_plugin_ddl_tracker` 目录放入 AstrBot 的插件目录：

   ```
   AstrBot/
   └── plugins/
       └── astrbot_plugin_ddl_tracker/
           ├── main.py
           ├── constants.py
           ├── utils.py
           ├── config.py
           ├── state_store.py
           ├── ddl_item.py
           ├── reminder_rules.py
           ├── future_task.py
           ├── extraction.py
           ├── commands.py
           ├── llm_tools.py
           ├── _conf_schema.json
           ├── metadata.yaml
           └── skills/
               └── ddl-tracker/
                   └── SKILL.md
   ```

2. 重启 AstrBot，插件自动加载。

3. AstrBot 启动日志中应出现：
   ```
   [ddl_tracker] loaded config=... state=...
   ```

### 配置说明

在 AstrBot 管理面板中，进入插件 `ddl_tracker` 的配置页：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `llm_provider_id` | string | (空) | DDL 提取使用的模型 Provider ID，留空则使用当前会话绑定的模型 |
| `auto_extract_enabled` | bool | true | 是否启用自动整理 |
| `auto_extract_interval_minutes` | int | 30 | 自动整理周期（分钟） |
| `auto_remind_enabled` | bool | true | 是否启用 DDL 提醒计划 |
| `remind_before_minutes` | int | 60 | 默认提前多少分钟提醒（没有匹配到分类规则时使用） |
| `extract_prompt` | text | (内置) | DDL 提取提示词，可自定义 |

### 快速上手

1. **开启 DDL 跟踪** — 在目标群聊中发送：
   ```
   /ddl_on
   ```

2. **让插件学习消息** — 在群聊中正常讨论作业、考试、实验截止时间，插件会自动记录消息。也可以主动发送包含 DDL 的消息：
   ```
   高数作业 2026-5-10
   线代考试 2026-5-20 下午3点
   ```

3. **手动提取 DDL** — 确保消息已被记录后，发送：
   ```
   /ddl_extract
   ```
   插件会调用 LLM 分析最近 30 分钟（或你指定的分钟数）的消息，提取 DDL。

4. **设置分类提醒规则** — 直接发送自然语言规则：
   ```
   作业提前1天提醒
   考试前一天晚上22点提醒
   ```
   或者让主 Agent 调用 `ddl_set_type_reminder` tool 设置。

5. **创建提醒任务** — 对主 Agent 说：「帮我创建 DDL 提醒」，Agent 会：
   - 调用 `ddl_get_pending_future_tasks` 获取待创建任务
   - 调用 AstrBot 官方 `future_task` 逐条创建
   - 调用 `ddl_mark_future_task_created` 标记已完成

6. **查看状态**：
   ```
   /ddl_status
   /ddl_nearest 5
   ```

### 提醒规则语法

插件支持两种规则模式，在群聊中直接发送即可自动识别：

**相对时间**（提前 N 分钟/小时/天/周）：
```
作业提前1天提醒
实验报告提前2小时通知
讲座提前一周提醒
```

**固定时间点**（前 N 天几点几分）：
```
考试前一天晚上22点提醒
作业当天早上8点通知
讲座前3天上午10点提醒
```

### 持久化数据

插件状态保存在 `ddl_groups.json` 文件中，结构如下：

```json
{
  "group_id": {
    "enabled": true,
    "messages": [...],        // 最近 500 条消息
    "ddl_items": [...],       // 去重后的 DDL 列表
    "reminder_rules": {...},  // 分类提醒规则
    "last_extract_at": 12345,
    "unified_msg_origin": "..."
  }
}
```

---

## 项目信息

- **作者**: Codex
- **版本**: v0.8.0
- **仓库**: (待填写)
- **协议**: 见 LICENSE 文件
