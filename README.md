# Bot Discord - Clear Pesan + Music Player dengan Optimasi Audio

## 📋 Penjelasan Singkat

Bot Discord yang bisa menghapus pesan dengan cepat **dan** memutar musik dari YouTube/Spotify dengan audio **mulus tanpa patah-patah** (suara robot).

---

## ⚙️ Cara Pakai

### 1. Install Dependencies
Buka terminal, masuk ke folder `botdiscord`, lalu jalan:
```bash
pip install -r requirements.txt
```

### 2. Pasang Token Discord
Buka file `.env`, ganti `token_discord_anda_sini` dengan token bot Discord Anda:
```
DISCORD_TOKEN=kode_token_anda_di_sini
```

> ⚠️ **JANGAN PERNAH SHARE TOKEN ini ke orang lain! Token = kunci bot.**

### 3. Jalankan Bot
```bash
python bot.py
```

---

## 🎮 Command yang Tersedia

### Clear Pesan
| Perintah | Deskripsi | Contoh |
|----------|-----------|--------|
| `!clear [jumlah]` | Hapus sejumlah pesan | `!clear 50` |
| `!clearuser @user [jumlah]` | Hapus pesan dari user tertentu | `!clearuser @spam 30` |

### Music Player
| Perintah | Deskripsi |
|----------|-----------|
| `!bj < judul / URL >` | Play lagu atau masukkan ke antrean |
| `!bjx` / `!bjs` | Skip lagu |
| `!bjq` / `!queue` | Lihat antrean lagu (dengan tombol kontrol) |
| `!bjpause` / `!bjz` | Pause lagu (z = tidur) |
| `!bjr` | Resume / Repeat lagu |
| `!bjls` | Loop lagu on/off |
| `!bjstop` | **Stop musik & kosongkan antrean** |
| `!bjprev` | **Putar lagu sebelumnya** |

### Optimasi Audio & Latency
| Perintah | Deskripsi |
|----------|-----------|
| `!bjbitrate [kbps]` | Atur bitrate audio (default: 192 kbps, range: 64-384) |
| `!bjregion [region]` | Pilih region voice Discord untuk latency optimal |

---

## 🎵 Kontrol Musik via Tombol (UI)

Setiap kali bot memutar lagu, akan muncul **embed dengan tombol interaktif**:

| Tombol | Fungsi |
|--------|--------|
| ⏮️ | Lagu sebelumnya |
| ⏹️ | Stop musik & keluar voice |
| ⏭️ | Skip ke lagu berikutnya |
| ⏸️ / ▶️ | Pause / Resume |
| 🔁 | Toggle loop lagu |

**Queue juga punya tombol:** ⏭️ Skip, ⏹️ Stop, ⏮️ Previous

---

## 📝 Catatan Penting

1. **Clear:** Bot butuh izin `Manage Messages`. Max 1000 pesan per command.
2. **Music:** Bot butuh izin `Connect` dan `Speak` di voice channel. Bot otomatis join voice channel tempat kamu berada.
3. **Audio Settings:** Optimized untuk suara **mulus**:
   - Bitrate: 192kbps (high quality)
   - Sample Rate: 48kHz
   - Opus FEC: Enabled (error correction)
   - Jitter Buffer: Optimized (latency rendah)
4. **History:** Bot otomatis menyimpan history lagu untuk fitur **Previous (⏮️ / !bjprev)**.
5. **Latency Optimization:** Sesuaikan bitrate dan region voice manual jika latency terasa tinggi:
   - `!bjbitrate 128` (untuk latency rendah)
   - `!bjregion singapore` (jika server di Asia)
6. **If failed:** Pastikan bot punya izin voice yang benar dan FFmpeg terinstall.

---

## 🔑 Cara Dapatkan Token Discord

1. Buka [Discord Developer Portal](https://discord.com/developers/applications)
2. Klik "New Application" → beri nama
3. Ke tab "Bot" → "Add Bot" → klik "Yes, do it!"
4. Klik "Copy" di kolom "Token"
5. Paste-kan ke file `.env` yang sudah disediakan

---

## 🔗 Invite Bot ke Server

1. Di Developer Portal, buka "OAuth2" → "URL Generator"
2. Pilih scope: `bot`
3. Pilih permissions: `Manage Messages`, `Connect`, `Speak`
4. Salin link URL, buka di browser, pilih server Anda, klik "Authorize"