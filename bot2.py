#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import io
import json
import time
import zipfile
import shutil
import subprocess
from typing import List, Tuple, Callable, Awaitable

import logging
logging.basicConfig(level=logging.INFO)  # log ke VPS/terminal

import asyncio
import aiohttp
from PIL import Image
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession

# ============================================================
# CONFIG & INIT
# ============================================================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("BOT_TOKEN belum di-set. Buat file .env dan isi BOT_TOKEN=...")

BASE_DIR = "stickers"
os.makedirs(BASE_DIR, exist_ok=True)

# timeout untuk polling bot (HARUS angka detik untuk aiogram v3)
bot_session = AiohttpSession(timeout=600)

# timeout detil untuk unduhan (dipakai oleh aiohttp.ClientSession)
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=600, connect=30, sock_connect=30, sock_read=300)

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    session=bot_session
)
dp = Dispatcher()
router = Router()

# user_id -> mode ("static" | "anim")
USER_MODE: dict[int, str] = {}

# ============================================================
# PROGRESS BAR + ETA (Telegram + Console) — THREAD-SAFE
# ============================================================

def _bar(percent: int, width: int = 20) -> str:
    filled = int(width * percent / 100)
    return "▰" * filled + "▱" * (width - filled)

def _fmt_eta(seconds: float) -> str:
    if seconds <= 0 or seconds == float("inf"):
        return "ETA --:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"ETA {h:02d}:{m:02d}:{s:02d}"
    return f"ETA {m:02d}:{s:02d}"

class DualProgress:
    """
    Progress ke Telegram & console.
    Aman dipanggil dari main-loop (async) maupun dari thread (blocking jobs).
    """
    def __init__(self, label: str, total: int,
                 set_status_async: Callable[[str], Awaitable[None]],
                 console_prefix: str = "",
                 loop: asyncio.AbstractEventLoop | None = None):
        self.label = label
        self.total = max(1, total)
        self.set_status_async = set_status_async
        self.console_prefix = console_prefix or label
        self._last_percent = -1
        self._last_time = 0.0
        self._start = time.time()
        self.loop = loop or asyncio.get_event_loop()

    async def _update(self, current: int, done: bool = False, extra: str = ""):
        now = time.time()
        current = max(0, min(self.total, current))
        percent = int(current * 100 / self.total)
        elapsed = max(1e-6, now - self._start)
        rate = current / elapsed
        remain = self.total - current
        eta = remain / rate if rate > 0 else float("inf")

        if done or percent != self._last_percent and (percent - self._last_percent >= 2 or now - self._last_time >= 0.5):
            text = (
                f"{self.label}\n"
                f"{_bar(percent)} {percent}% | {current}/{self.total}\n"
                f"⚡ {rate:.1f} file/s • {_fmt_eta(eta)}"
            )
            if done:
                text += " ✅"
            if extra:
                text += f"\n{extra}"

            try:
                await self.set_status_async(text)
            except Exception:
                pass
            print(f"\r{self.console_prefix}: {_bar(percent)} {percent}% | {current}/{self.total} | {rate:.1f} f/s | {_fmt_eta(eta)}   ", end="", flush=True)
            self._last_percent = percent
            self._last_time = now

    # dipanggil dari konteks ASYNC
    async def tick_async(self, current: int):
        await self._update(current, done=False)

    # dipanggil dari THREAD (blocking jobs)
    def tick_ts(self, current: int):
        asyncio.run_coroutine_threadsafe(self._update(current, done=False), self.loop)

    async def done_async(self, extra: str = ""):
        await self._update(self.total, done=True, extra=extra)
        print()

    def done_ts(self, extra: str = ""):
        asyncio.run_coroutine_threadsafe(self._update(self.total, done=True, extra=extra), self.loop)

# ============================================================
# HTTP helpers (retry/backoff)
# ============================================================

async def _retry_fetch_json(session, url, retries=3, backoff=1.6):
    last = None
    for i in range(retries):
        try:
            async with session.get(url) as resp:
                if resp.status in (429, 500, 502, 503, 504):
                    raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status)
                return await resp.json()
        except Exception as e:
            last = e
            await asyncio.sleep(backoff ** i)
    raise last

