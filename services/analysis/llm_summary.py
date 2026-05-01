from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context

from ...domain.models import Community, MemberNode, RelationEdge
from .community import CommunityDetector
from .llm_response_guard import request_json_from_provider, summarize_llm_exception


class LLMSummaryAnalyzer:
    """汇总小团体、热点话题与关系故事，并构建最终图谱数据。"""

    def __init__(
        self,
        context: Context,
        loner_score_threshold: float = 0.15,
        community_edge_threshold: float = 0.16,
        max_llm_communities: int = 4,
        max_hot_topics: int = 10,
    ):
        self.context = context
        self.max_llm_communities = max(1, int(max_llm_communities))
        self.max_hot_topics = max(1, int(max_hot_topics))
        self.community_detector = CommunityDetector(
            loner_score_threshold=loner_score_threshold,
            community_edge_threshold=community_edge_threshold,
        )

    def _select_visible_member_ids(
        self, members: list[MemberNode], relation_edges: list[RelationEdge], top_n: int
    ) -> set[int]:
        """按互动总分排序，选出最终入图的成员。"""

        if top_n <= 0 or top_n >= len(members):
            return {member.user_id for member in members}

        interaction_scores: dict[int, float] = {
            member.user_id: 0.0 for member in members
        }
        message_count_map = {member.user_id: member.message_count for member in members}
        for edge in relation_edges:
            interaction_scores[edge.source_id] = (
                interaction_scores.get(edge.source_id, 0.0) + edge.interaction_score
            )
            interaction_scores[edge.target_id] = (
                interaction_scores.get(edge.target_id, 0.0) + edge.interaction_score
            )

        sorted_member_ids = sorted(
            interaction_scores,
            key=lambda user_id: (
                -interaction_scores[user_id],
                -message_count_map.get(user_id, 0),
                user_id,
            ),
        )
        return set(sorted_member_ids[:top_n])

    def _limit_loner_edges(
        self,
        *,
        visible_edges: list[RelationEdge],
        visible_loner_id_set: set[int],
        member_map: dict[int, MemberNode],
    ) -> list[RelationEdge]:
        """每个游离成员只保留一条得分最高的连接线，减少图谱杂乱度。"""

        if not visible_loner_id_set:
            return visible_edges

        candidate_edges_by_loner: dict[int, list[RelationEdge]] = {
            user_id: [] for user_id in visible_loner_id_set
        }
        non_loner_edges: list[RelationEdge] = []

        for edge in visible_edges:
            loner_ids_in_edge = [
                user_id
                for user_id in (edge.source_id, edge.target_id)
                if user_id in visible_loner_id_set
            ]
            if not loner_ids_in_edge:
                non_loner_edges.append(edge)
                continue

            for loner_id in loner_ids_in_edge:
                candidate_edges_by_loner.setdefault(loner_id, []).append(edge)

        unique_loner_edges = self._assign_unique_loner_edges(
            candidate_edges_by_loner=candidate_edges_by_loner,
            visible_loner_id_set=visible_loner_id_set,
            member_map=member_map,
        )
        return non_loner_edges + unique_loner_edges

    def _assign_unique_loner_edges(
        self,
        *,
        candidate_edges_by_loner: dict[int, list[RelationEdge]],
        visible_loner_id_set: set[int],
        member_map: dict[int, MemberNode],
    ) -> list[RelationEdge]:
        """为游离成员分配唯一连接，允许回退到次优边以避免最终出现多条连接。"""

        sorted_loner_ids = sorted(
            visible_loner_id_set,
            key=lambda loner_id: self._build_best_available_loner_sort_key(
                loner_id=loner_id,
                candidate_edges=candidate_edges_by_loner.get(loner_id, []),
                member_map=member_map,
            ),
            reverse=True,
        )

        occupied_loner_ids: set[int] = set()
        added_edge_keys: set[tuple[int, int]] = set()
        selected_edges: list[RelationEdge] = []

        for loner_id in sorted_loner_ids:
            if loner_id in occupied_loner_ids:
                continue

            sorted_candidates = sorted(
                candidate_edges_by_loner.get(loner_id, []),
                key=lambda edge: self._build_loner_edge_sort_key(
                    edge=edge,
                    loner_id=loner_id,
                    member_map=member_map,
                ),
                reverse=True,
            )
            for edge in sorted_candidates:
                loner_ids_in_edge = [
                    user_id
                    for user_id in (edge.source_id, edge.target_id)
                    if user_id in visible_loner_id_set
                ]
                if any(user_id in occupied_loner_ids for user_id in loner_ids_in_edge):
                    continue

                edge_key = tuple(sorted((edge.source_id, edge.target_id)))
                if edge_key not in added_edge_keys:
                    selected_edges.append(edge)
                    added_edge_keys.add(edge_key)
                occupied_loner_ids.update(loner_ids_in_edge)
                break

        return selected_edges

    def _build_best_available_loner_sort_key(
        self,
        *,
        loner_id: int,
        candidate_edges: list[RelationEdge],
        member_map: dict[int, MemberNode],
    ) -> tuple[float, int, int]:
        """决定游离成员的分配顺序，优先处理拥有更强主连接的成员。"""

        if not candidate_edges:
            return (-1.0, -1, -loner_id)

        best_edge = max(
            candidate_edges,
            key=lambda edge: self._build_loner_edge_sort_key(
                edge=edge,
                loner_id=loner_id,
                member_map=member_map,
            ),
        )
        return self._build_loner_edge_sort_key(
            edge=best_edge,
            loner_id=loner_id,
            member_map=member_map,
        )

    def _build_loner_edge_sort_key(
        self,
        *,
        edge: RelationEdge,
        loner_id: int,
        member_map: dict[int, MemberNode],
    ) -> tuple[float, int, int]:
        """为游离成员边提供稳定排序，确保相同数据下结果可复现。"""

        partner_id = edge.target_id if edge.source_id == loner_id else edge.source_id
        partner_message_count = member_map.get(
            partner_id, MemberNode(partner_id, "", "", 0, 0.0, -1)
        ).message_count
        return (
            edge.interaction_score,
            partner_message_count,
            -partner_id,
        )

    def _limit_communities_for_llm(
        self,
        communities: list[Community],
        member_map: dict[int, MemberNode],
    ) -> list[Community]:
        """限制传给 LLM 的社群数量上限，但不裁剪最终展示的社群。"""

        sorted_communities = sorted(
            communities,
            key=lambda current_community: (
                -len(current_community.member_ids),
                -sum(
                    member_map.get(
                        user_id, MemberNode(user_id, "", "", 0, 0.0, -1)
                    ).message_count
                    for user_id in current_community.member_ids
                ),
                current_community.community_id,
            ),
        )
        return sorted_communities[: self.max_llm_communities]

    def _resolve_llm_community_name(
        self,
        *,
        community_id: int,
        summary_result: dict[str, Any],
    ) -> str:
        """兼容 LLM 返回的多种团体编号格式，避免已命名结果无法回填。"""

        community_name_map = summary_result.get("community_names", {})
        if not isinstance(community_name_map, dict):
            return ""

        candidate_keys = (
            str(community_id),
            f"团体{community_id}",
        )
        for candidate_key in candidate_keys:
            renamed = community_name_map.get(candidate_key)
            if renamed:
                return str(renamed)
        return ""

    async def analyze(
        self,
        members: list[MemberNode],
        messages: list,
        interaction_matrix: dict[tuple[int, int], dict[str, Any]],
        relation_edges: list[RelationEdge],
        top_n: int = 20,
        provider=None,
    ) -> dict[str, Any]:
        """产出模板所需的最终图谱数据。"""

        del interaction_matrix

        communities, loner_ids = self.community_detector.detect(members, relation_edges)
        member_map = {member.user_id: member for member in members}

        visible_member_ids = self._select_visible_member_ids(
            members=members,
            relation_edges=relation_edges,
            top_n=top_n,
        )
        visible_loner_id_set = {
            user_id for user_id in loner_ids if user_id in visible_member_ids
        }
        visible_members = [
            member
            for member in sorted(
                members,
                key=lambda current_member: (
                    -current_member.message_count,
                    current_member.user_id,
                ),
            )
            if member.user_id in visible_member_ids
        ]
        visible_edges = [
            edge
            for edge in relation_edges
            if edge.source_id in visible_member_ids
            and edge.target_id in visible_member_ids
            and (
                edge.interaction_score >= 0.1
                or edge.source_id in visible_loner_id_set
                or edge.target_id in visible_loner_id_set
            )
        ]
        visible_edges = self._limit_loner_edges(
            visible_edges=visible_edges,
            visible_loner_id_set=visible_loner_id_set,
            member_map=member_map,
        )

        visible_communities: list[Community] = []
        for community in communities:
            visible_member_list = [
                user_id
                for user_id in community.member_ids
                if user_id in visible_member_ids
            ]
            if not visible_member_list:
                continue
            visible_communities.append(
                Community(
                    community_id=community.community_id,
                    name=community.name,
                    color=community.color,
                    member_ids=visible_member_list,
                )
            )

        visible_loner_ids = sorted(visible_loner_id_set)

        member_by_community: dict[int, list[MemberNode]] = {}
        for member in members:
            member_by_community.setdefault(member.community_id, []).append(member)

        top_edges = sorted(
            relation_edges,
            key=lambda edge: edge.interaction_score,
            reverse=True,
        )[:5]
        community_messages: dict[int, list[str]] = {}
        for community_id, community_members in member_by_community.items():
            if community_id == -1:
                continue
            community_member_ids = {member.user_id for member in community_members}
            rendered_messages = [
                f"{message.sender_name}: {message.content[:50]}"
                for message in messages
                if message.sender_id in community_member_ids
            ]
            community_messages[community_id] = rendered_messages[:10]

        llm_communities = self._limit_communities_for_llm(
            communities=communities,
            member_map=member_map,
        )
        summary_result = await self._generate_summary(
            communities=llm_communities,
            community_messages=community_messages,
            top_edges=top_edges,
            member_map=member_map,
            provider=provider,
        )

        for community in visible_communities:
            renamed = self._resolve_llm_community_name(
                community_id=community.community_id,
                summary_result=summary_result,
            )
            if renamed:
                community.name = renamed

        return {
            "nodes": [
                {
                    "id": member.user_id,
                    "name": member.display_name,
                    "avatar": member.avatar_b64 or "",
                    "activity_score": member.activity_score,
                    "message_count": member.message_count,
                    "community": member.community_id,
                }
                for member in visible_members
            ],
            "links": [
                {
                    "source": edge.source_id,
                    "target": edge.target_id,
                    "weight": edge.weight,
                    "interaction_score": edge.interaction_score,
                    "sentiment": edge.sentiment,
                    "label": edge.label,
                }
                for edge in visible_edges
            ],
            "communities": [
                {
                    "id": community.community_id,
                    "name": community.name,
                    "color": community.color,
                }
                for community in visible_communities
            ],
            "loner_ids": visible_loner_ids,
            "summary": {
                "atmosphere": summary_result.get(
                    "atmosphere", "群氛围良好，成员互动积极"
                ),
                "hot_topics": summary_result.get("hot_topics", []),
                "community_names": summary_result.get("community_names", {}),
                "top3_stories": summary_result.get("top3_stories", []),
            },
        }

    async def _generate_summary(
        self,
        *,
        communities: list[Community],
        community_messages: dict[int, list[str]],
        top_edges: list[RelationEdge],
        member_map: dict[int, MemberNode],
        provider=None,
    ) -> dict[str, Any]:
        """调用 LLM 总结群氛围、热点与代表性关系故事。"""

        community_lines = []
        for community in communities:
            rendered_names = [
                member_map[user_id].display_name
                for user_id in community.member_ids
                if user_id in member_map
            ]
            community_lines.append(
                f"团体{community.community_id}: {', '.join(rendered_names)}"
            )

        community_message_lines = []
        for community_id, rendered_messages in community_messages.items():
            if rendered_messages:
                community_message_lines.append(
                    f"团体{community_id}消息:\n" + "\n".join(rendered_messages[:10])
                )

        top_pair_lines = []
        for edge in top_edges:
            source_name = member_map.get(
                edge.source_id, MemberNode(0, "未知", "", 0, 0.0, 0)
            ).display_name
            target_name = member_map.get(
                edge.target_id, MemberNode(0, "未知", "", 0, 0.0, 0)
            ).display_name
            top_pair_lines.append(
                f"{source_name} -> {target_name}（得分：{edge.interaction_score:.2f}）：{edge.label}"
            )

        prompt = f"""你是一个群聊分析专家。以下是一个 QQ 群的关系数据和消息样本，请只返回 JSON。
你最多只需要分析 {self.max_llm_communities} 个社群，这是上限，不要求必须输出该数量。
热聊话题最多输出 {self.max_hot_topics} 个，实际可以少于这个数量，请只输出真正有代表性的主题。

群成员及所属团体：
{chr(10).join(community_lines) if community_lines else "无团体信息"}

各团体代表性消息：
{chr(10).join(community_message_lines) if community_message_lines else "无消息样本"}

互动得分最高的成员对（Top5）：
{chr(10).join(top_pair_lines) if top_pair_lines else "无高互动成员对"}

返回格式：
{{
  "atmosphere": "100~200字，深入描述群氛围、互动风格、核心关系结构和整体情绪走向",
  "hot_topics": ["话题1", "话题2", "话题3"],
  "community_names": {{"0": "团体名称（2~6字）"}},
  "top3_stories": [
    {{"name_a": "...", "name_b": "...", "score": 0.0, "story": "60~120字，说明他们为什么关系强、常见互动方式、发生过什么典型故事或相处模式"}}
  ]
}}
其中 community_names 的键请直接使用数字字符串，如 "0"、"1"、"2"，不要写成 "团体0"。
"""

        try:
            active_provider = provider or self.context.get_using_provider()
            if not active_provider:
                return self._default_summary()

            return await request_json_from_provider(
                provider=active_provider,
                prompt=prompt,
                task_name="LLM摘要分析",
            )
        except Exception as exc:  # noqa: BLE001 - 这里需要兼容 provider/SDK 异常
            logger.error(f"LLM摘要分析失败: {summarize_llm_exception(exc)}")
            return self._default_summary()

    def _default_summary(self) -> dict[str, Any]:
        """当 LLM 不可用时，返回稳定的默认摘要。"""

        return {
            "atmosphere": (
                "群内整体交流比较活跃，成员之间已经形成较稳定的互动圈层。"
                "日常聊天和固定话题会反复出现，强关系成员之间往往能持续接话、互相调侃或高频回应，"
                "说明群聊氛围偏熟人化，既有固定核心圈，也保留了一定的开放参与感。"
            ),
            "hot_topics": ["日常聊天", "技术讨论", "摸鱼交流"][: self.max_hot_topics],
            "community_names": {},
            "top3_stories": [],
        }
