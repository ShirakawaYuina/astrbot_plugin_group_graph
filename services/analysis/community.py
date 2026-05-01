from __future__ import annotations

import networkx as nx

from astrbot.api import logger

from ...domain.models import Community, MemberNode, RelationEdge

try:
    import igraph as ig
except Exception:  # noqa: BLE001 - 可选依赖缺失时需要优雅回退
    ig = None

try:
    import leidenalg
except Exception:  # noqa: BLE001 - 可选依赖缺失时需要优雅回退
    leidenalg = None

COMMUNITY_COLORS = [
    "#a855f7",
    "#3b82f6",
    "#ef4444",
    "#10b981",
    "#f59e0b",
    "#ec4899",
    "#06b6d4",
]


class CommunityDetector:
    """根据互动边识别小团体，并单独标记游离成员。"""

    def __init__(
        self,
        loner_score_threshold: float = 0.15,
        community_edge_threshold: float = 0.16,
    ):
        self.loner_score_threshold = loner_score_threshold
        self.community_edge_threshold = community_edge_threshold

    def detect(
        self, members: list[MemberNode], edges: list[RelationEdge]
    ) -> tuple[list[Community], list[int]]:
        """构建社区图，并返回识别到的小团体与游离成员。"""

        graph, retained_edge_count = self._build_graph(
            member_ids=[member.user_id for member in members],
            edges=edges,
            edge_threshold=self.community_edge_threshold,
        )
        raw_communities, algorithm_name = self._detect_raw_communities(
            graph=graph,
            member_count=len(members),
            input_edge_count=len(edges),
            retained_edge_count=retained_edge_count,
        )
        refined_communities = self._refine_large_communities(
            graph=graph,
            edges=edges,
            raw_communities=raw_communities,
        )

        member_community_map: dict[int, int] = {}
        valid_communities: list[set[int]] = []

        for community_members in refined_communities:
            normalized_members = sorted(set(community_members))
            if len(normalized_members) <= 1:
                for user_id in normalized_members:
                    member_community_map[user_id] = -1
                continue

            # 先尊重 Leiden 的结构划分结果：
            # 1. 单点社区直接视为游离成员；
            # 2. 2 人极小碎片只有在内部连接本身也偏弱时才整体转为游离；
            # 3. 更大的社区只把“落在该社区里但总边权明显不足”的边缘成员修正为游离。
            loner_member_ids = self._identify_loner_members(
                graph=graph,
                community_members=normalized_members,
            )
            retained_members = [
                user_id
                for user_id in normalized_members
                if user_id not in loner_member_ids
            ]

            for user_id in loner_member_ids:
                member_community_map[user_id] = -1

            if len(retained_members) <= 1:
                for user_id in retained_members:
                    member_community_map[user_id] = -1
                continue

            valid_communities.append(set(retained_members))
            community_id = len(valid_communities) - 1
            for user_id in retained_members:
                member_community_map[user_id] = community_id

        for member in members:
            member_community_map.setdefault(member.user_id, -1)

        loner_ids = sorted(
            user_id
            for user_id, community_id in member_community_map.items()
            if community_id == -1
        )
        for member in members:
            member.community_id = member_community_map.get(member.user_id, -1)

        communities = [
            Community(
                community_id=index,
                name=f"团体{index + 1}",
                color=COMMUNITY_COLORS[index % len(COMMUNITY_COLORS)],
                member_ids=sorted(community_members),
            )
            for index, community_members in enumerate(
                sorted(
                    valid_communities,
                    key=lambda current_members: (
                        -len(current_members),
                        tuple(sorted(current_members)),
                    ),
                )
            )
        ]

        self._normalize_community_ids(members=members, communities=communities)
        logger.info(
            "[GroupGraph][Community] 小团体识别完成 "
            f"成员数={len(members)} 输入边数={len(edges)} "
            f"入社区图边数={retained_edge_count} 社区阈值={self.community_edge_threshold} "
            f"游离阈值={self.loner_score_threshold} 算法={algorithm_name} "
            f"小团体数={len(communities)} 游离成员数={len(loner_ids)}"
        )
        return communities, loner_ids

    def _identify_loner_members(
        self,
        *,
        graph,
        community_members: list[int],
    ) -> set[int]:
        """基于 Leiden 社区结果识别游离成员，而不是单纯按总边权粗暴裁切。"""

        if len(community_members) <= 1:
            return set(community_members)

        if len(community_members) == 2:
            source_id, target_id = community_members
            edge_payload = graph[source_id].get(target_id, {})
            internal_weight = float(edge_payload.get("weight", 0.0))
            if internal_weight < self.loner_score_threshold:
                return set(community_members)
            return set()

        return {
            user_id
            for user_id in community_members
            if self._calculate_member_edge_weight(graph, user_id)
            < self.loner_score_threshold
        }

    def _build_graph(
        self,
        *,
        member_ids: list[int],
        edges: list[RelationEdge],
        edge_threshold: float,
    ) -> tuple[nx.Graph, int]:
        """按照边阈值构建社区图，供社区识别与二次拆分共用。"""

        graph = nx.Graph()
        graph.add_nodes_from(member_ids)

        retained_edge_count = 0
        for edge in edges:
            if edge.interaction_score < edge_threshold:
                continue
            graph.add_edge(
                edge.source_id,
                edge.target_id,
                weight=edge.interaction_score,
            )
            retained_edge_count += 1
        return graph, retained_edge_count

    def _refine_large_communities(
        self,
        *,
        graph: nx.Graph,
        edges: list[RelationEdge],
        raw_communities: list[set[int]],
    ) -> list[set[int]]:
        """当只识别出一个明显过大的社区时，逐步提高阈值和分辨率做二次细分。"""

        if len(raw_communities) != 1:
            return raw_communities

        community_members = sorted(set(raw_communities[0]))
        if len(community_members) < 6:
            return raw_communities
        if len(community_members) != self._get_graph_node_count(graph):
            return raw_communities

        best_communities = raw_communities
        refined_edges = [
            edge
            for edge in edges
            if edge.source_id in community_members
            and edge.target_id in community_members
        ]

        for attempt_index, refined_threshold in enumerate(
            self._build_refine_thresholds()
        ):
            refined_graph, retained_edge_count = self._build_graph(
                member_ids=community_members,
                edges=refined_edges,
                edge_threshold=refined_threshold,
            )
            if retained_edge_count <= 0:
                continue

            refined_communities, _ = self._detect_raw_communities(
                graph=refined_graph,
                member_count=len(community_members),
                input_edge_count=len(refined_edges),
                retained_edge_count=retained_edge_count,
                resolution_parameter=1.0 + ((attempt_index + 1) * 0.4),
            )
            normalized_refined = [
                set(current_community)
                for current_community in refined_communities
                if len(set(current_community)) >= 2
            ]
            if self._is_meaningful_split(
                communities=normalized_refined,
                total_member_count=len(community_members),
            ):
                logger.info(
                    "[GroupGraph][Community] 触发二次细分成功 "
                    f"原始成员数={len(community_members)} 细分后团体数={len(normalized_refined)} "
                    f"细分阈值={refined_threshold:.2f}"
                )
                return normalized_refined
            if len(normalized_refined) > len(best_communities):
                best_communities = normalized_refined

        return best_communities

    def _build_refine_thresholds(self) -> list[float]:
        """为大团体二次细分生成逐步收紧的边阈值。"""

        base_threshold = self.community_edge_threshold
        candidate_thresholds = [
            min(0.92, max(base_threshold + 0.06, base_threshold * 1.25)),
            min(0.92, max(base_threshold + 0.12, base_threshold * 1.55)),
            min(0.92, max(base_threshold + 0.18, base_threshold * 1.85)),
        ]
        return sorted(set(candidate_thresholds))

    def _is_meaningful_split(
        self,
        *,
        communities: list[set[int]],
        total_member_count: int,
    ) -> bool:
        """过滤掉“一个大团体 + 一个极小碎片”的无效细分结果。"""

        community_sizes = sorted(
            (len(current_community) for current_community in communities),
            reverse=True,
        )
        if len(community_sizes) < 2:
            return False
        return community_sizes[1] >= 2 and community_sizes[0] <= max(
            2, total_member_count - 2
        )

    def _get_graph_node_count(self, graph: nx.Graph) -> int:
        """兼容不同 graph 实现，安全获取节点数量。"""

        graph_nodes = getattr(graph, "nodes", None)
        if graph_nodes is not None:
            try:
                return len(graph_nodes)
            except TypeError:
                pass

        fallback_nodes = getattr(graph, "_nodes", None)
        if fallback_nodes is not None:
            return len(fallback_nodes)
        return 0

    def _detect_raw_communities(
        self,
        *,
        graph,
        member_count: int,
        input_edge_count: int,
        retained_edge_count: int,
        resolution_parameter: float = 1.0,
    ) -> tuple[list[set[int]], str]:
        """优先使用 Leiden，失败时再逐级兼容回退。"""

        if self._get_graph_node_count(graph) <= 1:
            return [set(self._extract_graph_nodes(graph))], "single_node"

        try:
            return self._detect_with_leiden(
                graph=graph,
                resolution_parameter=resolution_parameter,
            )
        except Exception as exc:  # noqa: BLE001 - 需要收敛可选依赖和底层图库异常
            logger.warning(
                "[GroupGraph][Community] Leiden 社区识别失败，将回退到带权异步标签传播 "
                f"成员数={member_count} 输入边数={input_edge_count} "
                f"入社区图边数={retained_edge_count} 异常={exc}"
            )

        try:
            communities = list(
                nx.algorithms.community.asyn_lpa_communities(
                    graph,
                    weight="weight",
                    seed=7,
                )
            )
            return communities, "asyn_lpa_weighted"
        except Exception as exc:  # noqa: BLE001 - 需要收敛底层库异常
            logger.warning(
                "[GroupGraph][Community] 带权异步标签传播失败，将回退到无权标签传播 "
                f"成员数={member_count} 输入边数={input_edge_count} "
                f"入社区图边数={retained_edge_count} 异常={exc}"
            )

        try:
            communities = list(
                nx.algorithms.community.label_propagation_communities(graph)
            )
            return communities, "label_propagation_unweighted"
        except Exception as exc:  # noqa: BLE001 - 需要收敛底层库异常
            logger.error(
                "[GroupGraph][Community] 小团体识别失败 "
                f"成员数={member_count} 输入边数={input_edge_count} "
                f"入社区图边数={retained_edge_count} 异常={exc}"
            )
            return [], "failed"

    def _detect_with_leiden(
        self,
        *,
        graph,
        resolution_parameter: float,
    ) -> tuple[list[set[int]], str]:
        """使用 Leiden 算法做主社区划分，优先获得更稳定的结构聚类结果。"""

        if ig is None or leidenalg is None:
            raise RuntimeError("Leiden 依赖未安装")

        node_ids = sorted(self._extract_graph_nodes(graph))
        if len(node_ids) <= 1:
            return [set(node_ids)], "single_node"

        indexed_edges: list[tuple[int, int]] = []
        edge_weights: list[float] = []
        node_index_map = {user_id: index for index, user_id in enumerate(node_ids)}

        for source_id, target_id, weight in self._iter_graph_edges(graph):
            indexed_edges.append((node_index_map[source_id], node_index_map[target_id]))
            edge_weights.append(weight)

        if not indexed_edges:
            return [{user_id} for user_id in node_ids], "leiden_disconnected"

        igraph_graph = ig.Graph(
            n=len(node_ids),
            edges=indexed_edges,
            directed=False,
        )

        partition_type = getattr(
            leidenalg, "RBConfigurationVertexPartition", None
        ) or getattr(leidenalg, "ModularityVertexPartition", None)
        if partition_type is None:
            raise RuntimeError("Leiden 分区类型不可用")

        partition_kwargs = {"weights": edge_weights}
        algorithm_name = "leiden_modularity"
        if getattr(leidenalg, "RBConfigurationVertexPartition", None) is partition_type:
            partition_kwargs["resolution_parameter"] = resolution_parameter
            algorithm_name = "leiden_rb_configuration"

        try:
            partition = leidenalg.find_partition(
                igraph_graph,
                partition_type,
                **partition_kwargs,
            )
        except TypeError:
            # 部分 leidenalg 版本的参数签名略有差异，这里去掉分辨率做兼容兜底。
            partition_kwargs.pop("resolution_parameter", None)
            partition = leidenalg.find_partition(
                igraph_graph,
                partition_type,
                **partition_kwargs,
            )

        communities = [
            {node_ids[vertex_index] for vertex_index in current_cluster}
            for current_cluster in partition
        ]
        return communities, algorithm_name

    def _extract_graph_nodes(self, graph) -> list[int]:
        """兼容真实 networkx 图和测试替身，提取全部节点。"""

        graph_nodes = getattr(graph, "nodes", None)
        if graph_nodes is not None:
            try:
                return list(graph_nodes)
            except TypeError:
                pass

        fallback_nodes = getattr(graph, "_nodes", None)
        if fallback_nodes is not None:
            return list(fallback_nodes)
        return []

    def _iter_graph_edges(self, graph):
        """兼容真实 networkx 图和测试替身，输出不重复的无向边。"""

        graph_edges = getattr(graph, "edges", None)
        if graph_edges is not None:
            try:
                for source_id, target_id, payload in graph.edges(data=True):
                    yield source_id, target_id, float(payload.get("weight", 0.0))
                return
            except TypeError:
                pass

        fallback_edges = getattr(graph, "_edges", None)
        if fallback_edges is None:
            return

        emitted_pairs: set[tuple[int, int]] = set()
        for (source_id, target_id), payload in fallback_edges.items():
            pair = tuple(sorted((source_id, target_id)))
            if pair in emitted_pairs:
                continue
            emitted_pairs.add(pair)
            yield source_id, target_id, float(payload.get("weight", 0.0))

    def _calculate_member_edge_weight(self, graph, user_id: int) -> float:
        """统计单个成员在社区图中的总边权，用于识别游离成员。"""

        return sum(
            graph[user_id][neighbor_id].get("weight", 0)
            for neighbor_id in graph[user_id]
        )

    def _normalize_community_ids(
        self,
        *,
        members: list[MemberNode],
        communities: list[Community],
    ) -> None:
        """重排社区编号，确保成员编号与排序后的团体一一对应。"""

        for new_index, community in enumerate(communities):
            community.community_id = new_index
        for member in members:
            if member.community_id == -1:
                continue
            for community in communities:
                if member.user_id in community.member_ids:
                    member.community_id = community.community_id
                    break
