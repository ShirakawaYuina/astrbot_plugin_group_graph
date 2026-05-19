from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from astrbot.core import astrbot_config, file_token_service
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_group_graph"
SEND_MODE_BASE64 = "base64"
SEND_MODE_URL = "url"


class ImageSender:
    """调用插件实例的 html_render，并将生成图片归档到插件数据目录。"""

    def __init__(
        self,
        owner: Any,
        archive_dir: Path | None = None,
        send_mode: str = SEND_MODE_BASE64,
        url_base: str = "",
    ):
        self.owner = owner
        self.archive_dir = archive_dir or (
            Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "images"
        )
        self.send_mode = self._normalize_send_mode(send_mode)
        self.url_base = str(url_base or "").strip()

    async def send(
        self,
        template_html: str,
        template_data: dict | None,
        group_id: str | None = None,
        archive_timestamp: str | None = None,
    ) -> str:
        """渲染图片并在提供归档信息时复制到插件数据目录。"""

        # 渲染器在运行时可能直接返回最终 HTML，此时模板变量载荷会是 None。
        # AstrBot 的 html_render 第二个参数要求为字典，这里统一兜底为空字典，
        # 避免框架内部做字典合并时再次触发 `dict | NoneType` 异常。
        normalized_template_data = template_data or {}
        rendered_image_path = await self.owner.html_render(
            template_html,
            normalized_template_data,
            return_url=False,
            options={
                "full_page": True,
                "type": "png",
                "quality": 100,
                "scale": "device",
                "device_scale_factor_level": "high",
            },
        )
        return self._archive_image(
            rendered_image_path=rendered_image_path,
            group_id=group_id,
            archive_timestamp=archive_timestamp,
        )

    def _archive_image(
        self,
        *,
        rendered_image_path: str,
        group_id: str | None,
        archive_timestamp: str | None,
    ) -> str:
        """按群号和时间戳归档图片，保证每次命令都能回溯原始产物。"""

        if not group_id or not archive_timestamp:
            return rendered_image_path

        source_image_path = Path(rendered_image_path)
        archive_path = self.archive_dir / f"{group_id}_{archive_timestamp}.png"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_image_path, archive_path)
        return str(archive_path)

    async def send_rendered_image(self, event: Any, image_path: str):
        """按配置发送已渲染图片，URL 模式会直接调用 OneBot v11 接口。"""

        if self.send_mode == SEND_MODE_URL:
            await self._send_image_by_url(event=event, image_path=Path(image_path))
            return None

        # base64 模式沿用 AstrBot 图片链路：交给平台适配器把本地图片转为
        # 对应协议可发送的数据，保持旧版本插件的行为兼容。
        return event.image_result(image_path)

    async def _send_image_by_url(self, event: Any, image_path: Path) -> None:
        """注册图片 URL 并通过 aiocqhttp / OneBot v11 原生接口发送。"""

        platform_name = str(event.get_platform_name() or "").strip()
        if platform_name != "aiocqhttp":
            raise RuntimeError("URL 图片发送模式仅支持 aiocqhttp / OneBot v11")

        bot = getattr(event, "bot", None)
        if bot is None:
            raise RuntimeError("URL 图片发送模式需要事件对象暴露 bot 实例")

        image_message = {
            "type": "image",
            "data": {
                "file": await self._register_image_url(image_path),
            },
        }

        group_id = str(event.get_group_id() or "").strip()
        if group_id.isdigit():
            await bot.send_group_msg(group_id=int(group_id), message=[image_message])
            return

        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id.isdigit():
            await bot.send_private_msg(user_id=int(sender_id), message=[image_message])
            return

        raise RuntimeError("URL 图片发送模式缺少有效的群号或用户 ID")

    async def _register_image_url(self, image_path: Path) -> str:
        """将本地图片注册到 AstrBot 文件服务，返回 OneBot 可访问的 HTTP 地址。"""

        callback_base = self._resolve_url_base()
        token = await file_token_service.register_file(str(image_path))
        return f"{callback_base}/api/file/{token}"

    def _resolve_url_base(self) -> str:
        """读取插件 URL 前缀，未配置时回退到 AstrBot 全局回调地址。"""

        configured_base = (
            self.url_base
            or str(astrbot_config.get("callback_api_base", "") or "").strip()
        )
        clean_base = configured_base.rstrip("/")
        if not clean_base:
            raise RuntimeError(
                "URL 图片发送模式需要配置 image_send_url_base 或 AstrBot callback_api_base"
            )
        return clean_base

    @staticmethod
    def _normalize_send_mode(send_mode: str) -> str:
        """规范化发送模式，未知值回到 base64，避免旧配置升级后不可用。"""

        clean_mode = str(send_mode or "").strip().lower()
        if clean_mode == SEND_MODE_URL:
            return SEND_MODE_URL
        return SEND_MODE_BASE64
