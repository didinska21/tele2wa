import os
import re
import io
import math
import zipfile
import asyncio
import aiohttp
import subprocess
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from PIL import Image

# ============================================================
# CONFIG
# ============================================================
TOKEN = "ISI_TOKEN_BOT_KAMU"  # ganti dengan token bot Telegram kamu
BASE_DIR = "stickers"
os.makedirs(BASE_DIR, exist_ok=True)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def extract_pack_name(link: str) -> str:
    """Ambil nama pack dari link Telegram"""
    match = re.search(r"addstickers/([A-Za-z0-9_]+)", link)
    if not match:
        raise ValueError("❌ Link tidak valid. Gunakan format seperti:\nhttps://t.me/addstickers/namapack")
    return match.group(1)

async def download_stickers(bot_token: str, pack_name: str):
    """Ambil daftar stiker dari pack Telegram"""
    async with aiohttp.ClientSession() as session:
        url = f"https://api.telegram.org/bot{bot_token}/getStickerSet?name={pack_name}"
        async with session.get(url) as resp:
            data = await resp.json()
            if not data.get("ok"):
                raise ValueError("Gagal ambil pack: " + str(data))
            result = data["result"]

        stickers = result["stickers"]
        folder = os.path.join(BASE_DIR, pack_name)
        os.makedirs(folder, exist_ok=True)

        file_list = []
        for s in stickers:
            if s.get("is_animated") or s.get("is_video"):
                # stiker animasi atau video
                fmt = "tgs" if s.get("is_animated") else "webm"
            else:
                fmt = "png"

            file_id = s["file_id"]
            async with session.get(f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}") as r2:
                file_info = await r2.json()
                file_path = file_info["result"]["file_path"]

            file_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
            async with session.get(file_url) as r3:
                data = await r3.read()

            out_path = os.path.join(folder, f"{len(file_list):03d}.{fmt}")
            with open(out_path, "wb") as f:
                f.write(data)

            file_list.append(out_path)

        return file_list, folder


def convert_static_stickers(folder: str):
    """Konversi PNG/WEBP biasa ke format WhatsApp (WEBP 512x512)"""
    out_dir = os.path.join(folder, "converted_static")
    os.makedirs(out_dir, exist_ok=True)

    converted = []
    for file in sorted(os.listdir(folder)):
        if not (file.endswith(".png") or file.endswith(".webp")):
            continue
        img = Image.open(os.path.join(folder, file)).convert("RGBA")
        img.thumbnail((512, 512))
        out_path = os.path.join(out_dir, os.path.splitext(file)[0] + ".webp")
        img.save(out_path, "WEBP", quality=90)
        converted.append(out_path)
    return converted, out_dir


def convert_animated_stickers(folder: str):
    """Konversi animasi .tgs atau .webm ke animated .webp (butuh ffmpeg + rlottie-convert)"""
    out_dir = os.path.join(folder, "converted_anim")
    os.makedirs(out_dir, exist_ok=True)

    converted = []
    for file in sorted(os.listdir(folder)):
        path = os.path.join(folder, file)
        name, ext = os.path.splitext(file)
        out_path = os.path.join(out_dir, f"{name}.webp")

        try:
            if ext == ".tgs":
                # Konversi TGS -> PNG frames -> animated webp
                # Butuh tool eksternal rlottie-convert (install via apt atau build sendiri)
                frames_dir = os.path.join(out_dir, f"{name}_frames")
                os.makedirs(frames_dir, exist_ok=True)

                subprocess.run(["rlottie-convert", path, os.path.join(frames_dir, "%03d.png")], check=True)
                subprocess.run([
                    "img2webp",
                    "-loop", "0",
                    "-lossy",
                    "-q", "80",
                    "-o", out_path,
                    *sorted([os.path.join(frames_dir, f) for f in os.listdir(frames_dir)])
                ], check=True)

            elif ext == ".webm":
                # Konversi WEBM ke animated WEBP via ffmpeg
                subprocess.run([
                    "ffmpeg", "-y", "-i", path,
                    "-vf", "fps=15,scale=512:512:force_original_aspect_ratio=decrease,pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x00000000",
                    "-loop", "0", out_path
                ], check=True)

            converted.append(out_path)

        except Exception as e:
            print("Gagal konversi animasi:", e)
            continue

    return converted, out_dir


