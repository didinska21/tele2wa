#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import io
import math
import zipfile
import shutil
import subprocess
from typing import List, Tuple

import aiohttp
from PIL import Image
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import BufferedInputFile

# ============================================================
# CONFIG & INIT
# ============================================================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("BOT_TOKEN belum di-set. Buat file .env dan isi BOT_TOKEN=...")

BASE_DIR = "stickers"
os.makedirs(BASE_DIR, exist_ok=True)

from aiogram.client.default import DefaultBotProperties

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
)
dp = Dispatcher()
router = Router()

# Simpel state: user_id -> mode ("static" | "anim")
USER_MODE: dict[int, str] = {}

# ============================================================
# HELPERS
# ============================================================
LINK_RE = re.compile(r"addstickers/([A-Za-z0-9_]+)")

def extract_pack_name(link: str) -> str:
    m = LINK_RE.search(link)
    if not m:
        raise ValueError("❌ Link tidak valid. Gunakan format seperti:\nhttps://t.me/addstickers/namapack")
    return m.group(1)

async def tg_get_sticker_set(session: aiohttp.ClientSession, bot_token: str, pack_name: str) -> dict:
    async with session.get(f"https://api.telegram.org/bot{bot_token}/getStickerSet?name={pack_name}") as resp:
        data = await resp.json()
        if not data.get("ok"):
            raise ValueError("Gagal ambil pack: " + str(data))
        return data["result"]

async def tg_get_file_path(session: aiohttp.ClientSession, bot_token: str, file_id: str) -> str:
    async with session.get(f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}") as r2:
        info = await r2.json()
        return info["result"]["file_path"]

async def tg_download(session: aiohttp.ClientSession, bot_token: str, file_path: str) -> bytes:
    async with session.get(f"https://api.telegram.org/file/bot{bot_token}/{file_path}") as r3:
        return await r3.read()

async def download_pack(bot_token: str, pack_name: str) -> tuple[List[str], str]:
    """Unduh semua file pack. Simpan: .png (statis), .tgs / .webm (animasi)."""
    async with aiohttp.ClientSession() as session:
        result = await tg_get_sticker_set(session, bot_token, pack_name)
        stickers = result["stickers"]

        folder = os.path.join(BASE_DIR, pack_name)
        if os.path.exists(folder):
            shutil.rmtree(folder)
        os.makedirs(folder, exist_ok=True)

        file_list: List[str] = []
        for s in stickers:
            if s.get("is_animated"):
                ext = "tgs"
            elif s.get("is_video"):
                ext = "webm"
            else:
                ext = "png"

            file_path = await tg_get_file_path(session, bot_token, s["file_id"])
            data = await tg_download(session, bot_token, file_path)

            out_path = os.path.join(folder, f"{len(file_list):03d}.{ext}")
            with open(out_path, "wb") as f:
                f.write(data)
            file_list.append(out_path)

        return file_list, folder

def convert_static(folder: str) -> tuple[List[str], str]:
    """PNG/WEBP → WEBP 512x512 (transparansi dijaga)."""
    out_dir = os.path.join(folder, "converted_static")
    os.makedirs(out_dir, exist_ok=True)

    converted: List[str] = []
    for name in sorted(os.listdir(folder)):
        if not (name.endswith(".png") or name.endswith(".webp")):
            continue
        src = os.path.join(folder, name)
        img = Image.open(src).convert("RGBA")
        img.thumbnail((512, 512))
        dst = os.path.join(out_dir, os.path.splitext(name)[0] + ".webp")
        img.save(dst, "WEBP", quality=90)
        converted.append(dst)
    return converted, out_dir

