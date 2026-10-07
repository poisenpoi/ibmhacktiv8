"""
scripts/test_telegram_bot.py
Unit-test untuk alur konfirmasi Telegram di scripts/telegram_bot.py.

Pengujian alur menggunakan mock.

Skenario yang diuji:
  TB01 — pesanan → draft → YA → commit
  TB02 — restart (sesi persisten): simpan sesi, buat ulang koneksi, YA tetap commit
  TB03 — isolasi antar-user: sesi user A tidak bocor ke user B
  TB04 — draft ambigu → YA ditolak (diminta klarifikasi dahulu)
  TB05 — retry commit partial error: sesi dipertahankan, YA ulang → retry commit
  TB06 — BATAL/TIDAK → batalkan draft aktif
  TB07 — YA tanpa sesi aktif → pesan "tidak ada pesanan aktif"
  TB08 — routing: teks non-keyword → handle_process_order
  TB09 — session_save / session_get / session_clear idempoten

Jalankan:
  python scripts/test_telegram_bot.py
  python scripts/test_telegram_bot.py --verbose
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

TZ_JKT = ZoneInfo("Asia/Jakarta")
VERBOSE = "--verbose" in sys.argv

# DB test terpisah
_TEST_DB = Path(tempfile.mktemp(suffix=".db", prefix="umkm_tgbot_test_"))
os.environ["UMKM_JOURNAL_DB"] = str(_TEST_DB)
os.environ["UMKM_GOOGLE_LIVE"] = "0"

# Import setelah env diset
from scripts.telegram_bot import (
    dispatch,
    handle_confirm_commit,
    handle_cancel,
    handle_process_order,
    session_clear,
    session_get,
    session_save,
    _CONFIRM_KEYWORDS,
    _CANCEL_KEYWORDS,
)

# ── Test helpers ──────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0
ERRORS: List[str] = []


def ok(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        if VERBOSE:
            print(f"  ✅ {name}")
    else:
        FAIL += 1
        msg = f"  ❌ {name}" + (f": {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


def section(title: str) -> None:
    print(f"\n{'─'*58}")
    print(f"  {title}")
    print(f"{'─'*58}")


# ── Fake helpers ──────────────────────────────────────────────────────────────

TOKEN = "FAKE_TOKEN"

# Collect sent messages per chat_id for assertions
_sent: Dict[int, List[str]] = {}


def _fake_send(token: str, chat_id: int, text: str) -> None:
    _sent.setdefault(chat_id, []).append(text)


def _last(chat_id: int) -> str:
    return (_sent.get(chat_id) or [""])[-1]


def _clear_sent() -> None:
    _sent.clear()


def _make_update(
    chat_id: int,
    user_id: int,
    text: str,
    chat_type: str = "private",
    update_id: int = 1,
) -> Dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "first_name": "Tester"},
            "text": text,
        },
    }


def _make_process_result(
    order_id: str,
    draft_hash: str,
    needs_clarification: bool = False,
    ambiguitas: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "success": True,
        "order_id": order_id,
        "draft_hash": draft_hash,
        "revision": 0,
        "status": "needs_clarification" if needs_clarification else "draft",
        "needs_clarification": needs_clarification,
        "ambiguitas": ambiguitas or [],
        "draft": f"DRAFT PESANAN\nOrder ID: {order_id}\n",
        "langflow_available": True,
    }


def _make_confirm_result(
    success: bool = True,
    status: str = "simulated",
    dry_run: bool = True,
    errors: Optional[Dict[str, str]] = None,
    error: str = "",
) -> Dict[str, Any]:
    if not success:
        return {"success": False, "error": error or "Gagal commit"}
    return {
        "success": True,
        "status": status,
        "dry_run": dry_run,
        "google_doc_url":   "DRY://docs/TEST",
        "google_event_url": "DRY://calendar/TEST",
        "errors": errors or {},
    }


ALLOWED_IDS = {111, 222}

# ════════════════════════════════════════════════════════════════════════════
# TB01 — pesanan → draft → YA → commit
# ════════════════════════════════════════════════════════════════════════════
section("TB01 — pesanan → draft → YA → commit")

CHAT1, USER1 = 1001, 111

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send), \
     patch("scripts.telegram_bot.handle_process_order") as mock_proc:

    def _proc_side(token, chat_id, user_id, text, db_path=None):
        # Simpan sesi seolah-olah process_order berhasil
        session_save(chat_id, user_id, "ORD-TB01", "HASH-TB01", False, _TEST_DB)
        _fake_send(token, chat_id, "DRAFT PESANAN\n\nKonfirmasi simpan pesanan ini? (YA / TIDAK)")

    mock_proc.side_effect = _proc_side

    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT1, USER1, "pesanan bu siti 2 kg ayam besok jam 14"), ALLOWED_IDS, _TEST_DB)

ok("TB01a: pesan draft dikirim", "DRAFT PESANAN" in _last(CHAT1))

sess_tb01 = session_get(CHAT1, USER1, _TEST_DB)
ok("TB01b: sesi tersimpan setelah process_order", sess_tb01 is not None, str(sess_tb01))
ok("TB01c: order_id benar", sess_tb01 and sess_tb01["order_id"] == "ORD-TB01")
ok("TB01d: draft_hash benar", sess_tb01 and sess_tb01["draft_hash"] == "HASH-TB01")
ok("TB01e: needs_clarification=False", sess_tb01 and not sess_tb01["needs_clarification"])

# User ketik YA → commit
with patch("scripts.telegram_bot.send_message", side_effect=_fake_send), \
     patch("umkm_agent.mcp_server.handle_confirm_commit",
           return_value=_make_confirm_result()) as mock_commit:

    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT1, USER1, "YA"), ALLOWED_IDS, _TEST_DB)

ok("TB01f: commit dipanggil dengan confirmed=True",
   mock_commit.called and mock_commit.call_args[0][0].get("confirmed") is True,
   str(mock_commit.call_args))
ok("TB01g: order_id diteruskan ke commit",
   mock_commit.called and mock_commit.call_args[0][0].get("order_id") == "ORD-TB01")
ok("TB01h: draft_hash diteruskan ke commit",
   mock_commit.called and mock_commit.call_args[0][0].get("draft_hash") == "HASH-TB01")
ok("TB01i: pesan sukses dikirim", "berhasil disimpan" in _last(CHAT1).lower() or "✅" in _last(CHAT1))

# Setelah commit berhasil, sesi harus bersih
sess_after = session_get(CHAT1, USER1, _TEST_DB)
ok("TB01j: sesi dihapus setelah commit", sess_after is None, str(sess_after))

# ════════════════════════════════════════════════════════════════════════════
# TB02 — restart (sesi persisten): simpan, buat koneksi baru, YA tetap commit
# ════════════════════════════════════════════════════════════════════════════
section("TB02 — restart lalu YA: sesi persisten bertahan di SQLite")

CHAT2, USER2 = 1002, 111

# Simpan sesi langsung (simulasi bot sebelum restart)
session_save(CHAT2, USER2, "ORD-TB02", "HASH-TB02", False, _TEST_DB)

# "Restart": buat koneksi baru ke DB yang sama dan YA
with patch("scripts.telegram_bot.send_message", side_effect=_fake_send), \
     patch("umkm_agent.mcp_server.handle_confirm_commit",
           return_value=_make_confirm_result()) as mock_commit2:

    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT2, USER2, "YA"), ALLOWED_IDS, _TEST_DB)

ok("TB02a: commit dipanggil (sesi survive restart)",
   mock_commit2.called, str(mock_commit2.call_args))
ok("TB02b: order_id benar setelah restart",
   mock_commit2.called and mock_commit2.call_args[0][0].get("order_id") == "ORD-TB02")

# ════════════════════════════════════════════════════════════════════════════
# TB03 — isolasi antar-user
# ════════════════════════════════════════════════════════════════════════════
section("TB03 — isolasi antar-user: sesi user A tidak bocor ke user B")

CHAT3A, CHAT3B = 1003, 1004
USER_A, USER_B = 201, 202

session_save(CHAT3A, USER_A, "ORD-USERA", "HASH-A", False, _TEST_DB)
session_save(CHAT3B, USER_B, "ORD-USERB", "HASH-B", False, _TEST_DB)

sess_a = session_get(CHAT3A, USER_A, _TEST_DB)
sess_b = session_get(CHAT3B, USER_B, _TEST_DB)

ok("TB03a: user A punya sesi sendiri", sess_a is not None and sess_a["order_id"] == "ORD-USERA")
ok("TB03b: user B punya sesi sendiri", sess_b is not None and sess_b["order_id"] == "ORD-USERB")

# Sesi user A tidak mempengaruhi user B
ok("TB03c: sesi A tidak bocor ke B", sess_b and sess_b["order_id"] != "ORD-USERA")
ok("TB03d: sesi B tidak bocor ke A", sess_a and sess_a["order_id"] != "ORD-USERB")

# Hapus sesi A tidak mempengaruhi B
session_clear(CHAT3A, USER_A, _TEST_DB)
ok("TB03e: hapus sesi A tidak mengganggu B",
   session_get(CHAT3B, USER_B, _TEST_DB) is not None)
ok("TB03f: sesi A terhapus",
   session_get(CHAT3A, USER_A, _TEST_DB) is None)

# ════════════════════════════════════════════════════════════════════════════
# TB04 — draft ambigu: YA ditolak, diminta klarifikasi
# ════════════════════════════════════════════════════════════════════════════
section("TB04 — draft ambigu: YA ditolak sebelum klarifikasi tuntas")

CHAT4, USER4 = 1005, 111

# Sesi dengan needs_clarification=True
session_save(CHAT4, USER4, "ORD-AMBIG", "HASH-AMBIG", True, _TEST_DB)

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send), \
     patch("umkm_agent.mcp_server.handle_confirm_commit") as mock_commit4:

    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT4, USER4, "YA"), ALLOWED_IDS, _TEST_DB)

ok("TB04a: commit TIDAK dipanggil untuk draft ambigu", not mock_commit4.called)
ok("TB04b: pesan klarifikasi dikirim",
   "klarifikasi" in _last(CHAT4).lower(), _last(CHAT4))

# Sesi masih ada (belum dihapus)
ok("TB04c: sesi ambigu dipertahankan",
   session_get(CHAT4, USER4, _TEST_DB) is not None)

# ════════════════════════════════════════════════════════════════════════════
# TB05 — retry commit partial error: sesi dipertahankan, YA ulang → retry
# ════════════════════════════════════════════════════════════════════════════
section("TB05 — retry commit partial error: sesi dipertahankan, YA ulang berhasil")

CHAT5, USER5 = 1006, 111

session_save(CHAT5, USER5, "ORD-RETRY", "HASH-RETRY", False, _TEST_DB)

partial_result = _make_confirm_result(
    success=True,
    status="simulated",
    errors={"docs_content": "koneksi gagal"},
)

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send), \
     patch("umkm_agent.mcp_server.handle_confirm_commit",
           return_value=partial_result):

    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT5, USER5, "YA"), ALLOWED_IDS, _TEST_DB)

ok("TB05a: pesan partial error dikirim",
   "sebagian" in _last(CHAT5).lower() or "gagal" in _last(CHAT5).lower(), _last(CHAT5))

# Sesi dipertahankan agar retry bisa dilakukan
sess_retry = session_get(CHAT5, USER5, _TEST_DB)
ok("TB05b: sesi dipertahankan setelah partial error", sess_retry is not None, str(sess_retry))

# Retry YA berhasil penuh
full_result = _make_confirm_result(success=True, status="simulated", errors={})

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send), \
     patch("umkm_agent.mcp_server.handle_confirm_commit",
           return_value=full_result) as mock_retry:

    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT5, USER5, "YA"), ALLOWED_IDS, _TEST_DB)

ok("TB05c: retry commit dipanggil",
   mock_retry.called, str(mock_retry.call_args))
ok("TB05d: pesan sukses dikirim", "✅" in _last(CHAT5) or "berhasil" in _last(CHAT5).lower())
ok("TB05e: sesi bersih setelah retry berhasil",
   session_get(CHAT5, USER5, _TEST_DB) is None)

# ════════════════════════════════════════════════════════════════════════════
# TB06 — BATAL/TIDAK → batalkan draft aktif
# ════════════════════════════════════════════════════════════════════════════
section("TB06 — BATAL/TIDAK → batalkan draft aktif")

CHAT6, USER6 = 1007, 111

session_save(CHAT6, USER6, "ORD-BATAL", "HASH-BATAL", False, _TEST_DB)

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT6, USER6, "TIDAK"), ALLOWED_IDS, _TEST_DB)

ok("TB06a: pesan pembatalan dikirim", "dibatalkan" in _last(CHAT6).lower(), _last(CHAT6))
ok("TB06b: sesi dihapus setelah TIDAK",
   session_get(CHAT6, USER6, _TEST_DB) is None)

# Test BATAL juga
CHAT6B, USER6B = 1008, 111
session_save(CHAT6B, USER6B, "ORD-BATAL2", "HASH-BATAL2", False, _TEST_DB)

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT6B, USER6B, "batal"), ALLOWED_IDS, _TEST_DB)

ok("TB06c: 'batal' (lowercase) → pesan pembatalan", "dibatalkan" in _last(CHAT6B).lower())
ok("TB06d: sesi dihapus setelah 'batal'",
   session_get(CHAT6B, USER6B, _TEST_DB) is None)

# ════════════════════════════════════════════════════════════════════════════
# TB07 — YA tanpa sesi aktif → pesan "tidak ada pesanan aktif"
# ════════════════════════════════════════════════════════════════════════════
section("TB07 — YA tanpa sesi aktif → pesan 'tidak ada pesanan aktif'")

CHAT7, USER7 = 1009, 222

# Pastikan tidak ada sesi
session_clear(CHAT7, USER7, _TEST_DB)

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send), \
     patch("umkm_agent.mcp_server.handle_confirm_commit") as mock_commit7:

    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT7, USER7, "YA"), ALLOWED_IDS, _TEST_DB)

ok("TB07a: commit TIDAK dipanggil", not mock_commit7.called)
ok("TB07b: pesan 'tidak ada pesanan aktif' dikirim",
   "aktif" in _last(CHAT7).lower() or "pesanan" in _last(CHAT7).lower(), _last(CHAT7))

# BATAL tanpa sesi juga informatif
with patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT7, USER7, "BATAL"), ALLOWED_IDS, _TEST_DB)

ok("TB07c: BATAL tanpa sesi → pesan informatif",
   "tidak ada" in _last(CHAT7).lower(), _last(CHAT7))

# ════════════════════════════════════════════════════════════════════════════
# TB08 — routing: teks non-keyword → handle_process_order
# ════════════════════════════════════════════════════════════════════════════
section("TB08 — routing: teks non-keyword → handle_process_order")

CHAT8, USER8 = 1010, 111

called_with: List[str] = []

def _spy_process(token, chat_id, user_id, text, db_path=None):
    called_with.append(text)
    _fake_send(token, chat_id, "DRAFT dipanggil")

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send), \
     patch("scripts.telegram_bot.handle_process_order", side_effect=_spy_process):

    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT8, USER8, "pesanan pak budi 3 porsi mie"), ALLOWED_IDS, _TEST_DB)

ok("TB08a: handle_process_order dipanggil untuk teks pesanan",
   len(called_with) == 1, str(called_with))
ok("TB08b: teks diteruskan dengan benar",
   called_with and called_with[0] == "pesanan pak budi 3 porsi mie")

# Keyword "ok" → confirm (bukan process_order)
called_with.clear()
session_save(CHAT8, USER8, "ORD-OK", "HASH-OK", False, _TEST_DB)

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send), \
     patch("scripts.telegram_bot.handle_process_order", side_effect=_spy_process), \
     patch("umkm_agent.mcp_server.handle_confirm_commit",
           return_value=_make_confirm_result()) as mock_ok:

    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT8, USER8, "ok"), ALLOWED_IDS, _TEST_DB)

ok("TB08c: 'ok' → confirm commit (bukan process_order)",
   mock_ok.called and len(called_with) == 0, str(mock_ok.call_args))

# ════════════════════════════════════════════════════════════════════════════
# TB09 — session_save / session_get / session_clear idempoten
# ════════════════════════════════════════════════════════════════════════════
section("TB09 — session CRUD idempoten")

CHAT9, USER9 = 2001, 333

# Belum ada → None
ok("TB09a: session_get sebelum save → None",
   session_get(CHAT9, USER9, _TEST_DB) is None)

# Save pertama
session_save(CHAT9, USER9, "ORD-A", "HASH-A", False, _TEST_DB)
s = session_get(CHAT9, USER9, _TEST_DB)
ok("TB09b: session ada setelah save", s is not None)
ok("TB09c: order_id benar", s and s["order_id"] == "ORD-A")

# Upsert: save lagi dengan data baru
session_save(CHAT9, USER9, "ORD-B", "HASH-B", True, _TEST_DB)
s2 = session_get(CHAT9, USER9, _TEST_DB)
ok("TB09d: upsert menimpa data lama", s2 and s2["order_id"] == "ORD-B")
ok("TB09e: needs_clarification diperbarui", s2 and s2["needs_clarification"] is True)

# Clear
session_clear(CHAT9, USER9, _TEST_DB)
ok("TB09f: session_get setelah clear → None",
   session_get(CHAT9, USER9, _TEST_DB) is None)

# Clear lagi (idempoten)
session_clear(CHAT9, USER9, _TEST_DB)
ok("TB09g: clear ulang tidak crash", True)

# ════════════════════════════════════════════════════════════════════════════
# TB10 — pesan non-private diabaikan
# ════════════════════════════════════════════════════════════════════════════
section("TB10 — pesan group / non-private diabaikan")

CHAT10, USER10 = 3001, 111

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send), \
     patch("scripts.telegram_bot.handle_process_order") as mock_p10, \
     patch("scripts.telegram_bot.handle_confirm_commit") as mock_c10:

    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT10, USER10, "YA", chat_type="group"), ALLOWED_IDS, _TEST_DB)

ok("TB10a: pesan group → handle_confirm_commit TIDAK dipanggil", not mock_c10.called)
ok("TB10b: pesan group → handle_process_order TIDAK dipanggil", not mock_p10.called)

# ════════════════════════════════════════════════════════════════════════════
# TB11 — access denied
# ════════════════════════════════════════════════════════════════════════════
section("TB11 — access denied: user tidak ada dalam ALLOWED_IDS")

CHAT11, USER_UNKNOWN = 4001, 9999

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send), \
     patch("scripts.telegram_bot.handle_process_order") as mock_p11:

    _clear_sent()
    dispatch(TOKEN, _make_update(CHAT11, USER_UNKNOWN, "pesanan apa saja"), ALLOWED_IDS, _TEST_DB)

ok("TB11a: handle_process_order tidak dipanggil untuk user tak dikenal",
   not mock_p11.called)
ok("TB11b: pesan akses ditolak dikirim",
   "ditolak" in _last(CHAT11).lower(), _last(CHAT11))

# ════════════════════════════════════════════════════════════════════════════
# RINGKASAN
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*58}")
print("  HASIL TEST — Telegram Bot")
print(f"{'='*58}")
print(f"  ✅ LULUS : {PASS}")
print(f"  ❌ GAGAL : {FAIL}")
if ERRORS:
    print("\n  Test yang gagal:")
    for e in ERRORS:
        print(f"    {e}")
print(f"{'='*58}")
print(f"  STATUS: {'SEMUA LULUS' if FAIL == 0 else f'{FAIL} GAGAL — perbaikan diperlukan'}")
print(f"{'='*58}")

# Cleanup
try:
    _TEST_DB.unlink(missing_ok=True)
except Exception:
    pass

sys.exit(0 if FAIL == 0 else 1)
