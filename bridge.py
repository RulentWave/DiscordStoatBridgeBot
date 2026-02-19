#!/usr/bin/env python3
"""
Discord / Stoat Bidirectional Bridge

     ██╗ ███╗   ███╗  ██████╗
     ██║ ████╗ ████║ ██╔════╝
     ██║ ██╔████╔██║ ██║  ███╗
██   ██║ ██║╚██╔╝██║ ██║   ██║
╚█████╔╝ ██║ ╚═╝ ██║ ╚██████╔╝
 ╚════╝  ╚═╝     ╚═╝  ╚═════╝

 Thank you for downloading!

 Please refer to readme.md for more info.

 GitHub: https://github.com/JMGstudios/DiscordStoatBridgeBot
 
 Support stoat server: https://stt.gg/FH10z8eP

 Support discord server: https://discord.gg/QTVRxUDSMq

"""

import asyncio
import io
import logging
import os
import re
from collections import OrderedDict
from types import SimpleNamespace

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv
import stoat

load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
STOAT_BOT_TOKEN   = os.getenv("STOAT_BOT_TOKEN", "")

_discord_raw = os.getenv("DISCORD_CHANNEL_IDS", "")
_stoat_raw   = os.getenv("STOAT_CHANNEL_IDS", "")

DISCORD_CHANNEL_IDS: list[int] = [int(x.strip()) for x in _discord_raw.split(",") if x.strip()]
STOAT_CHANNEL_IDS:   list[str] = [x.strip()      for x in _stoat_raw.split(",")   if x.strip()]

REVOLT_API_URL = os.getenv("REVOLT_API_URL", "https://api.revolt.chat").rstrip("/")

if len(DISCORD_CHANNEL_IDS) != len(STOAT_CHANNEL_IDS):
    raise RuntimeError(
        f"Channel list length mismatch: "
        f"{len(DISCORD_CHANNEL_IDS)} Discord IDs vs {len(STOAT_CHANNEL_IDS)} Stoat IDs."
    )

PAIR_COUNT = len(DISCORD_CHANNEL_IDS)

STOAT_TO_DISCORD: dict[str, int] = {s: d for d, s in zip(DISCORD_CHANNEL_IDS, STOAT_CHANNEL_IDS)}
DISCORD_TO_STOAT: dict[int, str] = {d: s for d, s in zip(DISCORD_CHANNEL_IDS, STOAT_CHANNEL_IDS)}


# 25MB file size limit due to discord's restrictions
MAX_FILE_SIZE  = 25 * 1024 * 1024

# The amount of messages that are being stored in cache for reply support
MSG_CACHE_SIZE = 500

# ──────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bridge")

# ──────────────────────────────────────────────────────────────────────────────
#  SHARED STATE
# ──────────────────────────────────────────────────────────────────────────────

discord_webhooks: dict[int, discord.Webhook] = {}
stoat_channels:  dict[str, object]           = {}

_d2s: OrderedDict[int, str] = OrderedDict()   # discord_msg_id → stoat_msg_id
_s2d: OrderedDict[str, int] = OrderedDict()   # stoat_msg_id   → discord_msg_id


def _cache_pair(discord_id: int, stoat_id: str) -> None:
    for cache, key, val in ((_d2s, discord_id, stoat_id), (_s2d, stoat_id, discord_id)):
        if key in cache:
            cache.move_to_end(key)
        cache[key] = val
        if len(cache) > MSG_CACHE_SIZE:
            cache.popitem(last=False)


def _extract_id(obj) -> str | None:
    """Pull an ID string from a raw dict, object, or plain string."""
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj or None
    if isinstance(obj, dict):
        v = obj.get("_id") or obj.get("id")
        return str(v) if v else None
    v = getattr(obj, "_id", None) or getattr(obj, "id", None)
    return str(v) if v else None


