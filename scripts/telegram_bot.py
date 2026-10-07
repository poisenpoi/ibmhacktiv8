"""
scripts/telegram_bot.py
Telegram Bot — Asisten Pesanan UMKM
Long-polling menggunakan standard library Python (urllib + json).

Setup:
  1. Isi token di .secrets/telegram_bot_token.txt
  2. Isi Telegram user ID yang diizinkan di .secrets/telegram_allowed_user_ids.txt
     (satu ID per baris; file kosong berarti akses ditolak untuk semua)
  3. Jalankan: python scripts/telegram_bot.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

# ── Paths ─────────────────────────────────────────────────────────────────────

_HERE = Path(__file__).parent
_ROOT = _HERE.parent

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TOKEN_FILE        = _ROOT / ".secrets" / "telegram_bot_token.txt"
_ALLOWED_IDS_FILE  = _ROOT / ".secrets" / "telegram_allowed_user_ids.txt"

TZ_JKT = ZoneInfo("Asia/Jakarta")

# ── Token & ACL loading ───────────────────────────────────────────────────────

def _load_token() -> str:
    if not _TOKEN_FILE.exists():
        sys.exit(f"[ERROR] Token file tidak ditemukan: {_TOKEN_FILE}")
    token = _TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        sys.exit(f"[ERROR] Token kosong di {_TOKEN_FILE}")
    return token


def _load_allowed_ids() -> Set[int]:
    """Return set of allowed user IDs.

    - File tidak ada → set kosong (akses ditolak semua).
    - File ada tapi kosong / hanya komentar → set kosong (akses ditolak semua).
    """
    if not _ALLOWED_IDS_FILE.exists():
        return set()
    lines = _ALLOWED_IDS_FILE.read_text(encoding="utf-8").splitlines()
    ids: Set[int] = set()
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                ids.add(int(line))
            except ValueError:
                print(f"[WARN] Baris tidak valid di allowed_ids: {line!r}", flush=True)
    return ids


# ── Telegram HTTP helpers ─────────────────────────────────────────────────────

class TelegramError(Exception):
    pass


def _api(token: str, method: str, payload: Optional[Dict[str, Any]] = None, request_timeout: int = 30) -> Any:
    url  = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req  = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=request_timeout) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
    if not body.get("ok"):
        raise TelegramError(body.get("description", "Unknown Telegram error"))
    return body.get("result")


def send_message(token: str, chat_id: int, text: str) -> None:
    _api(token, "sendMessage", {"chat_id": chat_id, "text": text})


def get_updates(token: str, offset: int, timeout: int = 30) -> List[Dict[str, Any]]:
    return _api(token, "getUpdates", {"offset": offset, "timeout": timeout}, request_timeout=timeout + 15) or []


# ── Session journal (SQLite, persisted per chat_id + user_id) ─────────────────
#
# Schema:
#   chat_id          INTEGER  – Telegram chat ID
#   user_id          INTEGER  – Telegram user ID
#   order_id         TEXT     – from process_order / confirm_commit
#   draft_hash       TEXT     – hash dari draft terakhir yang ditampilkan ke user
#   needs_clarification INTEGER – 1 jika masih menunggu jawaban klarifikasi
#   updated_at       TEXT     – ISO timestamp terakhir diperbarui
#
# Primary key: (chat_id, user_id)

def _session_db_path() -> Path:
    """Lokasi DB sesi Telegram — bisa di-override via UMKM_JOURNAL_DB."""
    db_env = os.environ.get("UMKM_JOURNAL_DB")
    if db_env:
        return Path(db_env)
    return _ROOT / ".journal" / "orders.db"


def _get_session_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    p = db_path or _session_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_session_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_sessions (
            chat_id             INTEGER NOT NULL,
            user_id             INTEGER NOT NULL,
            order_id            TEXT,
            draft_hash          TEXT,
            needs_clarification INTEGER NOT NULL DEFAULT 0,
            updated_at          TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    conn.commit()


def session_save(
    chat_id: int,
    user_id: int,
    order_id: str,
    draft_hash: str,
    needs_clarification: bool,
    db_path: Optional[Path] = None,
) -> None:
    """Simpan atau perbarui sesi aktif untuk pasangan (chat_id, user_id)."""
    conn = _get_session_conn(db_path)
    _init_session_table(conn)
    now_s = datetime.now(tz=TZ_JKT).isoformat()
    conn.execute("""
        INSERT INTO telegram_sessions (chat_id, user_id, order_id, draft_hash, needs_clarification, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            order_id            = excluded.order_id,
            draft_hash          = excluded.draft_hash,
            needs_clarification = excluded.needs_clarification,
            updated_at          = excluded.updated_at
    """, (chat_id, user_id, order_id, draft_hash, 1 if needs_clarification else 0, now_s))
    conn.commit()
    conn.close()


def session_get(
    chat_id: int,
    user_id: int,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Ambil sesi aktif. Return None jika belum ada."""
    conn = _get_session_conn(db_path)
    _init_session_table(conn)
    row = conn.execute(
        "SELECT order_id, draft_hash, needs_clarification FROM telegram_sessions "
        "WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "order_id":            row["order_id"],
        "draft_hash":          row["draft_hash"],
        "needs_clarification": bool(row["needs_clarification"]),
    }


def session_clear(
    chat_id: int,
    user_id: int,
    db_path: Optional[Path] = None,
) -> None:
    """Hapus sesi aktif (setelah konfirmasi atau pembatalan)."""
    conn = _get_session_conn(db_path)
    _init_session_table(conn)
    conn.execute(
        "DELETE FROM telegram_sessions WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    conn.commit()
    conn.close()


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_start(token: str, chat_id: int, user_id: int, first_name: str) -> None:
    text = (
        f"Halo, {first_name}! 👋\n"
        "Saya Asisten Pesanan UMKM.\n\n"
        "Perintah tersedia:\n"
        "  /start  — sambutan ini\n"
        "  /id     — tampilkan user ID & chat ID kamu\n"
        "  /status — cek status sistem (Langflow & Google)"
    )
    send_message(token, chat_id, text)


def cmd_id(token: str, chat_id: int, user_id: int) -> None:
    text = f"User ID  : {user_id}\nChat ID  : {chat_id}"
    send_message(token, chat_id, text)


def cmd_status(
    token: str,
    chat_id: int,
    user_id: int,
    allowed_ids: Set[int],
) -> None:
    if user_id not in allowed_ids:
        send_message(token, chat_id, "⛔ Akses ditolak. User ID kamu tidak ada dalam daftar izin.")
        return

    from umkm_agent.mcp_server import handle_check_status

    try:
        result = handle_check_status({})
    except Exception as exc:  # noqa: BLE001
        send_message(token, chat_id, f"❌ Gagal memeriksa status:\n{exc}")
        return

    lf_info  = result.get("langflow", {})
    goog     = result.get("google", {})
    blockers = result.get("blocker", [])

    lf_icon  = "✅" if lf_info.get("reachable") else "❌"
    goog_mode = goog.get("mode", "unknown")
    goog_icon = "✅" if goog_mode == "live" else "⚠️"

    lines = [
        "📊 Status Sistem",
        "",
        f"{lf_icon} Langflow",
        f"   URL     : {lf_info.get('url', '-')}",
        f"   Versi   : {lf_info.get('version') or '-'}",
        f"   Flow ID : {'✅ dikonfigurasi' if lf_info.get('flow_configured') else '❌ belum diatur'}",
        f"   API Key : {'✅ dikonfigurasi' if bool(lf_info.get('api_key_set') or lf_info.get('api_key_file_set')) else '❌ belum dikonfigurasi'}",
        "",
        f"{goog_icon} Google",
        f"   Mode    : {goog_mode}",
    ]

    if blockers:
        lines += ["", "🚧 Blocker:"]
        for b in blockers:
            lines.append(f"   • {b}")

    send_message(token, chat_id, "\n".join(lines))


# ── Order handlers ────────────────────────────────────────────────────────────

def handle_process_order(
    token: str,
    chat_id: int,
    user_id: int,
    text: str,
    db_path: Optional[Path] = None,
) -> None:
    """Proses teks pesanan baru dan simpan sesi."""
    from umkm_agent.mcp_server import handle_process_order as mcp_process_order

    try:
        result = mcp_process_order({"teks": text})
    except Exception as exc:  # noqa: BLE001
        send_message(
            token,
            chat_id,
            f"❌ Terjadi kesalahan saat memproses pesanan:\n{exc}\n\nCoba lagi atau hubungi admin.",
        )
        return

    if not result.get("success"):
        error_msg = result.get("error", "Kesalahan tidak diketahui.")
        if not result.get("langflow_available", True):
            send_message(
                token,
                chat_id,
                (
                    "❌ Langflow tidak tersedia.\n\n"
                    f"Detail: {error_msg}\n\n"
                    "Pastikan Langflow Desktop sudah berjalan dan UMKM_FLOW_ID sudah dikonfigurasi."
                ),
            )
        else:
            send_message(token, chat_id, f"❌ Gagal memproses pesanan:\n{error_msg}")
        return

    order_id   = result["order_id"]
    draft_hash = result["draft_hash"]
    needs_clar = bool(result.get("needs_clarification"))

    # Simpan sesi persisten
    session_save(chat_id, user_id, order_id, draft_hash, needs_clar, db_path)

    # Susun satu pesan: draft + prompt tindakan
    draft_text = result.get("draft", "")
    lines: List[str] = [draft_text] if draft_text else []

    if needs_clar:
        ambiguitas = result.get("ambiguitas") or []
        if ambiguitas:
            lines.append("\n⚠️ Perlu klarifikasi:")
            for item in ambiguitas:
                lines.append(f"  • {item}")
            lines.append("\nJawab pertanyaan di atas, lalu ketik YA untuk menyimpan.")
        else:
            lines.append("\n⚠️ Ada hal yang perlu diperjelas. Mohon lengkapi informasi pesanan.")
    else:
        lines.append("\nKonfirmasi simpan pesanan ini? (YA / TIDAK)")

    send_message(token, chat_id, "\n".join(lines))


def handle_confirm_commit(
    token: str,
    chat_id: int,
    user_id: int,
    db_path: Optional[Path] = None,
) -> None:
    """
    Konfirmasi penyimpanan draft aktif.
    Mengambil sesi yang tersimpan lalu memanggil handle_confirm_commit
    dengan order_id, draft_hash, dan confirmed=True.
    """
    from umkm_agent.mcp_server import handle_confirm_commit as mcp_confirm

    sess = session_get(chat_id, user_id, db_path)
    if not sess or not sess.get("order_id"):
        send_message(token, chat_id, "⚠️ Tidak ada pesanan aktif. Kirim teks pesanan terlebih dahulu.")
        return

    order_id   = sess["order_id"]
    draft_hash = sess["draft_hash"]

    # Konfirmasi berlaku untuk draft lengkap; draft ambigu diminta klarifikasi dahulu
    if sess.get("needs_clarification"):
        send_message(
            token,
            chat_id,
            "⚠️ Pesanan ini masih membutuhkan klarifikasi.\n"
            "Jawab pertanyaan yang ada, lalu ketik YA untuk menyimpan.",
        )
        return

    try:
        result = mcp_confirm({
            "order_id":   order_id,
            "draft_hash": draft_hash,
            "confirmed":  True,
        })
    except Exception as exc:  # noqa: BLE001
        send_message(token, chat_id, f"❌ Terjadi kesalahan saat menyimpan:\n{exc}")
        return

    if not result.get("success"):
        error_msg = result.get("error", "Kesalahan tidak diketahui.")
        send_message(token, chat_id, f"❌ Gagal menyimpan pesanan:\n{error_msg}")
        return

    # Commit berhasil — hapus sesi sehingga YA berikutnya tidak salah referensi
    session_clear(chat_id, user_id, db_path)

    status = result.get("status", "")
    doc_url  = result.get("google_doc_url") or result.get("doc_url") or result.get("dry_doc_url") or "-"
    evt_url  = result.get("google_event_url") or result.get("event_url") or result.get("dry_event_url") or "-"

    if status == "already_committed":
        send_message(token, chat_id, "✅ Pesanan ini sudah tersimpan sebelumnya (tidak disimpan ulang).")
        return

    dry_note = " (dry-run — Google belum dikonfigurasi)" if result.get("dry_run") else ""
    lines = [
        f"✅ Pesanan berhasil disimpan{dry_note}.",
        "",
        f"📄 Dokumen : {doc_url}",
        f"📅 Kalender: {evt_url}",
    ]

    errors = result.get("errors") or {}
    if errors:
        lines.append("\n⚠️ Sebagian langkah gagal (bisa coba ulang):")
        for step, msg in errors.items():
            lines.append(f"  • {step}: {msg}")
        # Sesi tidak dihapus agar user bisa retry
        session_save(chat_id, user_id, order_id, draft_hash, False, db_path)

    send_message(token, chat_id, "\n".join(lines))


def handle_cancel(
    token: str,
    chat_id: int,
    user_id: int,
    db_path: Optional[Path] = None,
) -> None:
    """Batalkan draft aktif dan hapus sesi."""
    sess = session_get(chat_id, user_id, db_path)
    if not sess or not sess.get("order_id"):
        send_message(token, chat_id, "⚠️ Tidak ada pesanan aktif untuk dibatalkan.")
        return
    session_clear(chat_id, user_id, db_path)
    send_message(token, chat_id, "🗑️ Pesanan dibatalkan. Kirim teks pesanan baru kapan saja.")


# ── Update dispatcher ─────────────────────────────────────────────────────────

_CONFIRM_KEYWORDS = {"ya", "konfirmasi", "setuju", "simpan", "ok", "fix"}
_CANCEL_KEYWORDS  = {"batal", "tidak", "cancel", "no"}


def dispatch(
    token: str,
    update: Dict[str, Any],
    allowed_ids: Set[int],
    db_path: Optional[Path] = None,
) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    text      = (message.get("text") or "").strip()
    chat_id   = message["chat"]["id"]
    chat_type = message["chat"].get("type", "")
    user_info = message.get("from", {})
    user_id   = user_info.get("id", 0)
    first_name = user_info.get("first_name", "")

    # Strip bot-name suffix, e.g. /start@MyBot → /start
    command = text.split()[0].split("@")[0] if text.startswith("/") else ""

    if command == "/start":
        cmd_start(token, chat_id, user_id, first_name)
        return
    if command == "/id":
        cmd_id(token, chat_id, user_id)
        return
    if command == "/status":
        cmd_status(token, chat_id, user_id, allowed_ids)
        return

    # Plain-text in private chat only
    if command or chat_type != "private" or not text:
        return

    # Access check
    if user_id not in allowed_ids:
        send_message(
            token,
            chat_id,
            "⛔ Akses ditolak. User ID kamu tidak ada dalam daftar izin.\n"
            f"ID kamu: {user_id}",
        )
        return

    # ── Routing ───────────────────────────────────────────────────────────────
    normalized = text.lower().strip()

    if normalized in _CONFIRM_KEYWORDS:
        handle_confirm_commit(token, chat_id, user_id, db_path)
    elif normalized in _CANCEL_KEYWORDS:
        handle_cancel(token, chat_id, user_id, db_path)
    else:
        handle_process_order(token, chat_id, user_id, text, db_path)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    token      = _load_token()
    allowed_ids = _load_allowed_ids()

    try:
        me = _api(token, "getMe")
    except TelegramError as exc:
        sys.exit(f"[ERROR] Token tidak valid atau Telegram tidak terjangkau: {exc}")

    bot_name = me.get("username", "?")
    acl_info = (
        f"{len(allowed_ids)} user ID terdaftar"
        if allowed_ids
        else "DITOLAK SEMUA (daftar kosong)"
    )
    print(f"[INFO] Bot @{bot_name} aktif. Akses /status: {acl_info}", flush=True)
    print("[INFO] Tekan Ctrl+C untuk berhenti.", flush=True)

    offset = 0
    while True:
        try:
            updates = get_updates(token, offset=offset, timeout=30)
        except TelegramError as exc:
            print(f"[WARN] getUpdates error: {exc} — retry dalam 5 detik", flush=True)
            time.sleep(5)
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Koneksi error: {exc} — retry dalam 5 detik", flush=True)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                dispatch(token, update, allowed_ids)
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] dispatch gagal (update_id={update.get('update_id')}): {exc}", flush=True)


if __name__ == "__main__":
    main()
