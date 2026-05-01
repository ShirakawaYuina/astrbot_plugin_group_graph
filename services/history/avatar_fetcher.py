from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import aiohttp

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_group_graph"
AVATAR_CACHE_DIR = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "avatars"

DEFAULT_AVATAR_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">
  <defs>
    <radialGradient id="g" cx="35%" cy="28%">
      <stop offset="0%" stop-color="#a78bfa"/>
      <stop offset="100%" stop-color="#7c6bff"/>
    </radialGradient>
  </defs>
  <circle cx="50" cy="50" r="50" fill="url(#g)"/>
  <text x="50" y="55" dominant-baseline="middle" text-anchor="middle"
    font-family="PingFang SC,Microsoft YaHei,sans-serif"
    font-size="40px" font-weight="700" fill="rgba(255,255,255,0.95)">?</text>
</svg>"""


class AvatarFetcher:
    """负责获取并缓存 QQ 头像，供 HTML 渲染阶段直接内嵌使用。"""

    def __init__(self):
        self.cache_dir = AVATAR_CACHE_DIR
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=3)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def fetch_avatar(self, user_id: int) -> str:
        cache_path = self.cache_dir / f"{user_id}.jpg"
        if cache_path.exists():
            try:
                image_bytes = cache_path.read_bytes()
                return (
                    f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode()}"
                )
            except Exception:
                pass

        avatar_url = f"https://q.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=100"
        try:
            session = await self._get_session()
            async with session.get(avatar_url) as response:
                if response.status == 200:
                    image_bytes = await response.read()
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(image_bytes)
                    return f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode()}"
        except Exception:
            pass

        return self._get_default_avatar()

    async def fetch_avatars(self, user_ids: list[int]) -> dict[int, str]:
        avatar_results = await asyncio.gather(
            *(self.fetch_avatar(user_id) for user_id in user_ids),
            return_exceptions=True,
        )
        return {
            user_id: avatar if isinstance(avatar, str) else self._get_default_avatar()
            for user_id, avatar in zip(user_ids, avatar_results)
        }

    def _get_default_avatar(self) -> str:
        return (
            "data:image/svg+xml;base64,"
            f"{base64.b64encode(DEFAULT_AVATAR_SVG.encode()).decode()}"
        )
