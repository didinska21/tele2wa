# tele2wa
mengimport sticker telegram ke dalam whatsapp


# Telegram → WhatsApp Sticker Bot

Bot Telegram yang:
- Mengubah **stiker statis** (PNG/WEBP) ke **WEBP 512×512** siap WhatsApp.
- Mengubah **stiker animasi** (.TGS / .WEBM) ke **animated WEBP**.
- Otomatis **membagi 2** jika jumlah stiker > 30 (contoh 36 → 18+18, 40 → 20+20).
- Mengirim hasil sebagai **ZIP**.
- Menampilkan **log/progress** yang rapi.

## 1) Prasyarat

**Python 3.10/3.11** direkomendasikan.

### Linux/Ubuntu
```bash
sudo apt update
sudo apt install -y python3 python3-pip ffmpeg webp cmake build-essential git
# (animated .tgs butuh rlottie-convert)
git clone https://github.com/Samsung/rlottie.git
cd rlottie && mkdir build && cd build
cmake ..
make -j4
sudo make install
