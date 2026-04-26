---
name: ddl-tracker
description: 管理群聊 DDL、分类提醒规则，并通过 AstrBot 官方 future_task 工具创建提醒任务。
---

# DDL Tracker

这个 Skill 用来配合 `ddl_tracker` 插件工作。插件负责记录、提取、去重、查询和计算提醒时间；真正的提醒任务请使用 AstrBot 官方 `future_task` 工具创建。

## 你可以使用的插件工具

- `ddl_extract_recent_messages`
- `ddl_get_remaining`
- `ddl_get_due_within`
- `ddl_set_type_reminder`
- `ddl_get_reminder_rules`
- `ddl_get_pending_future_tasks`
- `ddl_mark_future_task_created`

## 标准流程

1. 如果用户刚刚聊到新的作业、考试、报告、讲座等 DDL，先调用 `ddl_extract_recent_messages`。
2. 如果用户说“作业提前1天提醒”“考试前一天晚上22点提醒”这类规则，先调用 `ddl_set_type_reminder`。
3. 然后调用 `ddl_get_pending_future_tasks`，查看当前群还有哪些 DDL 需要创建官方提醒任务。
4. 对返回的每一条待创建任务：
   - 优先使用返回的 `task_name` 作为官方任务名。
   - 使用返回的 `remind_at` 作为提醒时间。
   - 使用返回的 `task_note` 作为任务说明。
   - 调用 AstrBot 官方 `future_task` 工具创建一次性提醒任务。
5. 官方任务创建成功后，立刻调用 `ddl_mark_future_task_created`，把 `fingerprint`、`remind_key`、`task_name` 回写给插件。

## 重要约束

- `ddl_get_pending_future_tasks` 如果返回 `count=0`，不要重复创建提醒任务。
- 如果某条返回里带有 `stale_task_names`，优先尝试用官方 `future_task` 工具删除或替换这些旧任务，再创建新任务。
- 不要把“作业提前1天提醒”“考试前一天晚上22点提醒”这类消息当成 DDL 本体。
- 不要自己编造提醒时间，优先使用插件返回的 `remind_at`。
- 不要修改 `task_name` 的格式，除非官方工具强制要求；插件依赖它做去重记录。

## FutureTask 被唤醒时

当官方 `future_task` 触发后：

1. 如有必要，可先调用 `ddl_get_remaining` 或 `ddl_get_due_within` 确认该 DDL 仍存在且未截止。
2. 然后直接在当前群里发送一条简短中文提醒。
3. 不要重新创建 future_task，不要输出冗长解释，不要闲聊。
