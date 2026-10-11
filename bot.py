from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageOps, UnidentifiedImageError
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, KeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

SITE = "https://cosplaytele.com/"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_PATH = Path(os.getenv("BOT_DB_PATH", "bot.sqlite3"))
MAX_IMAGES = max(2, min(int(os.getenv("MAX_IMAGES", "10")), 10))
USER_COOLDOWN_SECONDS = max(10, int(os.getenv("USER_COOLDOWN_SECONDS", "45")))
MAX_IMAGE_BYTES = 9_500_000
USER_AGENT = "CosplayRandomTelegramBot/1.0 (personal use; contact: bot operator)"
CONTINUE_LABEL = "继续提取这一套"

CATEGORY_SOURCES = {
    "cosplay": ("Cosplay", urljoin(SITE, "category/cosplay/")),
    "nude": ("NudeCosplay", urljoin(SITE, "category/cosplay-nudee/")),
    "ero": ("EroCosplay", urljoin(SITE, "category/cosplay-ero/")),
}


def category_keyboard(show_continue: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("Cosplay"), KeyboardButton("NudeCosplay")],
        [KeyboardButton("EroCosplay")],
    ]
    if show_continue:
        rows.insert(0, [KeyboardButton(CONTINUE_LABEL)])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
        input_field_placeholder="选择分类或继续提取",
    )

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Telegram Bot API URLs contain the bot token in the path; httpx INFO logs
# print those URLs, so suppress request-level logs to keep tokens out of bot.log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("cosplay-bot")

_last_user_request: dict[int, float] = {}
_scrape_lock = asyncio.Lock()
_last_scrape_at = 0.0
_active_sets: dict[str, dict] = {}
SET_SESSION_TTL_SECONDS = 30 * 60


class DeliveryUncertainError(RuntimeError):
    """Telegram may have accepted an album even if the client timed out."""


def cleanup_active_sets() -> None:
    cutoff = time.time() - SET_SESSION_TTL_SECONDS
    for session_id, session in list(_active_sets.items()):
        if session["created_at"] < cutoff:
            _active_sets.pop(session_id, None)


def active_set_for_user(user_id: int, chat_id: int) -> tuple[str, dict] | None:
    cleanup_active_sets()
    matches = [
        (session_id, session)
        for session_id, session in _active_sets.items()
        if session["owner_id"] == user_id
        and session["chat_id"] == chat_id
        and session["next_index"] < len(session["images"])
    ]
    return max(matches, key=lambda item: item[1]["created_at"]) if matches else None


def keyboard_for_user(user_id: int | None, chat_id: int) -> ReplyKeyboardMarkup:
    return category_keyboard(show_continue=bool(user_id and active_set_for_user(user_id, chat_id)))


def group_menu_keyboard(user_id: int, chat_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Cosplay", callback_data="cat:cosplay"),
            InlineKeyboardButton("NudeCosplay", callback_data="cat:nude"),
        ],
        [InlineKeyboardButton("EroCosplay", callback_data="cat:ero")],
    ]
    active = active_set_for_user(user_id, chat_id)
    if active:
        session_id, _session = active
        rows.insert(0, [InlineKeyboardButton(CONTINUE_LABEL, callback_data=f"more:{session_id}")])
    return InlineKeyboardMarkup(rows)


def private_chat(update: Update) -> bool:
    # Kept as the existing helper name; group flows use inline buttons and
    # therefore do not depend on disabling Telegram's group privacy mode.
    return bool(
        update.effective_chat
        and update.effective_chat.type in {ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP}
    )


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS age_confirmations (user_id INTEGER PRIMARY KEY, confirmed_at INTEGER NOT NULL)")
    con.commit()
    return con


def is_confirmed(user_id: int) -> bool:
    with db_connect() as con:
        return con.execute("SELECT 1 FROM age_confirmations WHERE user_id=?", (user_id,)).fetchone() is not None


def confirm_age(user_id: int) -> None:
    with db_connect() as con:
        con.execute("INSERT OR REPLACE INTO age_confirmations(user_id, confirmed_at) VALUES (?, ?)", (user_id, int(time.time())))
        con.commit()