async def _retry_fetch_bytes(session, url, retries=3, backoff=1.6):
    last = None
    for i in range(retries):
        try:
            async with session.get(url) as resp:
                if resp.status in (429, 500, 502, 503, 504):
                    raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status)
                return await resp.read()
        except Exception as e:
            last = e
            await asyncio.sleep(backoff ** i)
    raise last

# ============================================================
# TELEGRAM API helpers
# ============================================================

LINK_RE = re.compile(r"addstickers/([A-Za-z0-9_]+)")

def extract_pack_name(link: str) -> str:
    m = LINK_RE.search(link)
    if not m:
        raise ValueError("❌ Link tidak valid. Gunakan format seperti:\nhttps://t.me/addstickers/namapack")
    return m.group(1)

async def tg_get_sticker_set(session: aiohttp.ClientSession, bot_token: str, pack_name: str) -> dict:
    url = f"https://api.telegram.org/bot{bot_token}/getStickerSet?name={pack_name}"
    return (await _retry_fetch_json(session, url))["result"]

async def tg_get_file_path(session: aiohttp.ClientSession, bot_token: str, file_id: str) -> str:
    url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
    return (await _retry_fetch_json(session, url))["result"]["file_path"]

async def tg_download(session: aiohttp.ClientSession, bot_token: str, file_path: str) -> bytes:
    url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    return await _retry_fetch_bytes(session, url)

# ============================================================
# DOWNLOAD (parallel + limited)
# ============================================================

async def download_pack(
    bot_token: str,
    pack_name: str,
    set_status_async: Callable[[str], Awaitable[None]]
) -> Tuple[List[str], str]:
    """
    Unduh semua file pack ke folder:
      .png (statis), .tgs / .webm (animasi).
    Paralel dengan semaphore (default 8).
    """
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        result = await tg_get_sticker_set(session, bot_token, pack_name)
        stickers = result["stickers"]
        total = len(stickers)

        loop = asyncio.get_running_loop()
        prog = DualProgress("⬇️ Mengunduh stiker …", total, set_status_async, "Download", loop=loop)

        folder = os.path.join(BASE_DIR, pack_name)
        if os.path.exists(folder):
            shutil.rmtree(folder)
        os.makedirs(folder, exist_ok=True)

        sem = asyncio.Semaphore(8)
        file_list: List[str] = [None] * total  # type: ignore

        async def worker(idx: int, s: dict):
            async with sem:
                if s.get("is_animated"):
                    ext = "tgs"
                elif s.get("is_video"):
                    ext = "webm"
                else:
                    ext = "png"
                fp = await tg_get_file_path(session, bot_token, s["file_id"])
                data = await tg_download(session, bot_token, fp)
                out_path = os.path.join(folder, f"{idx:03d}.{ext}")
                with open(out_path, "wb") as f:
                    f.write(data)
                file_list[idx] = out_path
                await prog.tick_async(sum(1 for x in file_list if x))

        await asyncio.gather(*(worker(i, s) for i, s in enumerate(stickers)))
        files = [x for x in file_list if x]  # remove None
        await prog.done_async(f"Total file: *{len(files)}*")
        return files, folder

# ============================================================
# CONVERT (thread pool; progress thread-safe)
# ============================================================

def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None

async def convert_static(folder: str, set_status_async: Callable[[str], Awaitable[None]]) -> Tuple[List[str], str]:
    names = [n for n in sorted(os.listdir(folder)) if n.endswith((".png", ".webp"))]
    loop = asyncio.get_running_loop()
    prog = DualProgress("⚙️ Konversi gambar ke WEBP …", len(names) or 1, set_status_async, "Convert", loop=loop)

    def _run() -> Tuple[List[str], str]:
        out_dir = os.path.join(folder, "converted_static")
        os.makedirs(out_dir, exist_ok=True)
        out: List[str] = []
        for i, name in enumerate(names, 1):
            src = os.path.join(folder, name)
            img = Image.open(src).convert("RGBA")
            img.thumbnail((512, 512))
            dst = os.path.join(out_dir, os.path.splitext(name)[0] + ".webp")
            img.save(dst, "WEBP", quality=90)
            out.append(dst)
            prog.tick_ts(i)  # thread-safe
        prog.done_ts(f"Total dikonversi: *{len(out)}*")
        return out, out_dir

    files, out_dir = await asyncio.to_thread(_run)
    return files, out_dir

