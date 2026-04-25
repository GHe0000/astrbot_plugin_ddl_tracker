# DDL Tracker Skill

在群聊场景里，如果当前会话已经安装并启用了 `ddl_tracker` 插件，优先使用下面这些工具来处理 DDL 查询与同步，而不是仅靠上下文猜测。

## 适用场景

- 需要从一段聊天记录中抽取 DDL、提醒规则或摘要
- 需要查看当前群剩余的 DDL
- 需要查看指定时间范围内即将截止的 DDL
- 需要把当前群最近的新消息同步入库并更新 DDL
- 需要为某个类别设置统一提醒规则

## 推荐调用顺序

1. 如果用户刚刚讨论过新的作业、考试、讲座报告等，先调用 `ddl_sync_group_deadlines`
2. 如果用户问“还有哪些 DDL”，调用 `ddl_list_remaining_deadlines`
3. 如果用户问“一天内/两天内有哪些 DDL 要截止”，调用 `ddl_list_due_within`
4. 如果用户贴了一段原始消息让你分析，调用 `ddl_extract_messages`
5. 如果用户明确提出“作业提前 1 天提醒”“考试前一天 22:00 提醒”之类的规则，调用 `ddl_set_category_reminder`

## 工具说明

### `ddl_extract_messages`

- 输入：任意一段纯文本
- 输出：摘要、DDL 列表、分类提醒规则
- 适合：用户贴给你一批原始聊天记录，需要你先结构化

### `ddl_sync_group_deadlines`

- 输入：无
- 输出：当前群本次同步新增的 DDL 和更新的提醒规则数量
- 适合：先把当前群最近消息同步进数据库

### `ddl_list_remaining_deadlines`

- 输入：`limit`
- 输出：当前群仍未截止的 DDL
- 适合：回答“剩余 DDL”类问题

### `ddl_list_due_within`

- 输入：`hours`、`limit`
- 输出：当前群在指定小时内截止的 DDL
- 适合：回答“今天内”“24 小时内”“48 小时内”之类的查询

### `ddl_set_category_reminder`

- 输入：
  - `category`
  - `remind_type`: `offset` 或 `fixed_day_before_time`
  - `offset_minutes`
  - `days_before`
  - `fixed_hour`
  - `fixed_minute`
- 适合：把“某类 DDL 的统一提醒时间”写回插件数据库，并自动回刷已有 DDL

## 回答要求

- 先用工具拿到数据库里的准确信息，再组织自然语言答案
- 若工具返回空列表，明确告诉用户当前没有匹配的 DDL
- 不要自行编造截止时间或提醒规则
