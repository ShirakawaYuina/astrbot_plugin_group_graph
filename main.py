from __future__ import annotations

import time
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .rendering.renderer import HtmlTemplateRenderer
from .rendering.sender import ImageSender
from .services.analysis.counter import InteractionCounter
from .services.analysis.llm_analyzer import LLMRelationAnalyzer
from .services.analysis.llm_summary import LLMSummaryAnalyzer
from .services.history.avatar_fetcher import AvatarFetcher
from .services.history.fetcher import MessageFetcher
from .services.history.preprocessor import MessagePreprocessor


class GroupGraphPlugin(Star):
    """群关系图谱插件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        self.cooldown_seconds = self.config.get("cooldown_seconds", 60)
        self.allow_member = self.config.get("allow_member", False)
        self.include_bot_messages = bool(self.config.get("include_bot_messages", False))
        self.min_message_count = self.config.get("min_message_count", 3)
        self.max_fetch_days = max(1, int(self.config.get("max_fetch_days", 30) or 30))
        self.default_top_n = self.config.get("default_top_n", 20)
        self.llm_relation_batch_size = max(
            1,
            int(self.config.get("llm_relation_batch_size", 5) or 5),
        )
        self.llm_provider_id = str(self.config.get("llm_provider_id", "") or "").strip()
        self.continuous_reply_window_sec = self.config.get(
            "continuous_reply_window_sec", 120
        )
        self.loner_score_threshold = self.config.get("loner_score_threshold", 0.15)
        self.relation_threshold = self.config.get("relation_threshold", 0.1)
        self.community_edge_threshold = self.config.get(
            "community_edge_threshold",
            max(0.16, self.relation_threshold),
        )
        self.max_llm_communities = self.config.get("max_llm_communities", 4)
        self.max_hot_topics = max(1, int(self.config.get("max_hot_topics", 10) or 10))
        self.image_send_mode = str(
            self.config.get("image_send_mode", "base64") or "base64"
        ).strip()
        self.image_send_url_base = str(
            self.config.get("image_send_url_base", "") or ""
        ).strip()

        self._cooldowns: dict[str, float] = {}
        self.fetcher = MessageFetcher()
        self.counter = InteractionCounter(self.continuous_reply_window_sec)
        self.llm_analyzer = LLMRelationAnalyzer(
            context,
            relation_threshold=self.relation_threshold,
            relation_batch_size=self.llm_relation_batch_size,
        )
        self.summary_analyzer = LLMSummaryAnalyzer(
            context,
            loner_score_threshold=self.loner_score_threshold,
            community_edge_threshold=self.community_edge_threshold,
            max_llm_communities=self.max_llm_communities,
            max_hot_topics=self.max_hot_topics,
        )
        self.avatar_fetcher = AvatarFetcher()
        self.renderer = HtmlTemplateRenderer()
        self.sender = ImageSender(
            self,
            send_mode=self.image_send_mode,
            url_base=self.image_send_url_base,
        )

    def _get_session_type(self, event: AstrMessageEvent) -> str:
        """根据事件判断当前会话类型，便于统一输出日志。"""

        return "群聊" if str(event.get_group_id() or "").strip() else "私聊"

    def _log_analysis_event(self, phase: str, **fields) -> None:
        """输出结构化日志，方便排查每个阶段的输入与结果。"""

        serialized_fields = " ".join(
            f"{key}={str(value).replace(chr(10), ' ').strip()}"
            for key, value in fields.items()
        )
        logger.info(f"[GroupGraph] {phase} {serialized_fields}".rstrip())

    def _is_cooldown_active(self, group_id: str) -> bool:
        """检查当前群是否还处于冷却期。"""

        if group_id not in self._cooldowns:
            return False
        if time.time() - self._cooldowns[group_id] < self.cooldown_seconds:
            return True
        del self._cooldowns[group_id]
        return False

    def _set_cooldown(self, group_id: str) -> None:
        """在开始分析时写入冷却时间。"""

        self._cooldowns[group_id] = time.time()

    def _clear_cooldown(self, group_id: str) -> None:
        """在失败或提前结束时清理冷却，避免误伤后续请求。"""

        self._cooldowns.pop(group_id, None)

    def _get_remaining_cooldown(self, group_id: str) -> int:
        """返回剩余冷却秒数，用于友好提示。"""

        if group_id not in self._cooldowns:
            return 0
        elapsed = time.time() - self._cooldowns[group_id]
        return max(0, int(self.cooldown_seconds - elapsed))

    async def _check_permission(self, event: AstrMessageEvent) -> bool:
        """校验当前用户是否有权限触发图谱分析。"""

        if self.allow_member:
            return True

        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        if not group_id:
            return True

        # 这里的“管理员”明确指 AstrBot 配置中的 Bot 管理员，
        # 不再混用 QQ 群管理员/群主角色，避免权限语义和配置文案不一致。
        is_bot_admin = bool(event.is_admin())
        self._log_analysis_event(
            "权限检查完成",
            群组=group_id,
            发送者=sender_id,
            是否机器人管理员=is_bot_admin,
            允许普通成员=self.allow_member,
        )
        return is_bot_admin

    def _parse_range_param(self, value: str) -> tuple[str, int]:
        """解析命令中的统计天数，仅接受 `xd` 形式。"""

        normalized_value = value.strip().lower()
        if normalized_value.endswith("d") and normalized_value[:-1].isdigit():
            return "time", max(1, int(normalized_value[:-1]))
        return "time", 1

    def _parse_command_options(
        self,
        range_param: str,
        raw_message: str,
    ) -> tuple[str, int]:
        """统一解析命令参数，只保留默认 1 天和显式 `xd` 两种入口。"""

        normalized_message = (raw_message or "").strip()
        if normalized_message:
            range_tokens = normalized_message.split()
            if range_tokens and range_tokens[0].lower() in {
                "group_graph",
                "/group_graph",
            }:
                range_tokens = range_tokens[1:]
            if range_tokens:
                range_param = range_tokens[0]

        range_type, range_value = self._parse_range_param(range_param or "1d")
        if range_type == "time":
            # 命令支持传入任意天数，但最终必须受插件配置约束，
            # 这样配置项文案“最大时间跨度”才与真实执行行为一致。
            range_value = min(range_value, self.max_fetch_days)
        return range_type, range_value

    def _get_bot_user_id(self, event: AstrMessageEvent) -> int | None:
        """尽量从事件或 bot 对象中解析机器人自身 ID。"""

        getter = getattr(event, "get_self_id", None)
        if callable(getter):
            candidate = str(getter() or "").strip()
            if candidate.isdigit():
                return int(candidate)

        bot = getattr(event, "bot", None)
        for attr_name in ("qq", "self_id", "bot_id"):
            candidate = getattr(bot, attr_name, None)
            candidate_text = str(candidate or "").strip()
            if candidate_text.isdigit():
                return int(candidate_text)
        return None

    def _resolve_llm_provider(self):
        """优先使用插件显式配置的模型，未配置时回退到当前会话模型。"""

        if self.llm_provider_id:
            provider = self.context.get_provider_by_id(self.llm_provider_id)
            if provider is not None:
                return provider, self.llm_provider_id, "plugin_config"
            return None, self.llm_provider_id, "plugin_config_missing"

        provider = self.context.get_using_provider()
        if provider is None:
            return None, "", "context_default_missing"
        return provider, "", "context_default"

    def _format_stats_parts(
        self, stats_timestamps: list[int]
    ) -> tuple[str, str, str, str]:
        """把起止时间拆成日期与时间两个层级，供模板双行展示。"""

        if not stats_timestamps:
            return "", "", "", ""

        start_time = datetime.fromtimestamp(stats_timestamps[0])
        end_time = datetime.fromtimestamp(stats_timestamps[-1])
        return (
            start_time.strftime("%m-%d"),
            start_time.strftime("%H:%M"),
            end_time.strftime("%m-%d"),
            end_time.strftime("%H:%M"),
        )

    def _resolve_group_name(self, event: AstrMessageEvent, group_id: str) -> str:
        """优先从事件对象读取真实群名，缺失时再回退到群号。"""

        # 大多数平台适配器会把群信息挂在 message_obj.group 上，
        # 这里优先复用现成字段，避免为了标题额外调用群资料接口。
        message_obj = getattr(event, "message_obj", None)
        group_info = getattr(message_obj, "group", None)
        group_name = str(getattr(group_info, "group_name", "") or "").strip()
        if group_name:
            return group_name
        return str(group_id or "").strip()

    @filter.command("group_graph")
    async def analyze_group_graph(
        self, event: AstrMessageEvent, range_param: str = "1d"
    ):
        """生成指定群聊的关系图谱。"""

        group_id = event.get_group_id()
        session_type = self._get_session_type(event)
        raw_message = (
            event.get_message_str() if hasattr(event, "get_message_str") else ""
        )
        self._log_analysis_event(
            "收到关系图谱命令",
            会话类型=session_type,
            平台=event.get_platform_id(),
            发送者=event.get_sender_id(),
            群组=str(group_id or "").strip(),
            消息=str(raw_message or "").strip(),
        )

        if not group_id:
            self._log_analysis_event(
                "解析分析目标失败",
                会话类型=session_type,
                发送者=event.get_sender_id(),
                原因="非群聊会话",
            )
            yield event.plain_result("此指令仅可在群聊中使用。")
            return

        if not await self._check_permission(event):
            self._log_analysis_event(
                "权限不足拒绝执行",
                会话类型=session_type,
                群组=group_id,
                发送者=event.get_sender_id(),
            )
            yield event.plain_result("权限不足，需要 Bot 管理员权限。")
            return

        if self._is_cooldown_active(str(group_id)):
            remaining_seconds = self._get_remaining_cooldown(str(group_id))
            self._log_analysis_event(
                "命中冷却",
                会话类型=session_type,
                群组=group_id,
                剩余秒数=remaining_seconds,
            )
            yield event.plain_result(f"分析冷却中，请 {remaining_seconds} 秒后再试。")
            return

        range_type, range_value = self._parse_command_options(
            range_param=range_param,
            raw_message=raw_message,
        )
        self._log_analysis_event(
            "已解析分析参数",
            会话类型=session_type,
            群组=group_id,
            范围类型=range_type,
            范围值=range_value,
            TopN=self.default_top_n,
        )

        provider, provider_id, provider_source = self._resolve_llm_provider()
        if provider is None and provider_source == "plugin_config_missing":
            self._log_analysis_event(
                "关系图谱模型不存在",
                会话类型=session_type,
                群组=group_id,
                模型提供商=provider_id,
            )
            yield event.plain_result("当前配置的关系图谱模型不存在，请检查插件配置。")
            return

        self._log_analysis_event(
            "已解析模型提供商",
            会话类型=session_type,
            群组=group_id,
            模型提供商=provider_id or "默认会话模型",
            模型来源=provider_source,
        )

        self._set_cooldown(str(group_id))
        yield event.plain_result("正在分析群关系图谱，请稍候...")

        try:
            messages = await self.fetcher.fetch_messages(
                event=event,
                group_id=str(group_id),
                range_type=range_type,
                range_value=range_value,
            )
            self._log_analysis_event(
                "历史消息加载完成",
                会话类型=session_type,
                群组=group_id,
                历史条数=len(messages),
                范围类型=range_type,
                范围值=range_value,
                时间跨度上限天数=self.max_fetch_days,
            )
            if len(messages) < 20:
                self._clear_cooldown(str(group_id))
                self._log_analysis_event(
                    "消息不足无法分析",
                    会话类型=session_type,
                    群组=group_id,
                    历史条数=len(messages),
                    最低要求=20,
                )
                yield event.plain_result("消息数量不足，无法生成有效图谱。")
                return

            bot_user_id = self._get_bot_user_id(event)
            preprocessor = MessagePreprocessor(
                min_message_count=self.min_message_count,
                continuous_reply_window_sec=self.continuous_reply_window_sec,
                bot_user_id=bot_user_id,
                include_bot_messages=self.include_bot_messages,
            )
            processed_data = preprocessor.process(messages)
            self._log_analysis_event(
                "消息预处理完成",
                会话类型=session_type,
                群组=group_id,
                有效消息数=len(processed_data.messages),
                有效成员数=len(processed_data.members),
                成员阈值=self.min_message_count,
                关系样本对数=len(processed_data.paired_messages),
                机器人ID=bot_user_id or "",
                纳入机器人消息=self.include_bot_messages,
            )
            self._log_analysis_event(
                "成员过滤统计",
                会话类型=session_type,
                群组=group_id,
                发言成员总数=processed_data.total_speaking_members,
                阈值过滤成员数=processed_data.filtered_out_member_count,
                成员阈值=self.min_message_count,
            )
            if len(processed_data.members) < 2:
                self._clear_cooldown(str(group_id))
                self._log_analysis_event(
                    "成员不足无法分析",
                    会话类型=session_type,
                    群组=group_id,
                    有效成员数=len(processed_data.members),
                )
                yield event.plain_result("有效成员数量不足，无法生成图谱。")
                return

            interaction_matrix = self.counter.count(processed_data.messages)
            self._log_analysis_event(
                "互动计分完成",
                会话类型=session_type,
                群组=group_id,
                关系对数=len(interaction_matrix),
                连续对话窗口秒=self.continuous_reply_window_sec,
            )

            relation_edges = await self.llm_analyzer.analyze(
                interaction_matrix=interaction_matrix,
                members=processed_data.members,
                paired_messages=processed_data.paired_messages,
                provider=provider,
            )
            self._log_analysis_event(
                "关系分析完成",
                会话类型=session_type,
                群组=group_id,
                边数量=len(relation_edges),
                LLM阈值=self.relation_threshold,
                模型提供商=provider_id or "默认会话模型",
            )

            member_ids = [member.user_id for member in processed_data.members]
            avatar_map = await self.avatar_fetcher.fetch_avatars(member_ids)
            max_message_count = max(
                member.message_count for member in processed_data.members
            )
            for member in processed_data.members:
                member.avatar_b64 = avatar_map.get(member.user_id, "")
                member.activity_score = (
                    member.message_count / max_message_count
                    if max_message_count
                    else 1.0
                )
            self._log_analysis_event(
                "头像加载完成",
                会话类型=session_type,
                群组=group_id,
                成员数=len(member_ids),
                命中头像数=sum(
                    1
                    for member in processed_data.members
                    if str(member.avatar_b64 or "").strip()
                ),
            )

            graph_data = await self.summary_analyzer.analyze(
                members=processed_data.members,
                messages=processed_data.messages,
                interaction_matrix=interaction_matrix,
                relation_edges=relation_edges,
                top_n=self.default_top_n,
                provider=provider,
            )
            self._log_analysis_event(
                "图数据构建完成",
                会话类型=session_type,
                群组=group_id,
                入图节点数=len(graph_data.get("nodes", [])),
                入图边数=len(graph_data.get("links", [])),
                小团体数=len(graph_data.get("communities", [])),
                游离成员数=len(graph_data.get("loner_ids", [])),
                模型提供商=provider_id or "默认会话模型",
            )

            stats_timestamps = sorted(
                int(message.timestamp)
                for message in messages
                if int(getattr(message, "timestamp", 0) or 0) > 0
            )
            (
                stats_start_date,
                stats_start_time,
                stats_end_date,
                stats_end_time,
            ) = self._format_stats_parts(stats_timestamps)
            archive_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            archive_group_id = str(group_id).strip()
            archive_name = f"{archive_group_id}_{archive_timestamp}"

            template_html, template_data = self.renderer.render(
                graph_data=graph_data,
                group_name=self._resolve_group_name(event, str(group_id)),
                analysis_desc=(
                    f"近 {range_value}{' 天' if range_type == 'time' else ' 条消息'}"
                    f" · 原始消息 {len(messages)} 条 · 入图成员 {len(graph_data['nodes'])} 人"
                ),
                stats_start_date=stats_start_date,
                stats_start_time=stats_start_time,
                stats_end_date=stats_end_date,
                stats_end_time=stats_end_time,
                archive_name=archive_name,
            )
            self._log_analysis_event(
                "模板渲染载荷准备完成",
                会话类型=session_type,
                群组=group_id,
                模板字符数=len(template_html),
                # 渲染器现在会直接返回已内联变量的完整 HTML，
                # 因此运行时模板载荷可能为 None，这里需要按 0 字段兼容日志统计。
                载荷字段数=len(template_data or {}),
            )

            image_path = await self.sender.send(
                template_html,
                template_data,
                group_id=archive_group_id,
                archive_timestamp=archive_timestamp,
            )
            self._log_analysis_event(
                "关系图谱渲染完成",
                会话类型=session_type,
                群组=group_id,
                图片路径=image_path,
                图片发送模式=self.sender.send_mode,
            )
            send_result = await self.sender.send_rendered_image(event, image_path)
            if send_result is not None:
                yield send_result
            self._log_analysis_event(
                "关系图谱发送完成",
                会话类型=session_type,
                群组=group_id,
                图片路径=image_path,
                图片发送模式=self.sender.send_mode,
            )
        except Exception as exc:  # noqa: BLE001 - 需要向用户返回失败原因
            self._clear_cooldown(str(group_id))
            self._log_analysis_event(
                "生成群关系图谱失败",
                会话类型=session_type,
                群组=group_id,
                原因=str(exc),
            )
            logger.error(f"生成群关系图谱失败: {exc}")
            yield event.plain_result(f"图谱渲染失败，请稍后重试。错误: {exc}")
