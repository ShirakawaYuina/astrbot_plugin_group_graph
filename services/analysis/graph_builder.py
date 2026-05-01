from typing import Any

from ...domain.models import Community, GraphSummary, MemberNode, RelationEdge


class RelationGraphBuilder:
    def build(
        self,
        members: list[MemberNode],
        edges: list[RelationEdge],
        communities: list[Community],
        loner_ids: list[int],
        summary: GraphSummary,
    ) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "id": m.user_id,
                    "name": m.display_name,
                    "avatar": m.avatar_b64 or "",
                    "activity_score": m.activity_score,
                    "message_count": m.message_count,
                    "community": m.community_id,
                }
                for m in members
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
                for edge in edges
            ],
            "communities": [
                {"id": c.community_id, "name": c.name, "color": c.color}
                for c in communities
            ],
            "loner_ids": loner_ids,
            "summary": {
                "atmosphere": summary.atmosphere,
                "hot_topics": summary.hot_topics,
                "top3_stories": summary.top3_stories,
            },
        }
