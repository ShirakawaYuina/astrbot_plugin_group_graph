from collections import defaultdict
from typing import Any

from ...domain.models import Message


class InteractionCounter:
    def __init__(self, continuous_reply_window_sec: int = 120):
        self.continuous_reply_window_sec = continuous_reply_window_sec

    def count(self, messages: list[Message]) -> dict[tuple[int, int], dict[str, Any]]:
        directed_scores: dict[tuple[int, int], float] = defaultdict(float)
        paired_messages: dict[tuple[int, int], list[tuple[str, str]]] = defaultdict(
            list
        )

        msg_by_id = {msg.msg_id: msg for msg in messages}
        messages_sorted = sorted(messages, key=lambda m: m.timestamp)

        for i, msg in enumerate(messages_sorted):
            for target_id in msg.at_targets:
                if target_id != msg.sender_id:
                    key = (msg.sender_id, target_id)
                    directed_scores[key] += 3.0
                    paired_messages[key].append((msg.sender_name, msg.content[:100]))

            if msg.reply_to_id and msg.reply_to_id in msg_by_id:
                replied_msg = msg_by_id[msg.reply_to_id]
                if replied_msg.sender_id != msg.sender_id:
                    key = (msg.sender_id, replied_msg.sender_id)
                    directed_scores[key] += 3.0
                    paired_messages[key].append((msg.sender_name, msg.content[:100]))

            if i > 0:
                prev_msg = messages_sorted[i - 1]
                time_diff = msg.timestamp - prev_msg.timestamp
                if (
                    time_diff <= self.continuous_reply_window_sec
                    and prev_msg.sender_id != msg.sender_id
                ):
                    key = (msg.sender_id, prev_msg.sender_id)
                    directed_scores[key] += 1.0
                    paired_messages[key].append((msg.sender_name, msg.content[:100]))

        interaction_matrix: dict[tuple[int, int], dict[str, Any]] = {}
        all_pairs = set()
        for a, b in directed_scores.keys():
            all_pairs.add(tuple(sorted([a, b])))

        for pair in all_pairs:
            score_ab = directed_scores.get((pair[0], pair[1]), 0.0)
            score_ba = directed_scores.get((pair[1], pair[0]), 0.0)
            raw_score = score_ab + score_ba

            msgs = paired_messages.get((pair[0], pair[1]), []) + paired_messages.get(
                (pair[1], pair[0]), []
            )

            interaction_matrix[pair] = {
                "score": raw_score,
                "score_ab": score_ab,
                "score_ba": score_ba,
                "messages": msgs[:20],
            }

        max_score = (
            max(entry["score"] for entry in interaction_matrix.values())
            if interaction_matrix
            else 1.0
        )

        for key in interaction_matrix:
            interaction_matrix[key]["normalized"] = (
                interaction_matrix[key]["score"] / max_score
            )

        return interaction_matrix
