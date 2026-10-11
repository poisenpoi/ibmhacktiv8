# Asisten Pesanan UMKM

AI agent untuk merapikan pesanan dari chat atau catatan menjadi draft yang siap dikonfirmasi. Setelah disetujui, pesanan disimpan ke Google Docs dan dijadwalkan di Google Calendar.

Dibangun menggunakan IBM Bob, Langflow, Python, dan Telegram Bot API.

## Masalah yang diselesaikan

Pesanan sering tersebar di media sosial dan catatan terpisah. Nama pelanggan, jumlah barang, serta waktu pengiriman mudah tertukar atau terlewat.

Asisten Pesanan UMKM membantu pemilik usaha mencatat pesanan dengan alur:

**Ketik pesanan → cek draft → konfirmasi → siapkan barang.**

## Fitur

- Ekstraksi nama pelanggan, barang, jumlah, satuan, jatuh tempo, dan catatan melalui Langflow.
- Klarifikasi untuk informasi yang kurang atau ambigu.
- Koreksi draft melalui percakapan.
- Parsing waktu Indonesia dengan zona waktu Asia/Jakarta.
- Penyimpanan ringkasan ke Google Docs.
- Pengingat Calendar 1 hari dan 1 jam sebelum jatuh tempo.
- Sesi Telegram persisten per pengguna dan chat.
- Pemulihan link pesanan melalui `/terakhir`.
- Retry penyimpanan menggunakan identitas pesanan yang sama.
- Mode simulasi dan mode live.


IBM Bob menggunakan MCP untuk menjalankan tool pesanan. Bot Telegram memanggil handler backend yang sama. Langflow menangani ekstraksi AI, sedangkan backend memvalidasi data dan mengatur penyimpanan.

## Cara kerja

1. Pengguna mengirim teks pesanan.
2. Backend mengirim teks dan konteks waktu ke Langflow.
3. Hasil ekstraksi diubah menjadi objek pesanan dan divalidasi.
4. Pengguna menerima draft atau pertanyaan klarifikasi.
5. Jawaban klarifikasi memperbarui draft aktif beserta hash-nya.
6. Pengguna mengetik `YA` setelah memeriksa draft.
7. Backend memvalidasi konfirmasi, hash, dan kelengkapan data.
8. Hasil penyimpanan beserta link Docs dan Calendar dikirim ke pengguna.

SQLite menyimpan draft, sesi, dan progres penyimpanan per tahap agar proses dapat dilanjutkan setelah restart atau kegagalan sebagian.

## Persiapan

- Python 3.11 atau lebih baru.
- IBM Bob.
- Langflow dengan model Gemini yang sudah dikonfigurasi.
- Akun Google untuk Docs dan Calendar.
- Bot Telegram dari BotFather.

### Instalasi

powershell
git clone https://github.com/poisenpoi/ibmhacktiv8.git
cd ibmhacktiv8

python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

New-Item -ItemType Directory -Force .secrets
Copy-Item .bob/mcp.example.json .bob/mcp.json

## Konfigurasi Langflow

Buat flow ekstraksi pesanan dengan susunan:

Chat Input → Prompt Template → Gemini → Chat Output

Instruksikan model untuk menghasilkan JSON dengan field berikut:

| Field | Isi |
|---|---|
| `nama_pelanggan` | Nama pelanggan |
| `items` | Array berisi `nama`, `jumlah`, `satuan`, dan `catatan_item` |
| `jatuh_tempo_raw` | Teks waktu dari pengguna |
| `jatuh_tempo_dt` | Waktu ISO 8601 dengan offset zona waktu |
| `catatan` | Catatan tambahan |
| `ambiguitas` | Array pertanyaan klarifikasi |

Prompt perlu meminta model mempertahankan data draft saat menerima koreksi, menggunakan konteks waktu dari backend, dan meminta klarifikasi jika informasi belum jelas.

Simpan API key Langflow di:

.secrets/langflow_api_key.txt

Isi konfigurasi `.bob/mcp.json` sesuai komputer yang digunakan:

| Pengaturan | Nilai |
|---|---|
| `command` | Path absolut `.venv/Scripts/python.exe` |
| `cwd` | Path absolut folder proyek |
| `LANGFLOW_URL` | URL Langflow, biasanya `http://127.0.0.1:7860` |
| `UMKM_FLOW_ID` | ID flow ekstraksi pesanan |
| `LANGFLOW_API_KEY_FILE` | Path absolut file API key Langflow |
| `UMKM_GOOGLE_TOKEN` | Path absolut `.secrets/token.json` |
| `UMKM_GOOGLE_LIVE` | `0` untuk simulasi, `1` untuk live |
| `PYTHONIOENCODING` | `utf-8` |

