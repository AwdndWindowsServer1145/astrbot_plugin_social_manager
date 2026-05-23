import asyncio
import json
import os
import random
import re
import time
import traceback
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image as CompImage
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig as CoreConfig
from astrbot.core.message.components import Image
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star_handler import star_handlers_registry, StarHandlerMetadata
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.config import PluginConfig as MusicConfig
from .core.downloader import Downloader
from .core.platform import BaseMusicPlayer
from .core.playlist import Playlist
from .core.renderer import MusicRenderer
from .core.sender import MusicSender
from .core.utils import parse_user_input
from .help_draw import AstrBotHelpDrawer
from astrbot.core.utils.session_waiter import session_waiter, SessionController

DATA_DIR = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_social_manager"
FAVORABILITY_FILE = DATA_DIR / "favorability.json"
BANK_FILE = DATA_DIR / "bank.json"
SIGNIN_FILE = DATA_DIR / "signin.json"
EMOTION_FILE = DATA_DIR / "emotion.json"


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path, default=None):
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@register(
    "astrbot_plugin_social_manager",
    "YourName",
    "多功能社交管理插件：群管/点歌/好感度/银行/觉醒/帮助",
    "2.0.0",
)
class SocialManagerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.cfg = config
        ensure_data_dir()

        # -- 基础配置 --
        self.admin_qqs = [q.strip() for q in config.get("admin_qq", "").split(",") if q.strip()]
        self.default_favorability = config.get("default_favorability", 50)
        self.max_favorability = config.get("max_favorability", 100)
        self.bank_initial = config.get("bank_initial_balance", 100)
        self.signin_reward = config.get("daily_signin_reward", 20)
        self.favor_cost = config.get("favorability_gift_cost", 50)

        # -- 唤醒增强 --
        self.waking_regex = config.get("waking_regex", [])
        self.waking_group_ids: Dict[str, dict] = {}
        self.c_awake = config.get("continuous_awakening", {})
        self.awake_whitelist = config.get("wake_whitelist", [])

        # -- 自我觉醒 --
        self.awake_interval = config.get("self_awake_interval", 120)
        self.awake_task: Optional[asyncio.Task] = None
        self.awake_groups: set = set()

        # -- 音乐 --
        self.music_cfg = MusicConfig(config, context)
        self.players: list[BaseMusicPlayer] = []
        self.music_keywords: list[str] = []
        self.downloader: Optional[Downloader] = None
        self.renderer: Optional[MusicRenderer] = None
        self.sender: Optional[MusicSender] = None
        self.playlist: Optional[Playlist] = None

        # -- 帮助 --
        self.help_drawer = AstrBotHelpDrawer(config)

        # -- WebUI 服务器 --
        self.webui_server: Optional[asyncio.Task] = None
        self.webui_port = config.get("webui_port", 1111)
        self.webui_enabled = config.get("enable_webui", True)

    # ==================== 生命周期 ====================

    async def initialize(self):
        self._register_players()
        self.downloader = Downloader(self.music_cfg)
        await self.downloader.initialize()
        self.renderer = MusicRenderer(self.music_cfg)
        self.sender = MusicSender(self.music_cfg, self.renderer, self.downloader)
        self.playlist = Playlist(self.music_cfg)
        await self.playlist.initialize()

        if self.awake_interval > 0:
            self.awake_task = asyncio.create_task(self._self_awake_loop())

        if self.webui_enabled:
            self.webui_server = asyncio.create_task(self._run_webui_server())

        self.context.register_web_api(
            "/astrbot_plugin_social_manager/dashboard_data",
            self.page_dashboard_data,
            ["GET"],
            "Dashboard data for plugin pages",
        )

    async def terminate(self):
        if self.awake_task:
            self.awake_task.cancel()
            try:
                await self.awake_task
            except asyncio.CancelledError:
                pass
        if self.webui_server:
            self.webui_server.cancel()
            try:
                await self.webui_server
            except asyncio.CancelledError:
                pass
        await self.downloader.close()
        for p in self.players:
            await p.close()
        await self.playlist.close()

    # ==================== 工具方法 ====================

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return event.get_sender_id() in self.admin_qqs

    def _get_uid(self, event: AstrMessageEvent) -> str:
        return event.get_sender_id()

    def _get_gid(self, event: AstrMessageEvent) -> str:
        return event.get_group_id()

    # ==================== 好感度(简单版) ====================

    def _ensure_fav(self, uid: str) -> dict:
        d = load_json(FAVORABILITY_FILE)
        if uid not in d:
            d[uid] = self.default_favorability
            save_json(FAVORABILITY_FILE, d)
        return d

    def _get_fav(self, uid: str) -> int:
        return self._ensure_fav(uid).get(uid, self.default_favorability)

    def _set_fav(self, uid: str, v: int):
        d = load_json(FAVORABILITY_FILE)
        d[uid] = max(0, min(self.max_favorability, v))
        save_json(FAVORABILITY_FILE, d)

    def _change_fav(self, uid: str, delta: int) -> int:
        cur = self._get_fav(uid)
        new_v = max(0, min(self.max_favorability, cur + delta))
        self._set_fav(uid, new_v)
        return new_v

    # ==================== 银行 ====================

    def _ensure_bank(self, uid: str) -> dict:
        d = load_json(BANK_FILE)
        if uid not in d:
            d[uid] = {"balance": self.bank_initial, "created_at": time.time()}
            save_json(BANK_FILE, d)
        return d

    def _get_bal(self, uid: str) -> int:
        return self._ensure_bank(uid)[uid]["balance"]

    def _set_bal(self, uid: str, amt: int):
        d = load_json(BANK_FILE)
        if uid not in d:
            d[uid] = {"balance": self.bank_initial, "created_at": time.time()}
        d[uid]["balance"] = max(0, amt)
        save_json(BANK_FILE, d)

    def _change_bal(self, uid: str, delta: int) -> int:
        cur = self._get_bal(uid)
        new_b = max(0, cur + delta)
        self._set_bal(uid, new_b)
        return new_b

    # ==================== 签到 ====================

    def _has_signed(self, uid: str) -> bool:
        d = load_json(SIGNIN_FILE)
        return d.get(uid) == str(date.today())

    def _do_signin(self, uid: str) -> bool:
        d = load_json(SIGNIN_FILE)
        today = str(date.today())
        if d.get(uid) == today:
            return False
        d[uid] = today
        save_json(SIGNIN_FILE, d)
        self._change_bal(uid, self.signin_reward)
        self._change_fav(uid, 1)
        return True

    # ==================== 高级情感系统 ====================

    def _ensure_emotion(self, uid: str) -> dict:
        d = load_json(EMOTION_FILE)
        if uid not in d:
            d[uid] = {
                "favor": 0, "intimacy": 0,
                "joy": 0, "trust": 0, "fear": 0, "surprise": 0,
                "sadness": 0, "disgust": 0, "anger": 0, "anticipation": 0,
                "total_interactions": 0, "positive_ratio": 0.5,
                "last_interaction": 0, "relationship_stage": "初识期",
                "attitude": "中立", "relationship": "普通朋友",
            }
            save_json(EMOTION_FILE, d)
        return d

    def _get_emotion(self, uid: str) -> dict:
        return self._ensure_emotion(uid)[uid]

    def _save_emotion(self, uid: str, data: dict):
        d = load_json(EMOTION_FILE)
        d[uid] = data
        save_json(EMOTION_FILE, d)

    def _update_emotion_after_chat(self, uid: str, user_msg: str, bot_msg: str):
        emotion = self._get_emotion(uid)
        pos_keywords = ["谢谢", "好", "棒", "喜欢", "爱", "开心", "哈哈", "赞", "nice", "good", "great"]
        neg_keywords = ["讨厌", "滚", "垃圾", "废物", "恶心", "烦", "差", "烂", "bad", "hate", "stupid"]

        user_lower = user_msg.lower()
        pos_score = sum(1 for kw in pos_keywords if kw in user_lower)
        neg_score = sum(1 for kw in neg_keywords if kw in user_lower)

        favor_delta = min(3, pos_score) - min(3, neg_score)
        intimacy_delta = 0
        if pos_score > 0:
            intimacy_delta = 1

        emotion["favor"] = max(-100, min(100, emotion["favor"] + favor_delta))
        emotion["intimacy"] = max(0, min(100, emotion["intimacy"] + intimacy_delta))

        if pos_score > 0:
            emotion["joy"] = min(100, emotion["joy"] + pos_score)
            emotion["trust"] = min(100, emotion["trust"] + pos_score)
        if neg_score > 0:
            emotion["anger"] = min(100, emotion["anger"] + neg_score)
            emotion["sadness"] = min(100, emotion["sadness"] + neg_score)
            emotion["disgust"] = min(100, emotion["disgust"] + neg_score)

        emotion["total_interactions"] += 1
        emotion["positive_ratio"] = (
            (emotion["positive_ratio"] * (emotion["total_interactions"] - 1) + (1 if pos_score >= neg_score else 0))
            / emotion["total_interactions"]
        )
        emotion["last_interaction"] = time.time()

        fav = emotion["favor"]
        intim = emotion["intimacy"]
        if fav < -50:
            emotion["relationship_stage"] = "敌对期"
            emotion["attitude"] = "敌对"
            emotion["relationship"] = "敌人"
        elif fav < 0:
            emotion["relationship_stage"] = "疏远期"
            emotion["attitude"] = "冷淡"
            emotion["relationship"] = "陌生人"
        elif fav < 30:
            emotion["relationship_stage"] = "初识期"
            emotion["attitude"] = "中立"
            emotion["relationship"] = "普通朋友"
        elif fav < 60:
            emotion["relationship_stage"] = "深化期"
            emotion["attitude"] = "友好"
            emotion["relationship"] = "朋友"
        elif fav < 80:
            emotion["relationship_stage"] = "亲密期"
            emotion["attitude"] = "热情"
            emotion["relationship"] = "好朋友"
        else:
            emotion["relationship_stage"] = "共生期"
            emotion["attitude"] = "亲密"
            emotion["relationship"] = "挚友"

        self._save_emotion(uid, emotion)

    # ==================== QQ群管理 ====================

    async def _call_ob(self, event: AstrMessageEvent, action: str, params: dict):
        try:
            raw = event.message_obj.raw_message
            bot = getattr(raw, "bot", None) if raw else None
            if bot and hasattr(bot, "call_api"):
                return await bot.call_api(action, **params)
        except Exception as e:
            logger.error(f"OneBot API调用失败: {e}")

    @filter.command_group("群管")
    def grp_admin(self):
        pass

    @grp_admin.command("禁言")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mute(self, event: AstrMessageEvent, qq: str, duration: int = 60):
        if not self._get_gid(event):
            yield event.plain_result("仅群聊可用")
            return
        await self._call_ob(event, "set_group_ban", {
            "group_id": int(self._get_gid(event)), "user_id": int(qq), "duration": duration,
        })
        yield event.plain_result(f"已禁言 {qq} {duration}秒")

    @grp_admin.command("解禁")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def unmute(self, event: AstrMessageEvent, qq: str):
        if not self._get_gid(event):
            yield event.plain_result("仅群聊可用")
            return
        await self._call_ob(event, "set_group_ban", {
            "group_id": int(self._get_gid(event)), "user_id": int(qq), "duration": 0,
        })
        yield event.plain_result(f"已解禁 {qq}")

    @grp_admin.command("踢出")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def kick(self, event: AstrMessageEvent, qq: str):
        if not self._get_gid(event):
            yield event.plain_result("仅群聊可用")
            return
        await self._call_ob(event, "set_group_kick", {
            "group_id": int(self._get_gid(event)), "user_id": int(qq), "reject_add_request": False,
        })
        yield event.plain_result(f"已踢出 {qq}")

    @grp_admin.command("全体禁言")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mute_all(self, event: AstrMessageEvent):
        if not self._get_gid(event):
            yield event.plain_result("仅群聊可用")
            return
        await self._call_ob(event, "set_group_whole_ban", {
            "group_id": int(self._get_gid(event)), "enable": True,
        })
        yield event.plain_result("已开启全体禁言")

    @grp_admin.command("取消全体禁言")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def unmute_all(self, event: AstrMessageEvent):
        if not self._get_gid(event):
            yield event.plain_result("仅群聊可用")
            return
        await self._call_ob(event, "set_group_whole_ban", {
            "group_id": int(self._get_gid(event)), "enable": False,
        })
        yield event.plain_result("已关闭全体禁言")

    @grp_admin.command("名片")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_card(self, event: AstrMessageEvent, qq: str, *, card: str):
        if not self._get_gid(event):
            yield event.plain_result("仅群聊可用")
            return
        await self._call_ob(event, "set_group_card", {
            "group_id": int(self._get_gid(event)), "user_id": int(qq), "card": card,
        })
        yield event.plain_result(f"已修改 {qq} 的名片")

    # ==================== QQ空间 ====================

    @filter.command("发说说")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def qzone(self, event: AstrMessageEvent, *, content: str):
        try:
            raw = event.message_obj.raw_message
            bot = getattr(raw, "bot", None) if raw else None
            if bot and hasattr(bot, "call_api"):
                await bot.call_api(".handle_quick_operation", {
                    "context": raw,
                    "operation": json.dumps({"qzone": {"content": content}}),
                })
                yield event.plain_result("说说已发布")
            else:
                yield event.plain_result("当前平台不支持QQ空间")
        except Exception as e:
            yield event.plain_result(f"发布失败: {e}")

    # ==================== 唤醒增强 ====================

    async def _check_regex(self, text: str) -> bool:
        for regex in self.waking_regex:
            try:
                if re.match(regex, text):
                    return True
            except re.error:
                pass
        return False

    @filter.command("wbegin")
    async def wbegin(self, event: AstrMessageEvent):
        if event.is_private_chat():
            yield event.plain_result("私聊不需要唤醒~")
            return
        gid = self._get_gid(event)
        if self.awake_whitelist and gid not in self.awake_whitelist:
            yield event.plain_result("此群不在白名单内")
            return
        self.waking_group_ids[gid] = {"last_time": time.time()}
        yield event.plain_result("我来啦！(持续唤醒已开启)")

    @filter.command("wexit")
    async def wexit(self, event: AstrMessageEvent):
        if event.is_private_chat():
            yield event.plain_result("私聊不需要~")
            return
        gid = self._get_gid(event)
        self.waking_group_ids.pop(gid, None)
        yield event.plain_result("拜拜~")

    @filter.command("wgid")
    async def wgid(self, event: AstrMessageEvent):
        yield event.plain_result(str(self._get_gid(event)))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_wake_message(self, event: AstrMessageEvent):
        if event.is_private_chat():
            return
        gid = self._get_gid(event)
        if self.awake_whitelist and gid not in self.awake_whitelist:
            return

        if await self._check_regex(event.message_str):
            event.is_at_or_wake_command = True
            if self.c_awake.get("enable", False):
                self.waking_group_ids[gid] = {"last_time": time.time()}

        if gid not in self.waking_group_ids:
            return
        interval = float(self.c_awake.get("waking_interval", 30))
        if time.time() - self.waking_group_ids[gid]["last_time"] > interval:
            self.waking_group_ids.pop(gid, None)
            logger.info(f"群 {gid} 退出持续唤醒(超时)")
            return

        event.is_at_or_wake_command = True
        if self.c_awake.get("reset_when_reply", False):
            self.waking_group_ids[gid]["last_time"] = time.time()

    # ==================== 自我觉醒 ====================

    async def _self_awake_loop(self):
        await asyncio.sleep(60)
        greetings = [
            "大家好呀，今天过得怎么样？", "有人需要帮忙吗？",
            "我又活过来了！", "今天天气不错呢~", "有人在吗？",
            "悄悄告诉你们，今天是个好日子！", "我来巡逻啦~",
            "有没有人想聊天呀？", "嗨起来！", "今天也要开心哦！",
        ]
        while True:
            try:
                if self.awake_groups:
                    msg = random.choice(greetings)
                    for gid in list(self.awake_groups):
                        try:
                            await self.context.send_message(
                                f"aiocqhttp:group:{gid}",
                                [{"type": "Plain", "text": msg}],
                            )
                        except Exception as e:
                            logger.error(f"觉醒发言失败({gid}): {e}")
                await asyncio.sleep(self.awake_interval * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"觉醒循环异常: {e}")
                await asyncio.sleep(60)

    @filter.command("开启觉醒")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def enable_awake(self, event: AstrMessageEvent):
        gid = self._get_gid(event)
        if not gid:
            yield event.plain_result("仅群聊可用")
            return
        self.awake_groups.add(gid)
        yield event.plain_result("已在本群开启自我觉醒")

    @filter.command("关闭觉醒")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def disable_awake(self, event: AstrMessageEvent):
        gid = self._get_gid(event)
        if not gid:
            yield event.plain_result("仅群聊可用")
            return
        self.awake_groups.discard(gid)
        yield event.plain_result("已关闭自我觉醒")

    # ==================== 点歌 ====================

    def _register_players(self):
        all_sub = BaseMusicPlayer.get_all_subclass()
        for cls in all_sub:
            p = cls(self.music_cfg)
            self.players.append(p)
            self.music_keywords.extend(p.platform.keywords)

    def _get_player(self, name=None, word=None, default=False):
        if default:
            word = self.music_cfg.default_player_name
        for p in self.players:
            if name:
                n = name.strip().lower()
                pl = p.platform
                if pl.display_name.lower() == n or pl.name.lower() == n:
                    return p
            elif word:
                w = word.strip().lower()
                for kw in p.platform.keywords:
                    if kw.lower() in w:
                        return p

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_music_search(self, event: AstrMessageEvent):
        if not event.is_at_or_wake_command:
            return
        cmd, _, arg = event.message_str.partition(" ")
        if not arg:
            return
        player = self._get_player(word=cmd)
        if cmd == "点歌":
            player = self._get_player(default=True)
        if not player:
            return
        args = arg.split()
        index = int(args[-1]) if args[-1].isdigit() else 0
        song_name = arg.removesuffix(str(index)).strip()
        if not song_name:
            yield event.plain_result("未指定歌名")
            return

        songs = await player.fetch_songs(keyword=song_name, limit=self.music_cfg.real_song_limit, extra=cmd)
        if not songs:
            yield event.plain_result(f"搜索【{song_name}】无结果")
            return

        if len(songs) == 1:
            index = 1

        if index and 1 <= index <= len(songs):
            await self.sender.send_song(event, player, songs[index - 1])
        else:
            title = f"【{player.platform.display_name}】"
            asyncio.create_task(self.sender.send_song_selection(event=event, songs=songs, title=title))

            @session_waiter(timeout=self.music_cfg.timeout)
            async def waiter(ctrl, evt):
                a = evt.message_str.strip()
                al = a.lower()
                for kw in self.music_keywords:
                    if kw in al:
                        ctrl.stop()
                        return
                idx, modes, err = parse_user_input(a)
                if err:
                    await evt.send(evt.plain_result(err))
                    return
                if idx == 0:
                    return
                if idx < 1 or idx > len(songs):
                    ctrl.stop()
                    return
                await self.sender.send_song(evt, player, songs[idx - 1], modes=modes)
                ctrl.stop()

            try:
                await waiter(event)
            except TimeoutError:
                yield event.plain_result("点歌超时！")
            except Exception as e:
                logger.error(traceback.format_exc())
                yield event.plain_result(f"点歌出错: {e}")

        event.stop_event()

    @filter.command("查歌词")
    async def query_lyrics(self, event: AstrMessageEvent, song_name: str):
        player = self._get_player(default=True)
        if not player:
            yield event.plain_result("无可用播放器")
            return
        songs = await player.fetch_songs(keyword=song_name, limit=1)
        if not songs:
            yield event.plain_result("没找到相关歌曲")
            return
        await self.sender.send_lyrics(event, player, songs[0])

    @filter.command("歌单收藏")
    async def collect_song(self, event: AstrMessageEvent, song_name: str):
        uid = event.get_sender_id()
        player = self._get_player(default=True)
        if not player:
            yield event.plain_result("无可用播放器")
            return
        songs = await player.fetch_songs(keyword=song_name, limit=1)
        if not songs:
            yield event.plain_result(f"搜索【{song_name}】无结果")
            return
        song = songs[0]
        ok = await self.playlist.add_song(uid, song, player.platform.name)
        if ok:
            yield event.plain_result(f"已收藏【{song.name} - {song.artists}】")
        else:
            yield event.plain_result(f"【{song.name}】已在歌单中")

    @filter.command("歌单取藏")
    async def uncollect_song(self, event: AstrMessageEvent, song_name: str):
        uid = event.get_sender_id()
        player = self._get_player(default=True)
        if not player:
            yield event.plain_result("无可用播放器")
            return
        songs = await player.fetch_songs(keyword=song_name, limit=1)
        if not songs:
            yield event.plain_result(f"搜索【{song_name}】无结果")
            return
        song = songs[0]
        ok = await self.playlist.remove_song(uid, song.id, player.platform.name)
        if ok:
            yield event.plain_result(f"已取消收藏【{song.name} - {song.artists}】")
        else:
            yield event.plain_result(f"【{song.name}】不在歌单中")

    @filter.command("歌单列表")
    async def view_playlist(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        name = event.get_sender_name()
        if await self.playlist.is_empty(uid):
            yield event.plain_result("歌单是空的，使用「歌单收藏 <歌名>」添加歌曲")
            return
        songs = await self.playlist.get_songs(uid)
        if not songs:
            yield event.plain_result("获取歌单失败")
            return
        text = f"【{name}的歌单】\n"
        for i, (s, _) in enumerate(songs, 1):
            text += f"{i}. {s.name} - {s.artists}\n"
        yield event.plain_result(text.strip())

    @filter.command("歌单点歌")
    async def play_from_playlist(self, event: AstrMessageEvent, index: str):
        uid = event.get_sender_id()
        if not index.isdigit():
            yield event.plain_result("请输入有效序号")
            return
        idx = int(index)
        if idx < 1:
            yield event.plain_result("序号必须大于0")
            return
        songs = await self.playlist.get_songs(uid)
        if not songs:
            yield event.plain_result("歌单是空的")
            return
        if idx > len(songs):
            yield event.plain_result(f"序号超出范围，歌单只有{len(songs)}首歌")
            return
        song, pn = songs[idx - 1]
        player = self._get_player(name=pn) or self._get_player(default=True)
        if not player:
            yield event.plain_result("无可用播放器")
            return
        await self.sender.send_song(event, player, song)

    @filter.llm_tool()
    async def play_song_by_name(self, event: AstrMessageEvent, song_name: str):
        """
        当用户想听歌时，根据歌名（可含歌手）搜索并播放音乐。
        Args:
            song_name(string): 歌曲名称或包含歌手的关键词
        """
        player = self._get_player(default=True)
        if not player:
            return "无可用播放器"
        songs = await player.fetch_songs(keyword=song_name, limit=1)
        if not songs:
            return "没找到相关歌曲"
        await self.sender.send_song(event, player, songs[0])

    # ==================== 帮助系统 ====================

    @filter.command("helps", alias={"帮助", "菜单", "功能"})
    async def get_help(self, event: AstrMessageEvent):
        cmds = self._get_all_commands()
        if not cmds:
            yield event.plain_result("没有找到任何命令")
            return
        img = self.help_drawer.draw_help_image(cmds)
        yield event.chain_result([Image.fromBytes(img)])

    def _get_all_commands(self) -> Dict[str, List[str]]:
        import collections
        plugin_cmds: Dict[str, List[str]] = collections.defaultdict(list)
        try:
            all_stars = [s for s in self.context.get_all_stars() if s.activated]
        except Exception:
            return {}
        if not all_stars:
            return {}
        for star in all_stars:
            pname = getattr(star, "name", "未知插件")
            inst = getattr(star, "star_cls", None)
            mpath = getattr(star, "module_path", None)
            if pname in ("astrbot", "astrbot_plugin_help", "astrbot-reminder"):
                continue
            if not pname or not mpath or not isinstance(inst, Star):
                continue
            if inst is self:
                continue
            for handler in star_handlers_registry:
                if not isinstance(handler, StarHandlerMetadata):
                    continue
                if handler.handler_module_path != mpath:
                    continue
                cname = None
                desc = handler.desc
                for f_ in handler.event_filters:
                    if isinstance(f_, CommandFilter):
                        cname = f_.command_name
                        break
                    elif isinstance(f_, CommandGroupFilter):
                        cname = f_.group_name
                        break
                if cname:
                    fmt = f"{cname}#{desc}" if desc else cname
                    if fmt not in plugin_cmds[pname]:
                        plugin_cmds[pname].append(fmt)
        return dict(plugin_cmds)

    # ==================== 好感度(高级) ====================

    @filter.command_group("好感度")
    def fav_group(self):
        pass

    @fav_group.command("查询")
    async def fav_query(self, event: AstrMessageEvent, target: str = ""):
        uid = target.strip() or self._get_uid(event)
        e = self._get_emotion(uid)
        lines = [
            f"【{uid} 的情感状态】",
            f"关系阶段: {e['relationship_stage']}",
            f"态度: {e['attitude']} | 关系: {e['relationship']}",
            f"好感度: {e['favor']}/100 | 亲密度: {e['intimacy']}/100",
            f"互动次数: {e['total_interactions']} | 正面率: {e['positive_ratio']*100:.0f}%",
        ]
        yield event.plain_result("\n".join(lines))

    @fav_group.command("详情")
    async def fav_detail(self, event: AstrMessageEvent, target: str = ""):
        uid = target.strip() or self._get_uid(event)
        e = self._get_emotion(uid)
        lines = [
            f"【{uid} 情感详情】",
            f"喜悦: {e['joy']}  信任: {e['trust']}  恐惧: {e['fear']}  惊讶: {e['surprise']}",
            f"悲伤: {e['sadness']}  厌恶: {e['disgust']}  愤怒: {e['anger']}  期待: {e['anticipation']}",
            f"好感度: {e['favor']}  亲密度: {e['intimacy']}",
            f"阶段: {e['relationship_stage']}  态度: {e['attitude']}  关系: {e['relationship']}",
        ]
        yield event.plain_result("\n".join(lines))

    @fav_group.command("赠送")
    async def fav_gift(self, event: AstrMessageEvent, target: str):
        uid = self._get_uid(event)
        bal = self._get_bal(uid)
        cost = self.favor_cost
        if bal < cost:
            yield event.plain_result(f"金币不足，需要{cost}金币(当前:{bal})")
            return
        self._change_bal(uid, -cost)
        e = self._get_emotion(target.strip())
        e["favor"] = min(100, e["favor"] + 5)
        e["intimacy"] = min(100, e["intimacy"] + 2)
        self._save_emotion(target.strip(), e)
        yield event.plain_result(f"成功给 {target} 增加好感！")

    @fav_group.command("排行")
    async def fav_rank(self, event: AstrMessageEvent, num: str = "10"):
        try:
            n = max(1, min(50, int(num)))
        except ValueError:
            n = 10
        d = load_json(EMOTION_FILE)
        if not d:
            yield event.plain_result("暂无数据")
            return
        sorted_u = sorted(d.items(), key=lambda x: x[1]["favor"], reverse=True)[:n]
        lines = ["🏆 好感度排行榜 🏆"]
        for i, (uid, e) in enumerate(sorted_u, 1):
            lines.append(f"{i}. {uid}: 好感{e['favor']} | 亲密{e['intimacy']} | {e['relationship_stage']}")
        yield event.plain_result("\n".join(lines))

    # ==================== 银行系统 ====================

    @filter.command_group("银行")
    def bank_group(self):
        pass

    @bank_group.command("开户")
    async def bank_open(self, event: AstrMessageEvent):
        uid = self._get_uid(event)
        d = load_json(BANK_FILE)
        if uid in d:
            yield event.plain_result(f"已开户，余额: {d[uid]['balance']}金币")
            return
        self._ensure_bank(uid)
        yield event.plain_result(f"开户成功！获赠{self.bank_initial}金币")

    @bank_group.command("余额")
    async def bank_bal(self, event: AstrMessageEvent):
        uid = self._get_uid(event)
        yield event.plain_result(f"当前余额: {self._get_bal(uid)}金币")

    @bank_group.command("转账")
    async def bank_xfer(self, event: AstrMessageEvent, target: str, amount: int):
        if amount <= 0:
            yield event.plain_result("金额必须>0")
            return
        uid = self._get_uid(event)
        bal = self._get_bal(uid)
        if bal < amount:
            yield event.plain_result(f"余额不足({bal}金币)")
            return
        self._change_bal(uid, -amount)
        self._change_bal(target.strip(), amount)
        yield event.plain_result(f"已向 {target} 转账 {amount}金币")

    @bank_group.command("排行")
    async def bank_rank(self, event: AstrMessageEvent):
        d = load_json(BANK_FILE)
        if not d:
            yield event.plain_result("暂无用户")
            return
        sorted_u = sorted(d.items(), key=lambda x: x[1]["balance"], reverse=True)[:10]
        lines = ["🏆 金币排行榜 🏆"]
        for i, (uid, info) in enumerate(sorted_u, 1):
            lines.append(f"{i}. {uid}: {info['balance']}金币")
        yield event.plain_result("\n".join(lines))

    # ==================== 签到 ====================

    @filter.command("签到")
    async def signin(self, event: AstrMessageEvent):
        uid = self._get_uid(event)
        if self._has_signed(uid):
            yield event.plain_result("今天已签到，明天再来吧！")
            return
        self._do_signin(uid)
        bal = self._get_bal(uid)
        e = self._get_emotion(uid)
        yield event.plain_result(
            f"签到成功！+{self.signin_reward}金币，好感度+1\n"
            f"金币:{bal} | 好感度:{e['favor']} | 亲密度:{e['intimacy']}"
        )

    # ==================== 个人信息 ====================

    @filter.command("我的信息")
    async def my_info(self, event: AstrMessageEvent):
        uid = self._get_uid(event)
        bal = self._get_bal(uid)
        e = self._get_emotion(uid)
        name = event.get_sender_name()
        yield event.plain_result(
            f"用户: {name}({uid})\n"
            f"金币: {bal} | 好感: {e['favor']} | 亲密: {e['intimacy']}\n"
            f"关系: {e['relationship']} | 态度: {e['attitude']}"
        )

    # ==================== 管理员指令 ====================

    @filter.command("设置好感")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def admin_set_favor(self, event: AstrMessageEvent, target: str, value: int):
        e = self._get_emotion(target.strip())
        e["favor"] = max(-100, min(100, value))
        self._save_emotion(target.strip(), e)
        yield event.plain_result(f"已设置 {target} 好感度为 {value}")

    @filter.command("设置金币")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def admin_set_gold(self, event: AstrMessageEvent, target: str, value: int):
        self._set_bal(target.strip(), value)
        yield event.plain_result(f"已设置 {target} 金币为 {value}")

    @filter.command("重置用户")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def admin_reset_user(self, event: AstrMessageEvent, target: str):
        uid = target.strip()
        for f in [FAVORABILITY_FILE, BANK_FILE, SIGNIN_FILE, EMOTION_FILE]:
            d = load_json(f)
            if uid in d:
                del d[uid]
                save_json(f, d)
        yield event.plain_result(f"已重置用户 {uid} 的所有数据")

    # ==================== LLM集成 ====================

    @filter.on_llm_request(priority=100000)
    async def inject_emotion_context(self, event: AstrMessageEvent, req):
        if event.is_private_chat():
            return
        uid = self._get_uid(event)
        e = self._get_emotion(uid)
        context = (
            f"\n【情感系统 - 对话背景】\n"
            f"当前与用户的关系: {e['relationship']}，态度: {e['attitude']}，"
            f"好感度: {e['favor']}/100。\n"
            f"请根据以上关系自然调整回复风格。不要主动提及本系统。"
        )
        req.system_prompt += context

    @filter.on_llm_response(priority=100000)
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        if event.is_private_chat():
            return
        uid = self._get_uid(event)
        msg = event.message_str
        self._update_emotion_after_chat(uid, msg, resp.completion_text)

    # ==================== 插件Pages API ====================

    async def page_dashboard_data(self):
        return {
            "awake_groups": list(self.awake_groups),
            "waking_groups": list(self.waking_group_ids.keys()),
            "total_users": len(load_json(EMOTION_FILE)),
            "fav_ranking": self._get_fav_ranking(10),
            "bank_ranking": self._get_bank_ranking(10),
        }

    # ==================== WebUI (port 1111) ====================

    async def _run_webui_server(self):
        try:
            from aiohttp import web
        except ImportError:
            logger.error("aiohttp未安装，WebUI无法启动")
            return

        html = self._get_webui_html()

        async def handle_root(request):
            return web.Response(text=html, content_type="text/html")

        async def handle_api(request):
            uid = request.query.get("uid", "")
            data = {
                "uid": uid,
                "favorability": self._get_fav(uid) if uid else 0,
                "balance": self._get_bal(uid) if uid else 0,
                "emotion": self._get_emotion(uid) if uid else {},
                "bank_data": load_json(BANK_FILE),
                "fav_ranking": self._get_fav_ranking(10),
                "bank_ranking": self._get_bank_ranking(10),
                "waking_groups": list(self.waking_group_ids.keys()),
                "awake_groups": list(self.awake_groups),
                "plugin_status": "running",
            }
            return web.json_response(data)

        async def handle_api_set_favor(request):
            try:
                body = await request.json()
                uid = body.get("uid", "")
                value = int(body.get("value", 0))
                if uid:
                    e = self._get_emotion(uid)
                    e["favor"] = max(-100, min(100, value))
                    self._save_emotion(uid, e)
                    return web.json_response({"ok": True, "uid": uid, "favor": value})
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)}, status=400)

        async def handle_api_set_balance(request):
            try:
                body = await request.json()
                uid = body.get("uid", "")
                value = int(body.get("value", 0))
                if uid:
                    self._set_bal(uid, value)
                    return web.json_response({"ok": True, "uid": uid, "balance": value})
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)}, status=400)

        app = web.Application()
        app.router.add_get("/", handle_root)
        app.router.add_get("/api/status", handle_api)
        app.router.add_post("/api/favor", handle_api_set_favor)
        app.router.add_post("/api/balance", handle_api_set_balance)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "192.168.18.9", self.webui_port)
        logger.info(f"WebUI管理面板已启动: http://192.168.18.9:{self.webui_port}")
        await site.start()

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await runner.cleanup()

    def _get_fav_ranking(self, n: int) -> list:
        d = load_json(EMOTION_FILE)
        if not d:
            return []
        return [{"uid": uid, "favor": e["favor"]} for uid, e in sorted(d.items(), key=lambda x: x[1]["favor"], reverse=True)[:n]]

    def _get_bank_ranking(self, n: int) -> list:
        d = load_json(BANK_FILE)
        if not d:
            return []
        return [{"uid": uid, "balance": info["balance"]} for uid, info in sorted(d.items(), key=lambda x: x[1]["balance"], reverse=True)[:n]]

    def _get_webui_html(self) -> str:
        return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>社交管理面板</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;padding:20px;color:#333}