def extract_post_links(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for a in soup.select("h5.post-title a[href], .post-title a[href]"):
        href = urljoin(SITE, a.get("href", ""))
        if urlparse(href).netloc.lower() != "cosplaytele.com":
            continue
        path = urlparse(href).path.strip("/")
        if not path or path.startswith(("page/", "category/", "tag/", "author/")):
            continue
        title = a.get_text(" ", strip=True) or path.replace("-", " ").title()
        found[href] = title
    return list(found.items())


def extract_max_page(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    pages = [1]
    for a in soup.select('a[href*="/page/"]'):
        match = re.search(r"/page/(\d+)/?", a.get("href", ""))
        if match:
            pages.append(int(match.group(1)))
    return max(pages)


def extract_images(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one("article")
    scope = article.select_one(".entry-content") if article else None
    scope = scope or soup.select_one(".entry-content") or article or soup
    # The site appends a "Recommend For You" block inside .entry-content.
    # Those thumbnails belong to other posts and must not enter this set.
    for related_block in scope.select(".crp_related"):
        related_block.decompose()
    urls: list[str] = []
    seen: set[str] = set()
    for img in scope.select("img"):
        candidates = [img.get("src"), img.get("data-src"), img.get("data-lazy-src")]
        srcset = img.get("srcset") or img.get("data-srcset") or ""
        candidates.extend(part.strip().split(" ")[0] for part in srcset.split(",") if part.strip())
        parent = img.find_parent("a", href=True)
        if parent:
            candidates.append(parent.get("href"))
        for candidate in candidates:
            if not candidate:
                continue
            url = urljoin(SITE, candidate.strip())
            parsed = urlparse(url)
            if parsed.netloc.lower() not in {"cosplaytele.com", "www.cosplaytele.com"}:
                continue
            if "/wp-content/uploads/" not in parsed.path:
                continue
            if not re.search(r"\.(?:jpe?g|png|webp)(?:$|\?)", url, re.I):
                continue
            if url not in seen:
                seen.add(url)
                urls.append(url)
            break
    return urls


async def get_html(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url, headers={"User-Agent": USER_AGENT})
    if response.status_code in (403, 429):
        raise RuntimeError(f"网站返回 HTTP {response.status_code}，已停止请求以遵守站点访问限制。")
    response.raise_for_status()
    return response.text


async def fetch_random_set(category_url: str | None = None) -> tuple[str, str, list[str]]:
    global _last_scrape_at
    async with _scrape_lock:
        # 全局限速：即使多个用户同时请求，也不密集访问目标站。
        wait = 2.0 - (time.monotonic() - _last_scrape_at)
        if wait > 0:
            await asyncio.sleep(wait)
        timeout = httpx.Timeout(20.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            if category_url:
                listing_html = await get_html(client, category_url)
                list_url = category_url
            else:
                home_html = await get_html(client, SITE)
                max_page = max(1, extract_max_page(home_html))
                page_no = random.randint(1, max_page)
                listing_html = home_html if page_no == 1 else await get_html(client, urljoin(SITE, f"page/{page_no}/"))
                list_url = SITE
            posts = extract_post_links(listing_html)
            if not posts and category_url:
                max_page = extract_max_page(listing_html)
                if max_page > 1:
                    page_no = random.randint(2, max_page)
                    listing_html = await get_html(client, urljoin(list_url, f"page/{page_no}/"))
                    posts = extract_post_links(listing_html)
            if not posts and not category_url and listing_html != home_html:
                posts = extract_post_links(home_html)
            if not posts:
                raise RuntimeError("这个页面没有识别到作品链接，可能是网站页面结构已变化。")

            # 最多尝试 3 个随机作品，以避开暂时没有可见预览图的页面。
            random.shuffle(posts)
            for post_url, title in posts[:3]:
                detail_html = await get_html(client, post_url)
                images = extract_images(detail_html)
                if images:
                    _last_scrape_at = time.monotonic()
                    return title, post_url, images
            raise RuntimeError("随机抽到的作品页没有找到公开预览图，请稍后再试。")


async def fetch_jpeg(client: httpx.AsyncClient, url: str) -> io.BytesIO:
    response = await client.get(url, headers={"User-Agent": USER_AGENT})
    if response.status_code in (403, 429):
        raise RuntimeError(f"图片站返回 HTTP {response.status_code}，已停止请求。")
    response.raise_for_status()
    if len(response.content) > MAX_IMAGE_BYTES:
        raise ValueError("图片超过单张大小限制")
    with Image.open(io.BytesIO(response.content)) as source:
        if source.width * source.height > 50_000_000:
            raise ValueError("图片尺寸超过安全限制")
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((2400, 2400))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=88, optimize=True)
    if output.tell() > MAX_IMAGE_BYTES:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=75, optimize=True)
    output.seek(0)
    output.name = "cosplay-preview.jpg"
    return output


async def send_set_batch(message, session_id: str, start: int, progress_message=None) -> None:
    session = _active_sets[session_id]
    images = session["images"]
    end = min(start + MAX_IMAGES, len(images))
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0), follow_redirects=True) as client:
        files: list[io.BytesIO] = []
        for image_url in images[start:end]:
            try:
                files.append(await fetch_jpeg(client, image_url))
            except (httpx.HTTPError, ValueError, UnidentifiedImageError) as exc:
                log.warning("Skipping image %s: %s", image_url, exc)
        if not files:
            raise RuntimeError("没能下载到这一批可发送的预览图。")
        caption = (
            f"<b>{session['title'][:180]}</b>\n"
            f"{session['category']} · 本套第 {start + 1}–{end} 张 / 共 {len(images)} 张"
        )
        try:
            if len(files) == 1:
                await message.reply_photo(photo=files[0], caption=caption,
                                          parse_mode=ParseMode.HTML, has_spoiler=True)
            else:
                media = [
                    InputMediaPhoto(media=file, caption=caption if i == 0 else None,
                                    parse_mode=ParseMode.HTML if i == 0 else None,
                                    has_spoiler=True)
                    for i, file in enumerate(files)
                ]
                await message.reply_media_group(media=media)
        except Exception as exc:
            # A timeout/network error can occur after Telegram accepted the
            # sendMediaGroup request. Never offer an automatic same-batch retry.
            raise DeliveryUncertainError(
                "Telegram 图片发送超时，无法确认相册是否已送达；为避免重复，本批不会自动重发。"
            ) from exc

    session["next_index"] = end
    session["busy"] = False
    if end < len(images):
        prompt = (
            f"已发送本套第 {start + 1}–{end} 张，还剩 {len(images) - end} 张。\n"
            f"续提按钮“{CONTINUE_LABEL}”已放在本条提示下方。"
        )
        if message.chat.type == ChatType.PRIVATE:
            markup = category_keyboard(show_continue=True)
            prompt += "输入框下方键盘也可直接续提。"
        else:
            markup = group_menu_keyboard(session["owner_id"], message.chat.id)
    else:
        prompt = "这一套的公开预览已全部提取完毕。"
        _active_sets.pop(session_id, None)
        markup = (
            category_keyboard()
            if message.chat.type == ChatType.PRIVATE
            else group_menu_keyboard(session["owner_id"], message.chat.id)
        )
    # Private chats use the persistent reply keyboard; group chats use inline
    # buttons so they work even when Telegram group privacy mode is enabled.
    await message.get_bot().send_message(
        chat_id=message.chat.id, text=prompt, reply_markup=markup
    )
    if progress_message:
        try:
            await progress_message.delete()
        except Exception as exc:
            log.warning("Could not remove batch status message: %s", exc)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private_chat(update):
        return
    if update.effective_chat.type == ChatType.PRIVATE:
        text = (
            "注意：该网站包含成人向内容。本机器人仅供年满 18 岁的用户私聊使用；"
            "图片来源于第三方网站，请尊重原作者与网站条款。\n\n"
            "确认你已年满 18 岁后发送 /confirm18。之后发送 /menu 开始。"
        )
    else:
        text = (
            "注意：该网站包含成人向内容；仅可在所有成员均年满 18 岁的群组使用。"
            "群内发送的图片对全体成员可见，请尊重原作者与网站条款。\n\n"
            "每位使用者都必须自行确认年满 18 岁：发送 /confirm18。之后发送 /menu 开始。"
        )
    await update.effective_message.reply_text(text, reply_markup=ReplyKeyboardRemove())


