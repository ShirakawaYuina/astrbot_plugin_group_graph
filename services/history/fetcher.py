from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger

from ...domain.models import Message


class MessageFetcher:
    """负责按 count / time 两种窗口稳定拉取 OneBot 群历史消息。"""

    BATCH_FETCH_SIZE = 100

    def __init__(self, max_fetch_count: int = 3000):
        self.max_fetch_count = max_fetch_count

    async def _call_history_action(
        self,
        *,
        bot: Any,
        group_id: str,
        count: int,
        message_seq: str | int | None = None,
    ) -> dict | None:
        normalized_group_id = int(group_id) if str(group_id).isdigit() else group_id

        params: dict[str, Any] = {
            "group_id": normalized_group_id,
            "count": count,
            "reverseOrder": True,
        }
        if message_seq is not None:
            params["message_seq"] = (
                int(message_seq) if str(message_seq).isdigit() else message_seq
            )

        if hasattr(bot, "api") and hasattr(bot.api, "call_action"):
            return await bot.api.call_action("get_group_msg_history", **params)
        if hasattr(bot, "call_api"):
            return await bot.call_api("get_group_msg_history", **params)
        if hasattr(bot, "call_action"):
            return await bot.call_action("get_group_msg_history", **params)
        raise AttributeError("bot 未提供可用的 get_group_msg_history 调用入口")

    def _extract_messages(self, response: Any) -> list[dict]:
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
        if not isinstance(response, dict):
            return []

        if isinstance(response.get("messages"), list):
            return [
                item for item in response.get("messages", []) if isinstance(item, dict)
            ]

        data = response.get("data")
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            return [item for item in data.get("messages", []) if isinstance(item, dict)]

        return []

    async def _fetch_history_batch(
        self,
        *,
        bot: Any,
        group_id: str,
        count: int,
        message_seq: str | int | None = None,
    ) -> list[dict]:
        try:
            response = await self._call_history_action(
                bot=bot,
                group_id=group_id,
                count=count,
                message_seq=message_seq,
            )
            return self._extract_messages(response)
        except Exception as exc:
            if message_seq is None:
                logger.info(
                    "群历史首次拉取失败，尝试使用 0 号锚点重试 群组=%s 错误=%s",
                    group_id,
                    exc,
                )
                try:
                    retry_response = await self._call_history_action(
                        bot=bot,
                        group_id=group_id,
                        count=count,
                        message_seq=0,
                    )
                    return self._extract_messages(retry_response)
                except Exception as retry_exc:
                    logger.error(f"拉取消息失败: {retry_exc}")
                    return []
            logger.error(f"拉取消息失败: {exc}")
            return []

    def _resolve_next_message_seq(
        self,
        current_batch: list[dict],
        previous_seq: str | int | None,
    ) -> str | int | None:
        if not current_batch:
            return None

        first_message = current_batch[0]
        last_message = current_batch[-1]
        if int(first_message.get("time", 0) or 0) <= int(
            last_message.get("time", 0) or 0
        ):
            earliest_message = first_message
        else:
            earliest_message = last_message

        next_seq = (
            earliest_message.get("message_seq")
            or earliest_message.get("real_id")
            or earliest_message.get("seq")
            or earliest_message.get("message_id")
        )
        if next_seq in ("", None, previous_seq):
            return None
        return next_seq

    def _build_message(self, message_data: dict) -> Message | None:
        sender = (
            message_data.get("sender")
            if isinstance(message_data.get("sender"), dict)
            else {}
        )
        sender_id = int(sender.get("user_id", 0) or 0)
        if sender_id <= 0:
            return None

        sender_name = str(
            sender.get("card") or sender.get("nickname") or sender_id
        ).strip()

        raw_message = message_data.get("message")
        message_segments = raw_message if isinstance(raw_message, list) else []

        content_parts: list[str] = []
        at_targets: list[int] = []
        reply_to_id: int | None = None

        for seg in message_segments:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type", "")).lower()
            seg_data = seg.get("data") if isinstance(seg.get("data"), dict) else {}

            if seg_type == "text":
                content_parts.append(str(seg_data.get("text", "") or ""))
                continue
            if seg_type == "at":
                qq = str(seg_data.get("qq", "") or "").strip()
                if qq and qq != "all" and qq.isdigit():
                    at_targets.append(int(qq))
                continue
            if seg_type == "reply":
                reply_candidate = (
                    seg_data.get("message_id")
                    or seg_data.get("id")
                    or seg_data.get("reply")
                )
                if str(reply_candidate or "").strip().isdigit():
                    reply_to_id = int(str(reply_candidate).strip())

        if isinstance(raw_message, str) and raw_message.strip():
            content_parts.append(raw_message.strip())

        content = "".join(content_parts).strip()
        if not content:
            return None

        return Message(
            msg_id=int(message_data.get("message_id", 0) or 0),
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
            reply_to_id=reply_to_id,
            at_targets=at_targets,
            timestamp=int(message_data.get("time", 0) or 0),
        )

    async def fetch_messages(
        self, event, group_id: str, range_type: str, range_value: int
    ) -> list[Message]:
        bot = getattr(event, "bot", None)
        if bot is None:
            return []

        cutoff_timestamp: int | None = None
        target_count: int | None = None
        if range_type == "time":
            cutoff_time = datetime.now() - timedelta(days=max(range_value, 0))
            cutoff_timestamp = int(cutoff_time.timestamp())
        else:
            target_count = max(1, min(range_value, self.max_fetch_count))

        messages: list[Message] = []
        seen_message_ids: set[int] = set()
        message_seq: str | int | None = None
        total_seen = 0

        # 时间模式必须完全由截止时间控制，不能再被内部条数上限提前截断；
        # 否则像“30d”这种命令会在高活跃群里只拿到最近几天的数据。
        while True:
            fetch_count = self.BATCH_FETCH_SIZE
            if target_count is not None:
                fetch_count = min(fetch_count, max(target_count - len(messages), 1))

            batch = await self._fetch_history_batch(
                bot=bot,
                group_id=group_id,
                count=fetch_count,
                message_seq=message_seq,
            )
            if not batch:
                break

            reached_cutoff = False
            for message_data in batch:
                message_id = int(message_data.get("message_id", 0) or 0)
                if message_id and message_id in seen_message_ids:
                    continue
                if message_id:
                    seen_message_ids.add(message_id)

                total_seen += 1
                message_time = int(message_data.get("time", 0) or 0)
                if cutoff_timestamp is not None and message_time < cutoff_timestamp:
                    reached_cutoff = True
                    continue

                normalized_message = self._build_message(message_data)
                if normalized_message is not None:
                    messages.append(normalized_message)

                if target_count is not None and len(messages) >= target_count:
                    return messages[:target_count]

            if reached_cutoff:
                break

            next_message_seq = self._resolve_next_message_seq(batch, message_seq)
            if next_message_seq is None:
                break

            message_seq = next_message_seq
            await asyncio.sleep(0.1)

        return messages[:target_count] if target_count is not None else messages