def make_zip(files: list, name: str):
    """Buat ZIP buffer"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, os.path.basename(f))
    buf.seek(0)
    return buf

# ============================================================
# COMMAND HANDLERS
# ============================================================

@dp.message(Command("start"))
async def start(message: types.Message):
    text = (
        "👋 *Selamat datang di Bot Konversi Stiker Telegram ke WhatsApp!*\n\n"
        "Saya bisa mengubah semua stiker Telegram menjadi format WhatsApp.\n\n"
        "🧩 Perintah yang tersedia:\n"
        "• `/stikerbiasa` — untuk stiker statis (gambar)\n"
        "• `/stikeranimasi` — untuk stiker bergerak (animasi/video)\n\n"
        "Ketik perintah di atas untuk mulai!"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ------------------------------------------------------------
# STIKER BIASA
# ------------------------------------------------------------
@dp.message(Command("stikerbiasa"))
async def stiker_biasa_cmd(message: types.Message):
    await message.answer("🖼 Kirim link pack stiker Telegram yang ingin kamu ubah jadi WhatsApp (stiker biasa).")

    @dp.message()
    async def get_link_biasa(msg: types.Message):
        try:
            pack_name = extract_pack_name(msg.text.strip())
        except Exception as e:
            await msg.reply(str(e))
            return

        loading = await msg.reply("🔍 Mengecek dan mengunduh stiker...")
        try:
            files, folder = await download_stickers(TOKEN, pack_name)
            await loading.edit_text("⚙️ Mengonversi ke format WhatsApp...")
            converted, _ = convert_static_stickers(folder)

            total = len(converted)
            if total > 30:
                half = math.ceil(total / 2)
                zip1 = make_zip(converted[:half], f"{pack_name}_1.zip")
                zip2 = make_zip(converted[half:], f"{pack_name}_2.zip")
                await loading.edit_text("📦 Stiker lebih dari 30, dibagi menjadi 2 ZIP...")
                await msg.answer_document(BufferedInputFile(zip1.read(), filename=f"{pack_name}_1.zip"))
                await msg.answer_document(BufferedInputFile(zip2.read(), filename=f"{pack_name}_2.zip"))
            else:
                zipbuf = make_zip(converted, f"{pack_name}.zip")
                await msg.answer_document(BufferedInputFile(zipbuf.read(), filename=f"{pack_name}.zip"))

            await msg.answer("✅ Selesai! Extract ZIP lalu impor ke WhatsApp menggunakan *Personal Stickers for WhatsApp*.")

        except Exception as e:
            await loading.edit_text(f"❌ Gagal memproses: {e}")

# ------------------------------------------------------------
# STIKER ANIMASI
# ------------------------------------------------------------
@dp.message(Command("stikeranimasi"))
async def stiker_animasi_cmd(message: types.Message):
    await message.answer("🎞 Kirim link pack stiker Telegram (animasi/video) untuk dikonversi ke WhatsApp.")

    @dp.message()
    async def get_link_anim(msg: types.Message):
        try:
            pack_name = extract_pack_name(msg.text.strip())
        except Exception as e:
            await msg.reply(str(e))
            return

        loading = await msg.reply("🔄 Mengunduh file animasi...")
        try:
            files, folder = await download_stickers(TOKEN, pack_name)
            await loading.edit_text("⚙️ Mengonversi ke animasi WhatsApp (animated WEBP)...\n⏳ Ini bisa agak lama...")

            converted, _ = convert_animated_stickers(folder)
            if not converted:
                await loading.edit_text("❌ Tidak ada stiker animasi yang bisa dikonversi.")
                return

            total = len(converted)
            if total > 30:
                half = math.ceil(total / 2)
                zip1 = make_zip(converted[:half], f"{pack_name}_anim_1.zip")
                zip2 = make_zip(converted[half:], f"{pack_name}_anim_2.zip")
                await loading.edit_text("📦 Stiker animasi >30, dibagi jadi dua ZIP...")
                await msg.answer_document(BufferedInputFile(zip1.read(), filename=f"{pack_name}_anim_1.zip"))
                await msg.answer_document(BufferedInputFile(zip2.read(), filename=f"{pack_name}_anim_2.zip"))
            else:
                zipbuf = make_zip(converted, f"{pack_name}_anim.zip")
                await msg.answer_document(BufferedInputFile(zipbuf.read(), filename=f"{pack_name}_anim.zip"))

            await msg.answer("✅ Selesai! Extract ZIP dan impor ke WhatsApp. 🎉")

        except Exception as e:
            await loading.edit_text(f"❌ Terjadi kesalahan: {e}")

# ============================================================
# RUN BOT
# ============================================================
if __name__ == "__main__":
    print("🤖 Bot konversi stiker Telegram → WhatsApp aktif...")
    import asyncio
    from aiogram import executor
    executor.start_polling(dp, skip_updates=True)