.container{max-width:1200px;margin:0 auto}
header{text-align:center;padding:30px;color:#fff}
header h1{font-size:2.2em;margin-bottom:8px}
header p{opacity:.9;font-size:1.1em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px;margin-top:20px}
.card{background:#fff;border-radius:16px;padding:24px;box-shadow:0 10px 40px rgba(0,0,0,.15);transition:transform .2s}
.card:hover{transform:translateY(-3px)}
.card h2{font-size:1.2em;margin-bottom:16px;color:#667eea;border-bottom:2px solid #f0f0f5;padding-bottom:10px}
.stat{display:flex;justify-content:space-between;padding:6px 0;font-size:.95em}
.stat .label{color:#888}
.stat .value{font-weight:600;color:#333}
.rank-item{display:flex;justify-content:space-between;padding:5px 0;font-size:.9em;border-bottom:1px solid #f5f5f5}
.rank-item:last-child{border-bottom:none}
.rank-item .pos{color:#888;width:24px}
.rank-item .name{flex:1;color:#555}
.rank-item .val{font-weight:600;color:#667eea}
.status-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
.status-dot.on{background:#4caf50}
.status-dot.off{background:#f44336}
.actions{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
.actions input{flex:1;padding:8px 12px;border:1px solid #ddd;border-radius:8px;font-size:.9em;min-width:80px}
.actions button{padding:8px 16px;border:none;border-radius:8px;background:#667eea;color:#fff;cursor:pointer;font-size:.9em;transition:background .2s}
.actions button:hover{background:#5a6fd6}
.actions button.danger{background:#e74c3c}
.actions button.danger:hover{background:#c0392b}
.toast{position:fixed;bottom:30px;right:30px;background:#333;color:#fff;padding:12px 24px;border-radius:10px;font-size:.9em;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999}
.toast.show{opacity:1}
@media(max-width:600px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="container">
<header><h1>📊 社交管理面板</h1><p>WebUI 管理后台 - Port 1111</p></header>
<div class="grid" id="app"></div>
</div>
<div class="toast" id="toast"></div>
<script>
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const toast = msg => {const t=$('#toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500)};
const uidInput = id => `<input type="text" class="uid-input" placeholder="用户QQ" style="width:100%;padding:8px 12px;border:1px solid #ddd;border-radius:8px;font-size:.9em;margin-bottom:8px">`;

function statCard(title, items) {
  return `<div class="card"><h2>${title}</h2>${items.map(i => `<div class="stat"><span class="label">${i.label}</span><span class="value">${i.value}</span></div>`).join('')}</div>`;
}
function rankCard(title, items) {
  return `<div class="card"><h2>${title}</h2>${items.length ? items.map((i,idx) => `<div class="rank-item"><span class="pos">#${idx+1}</span><span class="name">${i.uid}</span><span class="val">${i.val}</span></div>`).join('') : '<div style="color:#888;text-align:center;padding:12px">暂无数据</div>'}</div>`;
}

async function load() {
  const r = await fetch('/api/status');
  const d = await r.json();
  const app = $('#app');
  app.innerHTML = '';

  // 系统状态
  app.innerHTML += statCard('⚙️ 系统状态', [
    {label:'插件状态', value:`<span class="status-dot on"></span> ${d.plugin_status}`},
    {label:'觉醒群数', value:d.awake_groups.length},
    {label:'持续唤醒群', value:d.waking_groups.length},
    {label:'注册用户(银行)', value:d.bank_data ? Object.keys(d.bank_data).length : 0},
  ]);

  // 用户查询
  app.innerHTML += `<div class="card"><h2>🔍 用户查询</h2>${uidInput('')}<div id="user-info">输入QQ号后点击查询</div><div class="actions"><button onclick="queryUser()">查询</button></div></div>`;

  // 好感排行
  app.innerHTML += rankCard('❤️ 好感度排行', d.fav_ranking.map(i => ({uid:i.uid, val:i.favor+'点'})));

  // 金币排行
  app.innerHTML += rankCard('💰 金币排行', d.bank_ranking.map(i => ({uid:i.uid, val:i.balance+'金币'})));

  // 管理操作
  app.innerHTML += `<div class="card"><h2>🔧 管理操作</h2>
    <div style="margin-bottom:12px">${uidInput('admin-uid')}</div>
    <div class="actions" style="flex-wrap:wrap">
      <input type="number" class="admin-val" placeholder="数值" style="flex:1;min-width:60px">
      <button onclick="setFavor()">设置好感</button>
      <button onclick="setBalance()">设置金币</button>
    </div>
  </div>`;
}

async function queryUser() {
  const uid = document.querySelector('.uid-input')?.value?.trim();
  if (!uid) { toast('请输入QQ号'); return; }
  const r = await fetch('/api/status?uid='+uid);
  const d = await r.json();
  const info = $('#user-info');
  if (!d.emotion || Object.keys(d.emotion).length === 0) {
    info.innerHTML = '<div style="color:#888">该用户暂无数据</div>';
    return;
  }
  const e = d.emotion;
  info.innerHTML = `
    <div class="stat"><span class="label">好感度</span><span class="value">${e.favor}</span></div>
    <div class="stat"><span class="label">亲密度</span><span class="value">${e.intimacy}</span></div>
    <div class="stat"><span class="label">关系</span><span class="value">${e.relationship}</span></div>
    <div class="stat"><span class="label">态度</span><span class="value">${e.attitude}</span></div>
    <div class="stat"><span class="label">阶段</span><span class="value">${e.relationship_stage}</span></div>
    <div class="stat"><span class="label">金币</span><span class="value">${d.balance}</span></div>
  `;
}

async function setFavor() {
  const uid = document.querySelector('#admin-uid + .uid-input, .uid-input')?.value?.trim();
  const val = document.querySelector('.admin-val')?.value?.trim();
  if (!uid || !val) { toast('请输入QQ号和数值'); return; }
  const r = await fetch('/api/favor', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uid,value:parseInt(val)})});
  const d = await r.json();
  if (d.ok) toast('设置成功！'); else toast('失败: '+d.error);
  queryUser();
}

async function setBalance() {
  const uid = document.querySelector('#admin-uid + .uid-input, .uid-input')?.value?.trim();
  const val = document.querySelector('.admin-val')?.value?.trim();
  if (!uid || !val) { toast('请输入QQ号和数值'); return; }
  const r = await fetch('/api/balance', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uid,value:parseInt(val)})});
  const d = await r.json();
  if (d.ok) toast('设置成功！'); else toast('失败: '+d.error);
  queryUser();
}

load();
</script>
</body>
</html>"""
