from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context

from ...domain.models import MemberNode, RelationEdge
from .llm_response_guard import request_json_from_provider, summarize_llm_exception


class LLMRelationAnalyzer:
    """根据高互动成员对的聊天样本补充情感倾向与关系标签。"""

    def __init__(
        self,
        context: Context,
        relation_threshold: float = 0.1,
        relation_batch_size: int = 5,
    ):
        self.context = context
        self.relation_threshold = relation_threshold
        self.relation_batch_size = max(1, int(relation_batch_size or 5))

    async def analyze(
        self,
        interaction_matrix: dict[tuple[int, int], dict[str, Any]],
        members: list[MemberNode],
        paired_messages: dict[tuple[int, int], list[str]],
        provider=None,
    ) -> list[RelationEdge]:
        member_map = {member.user_id: member for member in members}
        high_score_pairs = [
            (pair, data)
            for pair, data in interaction_matrix.items()
            if data.get("normalized", 0) >= self.relation_threshold
        ]
        low_score_pairs = [
            (pair, data)
            for pair, data in interaction_matrix.items()
            if data.get("normalized", 0) < self.relation_threshold
        ]
        high_score_pairs.sort(
            key=lambda current_item: current_item[1].get("normalized", 0),
            reverse=True,
        )

        edges: list[RelationEdge] = []
        # 单次喂给 LLM 的关系对数量需要可配置，便于在效果与成本之间做权衡。
        for start_index in range(0, len(high_score_pairs), self.relation_batch_size):
            current_batch = high_score_pairs[
                start_index : start_index + self.relation_batch_size
            ]
            edges.extend(
                await self._analyze_batch(
                    batch=current_batch,
                    member_map=member_map,
                    paired_messages=paired_messages,
                    provider=provider,
                )
            )

        for pair, data in low_score_pairs:
            if pair[0] in member_map and pair[1] in member_map:
                edges.append(self._build_default_edge(pair, data, label="偶有互动"))

        return edges

    async def _analyze_batch(
        self,
        *,
        batch: list[tuple[tuple[int, int], dict[str, Any]]],
        member_map: dict[int, MemberNode],
        paired_messages: dict[tuple[int, int], list[str]],
        provider=None,
    ) -> list[RelationEdge]:
        prompt_items: list[str] = []
        pair_index_map: dict[int, tuple[int, int]] = {}

        for pair_index, (pair, data) in enumerate(batch, start=1):
            member_a = member_map.get(pair[0])
            member_b = member_map.get(pair[1])
            if member_a is None or member_b is None:
                continue

            pair_index_map[pair_index] = pair
            rendered_messages = paired_messages.get(tuple(sorted(pair))) or [
                f"{speaker}: {text}" for speaker, text in data.get("messages", [])[:20]
            ]
            message_lines = rendered_messages[:20] or ["无直接对话记录。"]
            prompt_items.append(
                "\n".join(
                    [
                        f"关系对#{pair_index}",
                        f"成员A: {member_a.display_name} (user_id={member_a.user_id})",
                        f"成员B: {member_b.display_name} (user_id={member_b.user_id})",
                        "对话片段:",
                        *[f"- {line}" for line in message_lines],
                    ]
                )
            )

        if not prompt_items:
            return [
                self._build_default_edge(pair, data)
                for pair, data in batch
                if pair[0] in member_map and pair[1] in member_map
            ]

        prompt = f"""你是一个群聊关系分析专家。请分析以下成员对的关系，只返回 JSON 数组，不要输出其他内容。

{chr(10).join(prompt_items)}

返回格式如下：
[
  {{
    "pair_index": 1,
    "sentiment": "friendly" | "neutral" | "tense",
    "label": "不超过8个字的关系描述"
  }}
]
"""

        try:
            active_provider = provider or self.context.get_using_provider()
            if not active_provider:
                return [self._build_default_edge(pair, data) for pair, data in batch]

            parsed_results = await request_json_from_provider(
                provider=active_provider,
                prompt=prompt,
                task_name="LLM关系分析",
            )
            result_map = {
                int(item.get("pair_index")): item
                for item in parsed_results
                if isinstance(item, dict) and str(item.get("pair_index", "")).isdigit()
            }
            edges: list[RelationEdge] = []
            for pair_index, (pair, data) in enumerate(batch, start=1):
                matched_result = result_map.get(pair_index)
                if matched_result is None:
                    edges.append(self._build_default_edge(pair, data))
                    continue
                edges.append(
                    RelationEdge(
                        source_id=pair[0],
                        target_id=pair[1],
                        interaction_score=data.get("normalized", 0),
                        sentiment=matched_result.get("sentiment", "neutral"),
                        label=str(matched_result.get("label", "有互动")).strip()
                        or "有互动",
                        weight=data.get("normalized", 0),
                    )
                )
            return edges
        except Exception as exc:
            logger.error(f"LLM关系分析失败: {summarize_llm_exception(exc)}")
            return [self._build_default_edge(pair, data) for pair, data in batch]

    def _build_default_edge(
        self,
        pair: tuple[int, int],
        data: dict[str, Any],
        label: str = "有互动",
    ) -> RelationEdge:
        return RelationEdge(
            source_id=pair[0],
            target_id=pair[1],
            interaction_score=data.get("normalized", 0),
            sentiment="neutral",
            label=label,
            weight=data.get("normalized", 0),
        )
