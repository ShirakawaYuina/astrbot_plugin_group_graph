from dataclasses import dataclass
from typing import Literal


@dataclass
class Message:
    msg_id: int
    sender_id: int
    sender_name: str
    content: str
    reply_to_id: int | None
    at_targets: list[int]
    timestamp: int


@dataclass
class MemberNode:
    user_id: int
    display_name: str
    avatar_b64: str
    message_count: int
    activity_score: float
    community_id: int


@dataclass
class RelationEdge:
    source_id: int
    target_id: int
    interaction_score: float
    sentiment: Literal["friendly", "neutral", "tense"]
    label: str
    weight: float


@dataclass
class Community:
    community_id: int
    name: str
    color: str
    member_ids: list[int]


@dataclass
class GraphSummary:
    atmosphere: str
    hot_topics: list[str]
    top3_stories: list[dict]
    loner_ids: list[int]