async def confirm18(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private_chat(update):
        return
    user = update.effective_user
    if not user:
        return
    confirm_age(user.id)
    if update.effective_chat.type == ChatType.PRIVATE:
        text = "已记录你的 18+ 确认。发送 /menu 开始。"
    else:
        text = "已记录你的 18+ 确认。请仅在全员成年（18+）的群组使用；发送 /menu 打开群菜单。"
    await update.effective_message.reply_text(
        text,
        reply_markup=ReplyKeyboardRemove(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private_chat(update):
        return
    user = update.effective_user
    chat = update.effective_chat
    await update.effective_message.reply_text(
        "用法：先发送 /confirm18，再发送 /menu。选择 Cosplay、NudeCosplay 或 EroCosplay 抽取套图；每批最多 10 张。如有后续图片，可使用菜单中的“继续提取这一套”；若按钮未显示，发送 /more 继续。群聊仅限所有成员均已成年（18+）的群组。私聊可用 /hide 收起键盘。",
        reply_markup=(
            (keyboard_for_user(user.id, chat.id) if chat.type == ChatType.PRIVATE else group_menu_keyboard(user.id, chat.id))
            if user and chat and is_confirmed(user.id)
            else ReplyKeyboardRemove()
        ),
    )


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if private_chat(update) and update.effective_message:
        user = update.effective_user
        if not user or not is_confirmed(user.id):
            await update.effective_message.reply_text(
                "请先确认你已年满 18 岁：发送 /confirm18。",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        if update.effective_chat.type == ChatType.PRIVATE:
            markup = keyboard_for_user(user.id, update.effective_chat.id)
        else:
            markup = group_menu_keyboard(user.id, update.effective_chat.id)
        await update.effective_message.reply_text(
            "菜单已打开。选择 Cosplay、NudeCosplay 或 EroCosplay；如有未完成套图，可点“继续提取这一套”。",
            reply_markup=markup,
        )


async def hide_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if private_chat(update) and update.effective_message:
        if update.effective_chat.type != ChatType.PRIVATE:
            await update.effective_message.reply_text("群组菜单是贴在 /menu 消息下方的按钮；/hide 仅用于私聊键盘。")
            return
        await update.effective_message.reply_text(
            "分类键盘已隐藏。发送 /menu 可重新显示。",
            reply_markup=ReplyKeyboardRemove(),
        )


async def send_cos(update: Update, context: ContextTypes.DEFAULT_TYPE, source_key: str | None = None) -> None:
    if not private_chat(update):
        return
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_confirmed(user.id):
        await message.reply_text("请先阅读提示并确认年满 18 岁：发送 /confirm18。")
        return
    now = time.monotonic()
    last = _last_user_request.get(user.id, 0.0)
    if now - last < USER_COOLDOWN_SECONDS:
        remaining = int(USER_COOLDOWN_SECONDS - (now - last)) + 1
        await message.reply_text(f"稍等 {remaining} 秒再抽取，避免对目标网站造成过多访问。")
        return
    _last_user_request[user.id] = now
    for old_id, old_session in list(_active_sets.items()):
        if old_session["owner_id"] == user.id and old_session["chat_id"] == message.chat.id:
            _active_sets.pop(old_id, None)
    status_markup = category_keyboard() if message.chat.type == ChatType.PRIVATE else None
    status = await message.reply_text(
        "正在随机挑选作品并准备预览图……",
        reply_markup=status_markup,
    )
    try:
        selected_source = CATEGORY_SOURCES.get(source_key) if source_key else None
        title, _source_url, all_images = await fetch_random_set(selected_source[1] if selected_source else None)
        cleanup_active_sets()
        session_id = secrets.token_urlsafe(8)
        _active_sets[session_id] = {
            "owner_id": user.id,
            "chat_id": message.chat.id,
            "title": title,
            "category": selected_source[0] if selected_source else "随机 Cosplay",
            "images": all_images,
            "next_index": 0,
            "created_at": time.time(),
            "busy": True,
            "last_click": 0.0,
        }
        session_state = _active_sets[session_id]
        await send_set_batch(message, session_id, 0, progress_message=status)
    except Exception as exc:
        log.exception("Failed to serve random cosplay set")
        if isinstance(exc, DeliveryUncertainError):
            if "session_id" in locals():
                _active_sets.pop(session_id, None)
            await status.edit_text(
                "图片发送时连接超时，Telegram 可能已经收到本批相册。为避免重复，本套续提已停止；请先检查聊天记录，不要立即重试同一批。"
            )
        elif "session_state" in locals() and session_state["next_index"] > 0:
            if session_state["next_index"] < len(session_state["images"]):
                # The album was sent and the session advanced, but the follow-up
                # text/keyboard failed. Keep the session so /menu or /more can recover it.
                session_state["busy"] = False
                recover_markup = (
                    category_keyboard(show_continue=True)
                    if message.chat.type == ChatType.PRIVATE
                    else group_menu_keyboard(user.id, message.chat.id)
                )
                try:
                    await context.bot.send_message(
                        chat_id=message.chat.id,
                        text=f"本批图片已发送；“{CONTINUE_LABEL}”续提按钮已恢复。",
                        reply_markup=recover_markup,
                    )
                except Exception:
                    log.exception("Could not resend continuation keyboard")
                await status.edit_text(
                    "本批图片已发送，但续提按钮提示没有发成功；续提状态已保留。请发送 /menu 重显按钮，或发送 /more 继续。"
                )
            else:
                _active_sets.pop(session_id, None)
                await status.edit_text(
                    "最后一批图片可能已经发送，但结束提示没有发成功。请检查聊天记录；整套已结束。"
                )
        else:
            if "session_id" in locals():
                _active_sets.pop(session_id, None)
            safe_text = str(exc)[:350] or "未知错误"
            await status.edit_text(f"这次没取到图片：{safe_text}\n请稍后再试；若网站限制访问，机器人会自动停止重试。")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not private_chat(update):
        return
    text = (message.text or "").strip()
    if text.lower() == "cos":
        await send_cos(update, context)
        return
    if text == CONTINUE_LABEL:
        await continue_set(update, context)
        return
    for key, (label, category_url) in CATEGORY_SOURCES.items():
        if text.casefold() == label.casefold():
            await send_cos(update, context, source_key=key)
            return
async def continue_session(message, context: ContextTypes.DEFAULT_TYPE, user, session_id: str) -> None:
    session = _active_sets.get(session_id)
    if not session or time.time() - session["created_at"] > SET_SESSION_TTL_SECONDS:
        _active_sets.pop(session_id, None)
        markup = category_keyboard() if message.chat.type == ChatType.PRIVATE else group_menu_keyboard(user.id, message.chat.id)
        await message.reply_text("这套图片的续提状态已过期，请重新发送 /cos。", reply_markup=markup)
        return
    if session["owner_id"] != user.id or session["chat_id"] != message.chat.id:
        await message.reply_text("这套续提只对发起该套图片的用户和聊天有效。")
        return
    if not is_confirmed(user.id):
        await message.reply_text("请先确认你已年满 18 岁：发送 /confirm18。")
        return
    if session["busy"]:
        await message.reply_text("正在提取，请稍等。")
        return
    now = time.monotonic()
    if session["last_click"] > 0 and now - session["last_click"] < 3:
        await message.reply_text("请稍等几秒再继续。")
        return
    session["busy"] = True
    session["last_click"] = now
    start = session["next_index"]
    status_markup = (
        category_keyboard(show_continue=True)
        if message.chat.type == ChatType.PRIVATE
        else group_menu_keyboard(user.id, message.chat.id)
    )
    status = await message.reply_text("正在提取这一套的后续图片……", reply_markup=status_markup)
    try:
        await send_set_batch(message, session_id, start, progress_message=status)
    except Exception as exc:
        log.exception("Failed to continue image set")
        if isinstance(exc, DeliveryUncertainError):
            _active_sets.pop(session_id, None)
            await status.edit_text(
                "图片发送时连接超时，Telegram 可能已经收到本批相册。为避免重复，本套续提已停止；请先检查聊天记录。"
            )
            reset_markup = category_keyboard() if message.chat.type == ChatType.PRIVATE else group_menu_keyboard(user.id, message.chat.id)
            await context.bot.send_message(
                chat_id=message.chat.id,
                text="续提已停止。",
                reply_markup=reset_markup,
            )
            return
        if session["next_index"] >= len(session["images"]):
            _active_sets.pop(session_id, None)
            await status.edit_text("最后一批图片可能已经发送，但结束提示没有发成功。请先检查聊天记录。")
            reset_markup = category_keyboard() if message.chat.type == ChatType.PRIVATE else group_menu_keyboard(user.id, message.chat.id)
            await context.bot.send_message(
                chat_id=message.chat.id,
                text="该套图片已结束。",
                reply_markup=reset_markup,
            )
            return
        session["busy"] = False
        if session["next_index"] > start:
            error_text = "上一批图片已发送，但状态提示没有发成功；可以继续提取下一批。"
        else:
            error_text = f"这批图片暂时没取到：{str(exc)[:250]}。可以稍后再试。"
        if message.chat.type == ChatType.PRIVATE:
            await status.edit_text(error_text)
        else:
            await status.edit_text(error_text, reply_markup=group_menu_keyboard(user.id, message.chat.id))


async def continue_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private_chat(update):
        return
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_confirmed(user.id):
        await message.reply_text("请先确认你已年满 18 岁：发送 /confirm18。")
        return
    current = active_set_for_user(user.id, message.chat.id)
    if not current:
        markup = category_keyboard() if message.chat.type == ChatType.PRIVATE else group_menu_keyboard(user.id, message.chat.id)
        await message.reply_text("当前聊天没有可继续提取的套图。发送 /cos 可获取新的一套。", reply_markup=markup)
        return
    session_id, _session = current
    await continue_session(message, context, user, session_id)


async def on_category_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not private_chat(update):
        await query.answer()
        return
    user = update.effective_user
    key = (query.data or "").removeprefix("cat:")
    if key not in CATEGORY_SOURCES:
        await query.answer("这个分类按钮无效。", show_alert=True)
        return
    if not user or not is_confirmed(user.id):
        await query.answer("请先自行确认年满 18 岁：在群内发送 /confirm18。", show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as exc:
        log.debug("Could not remove used group menu buttons: %s", exc)
    await send_cos(update, context, source_key=key)


async def on_continue_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not private_chat(update):
        await query.answer()
        return
    user = update.effective_user
    session_id = (query.data or "").removeprefix("more:")
    session = _active_sets.get(session_id)
    if not user or not session or time.time() - session["created_at"] > SET_SESSION_TTL_SECONDS:
        _active_sets.pop(session_id, None)
        await query.answer("续提状态已过期，请重新发送 /cos。", show_alert=True)
        return
    if session["owner_id"] != user.id or session["chat_id"] != query.message.chat.id:
        await query.answer("这套续提只对发起该套图片的用户和群聊有效。", show_alert=True)
        return
    if not is_confirmed(user.id):
        await query.answer("请先发送 /confirm18。", show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as exc:
        log.debug("Could not remove used continue button: %s", exc)
    await continue_session(query.message, context, user, session_id)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("请先在 .env 中设置 TELEGRAM_BOT_TOKEN（从 Telegram 的 @BotFather 获取）。")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("confirm18", confirm18))
    app.add_handler(CommandHandler("cos", send_cos))
    app.add_handler(CommandHandler("more", continue_set))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("hide", hide_menu))
    app.add_handler(CallbackQueryHandler(on_category_button, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(on_continue_button, pattern=r"^more:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Cosplay random bot is starting in polling mode")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
