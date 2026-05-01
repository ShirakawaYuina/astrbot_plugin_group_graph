from __future__ import annotations

import json
import re
from datetime import datetime
from html import escape
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_group_graph"


class HtmlTemplateRenderer:
    """负责读取图谱模板、准备渲染载荷，并额外落地一个本地预览文件。"""

    def __init__(
        self,
        template_path: Path | None = None,
        preview_dir: Path | None = None,
        archive_dir: Path | None = None,
    ):
        default_plugin_root = Path(__file__).resolve().parents[1]
        self.template_path = template_path or (
            default_plugin_root / "templates" / "graph.html"
        )
        self.plugin_root = self.template_path.resolve().parents[1]
        self.preview_dir = preview_dir or (self.plugin_root / "preview")
        self.archive_dir = archive_dir or (
            Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "html"
        )

    def render(
        self,
        graph_data: dict,
        group_name: str,
        analysis_desc: str,
        stats_start_date: str = "",
        stats_start_time: str = "",
        stats_end_date: str = "",
        stats_end_time: str = "",
        archive_name: str | None = None,
    ) -> tuple[str, None]:
        """返回已完成变量替换的最终 HTML，并同步生成 preview/preview.html。"""

        template_html = self.template_path.read_text(encoding="utf-8")
        template_data = {
            "graph_data": json.dumps(graph_data, ensure_ascii=False),
            "group_name": group_name,
            "analysis_desc": analysis_desc,
            "stats_start_date": stats_start_date,
            "stats_start_time": stats_start_time,
            "stats_end_date": stats_end_date,
            "stats_end_time": stats_end_time,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        rendered_html = self._build_preview_html(
            template_html=template_html,
            template_data=template_data,
        )
        self._write_preview_file(rendered_html=rendered_html)
        self._write_archive_file(
            rendered_html=rendered_html,
            archive_name=archive_name,
        )
        return rendered_html, None

    def _write_preview_file(self, *, rendered_html: str) -> None:
        """把最终渲染 HTML 输出到插件目录下的 preview/preview.html。"""

        self.preview_dir.mkdir(parents=True, exist_ok=True)
        preview_path = self.preview_dir / "preview.html"
        preview_path.write_text(rendered_html, encoding="utf-8")

    def _write_archive_file(
        self,
        *,
        rendered_html: str,
        archive_name: str | None,
    ) -> None:
        """将每次生成的 HTML 归档到插件数据目录，便于按群和时间追溯结果。"""

        if not archive_name:
            return

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self.archive_dir / f"{archive_name}.html"
        archive_path.write_text(rendered_html, encoding="utf-8")

    def _build_preview_html(self, *, template_html: str, template_data: dict) -> str:
        """最小化地替换模板变量，生成可以直接本地打开的调试页面。"""

        preview_html = template_html
        preview_html = preview_html.replace(
            "{{ graph_data | safe }}",
            template_data["graph_data"],
        )

        html_fields = {
            "group_name": escape(str(template_data["group_name"])),
            "analysis_desc": escape(str(template_data["analysis_desc"])),
            "stats_start_date": escape(str(template_data["stats_start_date"])),
            "stats_start_time": escape(str(template_data["stats_start_time"])),
            "stats_end_date": escape(str(template_data["stats_end_date"])),
            "stats_end_time": escape(str(template_data["stats_end_time"])),
            "timestamp": escape(str(template_data["timestamp"])),
        }
        for field_name, field_value in html_fields.items():
            preview_html = re.sub(
                rf"\{{\{{\s*{field_name}\s*\}}\}}",
                field_value,
                preview_html,
            )

        return preview_html