def _stoat_asset_url(asset) -> str | None:
    """asset.url is a METHOD on stoat Asset objects – call it safely."""
    if asset is None:
        return None
    url_attr = getattr(asset, "url", None)
    try:
        return url_attr() if callable(url_attr) else str(url_attr)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
#  FILE HELPERS  (Stoat → Discord direction only)
# ──────────────────────────────────────────────────────────────────────────────


async def fetch_bytes(session: aiohttp.ClientSession, url: str) -> tuple[bytes, str] | None:
    """Download url into RAM. Returns (data, filename) or None on any error."""
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning(f"File fetch {url} -> HTTP {resp.status}")
                return None
            cl = resp.headers.get("Content-Length")
            if cl and int(cl) > MAX_FILE_SIZE:
                logger.warning(f"Skipping oversized file ({cl} B): {url}")
                return None
            data = await resp.read()
            if len(data) > MAX_FILE_SIZE:
                logger.warning(f"Skipping oversized file ({len(data)} B) after download")
                return None
            filename = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "file"
            return data, filename
    except Exception as exc:
        logger.error(f"File fetch error for {url}: {exc}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
#  REVOLT MESSAGE FETCH  (for reply quotes)
# ──────────────────────────────────────────────────────────────────────────────


async def fetch_stoat_message(
    channel_id: str,
    message_id: str,
    stoat_client: "StoatBot",
) -> SimpleNamespace | None:

    def _build(raw: dict) -> SimpleNamespace:
        # Masquerade takes priority – it's the display name the relay set.
        masquerade = raw.get("masquerade") or {}
        display_name = (
            masquerade.get("name")
            or _nested_get(raw, "author", "display_name")
            or _nested_get(raw, "author", "username")
            or "unknown"
        )
        return SimpleNamespace(
            content=raw.get("content") or "",
            author=SimpleNamespace(display_name=display_name),
        )

    def _nested_get(d, *keys):
        cur = d
        for k in keys:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur

    # 1. Channel object method
    ch = stoat_channels.get(channel_id)
    if ch is not None:
        for attr in ("fetch_message", "get_message"):
            method = getattr(ch, attr, None)
            if not method:
                continue
            try:
                result = await method(message_id)
                if result is None:
                    continue
                # Already a library object?
                if not isinstance(result, dict):
                    # Get masquerade from raw attribute
                    masq = getattr(result, "masquerade", None)
                    masq_name = (
                        masq.get("name") if isinstance(masq, dict)
                        else getattr(masq, "name", None)
                    ) if masq else None
                    author = getattr(result, "author", None)
                    display_name = (
                        masq_name
                        or getattr(author, "display_name", None)
                        or getattr(author, "name", None)
                        or "unknown"
                    )
                    return SimpleNamespace(
                        content=getattr(result, "content", "") or "",
                        author=SimpleNamespace(display_name=display_name),
                    )
                return _build(result)
            except Exception as exc:
                logger.debug(f"fetch_stoat_message via ch.{attr}: {exc}")

    # 2. revolt.py HTTPClient.request
    http = getattr(stoat_client, "http", None)
    if http is not None:
        request_fn = getattr(http, "request", None)
        if request_fn:
            try:
                data = await request_fn("GET", f"/channels/{channel_id}/messages/{message_id}")
                if isinstance(data, dict):
                    return _build(data)
            except Exception as exc:
                logger.debug(f"fetch_stoat_message via http.request: {exc}")

    logger.warning(f"fetch_stoat_message: could not fetch {channel_id}/{message_id}")
    return None


# ──────────────────────────────────────────────────────────────────────────────
#  MENTION / EMOJI HELPERS
# ──────────────────────────────────────────────────────────────────────────────

_RE_DISCORD_USER    = re.compile(r"<@!?(\d+)>")
_RE_DISCORD_CHANNEL = re.compile(r"<#(\d+)>")
_RE_DISCORD_ROLE    = re.compile(r"<@&(\d+)>")
_RE_DISCORD_EMOJI   = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")

_RE_REVOLT_USER       = re.compile(r"<@([A-Z0-9]{26})>")
_RE_REVOLT_CUSTOM_EMO = re.compile(r":([A-Z0-9]{26}):")

_emoji_name_cache: dict[str, str] = {}


async def resolve_revolt_emoji(emoji_id: str, session: aiohttp.ClientSession, token: str) -> str:
    if emoji_id in _emoji_name_cache:
        return _emoji_name_cache[emoji_id]
    try:
        async with session.get(
            f"{REVOLT_API_URL}/custom/emoji/{emoji_id}",
            headers={"x-bot-token": token},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                name = data.get("name") or emoji_id
                _emoji_name_cache[emoji_id] = name
                return name
    except Exception as exc:
        logger.debug(f"Could not resolve Stoat emoji {emoji_id}: {exc}")
    return emoji_id


async def clean_discord_content(content: str, message: discord.Message) -> str:
    """Resolve Discord markup to plain text before forwarding to Stoat."""
    guild  = message.guild
    result = content

    for m in reversed(list(_RE_DISCORD_USER.finditer(result))):
        uid  = int(m.group(1))
        name = f"@user{uid}"
        if guild:
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except Exception:
                    member = None
            if member:
                name = f"@{member.display_name}"
        result = result[: m.start()] + name + result[m.end() :]

    def _channel(m: re.Match) -> str:
        ch = guild.get_channel(int(m.group(1))) if guild else None
        return f"#{ch.name}" if ch else "#channel"

    result = _RE_DISCORD_CHANNEL.sub(_channel, result)

    def _role(m: re.Match) -> str:
        role = guild.get_role(int(m.group(1))) if guild else None
        return f"@{role.name}" if role else "@role"

    result = _RE_DISCORD_ROLE.sub(_role, result)
    result = _RE_DISCORD_EMOJI.sub(lambda m: f":{m.group(1)}:", result)
    return result


async def clean_stoat_content(
    content: str,
    session: aiohttp.ClientSession,
    token: str,
) -> str:
    """Resolve Stoat markup to plain text before forwarding to Discord."""
    result = content

    for m in reversed(list(_RE_REVOLT_USER.finditer(result))):
        uid  = m.group(1)
        name = "@user"
        try:
            async with session.get(
                f"{REVOLT_API_URL}/users/{uid}",
                headers={"x-bot-token": token},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    name = "@" + (data.get("display_name") or data.get("username") or uid)
        except Exception as exc:
            logger.debug(f"Could not resolve Revolt user {uid}: {exc}")
        result = result[: m.start()] + name + result[m.end() :]

    matches = list(_RE_REVOLT_CUSTOM_EMO.finditer(result))
    if matches:
        names = await asyncio.gather(
            *[resolve_revolt_emoji(m.group(1), session, token) for m in matches]
        )
        for m, name in zip(reversed(matches), reversed(names)):
            result = result[: m.start()] + f":{name}:" + result[m.end() :]

    return result


# ──────────────────────────────────────────────────────────────────────────────
#  STOAT BOT
# ──────────────────────────────────────────────────────────────────────────────


class StoatBot(stoat.Client):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._http_session: aiohttp.ClientSession | None = None

    async def on_ready(self, event, /):
        self._http_session = aiohttp.ClientSession()
        logger.info(f"Stoat: connected as {self.me}")
        for stoat_id in STOAT_CHANNEL_IDS:
            try:
                ch = await self.fetch_channel(stoat_id)
                stoat_channels[stoat_id] = ch
                logger.info(f"Stoat: listening in #{ch.name} (id={stoat_id})")
            except Exception as exc:
                logger.error(f"Stoat: could not fetch channel {stoat_id} - {exc}")

    async def on_message_create(self, event: stoat.MessageCreateEvent, /):
        msg = event.message

        if msg.author_id == self.me.id:
            return

        stoat_id = msg.channel.id
        if stoat_id not in STOAT_TO_DISCORD:
            return

        discord_id = STOAT_TO_DISCORD[stoat_id]
        webhook    = discord_webhooks.get(discord_id)
        if webhook is None:
            logger.warning(f"Stoat -> Discord: webhook for {discord_id} not ready, dropped")
            return

        # ── Clean content ─────────────────────────────────────────────────────
        content = await clean_stoat_content(
            msg.content or "", self._http_session, STOAT_BOT_TOKEN
        )

        # ── Reply → quote fallback ────────────────────────────────────────────
        # Because webhook can't reply, quote fallback is used
        replies_raw = getattr(msg, "replies", None) or []
        reply_id: str | None = None
        if replies_raw:
            first    = replies_raw[0]
            reply_id = _extract_id(first) or (str(first) if isinstance(first, str) else None)

        if reply_id:
            orig = await fetch_stoat_message(stoat_id, reply_id, self)
            if orig is not None:
                orig_author  = orig.author.display_name[:50]
                orig_snippet = (orig.content or "")[:80].replace("\n", " ")
                content = f"-# ↩ **{orig_author}**: *{orig_snippet}*\n{content}"
            else:
                logger.warning(f"Stoat -> Discord: could not fetch reply target '{reply_id}'")

        # ── Attachments: download from Autumn, upload to Discord ──────────────
        discord_files: list[discord.File] = []
        for att in getattr(msg, "attachments", None) or []:
            url      = _stoat_asset_url(att)
            filename = getattr(att, "filename", None) or "file"
            if not url:
                continue
            result = await fetch_bytes(self._http_session, url)
            if result is None:
                content += f"\n{url}"
                continue
            data, fname = result
            discord_files.append(discord.File(io.BytesIO(data), filename=filename or fname))
            del data

        if not content.strip() and not discord_files:
            return

        author_name = (
            getattr(msg.author, "display_name", None)
            or getattr(msg.author, "name", None)
            or "unknown"
        )[:80]
        avatar_url = _stoat_asset_url(getattr(msg.author, "avatar", None))

        try:
            sent = await webhook.send(
                content    = content[:2000] if content.strip() else discord.utils.MISSING,
                username   = author_name,
                avatar_url = avatar_url,
                files      = discord_files or discord.utils.MISSING,
                wait       = True,
            )
            _cache_pair(sent.id, str(msg.id))
            logger.debug(f"Stoat -> Discord: cached discord={sent.id} <-> stoat={msg.id}")
        except Exception as exc:
            logger.error(f"Stoat -> Discord (channel {discord_id}): {exc}")
        finally:
            for f in discord_files:
                f.fp.close()


# ──────────────────────────────────────────────────────────────────────────────
#  DISCORD BOT
# ──────────────────────────────────────────────────────────────────────────────


class DiscordBot(commands.Bot):

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds          = True
        intents.webhooks        = True
        intents.members         = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.loop.create_task(self._setup_webhooks())

    async def _setup_webhooks(self):
        await self.wait_until_ready()
        for discord_id in DISCORD_CHANNEL_IDS:
            try:
                channel = self.get_channel(discord_id) or await self.fetch_channel(discord_id)
                for wh in await channel.webhooks():
                    if wh.user == self.user:
                        discord_webhooks[discord_id] = wh
                        logger.info(f"Discord: reusing webhook '{wh.name}' for channel {discord_id}")
                        break
                else:
                    wh = await channel.create_webhook(name="Stoat Bridge")
                    discord_webhooks[discord_id] = wh
                    logger.info(f"Discord: created webhook for channel {discord_id}")
            except Exception as exc:
                logger.error(f"Discord: could not set up webhook for channel {discord_id} - {exc}")

    async def on_ready(self):
        logger.info(f"Discord: connected as {self.user}")
        logger.info(f"Discord: bridging {PAIR_COUNT} channel pair(s)")

    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            return

        if message.webhook_id is not None:
            if message.webhook_id in {wh.id for wh in discord_webhooks.values()}:
                return

        discord_id = message.channel.id
        if discord_id not in DISCORD_TO_STOAT:
            return

        stoat_id = DISCORD_TO_STOAT[discord_id]
        ch       = stoat_channels.get(stoat_id)
        if ch is None:
            logger.warning(f"Discord -> Stoat: channel {stoat_id} not ready, dropped")
            return

        # ── Content ───────────────────────────────────────────────────────────
        content = await clean_discord_content(message.content or "", message)

        # ── Reply ─────────────────────────────────────────────────────────────
        # cached Stoat message ID for the referenced Discord message.
        stoat_replies: list = []
        if message.reference and message.reference.message_id:
            ref_discord_id  = message.reference.message_id
            cached_stoat_id = _d2s.get(ref_discord_id)

            if cached_stoat_id:
                # Native Stoat reply.
                stoat_replies = [SimpleNamespace(id=cached_stoat_id, mention=False)]
                logger.debug(
                    f"Discord -> Stoat: native reply to stoat_id={cached_stoat_id} "
                    f"(from discord ref={ref_discord_id})"
                )
            else:
                # Cache miss – quote fallback.
                logger.debug(
                    f"Discord -> Stoat: reply ref={ref_discord_id} not in cache, using quote"
                )
                try:
                    ref_msg = (
                        message.reference.resolved
                        if isinstance(message.reference.resolved, discord.Message)
                        else await message.channel.fetch_message(ref_discord_id)
                    )
                    ref_author  = ref_msg.author.display_name[:50]
                    ref_snippet = (ref_msg.content or "")[:80].replace("\n", " ")
                    content = f"-# ↩ **{ref_author}**: *{ref_snippet}*\n{content}"
                except Exception as exc:
                    logger.debug(f"Could not fetch Discord reply target {ref_discord_id}: {exc}")

        # ── Attachments: append the URL
        for att in message.attachments:
            content += f" {att.url}"

        if not content.strip():
            return

        avatar_url = (
            str(message.author.avatar.url)
            if message.author.avatar
            else str(message.author.default_avatar.url)
        )

        send_kwargs: dict = {
            "masquerade": stoat.Masquerade(
                name=message.author.display_name[:32],
                avatar=avatar_url,
            ),
            "content": content[:2000],
        }
        if stoat_replies:
            send_kwargs["replies"] = stoat_replies

        try:
            sent = await ch.send(**send_kwargs)

            sent_id = _extract_id(sent)
            if sent_id:
                _cache_pair(message.id, sent_id)
                logger.debug(f"Discord -> Stoat: cached discord={message.id} <-> stoat={sent_id}")
            else:
                logger.warning(
                    f"Discord -> Stoat: could not extract ID from sent object "
                    f"type={type(sent).__name__!r} repr={sent!r:.200}"
                )
        except Exception as exc:
            logger.error(f"Discord -> Stoat (channel {stoat_id}): {exc}")


# ──────────────────────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────────────────────


async def main():
    if not all([DISCORD_BOT_TOKEN, STOAT_BOT_TOKEN, DISCORD_CHANNEL_IDS, STOAT_CHANNEL_IDS]):
        raise RuntimeError("Missing configuration – check your .env file.")

    logger.info(f"Bridge starting with {PAIR_COUNT} channel pair(s)...")
    for i, (d, s) in enumerate(zip(DISCORD_CHANNEL_IDS, STOAT_CHANNEL_IDS), 1):
        logger.info(f"  Pair {i}: Discord {d} <-> Stoat {s}")

    await asyncio.gather(
        StoatBot(token=STOAT_BOT_TOKEN).start(),
        DiscordBot().start(DISCORD_BOT_TOKEN),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bridge stopped")
