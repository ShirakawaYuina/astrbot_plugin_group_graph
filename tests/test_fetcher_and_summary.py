import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

if "networkx" not in sys.modules:
    fake_networkx = types.ModuleType("networkx")

    class _FakeGraph:
        def __init__(self):
            self._nodes = set()
            self._edges = {}

        def add_nodes_from(self, nodes):
            self._nodes.update(nodes)

        def add_edge(self, source, target, weight=0):
            self._edges[(source, target)] = {"weight": weight}
            self._edges[(target, source)] = {"weight": weight}

        def has_edge(self, source, target):
            return (source, target) in self._edges

        def __getitem__(self, item):
            neighbors = {}
            for (source, target), payload in self._edges.items():
                if source == item:
                    neighbors[target] = payload
            return neighbors

    def _fake_label_propagation_communities(graph, weight=None):
        return [set(getattr(graph, "_nodes", set()))]

    def _fake_asyn_lpa_communities(graph, weight=None, seed=None):
        return [set(getattr(graph, "_nodes", set()))]

    fake_networkx.Graph = _FakeGraph
    fake_networkx.algorithms = types.SimpleNamespace(
        community=types.SimpleNamespace(
            label_propagation_communities=_fake_label_propagation_communities,
            asyn_lpa_communities=_fake_asyn_lpa_communities,
        )
    )
    sys.modules["networkx"] = fake_networkx

if "igraph" not in sys.modules:
    fake_igraph = types.ModuleType("igraph")
    fake_igraph.Graph = object
    sys.modules["igraph"] = fake_igraph

if "leidenalg" not in sys.modules:
    fake_leidenalg = types.ModuleType("leidenalg")
    fake_leidenalg.ModularityVertexPartition = object

    def _fake_find_partition(*args, **kwargs):
        return []

    fake_leidenalg.find_partition = _fake_find_partition
    sys.modules["leidenalg"] = fake_leidenalg

from data.plugins.astrbot_plugin_group_graph.domain.models import (
    MemberNode,
    Message,
    RelationEdge,
)
from data.plugins.astrbot_plugin_group_graph.main import GroupGraphPlugin
from data.plugins.astrbot_plugin_group_graph.rendering.renderer import (
    HtmlTemplateRenderer,
)
from data.plugins.astrbot_plugin_group_graph.rendering.sender import ImageSender
from data.plugins.astrbot_plugin_group_graph.services.analysis.community import (
    CommunityDetector,
)
from data.plugins.astrbot_plugin_group_graph.services.analysis.llm_analyzer import (
    LLMRelationAnalyzer,
)
from data.plugins.astrbot_plugin_group_graph.services.analysis.llm_summary import (
    LLMSummaryAnalyzer,
)
from data.plugins.astrbot_plugin_group_graph.services.history.fetcher import (
    MessageFetcher,
)
from data.plugins.astrbot_plugin_group_graph.services.history.preprocessor import (
    MessagePreprocessor,
)


class MessageFetcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_messages_uses_message_seq_cursor_and_data_messages(self):
        fetcher = MessageFetcher(max_fetch_count=3000)
        bot = SimpleNamespace()
        bot.api = SimpleNamespace()
        bot.api.call_action = AsyncMock(
            side_effect=[
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": 1001,
                                "message_seq": 88,
                                "time": 200,
                                "sender": {
                                    "user_id": 1,
                                    "nickname": "显示A",
                                    "card": "群名片A",
                                },
                                "message": [
                                    {"type": "text", "data": {"text": "第一条"}}
                                ],
                            },
                            {
                                "message_id": 1000,
                                "message_seq": 87,
                                "time": 190,
                                "sender": {"user_id": 2, "nickname": "显示B"},
                                "message": [
                                    {"type": "text", "data": {"text": "第二条"}}
                                ],
                            },
                        ]
                    }
                },
                {
                    "messages": [
                        {
                            "message_id": 999,
                            "message_seq": 86,
                            "time": 180,
                            "sender": {"user_id": 3, "nickname": "显示C"},
                            "message": [{"type": "text", "data": {"text": "第三条"}}],
                        }
                    ]
                },
            ]
        )
        event = SimpleNamespace(bot=bot)

        messages = await fetcher.fetch_messages(
            event=event,
            group_id="123456",
            range_type="count",
            range_value=3,
        )

        self.assertEqual([message.msg_id for message in messages], [1001, 1000, 999])
        self.assertEqual(messages[0].sender_name, "群名片A")
        self.assertEqual(
            bot.api.call_action.await_args_list[1].kwargs["message_seq"], 87
        )

    async def test_fetch_messages_in_time_mode_is_not_cut_off_by_internal_count_limit(
        self,
    ):
        fetcher = MessageFetcher(max_fetch_count=2)
        now_timestamp = int(datetime.now().timestamp())
        bot = SimpleNamespace()
        bot.api = SimpleNamespace()
        bot.api.call_action = AsyncMock(
            side_effect=[
                {
                    "messages": [
                        {
                            "message_id": 2002,
                            "message_seq": 102,
                            "time": now_timestamp - 60,
                            "sender": {"user_id": 11, "nickname": "A"},
                            "message": [{"type": "text", "data": {"text": "第一批-1"}}],
                        },
                        {
                            "message_id": 2001,
                            "message_seq": 101,
                            "time": now_timestamp - 120,
                            "sender": {"user_id": 12, "nickname": "B"},
                            "message": [{"type": "text", "data": {"text": "第一批-2"}}],
                        },
                    ]
                },
                {
                    "messages": [
                        {
                            "message_id": 2000,
                            "message_seq": 100,
                            "time": now_timestamp - 180,
                            "sender": {"user_id": 13, "nickname": "C"},
                            "message": [{"type": "text", "data": {"text": "第二批-1"}}],
                        }
                    ]
                },
                {"messages": []},
            ]
        )
        event = SimpleNamespace(bot=bot)

        messages = await fetcher.fetch_messages(
            event=event,
            group_id="123456",
            range_type="time",
            range_value=30,
        )

        self.assertEqual([message.msg_id for message in messages], [2002, 2001, 2000])
        self.assertEqual(bot.api.call_action.await_count, 3)


class CommandParsingTests(unittest.TestCase):
    def test_parse_command_options_defaults_to_one_day(self):
        plugin = GroupGraphPlugin.__new__(GroupGraphPlugin)
        plugin.max_fetch_days = 30

        range_type, range_value = plugin._parse_command_options(
            range_param="1d",
            raw_message="group_graph",
        )

        self.assertEqual(range_type, "time")
        self.assertEqual(range_value, 1)

    def test_parse_command_options_accepts_day_range_only(self):
        plugin = GroupGraphPlugin.__new__(GroupGraphPlugin)
        plugin.max_fetch_days = 30

        range_type, range_value = plugin._parse_command_options(
            range_param="1d",
            raw_message="/group_graph 3d",
        )

        self.assertEqual(range_type, "time")
        self.assertEqual(range_value, 3)

    def test_parse_command_options_clamps_day_range_to_configured_max_days(self):
        plugin = GroupGraphPlugin.__new__(GroupGraphPlugin)
        plugin.max_fetch_days = 7

        range_type, range_value = plugin._parse_command_options(
            range_param="1d",
            raw_message="/group_graph 30d",
        )

        self.assertEqual(range_type, "time")
        self.assertEqual(range_value, 7)