## Konfigurasi Google

1. Buat proyek Google Cloud.
2. Aktifkan Google Docs API dan Google Calendar API.
3. Konfigurasikan OAuth consent screen dan tambahkan akun pengujian.
4. Buat OAuth client bertipe Desktop app.
5. Simpan JSON client sebagai `.secrets/credentials.json`.
6. Jalankan:

& ".\.venv\Scripts\python.exe" scripts/setup_google_auth.py

Tekan Enter, login dengan akun tujuan, lalu izinkan akses yang diminta.

Untuk penyimpanan sungguhan, gunakan `UMKM_GOOGLE_LIVE=1` di `.bob/mcp.json`.

Aplikasi OAuth External berstatus Testing dengan scope ini memiliki masa berlaku refresh token 7 hari. Login ulang dapat dilakukan melalui script setup yang sama. [Referensi Google](https://developers.google.com/identity/protocols/oauth2#expiration)

## Menjalankan melalui IBM Bob

Buka folder proyek di IBM Bob dan muat konfigurasi MCP `.bob/mcp.json`.

Tool yang tersedia:

| Tool | Fungsi |
|---|---|
| `process_order` | Ekstraksi teks dan pembuatan draft |
| `update_order` | Memperbarui field draft |
| `confirm_commit` | Menyimpan draft yang dikonfirmasi |
| `check_status` | Memeriksa koneksi dan konfigurasi |

Kirim pesanan melalui chat Bob. Periksa draft yang ditampilkan, jawab klarifikasi, lalu ketik `YA` untuk menyimpan.

## Menjalankan bot Telegram

Simpan token BotFather di:

.secrets/telegram_bot_token.txt


Jalankan bot, kirim `/id`, lalu masukkan user ID yang diizinkan ke:

.secrets/telegram_allowed_user_ids.txt

Format file: satu user ID per baris.

Buka Langflow, lalu jalankan dari folder proyek:

$config = Get-Content .bob/mcp.json -Raw | ConvertFrom-Json

$config.mcpServers.'umkm-order-assistant'.env.PSObject.Properties |
    ForEach-Object {
        [Environment]::SetEnvironmentVariable(
            $_.Name, [string]$_.Value, "Process"
        )
    }

& ".\.venv\Scripts\python.exe" scripts/telegram_bot.py

Biarkan terminal terbuka selama bot digunakan. Tekan `Ctrl+C` untuk menghentikannya.

Setelah perubahan konfigurasi atau kode, jalankan ulang bot.

### Perintah Telegram

| Perintah | Fungsi |
|---|---|
| `/start` | Menampilkan bantuan |
| `/id` | Menampilkan user ID dan chat ID |
| `/status` | Menampilkan koneksi Langflow dan konfigurasi sistem |
| `/terakhir` | Mengambil link hasil pesanan terakhir |
| `YA` / `KONFIRMASI` | Mengonfirmasi draft |
| `BATAL` / `TIDAK` | Membatalkan draft aktif |

## Contoh penggunaan

Nama pelanggan: Bu Siti
Pesanan:
- 2 kg ayam goreng
- 10 porsi nasi kuning
Jatuh tempo: besok jam 14:00 WIB
Catatan: sambal dipisah

Bot akan menampilkan draft. Pengguna dapat mengoreksi:

Ayamnya jadi 3 kg

Setelah draft terbaru sesuai:

YA

## Output

### Google Docs

Dokumen berisi identitas pesanan, nama pelanggan, daftar barang, jumlah, satuan, jatuh tempo, dan catatan.

Format judul:

Pesanan-UMKM-{nama pelanggan}-{tanggal}

### Google Calendar

Event berjudul:

[UMKM] Pesanan {nama pelanggan}

Event dimulai pada jatuh tempo, berdurasi 1 jam, dan memiliki pengingat popup 1 hari serta 1 jam sebelumnya.

### Balasan pengguna

Pengguna menerima status penyimpanan dan link Docs serta Calendar. Jika penyimpanan gagal sebagian, bot menampilkan hasil tiap layanan dan menyediakan retry lewat `YA`.

## Pengujian

Jalankan dari folder proyek:

powershell
$env:PYTHONIOENCODING = "utf-8"

& ".\.venv\Scripts\python.exe" scripts/test_agent.py
& ".\.venv\Scripts\python.exe" scripts/test_telegram_bot.py
& ".\.venv\Scripts\python.exe" scripts/test_telegram_regression.py