async def convert_animated(folder: str, set_status_async: Callable[[str], Awaitable[None]]) -> Tuple[List[str], str]:
    names = sorted(os.listdir(folder))
    loop = asyncio.get_running_loop()
    prog = DualProgress("⚙️ Konversi animasi ke WEBP …", len(names) or 1, set_status_async, "Convert", loop=loop)

    def _run() -> Tuple[List[str], str]:
        if not _have("ffmpeg") or not _have("img2webp"):
            missing = []
            if not _have("ffmpeg"): missing.append("ffmpeg")
            if not _have("img2webp"): missing.append("img2webp (paket webp)")
            raise RuntimeError(f"Tool eksternal belum terpasang: {', '.join(missing)}")

        out_dir = os.path.join(folder, "converted_anim")
        os.makedirs(out_dir, exist_ok=True)

        out: List[str] = []
        for i, file in enumerate(names, 1):
            src = os.path.join(folder, file)
            name, ext = os.path.splitext(file)
            dst = os.path.join(out_dir, f"{name}.webp")

            try:
                if ext == ".tgs":
                    if _have("rlottie-convert"):
                        frames = os.path.join(out_dir, f"{name}_frames")
                        os.makedirs(frames, exist_ok=True)
                        subprocess.run(["rlottie-convert", src, os.path.join(frames, "%03d.png")], check=True)
                        frame_files = sorted([os.path.join(frames, f) for f in os.listdir(frames) if f.endswith(".png")])
                        if frame_files:
                            subprocess.run(["img2webp", "-loop", "0", "-lossy", "-q", "80", "-o", dst, *frame_files], check=True)
                    else:
                        logging.info("skip .tgs (rlottie-convert tidak ada): %s", file)
                elif ext == ".webm":
                    subprocess.run([
                        "ffmpeg", "-y", "-i", src,
                        "-vf", "fps=15,scale=512:512:force_original_aspect_ratio=decrease,"
                               "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x00000000",
                        "-loop", "0", dst
                    ], check=True)

                if os.path.exists(dst):
                    out.append(dst)
            except Exception as e:
                logging.warning("Gagal konversi animasi %s: %s", file, e)

            prog.tick_ts(i)

        prog.done_ts(f"Total animasi: *{len(out)}*")
        return out, out_dir

    files, out_dir = await asyncio.to_thread(_run)
    return files, out_dir

# ============================================================
# PACKING & SENDING
# ============================================================

def chunk_by_30(files: List[str]) -> List[List[str]]:
    """Potong list menjadi potongan 30 (pack WhatsApp)."""
    return [files[i:i+30] for i in range(0, len(files), 30)]

