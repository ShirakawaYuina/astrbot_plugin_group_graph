from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_group_graph"


class ImageSender:
    """调用插件实例的 html_render，并将生成图片归档到插件数据目录。"""

    def __init__(self, owner: Any, archive_dir: Path | None = None):
        self.owner = owner
        self.archive_dir = archive_dir or (
            Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "images"
        )

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