def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def convert_animated(folder: str) -> tuple[List[str], str]:
    """
    .tgs → frames (rlottie-convert) → img2webp → animated .webp
    .webm → ffmpeg → animated .webp
    """
    out_dir = os.path.join(folder, "converted_anim")
    os.makedirs(out_dir, exist_ok=True)

    # Wajib untuk animasi:
    missing = []
    if not _have("ffmpeg"): missing.append("ffmpeg")
    if not _have("img2webp"): missing.append("img2webp (paket webp)")
    if missing:
        raise RuntimeError(f"Tool eksternal belum terpasang: {', '.join(missing)}")

    converted: List[str] = []
    for file in sorted(os.listdir(folder)):
        src = os.path.join(folder, file)
        name, ext = os.path.splitext(file)
        dst = os.path.join(out_dir, f"{name}.webp")

        try:
            if ext == ".tgs":
                if not _have("rlottie-convert"):
                    # tidak fatal: skip .tgs jika tool tidak ada
                    print(f"skip .tgs (rlottie-convert tidak ada): {file}")
                    continue
                frames = os.path.join(out_dir, f"{name}_frames")
                os.makedirs(frames, exist_ok=True)
                subprocess.run(["rlottie-convert", src, os.path.join(frames, "%03d.png")], check=True)
                frame_files = sorted(
                    [os.path.join(frames, f) for f in os.listdir(frames) if f.endswith(".png")]
                )
                if not frame_files:
                    continue
                subprocess.run(
                    ["img2webp", "-loop", "0", "-lossy", "-q", "80", "-o", dst, *frame_files],
                    check=True
                )

            elif ext == ".webm":
                subprocess.run([
                    "ffmpeg", "-y", "-i", src,
                    "-vf", "fps=15,scale=512:512:force_original_aspect_ratio=decrease,"
                           "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x00000000",
                    "-loop", "0", dst
                ], check=True)
            else:
                continue

            if os.path.exists(dst):
                converted.append(dst)

        except Exception as e:
            print("Gagal konversi animasi:", e)
            continue

    return converted, out_dir

def split_two_equal(files: List[str]) -> tuple[List[str], List[str]]:
    """Bagi dua sama rata (36→18+18, 40→20+20)."""
    half = len(files) // 2
    return files[:half], files[half:]

def to_zip(files: List[str]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, os.path.basename(f))
    buf.seek(0)
    return buf

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
    "`https://t.me/addstickers/leonardicaprio`"
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

    try:
        await status.edit_text("⬇️ Mengunduh stiker dari Telegram ...")
        files, folder = await download_pack(TOKEN, pack)

        if mode == "static":
            await status.edit_text("⚙️ Mengonversi gambar ke WEBP 512×512 ...")
            converted, _ = convert_static(folder)
        else:
            await status.edit_text("⚙️ Mengonversi animasi (TGS/WEBM) ke animated WEBP ...\n⏳ Mohon tunggu.")
            converted, _ = convert_animated(folder)

        if not converted:
            await status.edit_text("⚠️ Tidak ada file yang bisa dikonversi pada pack ini.")
            USER_MODE.pop(message.from_user.id, None)
            return

        await status.edit_text("📦 Menyiapkan ZIP ...")

        if len(converted) > 30:
            a, b = split_two_equal(converted)
            zip1 = to_zip(a)
            zip2 = to_zip(b)
            await status.edit_text("📦 Jumlah > 30 → dibagi menjadi *dua bagian sama rata*.")
            await message.answer_document(BufferedInputFile(zip1.read(), filename=f"{pack}_1.zip"))
            await message.answer_document(BufferedInputFile(zip2.read(), filename=f"{pack}_2.zip"))
        else:
            zip_buf = to_zip(converted)
            await status.edit_text("✅ Selesai! Mengirim ZIP ...")
            await message.answer_document(BufferedInputFile(zip_buf.read(), filename=f"{pack}.zip"))

        await message.answer("🎉 Beres! Extract ZIP lalu impor ke WhatsApp dengan *Personal Stickers for WhatsApp*.")
    except RuntimeError as e:
        await status.edit_text(f"❌ {e}")
    except Exception as e:
        await status.edit_text(f"❌ Terjadi kesalahan: {e}")
    finally:
        USER_MODE.pop(message.from_user.id, None)

# ============================================================
# RUN (v3 style)
# ============================================================
import asyncio

async def main():
    print("🤖 Bot konversi stiker Telegram → WhatsApp aktif.")
    dp.include_router(router)
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())         