def build_pack_zip(packname: str, pack_index: int, files: List[str]) -> io.BytesIO:
    """
    ZIP siap “dibagikan ke Sticker Maker”.
    (root ZIP, tanpa subfolder)
      author.txt     -> nama pack
      title.txt      -> nama pack + info pack
      icon.png       -> dari stiker #1 (96x96)
      sticker_0.webp ... sticker_{N-1}.webp
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # author & title
        zf.writestr("author.txt", packname)
        zf.writestr("title.txt", f"{packname} (Pack {pack_index:02d})")

        # icon.png dari file pertama
        icon_io = io.BytesIO()
        icon_img = Image.open(files[0]).convert("RGBA")
        icon_img.thumbnail((96, 96))
        icon_img.save(icon_io, "PNG")
        icon_io.seek(0)
        zf.writestr("icon.png", icon_io.read())

        # sticker_0.webp ... sticker_{N-1}.webp
        for idx, f in enumerate(files):
            with open(f, "rb") as fp:
                zf.writestr(f"sticker_{idx}.webp", fp.read())

    buf.seek(0)
    return buf

def _zip_size(buf: io.BytesIO) -> int:
    pos = buf.tell()
    buf.seek(0, os.SEEK_END)
    size = buf.tell()
    buf.seek(pos)
    return size

async def send_zip_safely(message: types.Message, filename: str, buf: io.BytesIO):
    """Kirim ZIP + jeda kecil agar tidak rate-limit."""
    await message.answer_document(BufferedInputFile(buf.read(), filename=filename))
    await asyncio.sleep(0.7)

# ============================================================
# COMMANDS (v3 Router)
# ============================================================
WELCOME = (
    "👋 *Selamat datang di Bot Konversi Stiker Telegram → WhatsApp!*\n\n"
    "Saya bisa mengubah stiker Telegram menjadi format WhatsApp siap impor (WEBP).\n\n"
    "🧭 Cara pakai:\n"
    "• /stikerbiasa  → untuk stiker *statis* (PNG/WEBP)\n"
    "• /stikeranimasi → untuk stiker *bergerak* (TGS/WEBM → animated WEBP)\n\n"
    "Kirim perintahnya dulu, lalu kirim *link pack* seperti:\n"
    "`https://t.me/addstickers/namapack`"
)

@router.message(Command("start", "help"))
async def cmd_start(message: types.Message):
    await message.answer(WELCOME)

@router.message(Command("stikerbiasa"))
async def cmd_static(message: types.Message):
    USER_MODE[message.from_user.id] = "static"
    await message.answer("🖼 Mode *stiker biasa* aktif.\nKirim link pack Telegram-nya ya 🙂")

@router.message(Command("stikeranimasi"))
async def cmd_anim(message: types.Message):
    USER_MODE[message.from_user.id] = "anim"
    await message.answer("🎞 Mode *stiker animasi* aktif.\nKirim link pack Telegram-nya ya 🙂")

@router.message()  # terima link setelah user pilih mode
async def handle_link(message: types.Message):
    mode = USER_MODE.get(message.from_user.id)
    if mode not in ("static", "anim"):
        return

    # validasi link
    try:
        pack = extract_pack_name(message.text.strip())
    except Exception as e:
        await message.reply(str(e))
        return

    status = await message.answer(f"🔎 Memeriksa link *{pack}* ...")

    async def set_status(text: str):
        try:
            await status.edit_text(text)
        except Exception:
            pass

    try:
        # === UNDUH ===
        files, folder = await download_pack(TOKEN, pack, set_status_async=set_status)

        # === KONVERSI ===
        if mode == "static":
            converted, _ = await convert_static(folder, set_status)
        else:
            converted, _ = await convert_animated(folder, set_status)

        if not converted:
            await set_status("⚠️ Tidak ada file yang bisa dikonversi pada pack ini.")
            USER_MODE.pop(message.from_user.id, None)
            return

        # === PACKING ===
        packs = chunk_by_30(converted)  # N pack berisi 30; terakhir bisa <30
        total_packs = len(packs)
        loop = asyncio.get_running_loop()
        prog_pk = DualProgress("📦 Menyusun ZIP pack …", total_packs, set_status, "Packing", loop=loop)

        for idx, pack_files in enumerate(packs, 1):
            zip_buf = await asyncio.to_thread(build_pack_zip, pack, idx, pack_files)
            size = _zip_size(zip_buf)

            if size > 48 * 1024 * 1024 and len(pack_files) > 15:
                # fallback: pecah dua jika ZIP terlalu besar
                half = len(pack_files) // 2
                partA = await asyncio.to_thread(build_pack_zip, f"{pack}_A", idx, pack_files[:half])
                partB = await asyncio.to_thread(build_pack_zip, f"{pack}_B", idx, pack_files[half:])
                await send_zip_safely(message, f"{pack}_pack{idx:02d}_A.zip", partA)
                await send_zip_safely(message, f"{pack}_pack{idx:02d}_B.zip", partB)
            else:
                await send_zip_safely(message, f"{pack}_pack{idx:02d}.zip", zip_buf)

            await prog_pk.tick_async(idx)

        await prog_pk.done_async()
        await message.answer("🎉 Beres! Semua pack terkirim. ZIP bisa *dibagikan langsung ke Sticker Maker* atau diekstrak & diimpor.")
    except RuntimeError as e:
        await set_status(f"❌ {e}")
    except Exception as e:
        await set_status(f"❌ Terjadi kesalahan: {e}")
    finally:
        USER_MODE.pop(message.from_user.id, None)

# ============================================================
# RUN (v3 style)
# ============================================================

async def main():
    print("🤖 Bot konversi stiker Telegram → WhatsApp aktif.")
    dp.include_router(router)
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
