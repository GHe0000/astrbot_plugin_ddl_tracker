from pathlib import Path

STATE_FILE = Path(__file__).with_name("ddl_groups.json")
MAX_MESSAGES_PER_GROUP = 500
DEFAULT_AUTO_EXTRACT_INTERVAL_MINUTES = 30
DEFAULT_REMIND_BEFORE_MINUTES = 60
AUTO_LOOP_TICK_SECONDS = 15
COMMAND_NAMES = {"ddl_on", "ddl_off", "ddl_status", "ddl_extract", "ddl_nearest"}
DEFAULT_EXTRACT_PROMPT = """
你是一个负责提取群聊中 DDL 的助手。
请从群消息里识别明确提到的作业、考试、实验、报告、报名、提交截止等事项。
只输出 JSON，不要输出解释，不要输出 Markdown 代码块。
输出必须是一个对象，包含 summary 和 items 两个字段。
items 是数组；每个元素包含：message_index、type、title、deadline_text、normalized_deadline、source_text。
如果没有明确 DDL，请返回 {"summary":"未识别到明确 DDL","items":[]}。
不要编造不存在的信息，deadline_text 必须直接基于原消息。
要特别识别相对时间表达，例如"一小时后截止""今晚 11 点前""一周内提交""下周一前"。
如果消息里出现相对时间，请优先根据该条消息前面的 ts/time 字段来解析。
如果能推算出明确时间，请填写 normalized_deadline；不能精确到分钟时，也尽量给出最合理的截止时间文本。
如果某条消息只是设置提醒规则，例如"作业提前1天提醒""考试前一天晚上22点提醒"，这是提醒规则，不是 DDL，不要加入 items。
尽量把 type 归一化成稳定类别，例如作业、考试、实验、报告、讲座、报名、项目、论文。
""".strip()
