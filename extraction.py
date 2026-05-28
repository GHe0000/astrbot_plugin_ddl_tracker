import asyncio
import json
from time import time

from utils import safe_int, safe_json_loads, format_ts


class ExtractionMixin:
    async def _auto_extract_loop(self):
        while self._running:
            try:
                removed_count = self._purge_expired_from_all_groups()
                if removed_count > 0:
                    from astrbot.api import logger

                    logger.info("[ddl_tracker] auto purged expired ddls=%s", removed_count)
                    self._persist()

                if self._auto_extract_enabled():
                    await self._run_auto_extract_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from astrbot.api import logger

                logger.exception("[ddl_tracker] auto extract loop failed: %s", exc)

            try:
                from constants import AUTO_LOOP_TICK_SECONDS

                await asyncio.sleep(AUTO_LOOP_TICK_SECONDS)
            except asyncio.CancelledError:
                raise

    async def _run_auto_extract_once(self):
        now_ts = int(time())
        interval_minutes = self._auto_extract_interval_minutes()
        interval_seconds = interval_minutes * 60

        for group_id, group_state in list(self.state.items()):
            self._ensure_group_state_fields(group_state)
            if not bool(group_state.get("enabled", False)):
                continue
            if group_id in self._extracting_groups:
                continue

            unified_msg_origin = str(group_state.get("unified_msg_origin") or "")
            if not unified_msg_origin:
                continue

            last_extract_at = safe_int(group_state.get("last_extract_at"), default=0, minimum=0)
            if last_extract_at > 0 and now_ts - last_extract_at < interval_seconds:
                continue

            await self._extract_group_ddls(
                group_id=group_id,
                group_state=group_state,
                unified_msg_origin=unified_msg_origin,
                lookback_minutes=interval_minutes,
                source="auto",
            )

    async def _extract_group_ddls(
        self,
        group_id: str,
        group_state: dict,
        unified_msg_origin: str,
        lookback_minutes: int,
        source: str,
    ) -> dict:
        from astrbot.api import logger

        self._extracting_groups.add(group_id)
        try:
            self._ensure_group_state_fields(group_state)
            self._purge_expired_ddls(group_state)
            prompt_messages = self._select_recent_messages(
                group_state.get("messages", []),
                lookback_minutes=lookback_minutes,
            )
            if not prompt_messages:
                result = {
                    "provider_id": "",
                    "message_count": 0,
                    "extracted_count": 0,
                    "added_count": 0,
                    "updated_count": 0,
                    "parsed_result": {"summary": "没有可供整理的消息", "items": []},
                    "raw_result": "",
                }
                self._update_extract_meta(group_state, source, result)
                self.state[group_id] = group_state
                self._persist()
                return result

            provider_id = await self._get_provider_id(unified_msg_origin)
            if not provider_id:
                result = {
                    "provider_id": "",
                    "message_count": len(prompt_messages),
                    "extracted_count": 0,
                    "added_count": 0,
                    "updated_count": 0,
                    "parsed_result": {"summary": "没有可用的模型 provider", "items": []},
                    "raw_result": "",
                }
                self._update_extract_meta(group_state, source, result)
                self.state[group_id] = group_state
                self._persist()
                return result

            prompt = self._build_ai_extract_prompt(prompt_messages)
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            raw_result = str(getattr(llm_resp, "completion_text", "") or "").strip()
            parsed_result = safe_json_loads(raw_result)
            extracted_count = len(parsed_result.get("items") or [])
            added_count, updated_count = self._merge_ddl_items(
                group_state=group_state,
                parsed_result=parsed_result,
            )
            self._purge_expired_ddls(group_state)
            result = {
                "provider_id": provider_id,
                "message_count": len(prompt_messages),
                "extracted_count": extracted_count,
                "added_count": added_count,
                "updated_count": updated_count,
                "parsed_result": parsed_result,
                "raw_result": raw_result,
            }
            self._update_extract_meta(group_state, source, result)
            self.state[group_id] = group_state
            self._persist()
            logger.info(
                "[ddl_tracker] %s extract group=%s provider=%s message_count=%s extracted=%s added=%s updated=%s",
                source,
                group_id,
                provider_id,
                len(prompt_messages),
                extracted_count,
                added_count,
                updated_count,
            )
            return result
        except Exception as exc:
            logger.exception("[ddl_tracker] %s extract failed group=%s: %s", source, group_id, exc)
            result = {
                "provider_id": "",
                "message_count": 0,
                "extracted_count": 0,
                "added_count": 0,
                "updated_count": 0,
                "parsed_result": {"summary": f"{source} 提取失败", "items": []},
                "raw_result": str(exc),
            }
            self._update_extract_meta(group_state, source, result)
            self.state[group_id] = group_state
            self._persist()
            return result
        finally:
            self._extracting_groups.discard(group_id)

    def _update_extract_meta(self, group_state: dict, source: str, result: dict):
        group_state["last_extract_at"] = int(time())
        group_state["last_extract_source"] = source
        group_state["last_extract_result"] = {
            "provider_id": result.get("provider_id", ""),
            "message_count": result.get("message_count", 0),
            "extracted_count": result.get("extracted_count", 0),
            "added_count": result.get("added_count", 0),
            "updated_count": result.get("updated_count", 0),
            "parsed_result": result.get("parsed_result", {}),
            "raw_result": result.get("raw_result", ""),
        }

    def _build_ai_extract_prompt(self, messages: list[dict]) -> str:
        lines = []
        for index, message in enumerate(messages, start=1):
            sender_name = message.get("sender_name") or message.get("sender_id") or ""
            message_ts = int(message.get("message_ts") or 0)
            timestamp = format_ts(message_ts)
            lines.append(
                f"[{index}][ts={message_ts}][time={timestamp}][{sender_name}] "
                f"{message.get('message_text') or ''}"
            )

        example = {
            "summary": "群里提到了 3 条明确的 DDL。",
            "items": [
                {
                    "message_index": 1,
                    "type": "考试",
                    "title": "线代期中考试",
                    "deadline_text": "2026-04-26 10:00",
                    "normalized_deadline": "2026-04-26 10:00",
                    "source_text": "考试 2026-4-26-10:00",
                },
                {
                    "message_index": 2,
                    "type": "作业",
                    "title": "物理作业",
                    "deadline_text": "一小时后截止",
                    "normalized_deadline": "2026-04-25 23:30",
                    "source_text": "一小时后物理作业截止",
                },
                {
                    "message_index": 3,
                    "type": "作业",
                    "title": "实验报告",
                    "deadline_text": "一周内提交",
                    "normalized_deadline": "2026-05-02 21:30",
                    "source_text": "一周内提交实验报告",
                },
            ],
        }

        return "\n".join(
            [
                self._extract_prompt(),
                f"当前时间：{format_ts(int(time()))}",
                "示例：",
                json.dumps(example, ensure_ascii=False, indent=2),
                "待提取消息：",
                "\n".join(lines),
            ]
        )

    async def _get_provider_id(self, unified_msg_origin: str) -> str:
        from astrbot.api import logger

        configured_provider_id = str(self.config.get("llm_provider_id", "") or "").strip()
        if configured_provider_id:
            return configured_provider_id

        if not unified_msg_origin:
            return ""

        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=unified_msg_origin
            )
        except TypeError:
            provider_id = await self.context.get_current_chat_provider_id(
                unified_msg_origin
            )
        except Exception as exc:
            logger.exception("[ddl_tracker] get provider id failed: %s", exc)
            return ""
        return str(provider_id or "").strip()