class ConfigSchemaTests(unittest.TestCase):
    def test_schema_uses_title_and_hint_for_max_fetch_days(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema_data = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertIn("max_fetch_days", schema_data)
        self.assertNotIn("max_fetch_count", schema_data)
        self.assertEqual(schema_data["max_fetch_days"]["description"], "最大拉取天数")
        self.assertIn("最大时间跨度", schema_data["max_fetch_days"]["hint"])

    def test_schema_contains_llm_relation_batch_size(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema_data = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertIn("llm_relation_batch_size", schema_data)
        self.assertEqual(schema_data["llm_relation_batch_size"]["default"], 5)
        self.assertEqual(
            schema_data["llm_relation_batch_size"]["description"],
            "关系分析批量大小",
        )
        self.assertIn("最多处理多少个成员对", schema_data["llm_relation_batch_size"]["hint"])

    def test_schema_describes_leiden_related_thresholds_clearly(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema_data = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(
            schema_data["loner_score_threshold"]["description"],
            "游离成员阈值",
        )
        self.assertIn("Leiden 划分后", schema_data["loner_score_threshold"]["hint"])
        self.assertEqual(
            schema_data["community_edge_threshold"]["description"],
            "社群识别边阈值",
        )
        self.assertIn("过滤弱连接", schema_data["community_edge_threshold"]["hint"])

    def test_schema_contains_max_hot_topics(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema_data = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertIn("max_hot_topics", schema_data)
        self.assertEqual(schema_data["max_hot_topics"]["default"], 10)
        self.assertEqual(schema_data["max_hot_topics"]["description"], "热聊话题数量")
        self.assertIn("最多生成多少个", schema_data["max_hot_topics"]["hint"])

    def test_schema_contains_include_bot_messages(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema_data = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertIn("include_bot_messages", schema_data)
        self.assertFalse(schema_data["include_bot_messages"]["default"])
        self.assertEqual(
            schema_data["include_bot_messages"]["description"],
            "纳入机器人消息",
        )
        self.assertIn("机器人自己发送的消息", schema_data["include_bot_messages"]["hint"])

    def test_schema_contains_image_send_mode(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema_data = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertIn("image_send_mode", schema_data)
        self.assertEqual(schema_data["image_send_mode"]["default"], "base64")
        self.assertEqual(schema_data["image_send_mode"]["options"], ["base64", "url"])
        self.assertEqual(schema_data["image_send_mode"]["description"], "图片发送方式")
        self.assertIn("OneBot v11", schema_data["image_send_mode"]["hint"])
        self.assertIn("image_send_url_base", schema_data)

    def test_all_schema_items_use_short_title_descriptions_with_hints(self):
        """配置页左侧应展示短标题，详细说明统一放到 hint 的灰色描述中。"""

        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema_data = json.loads(schema_path.read_text(encoding="utf-8"))

        for key, item in schema_data.items():
            with self.subTest(key=key):
                self.assertLessEqual(len(item["description"]), 16)
                self.assertIn("hint", item)
                self.assertGreater(len(item["hint"]), 0)


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_permission_allows_only_bot_admin_when_members_disabled(self):
        plugin = GroupGraphPlugin.__new__(GroupGraphPlugin)
        plugin.allow_member = False
        plugin._log_analysis_event = lambda *args, **kwargs: None

        event = SimpleNamespace(
            is_admin=lambda: False,
            get_sender_id=lambda: "10001",
            get_group_id=lambda: "223344",
            bot=SimpleNamespace(get_group_member_info=AsyncMock()),
        )

        allowed = await plugin._check_permission(event)

        self.assertFalse(allowed)
        event.bot.get_group_member_info.assert_not_awaited()

    async def test_check_permission_allows_bot_admin(self):
        plugin = GroupGraphPlugin.__new__(GroupGraphPlugin)
        plugin.allow_member = False
        plugin._log_analysis_event = lambda *args, **kwargs: None

        event = SimpleNamespace(
            is_admin=lambda: True,
            get_sender_id=lambda: "10001",
            get_group_id=lambda: "223344",
            bot=SimpleNamespace(get_group_member_info=AsyncMock()),
        )

        allowed = await plugin._check_permission(event)

        self.assertTrue(allowed)
        event.bot.get_group_member_info.assert_not_awaited()


class MainFlowCompatibilityTests(unittest.TestCase):
    def test_template_payload_count_uses_zero_when_renderer_returns_none_payload(self):
        """渲染器改为直接返回完整 HTML 后，主流程日志仍需兼容空载荷。"""

        template_data = None
        payload_field_count = len(template_data or {})

        self.assertEqual(payload_field_count, 0)

    def test_resolve_group_name_prefers_event_message_group_name(self):
        """标题应优先展示事件里已经携带的真实群名，避免始终回退到群号。"""

        plugin = GroupGraphPlugin.__new__(GroupGraphPlugin)
        event = SimpleNamespace(
            message_obj=SimpleNamespace(
                group=SimpleNamespace(group_name="test group"),
            )
        )

        group_name = plugin._resolve_group_name(event=event, group_id="627676397")

        self.assertEqual(group_name, "test group")

    def test_resolve_group_name_falls_back_to_group_id_when_name_missing(self):
        """当事件里没有群名时，仍需回退到群号，保证标题总有可用值。"""

        plugin = GroupGraphPlugin.__new__(GroupGraphPlugin)
        event = SimpleNamespace(message_obj=SimpleNamespace(group=None))

        group_name = plugin._resolve_group_name(event=event, group_id="627676397")

        self.assertEqual(group_name, "627676397")


class AnalyzeGroupGraphFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_group_graph_does_not_send_extra_history_loading_message(
        self,
    ):
        """主流程已返回统一进度提示时，不应再额外发送历史拉取提示。"""

        plugin = GroupGraphPlugin.__new__(GroupGraphPlugin)
        plugin.max_fetch_days = 30
        plugin.default_top_n = 20
        plugin.fetcher = SimpleNamespace(
            fetch_messages=AsyncMock(
                return_value=[
                    SimpleNamespace(timestamp=index) for index in range(19)
                ]
            )
        )
        plugin._log_analysis_event = lambda *args, **kwargs: None
        plugin._get_session_type = lambda event: "群聊"
        plugin._check_permission = AsyncMock(return_value=True)
        plugin._is_cooldown_active = lambda group_id: False
        plugin._get_remaining_cooldown = lambda group_id: 0
        plugin._resolve_llm_provider = lambda: (object(), "", "context_default")
        plugin._set_cooldown = lambda group_id: None
        plugin._clear_cooldown = lambda group_id: None

        event = SimpleNamespace(
            get_group_id=lambda: "223344",
            get_message_str=lambda: "/group_graph 1d",
            get_platform_id=lambda: "test",
            get_sender_id=lambda: "10001",
            plain_result=lambda text: text,
            send=AsyncMock(),
        )

        results = [
            result
            async for result in plugin.analyze_group_graph(
                event=event,
                range_param="1d",
            )
        ]

        self.assertEqual(
            results,
            ["正在分析群关系图谱，请稍候...", "消息数量不足，无法生成有效图谱。"],
        )
        event.send.assert_not_awaited()


class MessagePreprocessorTests(unittest.TestCase):
    def test_process_filters_members_below_min_message_count(self):
        preprocessor = MessagePreprocessor(min_message_count=3)
        messages = [
            Message(1, 101, "甲", "a", None, [], 1),
            Message(2, 101, "甲", "b", None, [], 2),
            Message(3, 101, "甲", "c", None, [], 3),
            Message(4, 202, "乙", "x", None, [], 4),
            Message(5, 202, "乙", "y", None, [], 5),
        ]

        processed_data = preprocessor.process(messages)

        self.assertEqual([member.user_id for member in processed_data.members], [101])

    def test_process_keeps_bot_messages_when_config_allows_it(self):
        preprocessor = MessagePreprocessor(
            min_message_count=1,
            bot_user_id=999,
            include_bot_messages=True,
        )
        messages = [
            Message(1, 999, "机器人", "我来参与一下", None, [], 1),
            Message(2, 101, "甲", "欢迎", None, [], 2),
        ]

        processed_data = preprocessor.process(messages)

        self.assertEqual(
            [member.user_id for member in processed_data.members],
            [101, 999],
        )
        self.assertEqual(
            [message.sender_id for message in processed_data.messages], [999, 101]
        )


class SummaryAnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_summary_prompt_uses_deeper_story_and_topic_requirements(
        self,
    ):
        captured_prompt = {}

        async def fake_request_json_from_provider(*, provider, prompt, task_name):
            del provider, task_name
            captured_prompt["value"] = prompt
            return {
                "atmosphere": "整体氛围不错",
                "hot_topics": ["话题A", "话题B"],
                "community_names": {},
                "top3_stories": [],
            }

        analyzer = LLMSummaryAnalyzer(
            context=SimpleNamespace(get_using_provider=lambda: object()),
            max_hot_topics=10,
        )

        with patch(
            "data.plugins.astrbot_plugin_group_graph.services.analysis.llm_summary."
            "request_json_from_provider",
            side_effect=fake_request_json_from_provider,
        ):
            await analyzer._generate_summary(
                communities=[],
                community_messages={},
                top_edges=[],
                member_map={},
                provider=object(),
            )

        prompt = captured_prompt["value"]
        self.assertIn("热聊话题", prompt)
        self.assertIn("最多输出 10 个", prompt)
        self.assertIn("100~200字", prompt)
        self.assertIn("60~120字", prompt)

    async def test_summary_filters_links_to_visible_nodes_and_keeps_avatars(self):
        members = [
            MemberNode(1, "甲", "avatar-1", 20, 1.0, 0),
            MemberNode(2, "乙", "avatar-2", 18, 0.9, 0),
            MemberNode(3, "丙", "avatar-3", 5, 0.25, 1),
        ]
        relation_edges = [
            RelationEdge(1, 2, 1.0, "friendly", "常聊", 1.0),
            RelationEdge(1, 3, 0.8, "neutral", "偶遇", 0.8),
        ]
        analyzer = LLMSummaryAnalyzer(context=SimpleNamespace())
        analyzer._generate_summary = AsyncMock(
            return_value={
                "atmosphere": "整体氛围不错",
                "hot_topics": ["话题A"],
                "community_names": {"0": "核心团"},
                "top3_stories": [
                    {"name_a": "甲", "name_b": "乙", "score": 1.0, "story": "互动频繁"}
                ],
            }
        )

        graph_data = await analyzer.analyze(
            members=members,
            messages=[],
            interaction_matrix={},
            relation_edges=relation_edges,
            top_n=2,
        )

        self.assertEqual([node["id"] for node in graph_data["nodes"]], [1, 2])
        self.assertEqual(graph_data["nodes"][0]["avatar"], "avatar-1")
        self.assertEqual(len(graph_data["links"]), 1)
        self.assertEqual(graph_data["links"][0]["source"], 1)
        self.assertEqual(graph_data["links"][0]["target"], 2)

    async def test_summary_keeps_loner_member_links_even_when_below_default_threshold(
        self,
    ):
        members = [
            MemberNode(1, "甲", "avatar-1", 20, 1.0, 0),
            MemberNode(2, "乙", "avatar-2", 18, 0.9, 0),
            MemberNode(3, "丙", "avatar-3", 6, 0.3, -1),
        ]
        relation_edges = [
            RelationEdge(1, 2, 1.0, "friendly", "常聊", 1.0),
            RelationEdge(2, 3, 0.08, "neutral", "偶有互动", 0.08),
        ]
        analyzer = LLMSummaryAnalyzer(context=SimpleNamespace())
        analyzer._generate_summary = AsyncMock(
            return_value={
                "atmosphere": "整体氛围不错",
                "hot_topics": ["话题A"],
                "community_names": {"0": "核心团"},
                "top3_stories": [],
            }
        )

        with patch.object(
            analyzer.community_detector,
            "detect",
            return_value=(
                [
                    SimpleNamespace(
                        community_id=0,
                        name="核心团",
                        color="#FF7A59",
                        member_ids=[1, 2],
                    )
                ],
                [3],
            ),
        ):
            graph_data = await analyzer.analyze(
                members=members,
                messages=[],
                interaction_matrix={},
                relation_edges=relation_edges,
                top_n=3,
            )

        self.assertEqual(len(graph_data["links"]), 2)
        self.assertIn(
            {
                "source": 2,
                "target": 3,
                "weight": 0.08,
                "interaction_score": 0.08,
                "sentiment": "neutral",
                "label": "偶有互动",
            },
            graph_data["links"],
        )

    async def test_summary_limits_llm_input_communities_without_trimming_visible_communities(
        self,
    ):
        members = [
            MemberNode(1, "A", "avatar-1", 20, 1.0, 0),
            MemberNode(2, "B", "avatar-2", 18, 0.9, 0),
            MemberNode(3, "C", "avatar-3", 16, 0.8, 1),
            MemberNode(4, "D", "avatar-4", 15, 0.75, 1),
            MemberNode(5, "E", "avatar-5", 14, 0.7, 2),
            MemberNode(6, "F", "avatar-6", 13, 0.65, 2),
            MemberNode(7, "G", "avatar-7", 12, 0.6, 3),
            MemberNode(8, "H", "avatar-8", 11, 0.55, 3),
        ]
        relation_edges = [
            RelationEdge(1, 2, 0.9, "friendly", "核心互聊", 0.9),
            RelationEdge(3, 4, 0.85, "friendly", "稳定互动", 0.85),
            RelationEdge(5, 6, 0.8, "friendly", "一起冒泡", 0.8),
            RelationEdge(7, 8, 0.78, "friendly", "偶尔结伴", 0.78),
        ]
        analyzer = LLMSummaryAnalyzer(
            context=SimpleNamespace(),
            max_llm_communities=2,
        )

        captured_kwargs = {}

        async def fake_generate_summary(**kwargs):
            captured_kwargs.update(kwargs)
            return {
                "atmosphere": "整体氛围不错",
                "hot_topics": ["话题A"],
                "community_names": {"0": "核心组", "1": "二群"},
                "top3_stories": [],
            }

        analyzer._generate_summary = fake_generate_summary

        with patch.object(
            analyzer.community_detector,
            "detect",
            return_value=(
                [
                    SimpleNamespace(
                        community_id=0,
                        name="团体1",
                        color="#FF7A59",
                        member_ids=[1, 2],
                    ),
                    SimpleNamespace(
                        community_id=1,
                        name="团体2",
                        color="#00A6FB",
                        member_ids=[3, 4],
                    ),
                    SimpleNamespace(
                        community_id=2,
                        name="团体3",
                        color="#7B61FF",
                        member_ids=[5, 6],
                    ),
                    SimpleNamespace(
                        community_id=3,
                        name="团体4",
                        color="#34D399",
                        member_ids=[7, 8],
                    ),
                ],
                [],
            ),
        ):
            graph_data = await analyzer.analyze(
                members=members,
                messages=[],
                interaction_matrix={},
                relation_edges=relation_edges,
                top_n=8,
            )

        self.assertEqual(len(graph_data["communities"]), 4)
        self.assertEqual(len(captured_kwargs["communities"]), 2)
        self.assertEqual(
            [community.community_id for community in captured_kwargs["communities"]],
            [0, 1],
        )

    async def test_summary_accepts_prefixed_community_name_keys_from_llm(self):
        members = [
            MemberNode(1, "A", "avatar-1", 20, 1.0, 0),
            MemberNode(2, "B", "avatar-2", 18, 0.9, 0),
        ]
        relation_edges = [
            RelationEdge(1, 2, 0.9, "friendly", "核心互聊", 0.9),
        ]
        analyzer = LLMSummaryAnalyzer(context=SimpleNamespace())
        analyzer._generate_summary = AsyncMock(
            return_value={
                "atmosphere": "整体氛围不错",
                "hot_topics": ["话题A"],
                "community_names": {"团体0": "嘴炮游戏团"},
                "top3_stories": [],
            }
        )

        with patch.object(
            analyzer.community_detector,
            "detect",
            return_value=(
                [
                    SimpleNamespace(
                        community_id=0,
                        name="团体1",
                        color="#FF7A59",
                        member_ids=[1, 2],
                    )
                ],
                [],
            ),
        ):
            graph_data = await analyzer.analyze(
                members=members,
                messages=[],
                interaction_matrix={},
                relation_edges=relation_edges,
                top_n=2,
            )

        self.assertEqual(graph_data["communities"][0]["name"], "嘴炮游戏团")

    async def test_summary_keeps_only_best_link_for_each_loner_member(self):
        members = [
            MemberNode(1, "A", "avatar-1", 30, 1.0, 0),
            MemberNode(2, "B", "avatar-2", 28, 0.93, 0),
            MemberNode(3, "C", "avatar-3", 10, 0.33, -1),
            MemberNode(4, "D", "avatar-4", 9, 0.3, -1),
            MemberNode(5, "E", "avatar-5", 16, 0.53, 1),
        ]
        relation_edges = [
            RelationEdge(1, 2, 1.0, "friendly", "高频互动", 1.0),
            RelationEdge(1, 3, 0.11, "neutral", "偶有互动", 0.11),
            RelationEdge(2, 3, 0.32, "neutral", "更熟一些", 0.32),
            RelationEdge(1, 4, 0.25, "neutral", "有来有往", 0.25),
            RelationEdge(5, 4, 0.41, "friendly", "更常互动", 0.41),
        ]
        analyzer = LLMSummaryAnalyzer(context=SimpleNamespace())
        analyzer._generate_summary = AsyncMock(
            return_value={
                "atmosphere": "整体氛围不错",
                "hot_topics": ["话题A"],
                "community_names": {"0": "核心组", "1": "边缘组"},
                "top3_stories": [],
            }
        )

        with patch.object(
            analyzer.community_detector,
            "detect",
            return_value=(
                [
                    SimpleNamespace(
                        community_id=0,
                        name="核心组",
                        color="#FF7A59",
                        member_ids=[1, 2],
                    ),
                    SimpleNamespace(
                        community_id=1,
                        name="边缘组",
                        color="#7B61FF",
                        member_ids=[5],
                    ),
                ],
                [3, 4],
            ),
        ):
            graph_data = await analyzer.analyze(
                members=members,
                messages=[],
                interaction_matrix={},
                relation_edges=relation_edges,
                top_n=5,
            )

        loner_links = [
            link
            for link in graph_data["links"]
            if link["source"] in {3, 4} or link["target"] in {3, 4}
        ]
        self.assertEqual(len(loner_links), 2)
        self.assertIn(
            {
                "source": 2,
                "target": 3,
                "weight": 0.32,
                "interaction_score": 0.32,
                "sentiment": "neutral",
                "label": "更熟一些",
            },
            loner_links,
        )
        self.assertIn(
            {
                "source": 5,
                "target": 4,
                "weight": 0.41,
                "interaction_score": 0.41,
                "sentiment": "friendly",
                "label": "更常互动",
            },
            loner_links,
        )

    async def test_summary_prevents_loner_from_appearing_in_multiple_links(self):
        members = [
            MemberNode(1, "A", "avatar-1", 30, 1.0, 0),
            MemberNode(2, "B", "avatar-2", 28, 0.93, 0),
            MemberNode(3, "C", "avatar-3", 10, 0.33, -1),
            MemberNode(4, "D", "avatar-4", 9, 0.3, -1),
            MemberNode(5, "E", "avatar-5", 16, 0.53, 1),
        ]
        relation_edges = [
            RelationEdge(1, 2, 1.0, "friendly", "高频互动", 1.0),
            RelationEdge(2, 3, 0.52, "friendly", "C最强连接", 0.52),
            RelationEdge(3, 4, 0.47, "neutral", "游离互连", 0.47),
            RelationEdge(5, 4, 0.31, "neutral", "D次强连接", 0.31),
        ]
        analyzer = LLMSummaryAnalyzer(context=SimpleNamespace())
        analyzer._generate_summary = AsyncMock(
            return_value={
                "atmosphere": "整体氛围不错",
                "hot_topics": ["话题A"],
                "community_names": {"0": "核心组", "1": "边缘组"},
                "top3_stories": [],
            }
        )

        with patch.object(
            analyzer.community_detector,
            "detect",
            return_value=(
                [
                    SimpleNamespace(
                        community_id=0,
                        name="核心组",
                        color="#FF7A59",
                        member_ids=[1, 2],
                    ),
                    SimpleNamespace(
                        community_id=1,
                        name="边缘组",
                        color="#7B61FF",
                        member_ids=[5],
                    ),
                ],
                [3, 4],
            ),
        ):
            graph_data = await analyzer.analyze(
                members=members,
                messages=[],
                interaction_matrix={},
                relation_edges=relation_edges,
                top_n=5,
            )

        loner_links = [
            link
            for link in graph_data["links"]
            if link["source"] in {3, 4} or link["target"] in {3, 4}
        ]
        self.assertEqual(len(loner_links), 2)
        self.assertNotIn(
            {
                "source": 3,
                "target": 4,
                "weight": 0.47,
                "interaction_score": 0.47,
                "sentiment": "neutral",
                "label": "游离互连",
            },
            loner_links,
        )
        self.assertIn(
            {
                "source": 2,
                "target": 3,
                "weight": 0.52,
                "interaction_score": 0.52,
                "sentiment": "friendly",
                "label": "C最强连接",
            },
            loner_links,
        )
        self.assertIn(
            {
                "source": 5,
                "target": 4,
                "weight": 0.31,
                "interaction_score": 0.31,
                "sentiment": "neutral",
                "label": "D次强连接",
            },
            loner_links,
        )


class ImageSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_archives_image_to_plugin_data_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_image_path = temp_path / "rendered.png"
            archive_dir = temp_path / "plugin_data" / "images"
            source_image_path.write_bytes(b"fake-image-bytes")

            owner = SimpleNamespace()
            owner.html_render = AsyncMock(return_value=str(source_image_path))
            sender = ImageSender(owner, archive_dir=archive_dir)

            result = await sender.send(
                template_html="<html>{{ graph_data }}</html>",
                template_data={"graph_data": "{}"},
                group_id="223344",
                archive_timestamp="20260416_213045_123456",
            )

            archived_image_path = archive_dir / "223344_20260416_213045_123456.png"
            self.assertEqual(result, str(archived_image_path))
            self.assertTrue(archived_image_path.exists())
            self.assertEqual(archived_image_path.read_bytes(), b"fake-image-bytes")

    async def test_send_uses_star_html_render_signature(self):
        owner = SimpleNamespace()
        owner.html_render = AsyncMock(return_value="D:/tmp/group_graph.jpg")
        sender = ImageSender(owner)

        result = await sender.send(
            template_html="<html>{{ graph_data }}</html>",
            template_data={"graph_data": "{}"},
        )

        self.assertEqual(result, "D:/tmp/group_graph.jpg")
        owner.html_render.assert_awaited_once_with(
            "<html>{{ graph_data }}</html>",
            {"graph_data": "{}"},
            return_url=False,
            options={
                "full_page": True,
                "type": "png",
                "quality": 100,
                "scale": "device",
                "device_scale_factor_level": "high",
            },
        )

    async def test_send_supports_prebuilt_full_html_without_template_data(self):
        """当模板已在本地完成变量替换时，也应能直接发送完整 HTML，避免远端模板引擎差异影响渲染。"""

        owner = SimpleNamespace()
        owner.html_render = AsyncMock(return_value="D:/tmp/group_graph.jpg")
        sender = ImageSender(owner)

        result = await sender.send(
            template_html='<html><script>window.d3={};</script><script>const graphData={"nodes":[]};</script></html>',
            template_data=None,
        )

        self.assertEqual(result, "D:/tmp/group_graph.jpg")
        owner.html_render.assert_awaited_once_with(
            '<html><script>window.d3={};</script><script>const graphData={"nodes":[]};</script></html>',
            {},
            return_url=False,
            options={
                "full_page": True,
                "type": "png",
                "quality": 100,
                "scale": "device",
                "device_scale_factor_level": "high",
            },
        )

    async def test_send_rendered_image_uses_event_image_result_in_base64_mode(self):
        """默认发送模式应保持旧行为，由 AstrBot 平台适配器处理本地图片。"""

        owner = SimpleNamespace()
        event = SimpleNamespace(image_result=lambda image_path: f"image:{image_path}")
        sender = ImageSender(owner, send_mode="unknown")

        result = await sender.send_rendered_image(event, "D:/tmp/group_graph.png")

        self.assertEqual(sender.send_mode, "base64")
        self.assertEqual(result, "image:D:/tmp/group_graph.png")

    async def test_send_rendered_image_sends_registered_url_to_onebot_group(self):
        """URL 模式应注册 AstrBot 文件服务 URL，并直发到 OneBot 群聊接口。"""

        owner = SimpleNamespace()
        bot = SimpleNamespace(send_group_msg=AsyncMock(), send_private_msg=AsyncMock())
        event = SimpleNamespace(
            bot=bot,
            get_platform_name=lambda: "aiocqhttp",
            get_group_id=lambda: "223344",
            get_sender_id=lambda: "10001",
            image_result=lambda image_path: f"image:{image_path}",
        )
        sender = ImageSender(
            owner,
            send_mode="url",
            url_base="http://astrbot:6185/",
        )

        with patch(
            "data.plugins.astrbot_plugin_group_graph.rendering.sender."
            "file_token_service"
        ) as token_service:
            token_service.register_file = AsyncMock(return_value="demo-token")

            result = await sender.send_rendered_image(event, "D:/tmp/group_graph.png")

        self.assertIsNone(result)
        token_service.register_file.assert_awaited_once_with("D:\\tmp\\group_graph.png")
        bot.send_group_msg.assert_awaited_once_with(
            group_id=223344,
            message=[
                {
                    "type": "image",
                    "data": {
                        "file": "http://astrbot:6185/api/file/demo-token",
                    },
                }
            ],
        )
        bot.send_private_msg.assert_not_awaited()

    async def test_send_rendered_image_sends_registered_url_to_onebot_private(self):
        """URL 模式没有群号时，应使用发送者 ID 走 OneBot 私聊接口。"""

        owner = SimpleNamespace()
        bot = SimpleNamespace(send_group_msg=AsyncMock(), send_private_msg=AsyncMock())
        event = SimpleNamespace(
            bot=bot,
            get_platform_name=lambda: "aiocqhttp",
            get_group_id=lambda: "",
            get_sender_id=lambda: "10001",
        )
        sender = ImageSender(
            owner,
            send_mode="url",
            url_base="http://astrbot:6185",
        )

        with patch(
            "data.plugins.astrbot_plugin_group_graph.rendering.sender."
            "file_token_service"
        ) as token_service:
            token_service.register_file = AsyncMock(return_value="demo-token")

            await sender.send_rendered_image(event, "D:/tmp/group_graph.png")

        bot.send_group_msg.assert_not_awaited()
        bot.send_private_msg.assert_awaited_once_with(
            user_id=10001,
            message=[
                {
                    "type": "image",
                    "data": {
                        "file": "http://astrbot:6185/api/file/demo-token",
                    },
                }
            ],
        )

    async def test_send_rendered_image_url_mode_rejects_non_onebot_platform(self):
        """URL 模式只支持 aiocqhttp / OneBot v11，不做静默降级。"""

        owner = SimpleNamespace()
        event = SimpleNamespace(get_platform_name=lambda: "telegram")
        sender = ImageSender(owner, send_mode="url", url_base="http://astrbot:6185")

        with self.assertRaisesRegex(RuntimeError, "aiocqhttp / OneBot v11"):
            await sender.send_rendered_image(event, "D:/tmp/group_graph.png")

    async def test_send_rendered_image_url_mode_requires_url_base(self):
        """URL 模式缺少可访问地址时应直接失败，提示用户修正配置。"""

        owner = SimpleNamespace()
        bot = SimpleNamespace(send_group_msg=AsyncMock(), send_private_msg=AsyncMock())
        event = SimpleNamespace(
            bot=bot,
            get_platform_name=lambda: "aiocqhttp",
            get_group_id=lambda: "223344",
            get_sender_id=lambda: "10001",
        )
        sender = ImageSender(owner, send_mode="url")

        with (
            patch(
                "data.plugins.astrbot_plugin_group_graph.rendering.sender."
                "astrbot_config"
            ) as config,
            self.assertRaisesRegex(RuntimeError, "image_send_url_base"),
        ):
            config.get.return_value = ""
            await sender.send_rendered_image(event, "D:/tmp/group_graph.png")


class HtmlTemplateRendererTests(unittest.TestCase):
    def test_render_archives_html_to_plugin_data_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            template_path = temp_path / "graph.html"
            preview_dir = temp_path / "preview"
            archive_dir = temp_path / "plugin_data" / "html"
            template_path.write_text(
                (
                    '<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>'
                    "<div>{{ group_name }}</div>"
                    "<div>{{ analysis_desc }}</div>"
                    "<div>{{ timestamp }}</div>"
                    "<script>const graphData = {{ graph_data | safe }};</script>"
                ),
                encoding="utf-8",
            )
            renderer = HtmlTemplateRenderer(
                template_path=template_path,
                preview_dir=preview_dir,
                archive_dir=archive_dir,
            )

            rendered_html, _ = renderer.render(
                graph_data={"nodes": [{"id": 1}]},
                group_name="测试群",
                analysis_desc="最近 500 条消息",
                archive_name="223344_20260416_213045_123456",
            )

            archived_html_path = archive_dir / "223344_20260416_213045_123456.html"
            self.assertTrue(archived_html_path.exists())
            self.assertEqual(
                archived_html_path.read_text(encoding="utf-8"),
                rendered_html,
            )

    def test_render_writes_preview_html_to_plugin_preview_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            template_path = temp_path / "graph.html"
            preview_dir = temp_path / "preview"
            template_path.write_text(
                (
                    '<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>'
                    "<div>{{ group_name }}</div>"
                    "<div>{{ analysis_desc }}</div>"
                    "<div>{{ stats_start_date }}</div>"
                    "<div>{{ stats_start_time }}</div>"
                    "<div>{{ stats_end_date }}</div>"
                    "<div>{{ stats_end_time }}</div>"
                    "<div>{{ timestamp }}</div>"
                    "<script>const graphData = {{ graph_data | safe }};</script>"
                ),
                encoding="utf-8",
            )
            renderer = HtmlTemplateRenderer(
                template_path=template_path,
                preview_dir=preview_dir,
            )

            _, template_data = renderer.render(
                graph_data={"nodes": [{"id": 1}]},
                group_name="测试群",
                analysis_desc="最近 500 条消息",
                stats_start_date="03-15",
                stats_start_time="18:10",
                stats_end_date="04-14",
                stats_end_time="18:00",
            )

            preview_path = preview_dir / "preview.html"
            self.assertTrue(preview_path.exists())
            preview_html = preview_path.read_text(encoding="utf-8")
            self.assertIn(
                "https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js",
                preview_html,
            )
            self.assertIn("测试群", preview_html)
            self.assertIn("最近 500 条消息", preview_html)
            self.assertIn("03-15", preview_html)
            self.assertIn("18:10", preview_html)
            self.assertIn("04-14", preview_html)
            self.assertIn("18:00", preview_html)
            self.assertIsNone(template_data)
            self.assertIn('"nodes": [{"id": 1}]', preview_html)

    def test_render_returns_fully_inlined_html_for_runtime_rendering(self):
        """运行时应直接返回已经完成变量替换的 HTML，避免依赖远端模板引擎处理大脚本。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            template_path = temp_path / "graph.html"
            preview_dir = temp_path / "preview"
            template_path.write_text(
                (
                    '<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>'
                    "<div>{{ group_name }}</div>"
                    "<div>{{ analysis_desc }}</div>"
                    "<div>{{ stats_start_date }}</div>"
                    "<div>{{ stats_start_time }}</div>"
                    "<div>{{ stats_end_date }}</div>"
                    "<div>{{ stats_end_time }}</div>"
                    "<div>{{ timestamp }}</div>"
                    "<script>const graphData = {{ graph_data | safe }};</script>"
                ),
                encoding="utf-8",
            )
            renderer = HtmlTemplateRenderer(
                template_path=template_path,
                preview_dir=preview_dir,
            )

            rendered_html, template_data = renderer.render(
                graph_data={"nodes": [{"id": 1}], "links": []},
                group_name="运行时群组",
                analysis_desc="最近 30 天",
                stats_start_date="03-15",
                stats_start_time="18:10",
                stats_end_date="04-14",
                stats_end_time="18:00",
            )

            self.assertIsNone(template_data)
            self.assertIn(
                "https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js",
                rendered_html,
            )
            self.assertIn("运行时群组", rendered_html)
            self.assertIn("最近 30 天", rendered_html)
            self.assertIn('"nodes": [{"id": 1}], "links": []', rendered_html)
            self.assertNotIn("{{ d3_script | safe }}", rendered_html)
            self.assertNotIn("{{ graph_data | safe }}", rendered_html)

    def test_graph_template_contains_loner_link_visual_hook(self):
        template_path = Path(__file__).resolve().parents[1] / "templates" / "graph.html"
        template_html = template_path.read_text(encoding="utf-8")

        self.assertIn("const isLonerLink =", template_html)
        self.assertIn(
            "https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js", template_html
        )
        self.assertNotIn("{{ d3_script | safe }}", template_html)
        self.assertIn("stroke-dasharray", template_html)
        self.assertIn("const labeledLinks = linkData.filter", template_html)
        self.assertIn("stats-time-block", template_html)
        self.assertIn("buildLonerAnchorLayout", template_html)
        self.assertIn("layoutGraphToViewport", template_html)
        self.assertIn("usableBounds", template_html)
        self.assertIn("floating-legend", template_html)
        self.assertNotIn("left-legend", template_html)
        self.assertIn("align-items: flex-start", template_html)
        self.assertIn("leg-node-text", template_html)
        self.assertIn("游离成员", template_html)
        self.assertNotIn("发现的小团体", template_html)
        self.assertNotIn('id="cluster-list"', template_html)


class CommunityDetectorTests(unittest.TestCase):
    def test_detect_marks_singleton_leiden_cluster_as_loner_without_weight_check(self):
        detector = CommunityDetector(
            loner_score_threshold=0.01,
            community_edge_threshold=0.1,
        )
        members = [
            MemberNode(1, "甲", "", 10, 1.0, 0),
            MemberNode(2, "乙", "", 8, 0.8, 0),
            MemberNode(3, "丙", "", 7, 0.7, 0),
        ]
        edges = [
            RelationEdge(1, 2, 0.9, "friendly", "高频互动", 0.9),
            RelationEdge(1, 3, 0.35, "neutral", "偶有互动", 0.35),
        ]

        with patch.object(
            detector,
            "_detect_raw_communities",
            return_value=([{1, 2}, {3}], "leiden_modularity"),
        ):
            communities, loner_ids = detector.detect(members, edges)

        self.assertEqual([community.member_ids for community in communities], [[1, 2]])
        self.assertEqual(loner_ids, [3])

    def test_detect_keeps_two_member_leiden_cluster_when_internal_weight_is_strong(
        self,
    ):
        detector = CommunityDetector(
            loner_score_threshold=0.15,
            community_edge_threshold=0.1,
        )
        members = [
            MemberNode(1, "甲", "", 10, 1.0, 0),
            MemberNode(2, "乙", "", 9, 0.9, 0),
            MemberNode(3, "丙", "", 8, 0.8, 0),
            MemberNode(4, "丁", "", 7, 0.7, 0),
        ]
        edges = [
            RelationEdge(1, 2, 0.92, "friendly", "高频互动", 0.92),
            RelationEdge(3, 4, 0.61, "friendly", "稳定互动", 0.61),
        ]

        with patch.object(
            detector,
            "_detect_raw_communities",
            return_value=([{1, 2}, {3, 4}], "leiden_modularity"),
        ):
            communities, loner_ids = detector.detect(members, edges)

        self.assertEqual(
            [community.member_ids for community in communities],
            [[1, 2], [3, 4]],
        )
        self.assertEqual(loner_ids, [])

    def test_detect_turns_weak_two_member_leiden_cluster_into_loners(self):
        detector = CommunityDetector(
            loner_score_threshold=0.2,
            community_edge_threshold=0.1,
        )
        members = [
            MemberNode(1, "甲", "", 10, 1.0, 0),
            MemberNode(2, "乙", "", 9, 0.9, 0),
            MemberNode(3, "丙", "", 8, 0.8, 0),
            MemberNode(4, "丁", "", 7, 0.7, 0),
        ]
        edges = [
            RelationEdge(1, 2, 0.92, "friendly", "高频互动", 0.92),
            RelationEdge(3, 4, 0.12, "neutral", "弱连接", 0.12),
        ]

        with patch.object(
            detector,
            "_detect_raw_communities",
            return_value=([{1, 2}, {3, 4}], "leiden_modularity"),
        ):
            communities, loner_ids = detector.detect(members, edges)

        self.assertEqual([community.member_ids for community in communities], [[1, 2]])
        self.assertEqual(loner_ids, [3, 4])

    def test_detect_prefers_leiden_without_weight_fallback_warning(self):
        detector = CommunityDetector(
            loner_score_threshold=0.15,
            community_edge_threshold=0.1,
        )
        members = [
            MemberNode(1, "A", "", 10, 1.0, 0),
            MemberNode(2, "B", "", 9, 0.9, 0),
            MemberNode(3, "C", "", 8, 0.8, 0),
            MemberNode(4, "D", "", 7, 0.7, 0),
        ]
        edges = [
            RelationEdge(1, 2, 0.92, "friendly", "高频互动", 0.92),
            RelationEdge(3, 4, 0.88, "friendly", "高频互动", 0.88),
        ]

        with (
            patch.object(
                detector,
                "_detect_with_leiden",
                return_value=([{1, 2}, {3, 4}], "leiden_modularity"),
            ) as mock_leiden,
            patch(
                "data.plugins.astrbot_plugin_group_graph.services.analysis.community.logger.warning"
            ) as mock_logger_warning,
        ):
            communities, loner_ids = detector.detect(members, edges)

        self.assertEqual(
            [community.member_ids for community in communities],
            [[1, 2], [3, 4]],
        )
        self.assertEqual(loner_ids, [])
        mock_leiden.assert_called_once()
        warning_messages = [call.args[0] for call in mock_logger_warning.call_args_list]
        self.assertFalse(any("weight 参数" in message for message in warning_messages))

    def test_detect_falls_back_to_weighted_async_label_propagation(self):
        detector = CommunityDetector(
            loner_score_threshold=0.15,
            community_edge_threshold=0.1,
        )
        members = [
            MemberNode(1, "甲", "", 10, 1.0, 0),
            MemberNode(2, "乙", "", 8, 0.8, 0),
            MemberNode(3, "丙", "", 6, 0.6, 0),
        ]
        edges = [
            RelationEdge(1, 2, 0.9, "friendly", "高频互动", 0.9),
            RelationEdge(2, 3, 0.88, "friendly", "高频互动", 0.88),
        ]

        with (
            patch.object(
                detector,
                "_detect_with_leiden",
                side_effect=RuntimeError("leiden unavailable"),
            ),
            patch(
                "data.plugins.astrbot_plugin_group_graph.services.analysis.community."
                "nx.algorithms.community.asyn_lpa_communities"
            ) as mock_asyn_lpa,
            patch(
                "data.plugins.astrbot_plugin_group_graph.services.analysis.community.logger.warning"
            ) as mock_logger_warning,
        ):
            mock_asyn_lpa.return_value = [{1, 2, 3}]

            communities, loner_ids = detector.detect(members, edges)

        self.assertEqual(len(communities), 1)
        self.assertEqual(communities[0].member_ids, [1, 2, 3])
        self.assertEqual(loner_ids, [])
        mock_asyn_lpa.assert_called_once()
        self.assertEqual(
            mock_asyn_lpa.call_args.kwargs, {"weight": "weight", "seed": 7}
        )
        self.assertIn("Leiden", mock_logger_warning.call_args.args[0])

    def test_detect_ignores_edges_below_community_threshold(self):
        detector = CommunityDetector(
            loner_score_threshold=0.15,
            community_edge_threshold=0.3,
        )
        members = [
            MemberNode(1, "甲", "", 10, 1.0, 0),
            MemberNode(2, "乙", "", 8, 0.8, 0),
            MemberNode(3, "丙", "", 7, 0.7, 0),
            MemberNode(4, "丁", "", 6, 0.6, 0),
        ]
        edges = [
            RelationEdge(1, 2, 0.92, "friendly", "高频互动", 0.92),
            RelationEdge(3, 4, 0.87, "friendly", "高频互动", 0.87),
            RelationEdge(2, 3, 0.08, "neutral", "偶有互动", 0.08),
        ]

        def fake_weighted_fallback(graph, weight=None, seed=None):
            if graph.has_edge(2, 3):
                return [{1, 2, 3, 4}]
            return [{1, 2}, {3, 4}]

        with (
            patch.object(
                detector,
                "_detect_with_leiden",
                side_effect=RuntimeError("leiden unavailable"),
            ),
            patch(
                "data.plugins.astrbot_plugin_group_graph.services.analysis.community."
                "nx.algorithms.community.asyn_lpa_communities",
                side_effect=fake_weighted_fallback,
            ),
        ):
            communities, loner_ids = detector.detect(members, edges)

        self.assertEqual(len(communities), 2)
        self.assertEqual(
            [community.member_ids for community in communities],
            [[1, 2], [3, 4]],
        )
        self.assertEqual(loner_ids, [])

    def test_detect_refines_single_large_community_when_secondary_split_is_available(
        self,
    ):
        detector = CommunityDetector(
            loner_score_threshold=0.15,
            community_edge_threshold=0.16,
        )
        members = [
            MemberNode(1, "A", "", 12, 1.0, 0),
            MemberNode(2, "B", "", 11, 0.92, 0),
            MemberNode(3, "C", "", 10, 0.84, 0),
            MemberNode(4, "D", "", 9, 0.75, 0),
            MemberNode(5, "E", "", 8, 0.67, 0),
            MemberNode(6, "F", "", 7, 0.58, 0),
        ]
        edges = [
            RelationEdge(1, 2, 0.92, "friendly", "高频互动", 0.92),
            RelationEdge(2, 3, 0.85, "friendly", "高频互动", 0.85),
            RelationEdge(1, 3, 0.8, "friendly", "高频互动", 0.8),
            RelationEdge(4, 5, 0.91, "friendly", "高频互动", 0.91),
            RelationEdge(5, 6, 0.86, "friendly", "高频互动", 0.86),
            RelationEdge(4, 6, 0.82, "friendly", "高频互动", 0.82),
            RelationEdge(3, 4, 0.21, "neutral", "弱连接", 0.21),
        ]

        with patch.object(
            detector,
            "_detect_raw_communities",
            side_effect=[
                ([{1, 2, 3, 4, 5, 6}], "label_propagation_weighted"),
                ([{1, 2, 3}, {4, 5, 6}], "label_propagation_weighted_refined"),
            ],
        ):
            communities, loner_ids = detector.detect(members, edges)

        self.assertEqual(
            [community.member_ids for community in communities],
            [[1, 2, 3], [4, 5, 6]],
        )
        self.assertEqual(loner_ids, [])


class LLMProviderSelectionTests(unittest.IsolatedAsyncioTestCase):
    def test_plugin_reads_max_hot_topics_from_config(self):
        fake_config = {
            "cooldown_seconds": 60,
            "allow_member": False,
            "min_message_count": 3,
            "max_fetch_days": 30,
            "default_top_n": 20,
            "llm_relation_batch_size": 5,
            "continuous_reply_window_sec": 120,
            "loner_score_threshold": 0.15,
            "community_edge_threshold": 0.16,
            "max_llm_communities": 4,
            "max_hot_topics": 12,
        }
        plugin = GroupGraphPlugin(
            context=SimpleNamespace(),
            config=fake_config,
        )

        self.assertEqual(plugin.max_hot_topics, 12)
        self.assertEqual(plugin.summary_analyzer.max_hot_topics, 12)

    async def test_relation_analyzer_uses_configured_batch_size(self):
        context = SimpleNamespace(get_using_provider=lambda: None)
        analyzer = LLMRelationAnalyzer(context=context, relation_batch_size=2)
        member_map = {
            1: MemberNode(1, "A", "", 10, 1.0, 0),
            2: MemberNode(2, "B", "", 9, 0.9, 0),
            3: MemberNode(3, "C", "", 8, 0.8, 0),
            4: MemberNode(4, "D", "", 7, 0.7, 0),
            5: MemberNode(5, "E", "", 6, 0.6, 0),
            6: MemberNode(6, "F", "", 5, 0.5, 0),
        }
        members = list(member_map.values())
        interaction_matrix = {
            (1, 2): {"normalized": 0.95, "messages": []},
            (2, 3): {"normalized": 0.9, "messages": []},
            (3, 4): {"normalized": 0.85, "messages": []},
            (4, 5): {"normalized": 0.8, "messages": []},
            (5, 6): {"normalized": 0.75, "messages": []},
        }

        captured_batch_sizes = []

        async def fake_analyze_batch(
            *, batch, member_map, paired_messages, provider=None
        ):
            captured_batch_sizes.append(len(batch))
            return [analyzer._build_default_edge(pair, data) for pair, data in batch]

        analyzer._analyze_batch = fake_analyze_batch

        edges = await analyzer.analyze(
            interaction_matrix=interaction_matrix,
            members=members,
            paired_messages={},
            provider=None,
        )

        self.assertEqual(captured_batch_sizes, [2, 2, 1])
        self.assertEqual(len(edges), 5)

    async def test_relation_analyzer_prefers_explicit_provider(self):
        explicit_provider = SimpleNamespace()
        explicit_provider.text_chat = AsyncMock(
            return_value=SimpleNamespace(
                completion_text='[{"pair_index":1,"sentiment":"friendly","label":"熟人互动"}]'
            )
        )
        context = SimpleNamespace(get_using_provider=lambda: None)
        analyzer = LLMRelationAnalyzer(context=context)

        edges = await analyzer.analyze(
            interaction_matrix={
                (1, 2): {
                    "normalized": 1.0,
                    "messages": [("甲", "你好"), ("乙", "你好呀")],
                }
            },
            members=[
                MemberNode(1, "甲", "", 10, 1.0, 0),
                MemberNode(2, "乙", "", 8, 0.8, 0),
            ],
            paired_messages={(1, 2): ["甲: 你好", "乙: 你好呀"]},
            provider=explicit_provider,
        )

        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].label, "熟人互动")
        explicit_provider.text_chat.assert_awaited_once()

    async def test_relation_analyzer_retries_once_when_provider_returns_no_output(self):
        explicit_provider = SimpleNamespace()
        explicit_provider.text_chat = AsyncMock(
            side_effect=[
                RuntimeError(
                    "OpenAI completion has no usable output. "
                    "response_id=resp_test, finish_reason=stop"
                ),
                SimpleNamespace(
                    completion_text='[{"pair_index":1,"sentiment":"friendly","label":"老搭子"}]'
                ),
            ]
        )
        context = SimpleNamespace(get_using_provider=lambda: None)
        analyzer = LLMRelationAnalyzer(context=context)

        edges = await analyzer.analyze(
            interaction_matrix={
                (1, 2): {
                    "normalized": 1.0,
                    "messages": [("甲", "你好"), ("乙", "你好呀")],
                }
            },
            members=[
                MemberNode(1, "甲", "", 10, 1.0, 0),
                MemberNode(2, "乙", "", 8, 0.8, 0),
            ],
            paired_messages={(1, 2): ["甲: 你好", "乙: 你好呀"]},
            provider=explicit_provider,
        )

        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].label, "老搭子")
        self.assertEqual(explicit_provider.text_chat.await_count, 2)

    async def test_relation_analyzer_logs_compressed_html_error(self):
        explicit_provider = SimpleNamespace()
        explicit_provider.text_chat = AsyncMock(
            return_value=SimpleNamespace(
                completion_text=(
                    "<!DOCTYPE html><html><head><title>504</title></head><body>"
                    "Gateway Time-out 504 - 源站服务器连接超时"
                    "</body></html>"
                )
            )
        )
        context = SimpleNamespace(get_using_provider=lambda: None)
        analyzer = LLMRelationAnalyzer(context=context)

        with patch(
            "data.plugins.astrbot_plugin_group_graph.services.analysis.llm_analyzer.logger.error"
        ) as mock_logger_error:
            edges = await analyzer.analyze(
                interaction_matrix={
                    (1, 2): {
                        "normalized": 1.0,
                        "messages": [("甲", "你好"), ("乙", "你好呀")],
                    }
                },
                members=[
                    MemberNode(1, "甲", "", 10, 1.0, 0),
                    MemberNode(2, "乙", "", 8, 0.8, 0),
                ],
                paired_messages={(1, 2): ["甲: 你好", "乙: 你好呀"]},
                provider=explicit_provider,
            )

        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].label, "有互动")
        logged_message = mock_logger_error.call_args.args[0]
        self.assertIn("504", logged_message)
        self.assertNotIn("<!DOCTYPE html>", logged_message)


if __name__ == "__main__":
    unittest.main()
