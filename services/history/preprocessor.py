from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ...domain.models import MemberNode, Message


@dataclass
class ProcessedData:
    messages: list[Message]
    members: list[MemberNode]
    paired_messages: dict[tuple[int, int], list[str]]
    total_speaking_members: int
    filtered_out_member_count: int


class MessagePreprocessor:
    """负责过滤无效消息并为后续关系分析准备成员与样本数据。"""

    def __init__(
        self,
        min_message_count: int = 3,
        continuous_reply_window_sec: int = 120,
        bot_user_id: int | None = None,
        include_bot_messages: bool = False,
    ):
        self.min_message_count = min_message_count
        self.continuous_reply_window_sec = continuous_reply_window_sec
        self.bot_user_id = bot_user_id
        self.include_bot_messages = include_bot_messages

    def _is_valid_message(self, message: Message) -> bool:
        """过滤机器人自身消息与空文本，避免污染后续统计。"""
        # 默认仍然过滤机器人自身消息，避免把播报、提示词或命令回执误算成成员互动；
        # 只有显式打开配置后，才允许机器人消息参与后续关系分析。
        if (
            not self.include_bot_messages
            and self.bot_user_id is not None
            and message.sender_id == self.bot_user_id
        ):
            return False
        return bool(message.content.strip())

    def process(self, messages: list[Message]) -> ProcessedData:
        valid_messages = [
            message for message in messages if self._is_valid_message(message)
        ]

        member_counts: dict[int, int] = defaultdict(int)
        member_names: dict[int, str] = {}
        for message in valid_messages:
            member_counts[message.sender_id] += 1
            if message.sender_name and (
                message.sender_id not in member_names
                or member_names[message.sender_id] == str(message.sender_id)
            ):
                member_names[message.sender_id] = message.sender_name

        filtered_member_ids = {
            user_id
            for user_id, count in member_counts.items()
            if count >= self.min_message_count
        }
        total_speaking_members = len(member_counts)
        filtered_out_member_count = total_speaking_members - len(filtered_member_ids)

        max_message_count = (
            max(member_counts[user_id] for user_id in filtered_member_ids)
            if filtered_member_ids
            else 1
        )
        member_nodes = [
            MemberNode(
                user_id=user_id,
                display_name=member_names.get(user_id, str(user_id)),
                avatar_b64="",
                message_count=member_counts[user_id],
                activity_score=member_counts[user_id] / max_message_count,
                community_id=0,
            )
            for user_id in sorted(
                filtered_member_ids,
                key=lambda current_user_id: (
                    -member_counts[current_user_id],
                    current_user_id,
                ),
            )
        ]

        message_by_id = {
            message.msg_id: message
            for message in valid_messages
            if message.msg_id and message.sender_id in filtered_member_ids
        }
        paired_messages: dict[tuple[int, int], list[str]] = defaultdict(list)

        for current_index, message in enumerate(valid_messages):
            if message.sender_id not in filtered_member_ids:
                continue

            for target_id in message.at_targets:
                if target_id in filtered_member_ids and target_id != message.sender_id:
                    pair_key = tuple(sorted((message.sender_id, target_id)))
                    paired_messages[pair_key].append(
                        f"{member_names.get(message.sender_id, '某成员')}: {message.content[:100]}"
                    )

            if message.reply_to_id and message.reply_to_id in message_by_id:
                replied_message = message_by_id[message.reply_to_id]
                if replied_message.sender_id != message.sender_id:
                    pair_key = tuple(
                        sorted((message.sender_id, replied_message.sender_id))
                    )
                    paired_messages[pair_key].append(
                        f"{member_names.get(message.sender_id, '某成员')}: {message.content[:100]}"
                    )

            if current_index <= 0:
                continue

            previous_message = valid_messages[current_index - 1]
            if previous_message.sender_id not in filtered_member_ids:
                continue
            if previous_message.sender_id == message.sender_id:
                continue
            if (
                message.timestamp - previous_message.timestamp
                > self.continuous_reply_window_sec
            ):
                continue

            pair_key = tuple(sorted((message.sender_id, previous_message.sender_id)))
            rendered_sample = f"{member_names.get(message.sender_id, '某成员')}: {message.content[:100]}"
            if rendered_sample not in paired_messages[pair_key]:
                paired_messages[pair_key].append(rendered_sample)

        return ProcessedData(
            messages=[
                message
                for message in valid_messages
                if message.sender_id in filtered_member_ids
            ],
            members=member_nodes,
            paired_messages=dict(paired_messages),
            total_speaking_members=total_speaking_members,
            filtered_out_member_count=filtered_out_member_count,
        )
