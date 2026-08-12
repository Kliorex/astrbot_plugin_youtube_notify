"""AstrBot 插件：YouTube 频道更新推送（基于官方 RSS，无需 API Key）。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.api.event.filter import GreedyStr

# Atom namespaces used by YouTube RSS
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
HANDLE_RE = re.compile(r"^@?[\w.-]{3,}$")


@dataclass
class VideoItem:
    video_id: str
    title: str
    url: str
    published: str
    author: str
    thumbnail: str = ""
    description: str = ""


def _parse_iso(ts: str) -> datetime:
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def extract_channel_id_from_html(html: str) -> Optional[str]:
    """从频道页 HTML 中提取 channel_id。"""
    patterns = [
        r'"channelId"\s*:\s*"(UC[\w-]{22})"',
        r'"externalId"\s*:\s*"(UC[\w-]{22})"',
        r'<meta\s+itemprop="channelId"\s+content="(UC[\w-]{22})"',
        r'"browse_id"\s*:\s*"(UC[\w-]{22})"',
        r"/channel/(UC[\w-]{22})",
    ]
    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1)
    return None


def extract_handle_or_id(raw: str) -> tuple[str, str]:
    """
    解析用户输入，返回 (kind, value)
    kind: channel_id | handle | url
    """
    s = (raw or "").strip()
    if not s:
        return "empty", ""

    if CHANNEL_ID_RE.match(s):
        return "channel_id", s

    if s.startswith("@") or (HANDLE_RE.match(s) and not s.startswith("http")):
        handle = s if s.startswith("@") else f"@{s}"
        return "handle", handle

    if "youtube.com" in s or "youtu.be" in s:
        return "url", s

    # 兜底当 handle
    if HANDLE_RE.match(s):
        return "handle", f"@{s}" if not s.startswith("@") else s

    return "unknown", s


class YouTubeClient:
    def __init__(self, proxy: str = "", user_agent: str = ""):
        self.proxy = proxy.strip() or None
        self.user_agent = user_agent or "Mozilla/5.0 (compatible; AstrBot-YouTubeNotify/1.0)"

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    async def _get_text(self, url: str, timeout: float = 20.0) -> str:
        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg, headers=self._headers()) as session:
            async with session.get(url, proxy=self.proxy, allow_redirects=True) as resp:
                resp.raise_for_status()
                return await resp.text()

    async def resolve_channel_id(self, raw: str) -> tuple[str, str]:
        """
        返回 (channel_id, display_name_hint)
        display_name_hint 可能为空，后续从 RSS 补全。
        """
        kind, value = extract_handle_or_id(raw)
        if kind == "channel_id":
            return value, ""

        if kind == "handle":
            url = f"https://www.youtube.com/{value}"
            html = await self._get_text(url)
            cid = extract_channel_id_from_html(html)
            if not cid:
                raise ValueError(f"无法从 handle 解析 channel_id: {value}")
            return cid, value

        if kind == "url":
            parsed = urlparse(value)
            path = unquote(parsed.path or "")
            qs = parse_qs(parsed.query or "")

            # /channel/UCxxxx
            m = re.search(r"/channel/(UC[\w-]{22})", path)
            if m:
                return m.group(1), ""

            # /@handle
            m = re.search(r"/(@[\w.-]+)", path)
            if m:
                return await self.resolve_channel_id(m.group(1))

            # /c/name or /user/name
            m = re.search(r"/(?:c|user)/([^/]+)", path)
            if m:
                html = await self._get_text(value)
                cid = extract_channel_id_from_html(html)
                if not cid:
                    raise ValueError(f"无法从频道链接解析 channel_id: {value}")
                return cid, m.group(1)

            # 带 list / 其他页面，尝试整页解析
            html = await self._get_text(value)
            cid = extract_channel_id_from_html(html)
            if cid:
                return cid, ""
            raise ValueError(f"无法识别的 YouTube 链接: {value}")

        raise ValueError(f"无法识别的频道标识: {raw}")

    async def fetch_feed(self, channel_id: str) -> tuple[str, list[VideoItem]]:
        """拉取频道 RSS，返回 (channel_title, videos 按时间新到旧)。"""
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        xml_text = await self._get_text(url)
        root = ET.fromstring(xml_text)

        title_el = root.find("atom:title", NS)
        channel_title = (title_el.text or "").strip() if title_el is not None else channel_id

        videos: list[VideoItem] = []
        for entry in root.findall("atom:entry", NS):
            vid_el = entry.find("yt:videoId", NS)
            title_el = entry.find("atom:title", NS)
            link_el = entry.find("atom:link", NS)
            published_el = entry.find("atom:published", NS)
            author_el = entry.find("atom:author/atom:name", NS)
            thumb_el = entry.find("media:group/media:thumbnail", NS)
            desc_el = entry.find("media:group/media:description", NS)

            video_id = (vid_el.text or "").strip() if vid_el is not None else ""
            if not video_id:
                continue

            title = (title_el.text or "").strip() if title_el is not None else "(无标题)"
            href = link_el.get("href") if link_el is not None else ""
            url_v = href or f"https://www.youtube.com/watch?v={video_id}"
            published = (published_el.text or "").strip() if published_el is not None else ""
            author = (author_el.text or "").strip() if author_el is not None else channel_title
            thumb = thumb_el.get("url") if thumb_el is not None else ""
            desc = (desc_el.text or "").strip() if desc_el is not None else ""

            videos.append(
                VideoItem(
                    video_id=video_id,
                    title=title,
                    url=url_v,
                    published=published,
                    author=author,
                    thumbnail=thumb or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    description=desc,
                )
            )

        videos.sort(key=lambda v: _parse_iso(v.published), reverse=True)
        return channel_title, videos


@register(
    "astrbot_plugin_youtube_notify",
    "local",
    "订阅 YouTube 频道并通过 RSS 推送更新",
    "1.0.0",
)
class YouTubeNotifyPlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = os.path.join("data", "plugin_data", "astrbot_plugin_youtube_notify")
        self.data_file = os.path.join(self.data_dir, "subscriptions.json")
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # structure:
        # {
        #   "channels": { channel_id: {"title": str, "last_video_id": str} },
        #   "sessions": { umo: [channel_id, ...] }
        # }
        self.store: dict[str, Any] = {"channels": {}, "sessions": {}}

    # ---------- lifecycle ----------
    async def initialize(self):
        os.makedirs(self.data_dir, exist_ok=True)
        self._load()
        self._stop.clear()
        self._task = asyncio.create_task(self._poll_loop(), name="youtube_notify_poll")
        logger.info("[youtube_notify] 插件已初始化，订阅频道数=%s", len(self.store.get("channels", {})))

    async def terminate(self):
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._save()
        logger.info("[youtube_notify] 插件已卸载")

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def _client(self) -> YouTubeClient:
        return YouTubeClient(
            proxy=str(self._cfg("http_proxy", "") or ""),
            user_agent=str(self._cfg("user_agent", "") or ""),
        )

    # ---------- persistence ----------
    def _load(self) -> None:
        if not os.path.isfile(self.data_file):
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.store["channels"] = data.get("channels") or {}
                self.store["sessions"] = data.get("sessions") or {}
        except Exception as e:
            logger.error("[youtube_notify] 加载订阅数据失败: %s", e)

    def _save(self) -> None:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = self.data_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.store, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.data_file)
        except Exception as e:
            logger.error("[youtube_notify] 保存订阅数据失败: %s", e)

    # ---------- formatting ----------
    def _format_video(self, channel_title: str, v: VideoItem, prefix: str = "🎬 新视频") -> str:
        published = v.published
        try:
            dt = _parse_iso(v.published)
            if dt.year > 1970:
                published = dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        lines = [
            f"{prefix}",
            f"频道：{channel_title or v.author}",
            f"标题：{v.title}",
            f"时间：{published}",
            f"链接：{v.url}",
        ]
        return "\n".join(lines)

    async def _push_video(self, umo: str, channel_title: str, v: VideoItem) -> None:
        text = self._format_video(channel_title, v)
        include_thumb = bool(self._cfg("include_thumbnail", True))
        try:
            if include_thumb and v.thumbnail:
                chain = MessageChain([Plain(text), Image.fromURL(v.thumbnail)])
            else:
                chain = MessageChain().message(text)
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.warning("[youtube_notify] 图文推送失败，回退纯文本: %s", e)
            try:
                await self.context.send_message(umo, MessageChain().message(text))
            except Exception as e2:
                logger.error("[youtube_notify] 推送到 %s 失败: %s", umo, e2)

    # ---------- core ops ----------
    async def _subscribe(self, umo: str, raw: str) -> str:
        client = self._client()
        channel_id, hint = await client.resolve_channel_id(raw)
        title, videos = await client.fetch_feed(channel_id)
        if not title and hint:
            title = hint

        async with self._lock:
            ch = self.store["channels"].setdefault(channel_id, {"title": title, "last_video_id": ""})
            ch["title"] = title or ch.get("title") or channel_id
            # 首次订阅：记下当前最新，避免把旧视频全推一遍
            if videos and not ch.get("last_video_id"):
                ch["last_video_id"] = videos[0].video_id

            sess = self.store["sessions"].setdefault(umo, [])
            if channel_id not in sess:
                sess.append(channel_id)
            self._save()

        latest = videos[0] if videos else None
        msg = f"✅ 已订阅频道：{title}\nID：{channel_id}"
        if latest:
            msg += f"\n当前最新：{latest.title}\n{latest.url}"
        return msg

    async def _unsubscribe(self, umo: str, raw: str) -> str:
        client = self._client()
        # 允许用 id / handle / 已存 title 模糊匹配
        target_id = None
        kind, value = extract_handle_or_id(raw)
        if kind == "channel_id":
            target_id = value
        else:
            try:
                target_id, _ = await client.resolve_channel_id(raw)
            except Exception:
                # 按标题匹配本会话订阅
                async with self._lock:
                    for cid in self.store["sessions"].get(umo, []):
                        title = (self.store["channels"].get(cid) or {}).get("title") or ""
                        if raw.lower() in title.lower() or raw.lower() in cid.lower():
                            target_id = cid
                            break

        if not target_id:
            return f"❌ 未找到可取消的订阅：{raw}"

        async with self._lock:
            sess = self.store["sessions"].get(umo, [])
            if target_id not in sess:
                return f"❌ 当前会话未订阅该频道：{target_id}"
            sess.remove(target_id)
            # 若无任何会话订阅该频道，清理频道记录
            still = any(target_id in ids for ids in self.store["sessions"].values())
            title = (self.store["channels"].get(target_id) or {}).get("title") or target_id
            if not still:
                self.store["channels"].pop(target_id, None)
            self._save()
        return f"✅ 已取消订阅：{title}"

    def _list_session(self, umo: str) -> str:
        ids = self.store["sessions"].get(umo, [])
        if not ids:
            return "当前会话还没有订阅任何 YouTube 频道。\n使用：/yt 订阅 @频道名"
        lines = ["📺 本会话订阅列表："]
        for i, cid in enumerate(ids, 1):
            meta = self.store["channels"].get(cid) or {}
            title = meta.get("title") or cid
            last = meta.get("last_video_id") or "-"
            lines.append(f"{i}. {title}\n   ID: {cid}\n   已记录最新视频: {last}")
        return "\n".join(lines)

    async def _latest(self, raw: str, limit: int = 3) -> str:
        client = self._client()
        channel_id, _ = await client.resolve_channel_id(raw)
        title, videos = await client.fetch_feed(channel_id)
        if not videos:
            return f"频道 {title or channel_id} 暂无视频。"
        lines = [f"📺 {title} 最新视频："]
        for v in videos[: max(1, limit)]:
            lines.append(self._format_video(title, v, prefix="•"))
            lines.append("")
        return "\n".join(lines).strip()

    async def _check_and_push_for_session(self, umo: str) -> str:
        ids = list(self.store["sessions"].get(umo, []))
        if not ids:
            return "当前会话没有订阅。"
        pushed = 0
        client = self._client()
        max_n = int(self._cfg("max_push_per_channel", 3) or 3)

        for cid in ids:
            try:
                title, videos = await client.fetch_feed(cid)
            except Exception as e:
                logger.warning("[youtube_notify] 拉取 %s 失败: %s", cid, e)
                continue

            async with self._lock:
                meta = self.store["channels"].setdefault(cid, {"title": title, "last_video_id": ""})
                if title:
                    meta["title"] = title
                last_id = meta.get("last_video_id") or ""

            if not videos:
                continue

            # 收集比 last_id 更新的视频
            new_items: list[VideoItem] = []
            for v in videos:
                if v.video_id == last_id:
                    break
                new_items.append(v)
            # videos 是新到旧；new_items 也是新到旧。推送时按旧到新，体验更好
            new_items = list(reversed(new_items[:max_n]))

            if not new_items:
                # 仍更新标题
                async with self._lock:
                    self.store["channels"][cid]["title"] = title or meta.get("title")
                    self._save()
                continue

            for v in new_items:
                await self._push_video(umo, title, v)
                pushed += 1

            async with self._lock:
                self.store["channels"][cid]["last_video_id"] = videos[0].video_id
                self.store["channels"][cid]["title"] = title or meta.get("title")
                self._save()

        if pushed == 0:
            return "没有检测到新视频。"
        return f"检查完成，共推送 {pushed} 条更新。"

    async def _poll_loop(self) -> None:
        # 启动后稍等，避免与其它初始化抢资源
        await asyncio.sleep(8)
        while not self._stop.is_set():
            try:
                if bool(self._cfg("enabled", True)):
                    await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[youtube_notify] 轮询异常: %s", e)

            interval = int(self._cfg("poll_interval_seconds", 600) or 600)
            interval = max(60, interval)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self) -> None:
        # 按频道聚合，再向所有订阅该频道的会话推送
        async with self._lock:
            channels = dict(self.store.get("channels") or {})
            sessions = {k: list(v) for k, v in (self.store.get("sessions") or {}).items()}

        if not channels:
            return

        client = self._client()
        max_n = int(self._cfg("max_push_per_channel", 3) or 3)

        for cid, meta in channels.items():
            try:
                title, videos = await client.fetch_feed(cid)
            except Exception as e:
                logger.warning("[youtube_notify] 轮询拉取失败 %s: %s", cid, e)
                continue
            if not videos:
                continue

            last_id = (meta or {}).get("last_video_id") or ""
            new_items: list[VideoItem] = []
            for v in videos:
                if v.video_id == last_id:
                    break
                new_items.append(v)
            new_items = list(reversed(new_items[:max_n]))

            # 更新 last_id / title
            async with self._lock:
                ch = self.store["channels"].setdefault(cid, {"title": title, "last_video_id": ""})
                if title:
                    ch["title"] = title
                if videos:
                    # 即使没有订阅会话，也推进游标，避免之后一次推很多
                    if not last_id:
                        ch["last_video_id"] = videos[0].video_id
                    elif new_items:
                        ch["last_video_id"] = videos[0].video_id
                self._save()

            if not new_items:
                continue

            target_umos = [umo for umo, ids in sessions.items() if cid in ids]
            for umo in target_umos:
                for v in new_items:
                    await self._push_video(umo, title or (meta or {}).get("title") or cid, v)

            await asyncio.sleep(1.0)  # 轻微限速

    # ---------- commands ----------
    @filter.command_group("yt")
    def yt(self):
        """YouTube 频道更新推送"""

    @yt.command("帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看帮助"""
        text = (
            "📺 YouTube 频道更新推送\n"
            "指令：\n"
            "/yt 订阅 <频道>  - 订阅到当前会话\n"
            "/yt 取消 <频道>  - 取消订阅\n"
            "/yt 列表         - 查看本会话订阅\n"
            "/yt 最新 <频道>  - 手动看最新视频\n"
            "/yt 检查         - 立即检查本会话更新\n"
            "/yt 帮助         - 本说明\n\n"
            "频道可写：@handle / UCxxxx / 频道链接\n"
            "示例：/yt 订阅 @MrBeast"
        )
        yield event.plain_result(text)

    @yt.command("订阅", alias={"sub", "subscribe"})
    async def cmd_sub(self, event: AstrMessageEvent, target: GreedyStr = ""):
        """订阅频道：/yt 订阅 <频道>"""
        payload = (target or "").strip()
        if not payload:
            yield event.plain_result("用法：/yt 订阅 @频道名 或 channel_id 或频道链接")
            return

        yield event.plain_result("正在解析频道，请稍候…")
        try:
            msg = await self._subscribe(event.unified_msg_origin, payload)
            yield event.plain_result(msg)
        except Exception as e:
            logger.exception("[youtube_notify] 订阅失败")
            yield event.plain_result(f"❌ 订阅失败：{e}")

    @yt.command("取消", alias={"unsub", "unsubscribe", "退订"})
    async def cmd_unsub(self, event: AstrMessageEvent, target: GreedyStr = ""):
        """取消订阅：/yt 取消 <频道>"""
        payload = (target or "").strip()
        if not payload:
            yield event.plain_result("用法：/yt 取消 @频道名 或 channel_id")
            return
        try:
            msg = await self._unsubscribe(event.unified_msg_origin, payload)
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"❌ 取消失败：{e}")

    @yt.command("列表", alias={"list", "ls"})
    async def cmd_list(self, event: AstrMessageEvent):
        """查看本会话订阅列表"""
        yield event.plain_result(self._list_session(event.unified_msg_origin))

    @yt.command("最新", alias={"latest", "new"})
    async def cmd_latest(self, event: AstrMessageEvent, target: GreedyStr = ""):
        """查看最新视频：/yt 最新 <频道>"""
        payload = (target or "").strip()
        if not payload:
            yield event.plain_result("用法：/yt 最新 @频道名")
            return
        yield event.plain_result("正在获取…")
        try:
            msg = await self._latest(payload, limit=3)
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"❌ 获取失败：{e}")

    @yt.command("检查", alias={"check", "poll"})
    async def cmd_check(self, event: AstrMessageEvent):
        """立即检查本会话订阅更新"""
        yield event.plain_result("正在检查更新…")
        try:
            msg = await self._check_and_push_for_session(event.unified_msg_origin)
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"❌ 检查失败：{e}")
