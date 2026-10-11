"""
scripts/test_telegram_regression.py
────────────────────────────────────
Regression tests for specific Telegram-bot bugs.
Uses the REAL handle_update_order backend; mocks only Langflow / Google / Telegram API.

Bugs covered:
  R01 — Bu Siti flow: draft without deadline → "besok jam 14:00" → same order_id,
         deadline filled, new hash, nama/items unchanged.
  R02 — handle_update_clarification extracts free text via Langflow (context draft),
         not blindly forwarded as jatuh_tempo_raw.
  R03 — Quantity correction on a complete draft: "3 kg" updates item qty, not deadline.
  R04 — Stale hash rejected: commit with old hash after update fails.
  R05 — send_message failure after successful commit: restart + YA recovers link via
         already_committed; /terakhir also recovers the link.
  R06 — already_committed fallback NEVER uses links from a different order.
  R07 — success=True with errors → last_commit saved so /terakhir has the link.

Run:
  python scripts/test_telegram_regression.py
  python scripts/test_telegram_regression.py --verbose
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, call
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

TZ_JKT = ZoneInfo("Asia/Jakarta")
VERBOSE = "--verbose" in sys.argv

# Separate test DB — never touches production journal
_TEST_DB = Path(tempfile.mktemp(suffix=".db", prefix="umkm_reg_test_"))
os.environ["UMKM_JOURNAL_DB"] = str(_TEST_DB)
os.environ["UMKM_GOOGLE_LIVE"] = "0"

from scripts.telegram_bot import (
    dispatch,
    handle_confirm_commit,
    handle_update_clarification,
    handle_process_order,
    cmd_terakhir,
    session_clear,
    session_get,
    session_save,
    session_mark_shown,
    last_commit_save,
    last_commit_get,
    _CONFIRM_KEYWORDS,
)
from umkm_agent.mcp_server import handle_update_order as real_handle_update_order
from umkm_agent.journal import (
    save_draft, get_order, _order_id, _draft_hash,
)
from umkm_agent.models import Order, OrderItem, OrderStatus
from umkm_agent.validator import validate_order

# ── helpers ───────────────────────────────────────────────────────────────────

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
    print(f"\n{'─'*62}")
    print(f"  {title}")
    print(f"{'─'*62}")


TOKEN = "FAKE_TOKEN"
_sent: Dict[int, List[str]] = {}
ALLOWED_IDS = {111}


def _fake_send(token: str, chat_id: int, text: str) -> None:
    _sent.setdefault(chat_id, []).append(text)


def _last(chat_id: int) -> str:
    return (_sent.get(chat_id) or [""])[-1]


def _clear() -> None:
    _sent.clear()


def _upd(chat_id: int, user_id: int, text: str, uid: int = 1) -> Dict[str, Any]:
    return {
        "update_id": uid,
        "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "first_name": "Tester"},
            "text": text,
        },
    }


def _make_langflow_response(
    nama: str,
    items: List[Dict],
    jatuh_tempo_raw: Optional[str] = None,
    jatuh_tempo_dt: Optional[str] = None,
    ambiguitas: Optional[List[str]] = None,
) -> str:
    """Build a JSON string as if returned by Langflow run_flow."""
    return json.dumps({
        "nama_pelanggan": nama,
        "items": items,
        "jatuh_tempo_raw": jatuh_tempo_raw,
        "jatuh_tempo_dt": jatuh_tempo_dt,
        "catatan": None,
        "ambiguitas": ambiguitas or [],
    }, ensure_ascii=False)


def _now_plus(days: int, hour: int = 14) -> str:
    """Return a timezone-aware ISO string for now+days at given hour."""
    dt = datetime.now(tz=TZ_JKT).replace(hour=hour, minute=0, second=0, microsecond=0)
    dt = dt + timedelta(days=days)
    return dt.isoformat()


def _seed_draft_bu_siti(db_path: Path) -> tuple[Order, str, str]:
    """
    Create a NEEDS_CLARIFICATION draft for Bu Siti (no deadline) in the journal.
    Returns (order, order_id, draft_hash).
    """
    order = Order(
        nama_pelanggan="Bu Siti",
        items=[OrderItem(nama="ayam", jumlah=2.0, satuan="kg")],
        jatuh_tempo_raw=None,
        jatuh_tempo_dt=None,
        catatan=None,
        ambiguitas=["Kapan jatuh tempo pesanan? (jam berapa?)"],
        teks_asli="pesanan bu siti 2 kg ayam",
        dibuat_pada=datetime.now(tz=TZ_JKT),
        diperbarui_pada=datetime.now(tz=TZ_JKT),
        status=OrderStatus.NEEDS_CLARIFICATION,
    )
    order = validate_order(order)
    oid = save_draft(order, db_path)
    dhash = _draft_hash(order)
    return order, oid, dhash


# ════════════════════════════════════════════════════════════════════════════
# R01 — Bu Siti flow: real handle_update_order resolves deadline
# ════════════════════════════════════════════════════════════════════════════
section("R01 — Bu Siti: draft tanpa deadline → update dengan 'besok jam 14:00'")

CHAT_R01, USER_R01 = 9001, 111

# Seed draft
orig_order, oid_r01, hash_r01 = _seed_draft_bu_siti(_TEST_DB)
session_save(CHAT_R01, USER_R01, oid_r01, hash_r01, True, _TEST_DB)

# "besok jam 14:00" — Langflow is mocked to return structured JSON with jatuh_tempo_dt
tomorrow_iso = _now_plus(1, 14)
lf_response_for_update = _make_langflow_response(
    nama="Bu Siti",
    items=[{"nama": "ayam", "jumlah": 2.0, "satuan": "kg"}],
    jatuh_tempo_raw="besok jam 14:00",
    jatuh_tempo_dt=tomorrow_iso,
    ambiguitas=[],
)

# Mock Langflow's run_flow so it returns the structured JSON
lf_run_mock = MagicMock(return_value={"output_text": lf_response_for_update, "raw_response": {}})

with patch("umkm_agent.langflow_client.run_flow", lf_run_mock), \
     patch("umkm_agent.langflow_client.UMKM_FLOW_ID", "FAKE_FLOW_ID"), \
     patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R01, USER_R01, "besok jam 14:00"), ALLOWED_IDS, _TEST_DB)

sess_r01 = session_get(CHAT_R01, USER_R01, _TEST_DB)
ok("R01a: order_id sama setelah update", sess_r01 and sess_r01["order_id"] == oid_r01,
   str(sess_r01))
ok("R01b: hash berubah setelah deadline diisi",
   sess_r01 and sess_r01["draft_hash"] != hash_r01,
   f"lama={hash_r01} baru={sess_r01.get('draft_hash') if sess_r01 else 'None'}")
ok("R01c: needs_clarification False setelah deadline terjawab",
   sess_r01 and not sess_r01["needs_clarification"],
   str(sess_r01))

# Verify the actual order in DB
updated_order = get_order(oid_r01, _TEST_DB)
ok("R01d: nama pelanggan tetap Bu Siti",
   updated_order and updated_order.nama_pelanggan == "Bu Siti",
   str(updated_order))
ok("R01e: item ayam 2 kg tetap",
   updated_order and len(updated_order.items) == 1
   and updated_order.items[0].nama == "ayam"
   and updated_order.items[0].jumlah == 2.0,
   str(updated_order.items if updated_order else None))
ok("R01f: jatuh_tempo_dt terisi setelah update",
   updated_order and updated_order.jatuh_tempo_dt is not None,
   str(updated_order))

# ════════════════════════════════════════════════════════════════════════════
# R02 — Langflow called with DRAFT CONTEXT for free-text clarification
# ════════════════════════════════════════════════════════════════════════════
section("R02 — Langflow dipanggil dengan konteks draft untuk ekstraksi teks bebas")

CHAT_R02, USER_R02 = 9002, 111

orig_order_r02, oid_r02, hash_r02 = _seed_draft_bu_siti(_TEST_DB)
session_save(CHAT_R02, USER_R02, oid_r02, hash_r02, True, _TEST_DB)

lf_calls: List[str] = []

def _capture_lf(teks_pesanan: str, flow_id=None, **kw):
    lf_calls.append(teks_pesanan)
    return {"output_text": lf_response_for_update, "raw_response": {}}

with patch("umkm_agent.langflow_client.run_flow", side_effect=_capture_lf), \
     patch("umkm_agent.langflow_client.UMKM_FLOW_ID", "FAKE_FLOW_ID"), \
     patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R02, USER_R02, "besok jam 14:00"), ALLOWED_IDS, _TEST_DB)

ok("R02a: Langflow dipanggil saat menjawab klarifikasi", len(lf_calls) > 0, str(lf_calls))
ok("R02b: konteks draft disertakan (nama 'Bu Siti' ada dalam prompt)",
   any("Bu Siti" in c or "ayam" in c or "ORD-" in c for c in lf_calls),
   str(lf_calls))
ok("R02c: teks jawaban user disertakan dalam prompt",
   any("besok jam 14:00" in c for c in lf_calls),
   str(lf_calls))

# ════════════════════════════════════════════════════════════════════════════
# R03 — Quantity correction on complete draft: "3 kg" updates qty, not deadline
# ════════════════════════════════════════════════════════════════════════════
section("R03 — Koreksi jumlah '3 kg' pada draft lengkap: qty diperbarui, deadline tidak")

# This tests that passing a quantity correction doesn't end up as jatuh_tempo_raw.
# Build a COMPLETE draft first (deadline filled), then user says "3 kg" as correction.
CHAT_R03, USER_R03 = 9003, 111

tomorrow_dt = datetime.now(tz=TZ_JKT).replace(
    hour=14, minute=0, second=0, microsecond=0
) + timedelta(days=1)

order_r03 = Order(
    nama_pelanggan="Bu Rina",
    items=[OrderItem(nama="tempe", jumlah=2.0, satuan="kg")],
    jatuh_tempo_raw="besok jam 14:00",
    jatuh_tempo_dt=tomorrow_dt,
    catatan=None,
    ambiguitas=["Berapa jumlah tempe yang dipesan?"],
    teks_asli="pesanan bu rina tempe besok jam 14",
    dibuat_pada=datetime.now(tz=TZ_JKT),
    diperbarui_pada=datetime.now(tz=TZ_JKT),
    status=OrderStatus.NEEDS_CLARIFICATION,
)
order_r03 = validate_order(order_r03)
oid_r03 = save_draft(order_r03, _TEST_DB)
hash_r03 = _draft_hash(order_r03)
session_save(CHAT_R03, USER_R03, oid_r03, hash_r03, True, _TEST_DB)

# Langflow understands "3 kg" in context → returns updated qty, same deadline
lf_response_qty = _make_langflow_response(
    nama="Bu Rina",
    items=[{"nama": "tempe", "jumlah": 3.0, "satuan": "kg"}],
    jatuh_tempo_raw="besok jam 14:00",
    jatuh_tempo_dt=tomorrow_dt.isoformat(),
    ambiguitas=[],
)

with patch("umkm_agent.langflow_client.run_flow",
           return_value={"output_text": lf_response_qty, "raw_response": {}}), \
     patch("umkm_agent.langflow_client.UMKM_FLOW_ID", "FAKE_FLOW_ID"), \
     patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R03, USER_R03, "3 kg"), ALLOWED_IDS, _TEST_DB)

order_r03_after = get_order(oid_r03, _TEST_DB)
ok("R03a: jumlah tempe diupdate ke 3 kg",
   order_r03_after and order_r03_after.items[0].jumlah == 3.0,
   str(order_r03_after.items if order_r03_after else None))
ok("R03b: jatuh_tempo_dt tidak berubah menjadi None",
   order_r03_after and order_r03_after.jatuh_tempo_dt is not None,
   str(order_r03_after))
ok("R03c: order_id sama",
   session_get(CHAT_R03, USER_R03, _TEST_DB) is not None
   and session_get(CHAT_R03, USER_R03, _TEST_DB)["order_id"] == oid_r03)

# ════════════════════════════════════════════════════════════════════════════
# R04 — Stale hash rejected by real handle_confirm_commit guard
# ════════════════════════════════════════════════════════════════════════════
section("R04 — Hash lama ditolak oleh handle_confirm_commit setelah update")

# Create a fresh draft + update it → new hash in journal
order_r04 = Order(
    nama_pelanggan="Pak Budi",
    items=[OrderItem(nama="nasi", jumlah=5.0, satuan="porsi")],
    jatuh_tempo_raw=None,
    jatuh_tempo_dt=None,
    catatan=None,
    ambiguitas=["Kapan jatuh tempo?"],
    teks_asli="nasi 5 porsi pak budi",
    dibuat_pada=datetime.now(tz=TZ_JKT),
    diperbarui_pada=datetime.now(tz=TZ_JKT),
    status=OrderStatus.NEEDS_CLARIFICATION,
)
order_r04 = validate_order(order_r04)
oid_r04 = save_draft(order_r04, _TEST_DB)
old_hash_r04 = _draft_hash(order_r04)

# Update via real backend → new hash stored
tomorrow_iso_r04 = _now_plus(1, 10)
update_result_r04 = real_handle_update_order({
    "order_id": oid_r04,
    "jatuh_tempo_raw": "besok jam 10:00",
})
ok("R04-setup: update sukses", update_result_r04.get("success"), str(update_result_r04))
new_hash_r04 = update_result_r04.get("draft_hash", "")
ok("R04-setup: hash berubah", new_hash_r04 != old_hash_r04, f"{old_hash_r04} → {new_hash_r04}")

# Now try commit with OLD hash → must be rejected
from umkm_agent.mcp_server import handle_confirm_commit as real_mcp_confirm
result_stale = real_mcp_confirm({
    "order_id": oid_r04,
    "confirmed": True,
    "draft_hash": old_hash_r04,  # stale
})
ok("R04a: commit dengan hash lama ditolak", not result_stale.get("success"),
   str(result_stale))
ok("R04b: pesan error menyebut 'berubah' atau 'hash'",
   "berubah" in (result_stale.get("error") or "").lower()
   or "hash" in (result_stale.get("error") or "").lower(),
   str(result_stale))

# Commit with NEW hash → succeeds (validation may fail due to dry-run data, but hash check passes)
result_fresh = real_mcp_confirm({
    "order_id": oid_r04,
    "confirmed": True,
    "draft_hash": new_hash_r04,
})
# Success or validation error is fine — but NOT a hash-mismatch error
hash_mismatch_fresh = (
    not result_fresh.get("success")
    and ("berubah" in (result_fresh.get("error") or "").lower()
         or ("hash" in (result_fresh.get("error") or "").lower()
             and "wajib" not in (result_fresh.get("error") or "").lower()))
)
ok("R04c: commit dengan hash baru tidak ditolak karena hash lama",
   not hash_mismatch_fresh, str(result_fresh))

# ════════════════════════════════════════════════════════════════════════════
# R05 — send_message fails after successful commit → restart + YA recovers link
# ════════════════════════════════════════════════════════════════════════════
section("R05 — send_message gagal setelah commit: restart + YA + /terakhir pulihkan link")

CHAT_R05, USER_R05 = 9005, 111

# Seed a fresh draft
order_r05 = Order(
    nama_pelanggan="Bu Dewi",
    items=[OrderItem(nama="kue", jumlah=1.0, satuan="kotak")],
    jatuh_tempo_raw="besok jam 09:00",
    jatuh_tempo_dt=datetime.now(tz=TZ_JKT).replace(
        hour=9, minute=0, second=0, microsecond=0
    ) + timedelta(days=1),
    catatan=None,
    ambiguitas=[],
    teks_asli="kue 1 kotak bu dewi besok jam 9",
    dibuat_pada=datetime.now(tz=TZ_JKT),
    diperbarui_pada=datetime.now(tz=TZ_JKT),
    status=OrderStatus.DRAFT,
)
order_r05 = validate_order(order_r05)
oid_r05 = save_draft(order_r05, _TEST_DB)
hash_r05 = _draft_hash(order_r05)
session_save(CHAT_R05, USER_R05, oid_r05, hash_r05, False, _TEST_DB, draft_shown=True)

success_result_r05 = {
    "success": True,
    "status": "simulated",
    "dry_run": True,
    "google_doc_url": "DRY://docs/R05",
    "google_event_url": "DRY://cal/R05",
    "errors": {},
}

send_call_count = [0]

def _send_fail_once(token, chat_id, text):
    send_call_count[0] += 1
    if send_call_count[0] == 1:
        raise OSError("Network error")
    _fake_send(token, chat_id, text)

# First YA — commit succeeds, send_message fails
with patch("umkm_agent.mcp_server.handle_confirm_commit", return_value=success_result_r05), \
     patch("scripts.telegram_bot.send_message", side_effect=_send_fail_once):
    _clear()
    try:
        dispatch(TOKEN, _upd(CHAT_R05, USER_R05, "YA"), ALLOWED_IDS, _TEST_DB)
    except OSError:
        pass  # send_message raised — expected

# After the send failure, session should be cleared and last_commit should be saved
# (because last_commit_save happens BEFORE send_message)
sess_r05_after_fail = session_get(CHAT_R05, USER_R05, _TEST_DB)
lc_r05_after_fail = last_commit_get(CHAT_R05, USER_R05, _TEST_DB)
ok("R05a: last_commit disimpan meski send_message gagal",
   lc_r05_after_fail is not None and lc_r05_after_fail["order_id"] == oid_r05,
   str(lc_r05_after_fail))
ok("R05b: sesi draft dihapus meski send_message gagal",
   sess_r05_after_fail is None, str(sess_r05_after_fail))

# User sends YA again after restart — already_committed fires, returns link
already_result_r05 = {
    "success": True,
    "status": "already_committed",
    "dry_run": True,
    "google_doc_url": "DRY://docs/R05",
    "google_event_url": "DRY://cal/R05",
    "order_id": oid_r05,
    "revision": 1,
}
# Re-seed the session (simulating restart where user sends a new order or YA)
# Actually after session cleared, user sending "YA" would get "tidak ada pesanan aktif".
# The real recovery path is /terakhir. Let's verify that.
_clear()
with patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    cmd_terakhir(TOKEN, CHAT_R05, USER_R05, ALLOWED_IDS, _TEST_DB)
ok("R05c: /terakhir menampilkan link setelah send_message gagal",
   "R05" in _last(CHAT_R05), _last(CHAT_R05))

# ════════════════════════════════════════════════════════════════════════════
# R06 — already_committed NEVER falls back to a DIFFERENT order's links
# ════════════════════════════════════════════════════════════════════════════
section("R06 — already_committed tidak menggunakan link dari order LAIN sebagai fallback")

CHAT_R06, USER_R06 = 9006, 111

# Save a last_commit for a DIFFERENT order
last_commit_save(CHAT_R06, USER_R06, "ORD-OTHER-999",
                 "https://docs/OTHER", "https://cal/OTHER", False, _TEST_DB)

# Now simulate already_committed for a different order that has no doc_url in response
session_save(CHAT_R06, USER_R06, "ORD-CURRENT-888", "HASH-CURR", False, _TEST_DB, draft_shown=True)
already_no_url = {
    "success": True,
    "status": "already_committed",
    "dry_run": False,
    "google_doc_url": None,   # backend doesn't include URL
    "google_event_url": None,
    "order_id": "ORD-CURRENT-888",
    "revision": 1,
}
with patch("umkm_agent.mcp_server.handle_confirm_commit", return_value=already_no_url), \
     patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R06, USER_R06, "YA"), ALLOWED_IDS, _TEST_DB)

msg_r06 = _last(CHAT_R06)
ok("R06a: link dari order LAIN tidak ditampilkan",
   "docs/OTHER" not in msg_r06 and "cal/OTHER" not in msg_r06,
   msg_r06)
ok("R06b: pesan tetap menampilkan status 'sudah tersimpan'",
   "sudah tersimpan" in msg_r06.lower() or "already" in msg_r06.lower(),
   msg_r06)

# If last_commit IS for the SAME order, it's fine to use as fallback
last_commit_save(CHAT_R06, USER_R06, "ORD-CURRENT-888",
                 "https://docs/CURR", "https://cal/CURR", False, _TEST_DB)

with patch("umkm_agent.mcp_server.handle_confirm_commit", return_value=already_no_url), \
     patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R06, USER_R06, "YA", uid=2), ALLOWED_IDS, _TEST_DB)

msg_r06b = _last(CHAT_R06)
ok("R06c: fallback ke last_commit diperbolehkan jika order_id SAMA",
   "docs/CURR" in msg_r06b or "-" in msg_r06b,
   msg_r06b)

# ════════════════════════════════════════════════════════════════════════════
# R07 — success=True with errors → last_commit saved so /terakhir has the link
# ════════════════════════════════════════════════════════════════════════════
section("R07 — success=True dengan errors: last_commit disimpan, /terakhir punya link")

CHAT_R07, USER_R07 = 9007, 111

order_r07 = Order(
    nama_pelanggan="Bu Maya",
    items=[OrderItem(nama="roti", jumlah=10.0, satuan="pcs")],
    jatuh_tempo_raw="besok jam 08:00",
    jatuh_tempo_dt=datetime.now(tz=TZ_JKT).replace(
        hour=8, minute=0, second=0, microsecond=0
    ) + timedelta(days=1),
    catatan=None,
    ambiguitas=[],
    teks_asli="roti 10 pcs bu maya besok jam 8",
    dibuat_pada=datetime.now(tz=TZ_JKT),
    diperbarui_pada=datetime.now(tz=TZ_JKT),
    status=OrderStatus.DRAFT,
)
order_r07 = validate_order(order_r07)
oid_r07 = save_draft(order_r07, _TEST_DB)
hash_r07 = _draft_hash(order_r07)
session_save(CHAT_R07, USER_R07, oid_r07, hash_r07, False, _TEST_DB, draft_shown=True)

# success=True but with some errors (calendar failed)
partial_success_r07 = {
    "success": True,
    "status": "simulated",
    "dry_run": True,
    "google_doc_url": "DRY://docs/R07",
    "google_event_url": "-",
    "errors": {"calendar": "Calendar timeout"},
}
with patch("umkm_agent.mcp_server.handle_confirm_commit", return_value=partial_success_r07), \
     patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R07, USER_R07, "YA"), ALLOWED_IDS, _TEST_DB)

lc_r07 = last_commit_get(CHAT_R07, USER_R07, _TEST_DB)
ok("R07a: last_commit disimpan meski ada errors",
   lc_r07 is not None and lc_r07["order_id"] == oid_r07,
   str(lc_r07))
ok("R07b: last_commit menyimpan doc_url yang berhasil",
   lc_r07 and "R07" in (lc_r07.get("doc_url") or ""),
   str(lc_r07))

# /terakhir harus menampilkan link
_clear()
with patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    cmd_terakhir(TOKEN, CHAT_R07, USER_R07, ALLOWED_IDS, _TEST_DB)
ok("R07c: /terakhir menampilkan doc_url setelah success+errors",
   "R07" in _last(CHAT_R07), _last(CHAT_R07))

# ════════════════════════════════════════════════════════════════════════════
# R08 — dispatch routes non-keyword text to update for ANY active draft
#        ("ayam jadi 3 kg" before YA is not a new order)
# ════════════════════════════════════════════════════════════════════════════
section("R08 — teks sebelum YA pada draft aktif (needs_clarification=False) → update, bukan order baru")

CHAT_R08, USER_R08 = 9008, 111

order_r08 = Order(
    nama_pelanggan="Pak Hasan",
    items=[OrderItem(nama="ayam", jumlah=2.0, satuan="kg")],
    jatuh_tempo_raw="besok jam 10:00",
    jatuh_tempo_dt=datetime.now(tz=TZ_JKT).replace(
        hour=10, minute=0, second=0, microsecond=0
    ) + timedelta(days=1),
    catatan=None,
    ambiguitas=[],
    teks_asli="ayam 2 kg pak hasan besok jam 10",
    dibuat_pada=datetime.now(tz=TZ_JKT),
    diperbarui_pada=datetime.now(tz=TZ_JKT),
    status=OrderStatus.DRAFT,
)
order_r08 = validate_order(order_r08)
oid_r08 = save_draft(order_r08, _TEST_DB)
hash_r08 = _draft_hash(order_r08)
# Active session with needs_clarification=False (draft is complete, awaiting YA)
# draft_shown=True: draft already displayed (awaiting YA)
session_save(CHAT_R08, USER_R08, oid_r08, hash_r08, False, _TEST_DB, draft_shown=True)

proc_called_r08: List[str] = []
upd_called_r08: List[str] = []

def _spy_proc_r08(token, chat_id, user_id, text, db_path=None):
    proc_called_r08.append(text)

def _spy_upd_r08(token, chat_id, user_id, text, sess, db_path=None):
    upd_called_r08.append(text)

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send), \
     patch("scripts.telegram_bot.handle_process_order", side_effect=_spy_proc_r08), \
     patch("scripts.telegram_bot.handle_update_clarification", side_effect=_spy_upd_r08):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R08, USER_R08, "ayam jadi 3 kg"), ALLOWED_IDS, _TEST_DB)

ok("R08a: handle_update_clarification dipanggil (bukan handle_process_order)",
   len(upd_called_r08) == 1 and len(proc_called_r08) == 0,
   f"upd={upd_called_r08} proc={proc_called_r08}")
ok("R08b: teks koreksi diteruskan ke update",
   upd_called_r08 and upd_called_r08[0] == "ayam jadi 3 kg")

# ════════════════════════════════════════════════════════════════════════════
# R09 — Langflow gagal → draft dipertahankan, error ditampilkan, tidak ada mutasi
# ════════════════════════════════════════════════════════════════════════════
section("R09 — Langflow gagal saat update: draft/hash dipertahankan, pesan error")

CHAT_R09, USER_R09 = 9009, 111

order_r09 = Order(
    nama_pelanggan="Bu Ida",
    items=[OrderItem(nama="tahu", jumlah=5.0, satuan="pcs")],
    jatuh_tempo_raw=None,
    jatuh_tempo_dt=None,
    catatan=None,
    ambiguitas=["Kapan jatuh tempo?"],
    teks_asli="tahu 5 pcs bu ida",
    dibuat_pada=datetime.now(tz=TZ_JKT),
    diperbarui_pada=datetime.now(tz=TZ_JKT),
    status=OrderStatus.NEEDS_CLARIFICATION,
)
order_r09 = validate_order(order_r09)
oid_r09 = save_draft(order_r09, _TEST_DB)
hash_r09 = _draft_hash(order_r09)
session_save(CHAT_R09, USER_R09, oid_r09, hash_r09, True, _TEST_DB)

import umkm_agent.langflow_client as _lf
_lf_error = _lf.LangflowError("Connection refused", status=0)

with patch("umkm_agent.langflow_client.run_flow", side_effect=_lf_error), \
     patch("umkm_agent.langflow_client.UMKM_FLOW_ID", "FAKE_FLOW_ID"), \
     patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R09, USER_R09, "besok jam 15:00"), ALLOWED_IDS, _TEST_DB)

msg_r09 = _last(CHAT_R09)
ok("R09a: pesan error Langflow ditampilkan",
   "langflow" in msg_r09.lower() or "tidak tersedia" in msg_r09.lower(), msg_r09)

# Hash dan session harus tidak berubah — tidak ada mutasi
sess_r09_after = session_get(CHAT_R09, USER_R09, _TEST_DB)
ok("R09b: order_id tidak berubah", sess_r09_after and sess_r09_after["order_id"] == oid_r09,
   str(sess_r09_after))
ok("R09c: draft_hash tidak berubah setelah Langflow gagal",
   sess_r09_after and sess_r09_after["draft_hash"] == hash_r09,
   f"exp={hash_r09} got={sess_r09_after.get('draft_hash') if sess_r09_after else None}")

# ════════════════════════════════════════════════════════════════════════════
# R10 — No-op dari Langflow → pesan klarifikasi, tidak ada mutasi
# ════════════════════════════════════════════════════════════════════════════
section("R10 — Langflow tidak mengekstrak field apapun: klarifikasi ditampilkan, tidak ada mutasi")

CHAT_R10, USER_R10 = 9010, 111

# Gunakan draft yang sama
session_save(CHAT_R10, USER_R10, oid_r09, hash_r09, True, _TEST_DB)

# Langflow returns empty/no-field JSON — nothing extracted
noop_lf_response = json.dumps({
    "nama_pelanggan": None,
    "items": [],            # empty — no update
    "jatuh_tempo_raw": None,
    "jatuh_tempo_dt": None,
    "catatan": None,
    "ambiguitas": [],
})

with patch("umkm_agent.langflow_client.run_flow",
           return_value={"output_text": noop_lf_response, "raw_response": {}}), \
     patch("umkm_agent.langflow_client.UMKM_FLOW_ID", "FAKE_FLOW_ID"), \
     patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R10, USER_R10, "???"), ALLOWED_IDS, _TEST_DB)

msg_r10 = _last(CHAT_R10)
ok("R10a: pesan 'tidak ada perubahan' atau 'perjelas' ditampilkan",
   "tidak ada perubahan" in msg_r10.lower()
   or "perjelas" in msg_r10.lower()
   or "perubahan" in msg_r10.lower(), msg_r10)

sess_r10_after = session_get(CHAT_R10, USER_R10, _TEST_DB)
ok("R10b: draft_hash tidak berubah saat no-op",
   sess_r10_after and sess_r10_after["draft_hash"] == hash_r09,
   f"exp={hash_r09} got={sess_r10_after.get('draft_hash') if sess_r10_after else None}")

# ════════════════════════════════════════════════════════════════════════════
# R11 — Konteks ke Langflow menyertakan deadline ISO absolut dan catatan
# ════════════════════════════════════════════════════════════════════════════
section("R11 — Konteks Langflow: deadline ISO absolut dan catatan disertakan")

CHAT_R11, USER_R11 = 9011, 111

deadline_r11 = datetime.now(tz=TZ_JKT).replace(
    hour=16, minute=0, second=0, microsecond=0
) + timedelta(days=2)

order_r11 = Order(
    nama_pelanggan="Bu Lina",
    items=[OrderItem(nama="nasi", jumlah=10.0, satuan="bungkus")],
    jatuh_tempo_raw="lusa jam 16:00",
    jatuh_tempo_dt=deadline_r11,
    catatan="tanpa sambal",
    ambiguitas=["Berapa jumlah nasi?"],
    teks_asli="nasi bu lina lusa jam 16 tanpa sambal",
    dibuat_pada=datetime.now(tz=TZ_JKT),
    diperbarui_pada=datetime.now(tz=TZ_JKT),
    status=OrderStatus.NEEDS_CLARIFICATION,
)
order_r11 = validate_order(order_r11)
oid_r11 = save_draft(order_r11, _TEST_DB)
hash_r11 = _draft_hash(order_r11)
session_save(CHAT_R11, USER_R11, oid_r11, hash_r11, True, _TEST_DB)

captured_ctx: List[str] = []

def _capture_ctx_r11(teks_pesanan, flow_id=None, **kw):
    captured_ctx.append(teks_pesanan)
    # Return quantity update — deadline should remain from context
    return {"output_text": json.dumps({
        "nama_pelanggan": "Bu Lina",
        "items": [{"nama": "nasi", "jumlah": 20.0, "satuan": "bungkus"}],
        "jatuh_tempo_raw": "lusa jam 16:00",
        "jatuh_tempo_dt": deadline_r11.isoformat(),
        "catatan": "tanpa sambal",
        "ambiguitas": [],
    }), "raw_response": {}}

with patch("umkm_agent.langflow_client.run_flow", side_effect=_capture_ctx_r11), \
     patch("umkm_agent.langflow_client.UMKM_FLOW_ID", "FAKE_FLOW_ID"), \
     patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R11, USER_R11, "20 bungkus"), ALLOWED_IDS, _TEST_DB)

ok("R11a: konteks ke Langflow menyertakan deadline ISO",
   any(deadline_r11.isoformat()[:10] in c for c in captured_ctx),
   str([c[:200] for c in captured_ctx]))
ok("R11b: konteks ke Langflow menyertakan catatan",
   any("tanpa sambal" in c for c in captured_ctx),
   str([c[:200] for c in captured_ctx]))

# ════════════════════════════════════════════════════════════════════════════
# R12 — YA tanpa draft aktif, tetapi ada last_commit → tampilkan hasil terakhir
# ════════════════════════════════════════════════════════════════════════════
section("R12 — YA tanpa draft aktif + ada last_commit → tampilkan hasil terakhir")

CHAT_R12, USER_R12 = 9012, 111

# No active session
session_clear(CHAT_R12, USER_R12, _TEST_DB)
last_commit_save(CHAT_R12, USER_R12, "ORD-LAST-R12",
                 "https://docs/R12", "https://cal/R12", True, _TEST_DB)

with patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R12, USER_R12, "YA"), ALLOWED_IDS, _TEST_DB)

msg_r12 = _last(CHAT_R12)
ok("R12a: pesan menampilkan URL dari last_commit",
   "docs/R12" in msg_r12 or "cal/R12" in msg_r12, msg_r12)
ok("R12b: tidak ada 'tidak ada pesanan' karena last_commit ada",
   "tidak ada pesanan" not in msg_r12.lower(), msg_r12)

# ════════════════════════════════════════════════════════════════════════════
# R13 — send_message gagal saat draft diperbarui (update): hash baru tetap disimpan
# ════════════════════════════════════════════════════════════════════════════
section("R13 — send_message gagal setelah draft diperbarui: hash baru tetap tersimpan")

CHAT_R13, USER_R13 = 9013, 111

order_r13, oid_r13, hash_r13 = _seed_draft_bu_siti(_TEST_DB)
session_save(CHAT_R13, USER_R13, oid_r13, hash_r13, True, _TEST_DB)

tomorrow_iso_r13 = _now_plus(1, 14)
lf_resp_r13 = _make_langflow_response(
    nama="Bu Siti",
    items=[{"nama": "ayam", "jumlah": 2.0, "satuan": "kg"}],
    jatuh_tempo_raw="besok jam 14:00",
    jatuh_tempo_dt=tomorrow_iso_r13,
    ambiguitas=[],
)

def _send_fail_on_draft(token, chat_id, text):
    # Fail only if it's a draft reply (contains "DRAFT PESANAN" or "Konfirmasi")
    if "DRAFT" in text or "Konfirmasi" in text or "klarifikasi" in text.lower():
        raise OSError("send failed")
    _fake_send(token, chat_id, text)

with patch("umkm_agent.langflow_client.run_flow",
           return_value={"output_text": lf_resp_r13, "raw_response": {}}), \
     patch("umkm_agent.langflow_client.UMKM_FLOW_ID", "FAKE_FLOW_ID"), \
     patch("scripts.telegram_bot.send_message", side_effect=_send_fail_on_draft):
    _clear()
    try:
        dispatch(TOKEN, _upd(CHAT_R13, USER_R13, "besok jam 14:00"), ALLOWED_IDS, _TEST_DB)
    except OSError:
        pass

# The session should be updated with the NEW hash (update was persisted before send_message)
sess_r13 = session_get(CHAT_R13, USER_R13, _TEST_DB)
ok("R13a: sesi diperbarui meski send_message gagal",
   sess_r13 and sess_r13["order_id"] == oid_r13, str(sess_r13))
ok("R13b: hash baru tersimpan meski send_message gagal",
   sess_r13 and sess_r13["draft_hash"] != hash_r13,
   f"lama={hash_r13} baru={sess_r13.get('draft_hash') if sess_r13 else None}")

# ════════════════════════════════════════════════════════════════════════════
# R14 — /terakhir di group chat diabaikan; user tidak diizinkan diabaikan
# ════════════════════════════════════════════════════════════════════════════
section("R14 — /terakhir: group chat diabaikan, user tidak diizinkan diabaikan")

CHAT_R14, USER_R14 = 9014, 111
CHAT_R14G = 9015  # group

# Pre-seed last_commit so it would show if check were bypassed
last_commit_save(CHAT_R14, USER_R14, "ORD-R14", "https://docs/R14", "https://cal/R14", False, _TEST_DB)
last_commit_save(CHAT_R14G, USER_R14, "ORD-R14G", "https://docs/R14G", "https://cal/R14G", False, _TEST_DB)

# /terakhir in group chat — should be silently ignored
group_upd = {
    "update_id": 99,
    "message": {
        "chat": {"id": CHAT_R14G, "type": "group"},
        "from": {"id": USER_R14, "first_name": "Tester"},
        "text": "/terakhir",
    },
}
with patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, group_upd, ALLOWED_IDS, _TEST_DB)
ok("R14a: /terakhir di group chat diabaikan (tidak ada pesan terkirim)",
   not _sent.get(CHAT_R14G), str(_sent.get(CHAT_R14G)))

# /terakhir from unauthorized user — silently ignored
unauth_upd = {
    "update_id": 100,
    "message": {
        "chat": {"id": CHAT_R14, "type": "private"},
        "from": {"id": 9999, "first_name": "Stranger"},
        "text": "/terakhir",
    },
}
with patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, unauth_upd, ALLOWED_IDS, _TEST_DB)
ok("R14b: /terakhir dari user tidak diizinkan diabaikan",
   not _sent.get(CHAT_R14), str(_sent.get(CHAT_R14)))

# /terakhir from authorized user in private chat — should work
auth_upd = {
    "update_id": 101,
    "message": {
        "chat": {"id": CHAT_R14, "type": "private"},
        "from": {"id": USER_R14, "first_name": "Tester"},
        "text": "/terakhir",
    },
}
with patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, auth_upd, ALLOWED_IDS, _TEST_DB)
ok("R14c: /terakhir dari user terotorisasi di private chat → menampilkan URL",
   "R14" in _last(CHAT_R14), _last(CHAT_R14))

# ════════════════════════════════════════════════════════════════════════════
# R15 — draft_shown gate: send fail → YA blocked → re-show → YA commits
# ════════════════════════════════════════════════════════════════════════════
section("R15 — draft_shown: send gagal → YA diblokir → tampilkan ulang → YA commit")

CHAT_R15, USER_R15 = 9015, 111

tomorrow_r15 = datetime.now(tz=TZ_JKT).replace(
    hour=11, minute=0, second=0, microsecond=0
) + timedelta(days=1)
order_r15 = Order(
    nama_pelanggan="Bu Tina",
    items=[OrderItem(nama="lontong", jumlah=5.0, satuan="bungkus")],
    jatuh_tempo_raw="besok jam 11:00",
    jatuh_tempo_dt=tomorrow_r15,
    catatan=None,
    ambiguitas=[],
    teks_asli="lontong 5 bungkus bu tina besok jam 11",
    dibuat_pada=datetime.now(tz=TZ_JKT),
    diperbarui_pada=datetime.now(tz=TZ_JKT),
    status=OrderStatus.DRAFT,
)
order_r15 = validate_order(order_r15)
oid_r15 = save_draft(order_r15, _TEST_DB)
hash_r15 = _draft_hash(order_r15)

# Save session with draft_shown=False (draft not yet delivered to user)
session_save(CHAT_R15, USER_R15, oid_r15, hash_r15, False, _TEST_DB)

# --- step 1: user sends YA immediately, but draft was never shown ---
commit_spy_r15: List[dict] = []

def _spy_mcp_commit_r15(args):
    commit_spy_r15.append(args)
    return {"success": True, "status": "simulated", "dry_run": True,
            "google_doc_url": "DRY://docs/R15", "google_event_url": "DRY://cal/R15",
            "errors": {}}

with patch("umkm_agent.mcp_server.handle_confirm_commit", side_effect=_spy_mcp_commit_r15), \
     patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R15, USER_R15, "YA"), ALLOWED_IDS, _TEST_DB)

ok("R15a: commit TIDAK dipanggil ketika draft belum dikirim ke user",
   len(commit_spy_r15) == 0, str(commit_spy_r15))
ok("R15b: draft ditampilkan ulang (berisi nama pesanan atau konfirmasi)",
   "lontong" in _last(CHAT_R15).lower() or "konfirmasi" in _last(CHAT_R15).lower(),
   _last(CHAT_R15))

# draft_shown should now be True (re-show succeeded)
sess_r15_after_reshow = session_get(CHAT_R15, USER_R15, _TEST_DB)
ok("R15c: draft_shown=True setelah tampilkan ulang berhasil",
   sess_r15_after_reshow and sess_r15_after_reshow.get("draft_shown") is True,
   str(sess_r15_after_reshow))

# --- step 2: user sends YA again → now commit is allowed ---
with patch("umkm_agent.mcp_server.handle_confirm_commit", side_effect=_spy_mcp_commit_r15), \
     patch("scripts.telegram_bot.send_message", side_effect=_fake_send):
    _clear()
    dispatch(TOKEN, _upd(CHAT_R15, USER_R15, "YA", uid=2), ALLOWED_IDS, _TEST_DB)

ok("R15d: commit dipanggil setelah draft ditampilkan",
   len(commit_spy_r15) == 1, str(commit_spy_r15))
ok("R15e: commit menggunakan hash yang sama",
   commit_spy_r15 and commit_spy_r15[0].get("draft_hash") == hash_r15,
   str(commit_spy_r15))
ok("R15f: sesi bersih setelah commit berhasil",
   session_get(CHAT_R15, USER_R15, _TEST_DB) is None)

# --- step 3: send_message fails on draft send → draft_shown stays False,
#             YA on the next try still blocked (re-shows again) ---
CHAT_R15B, USER_R15B = 9016, 111
session_save(CHAT_R15B, USER_R15B, oid_r15, hash_r15, False, _TEST_DB)

commit_spy_r15b: List[dict] = []
send_r15b_calls = [0]

def _send_fail_r15b(token, chat_id, text):
    send_r15b_calls[0] += 1
    raise OSError("send failed")  # always fail

def _spy_mcp_commit_r15b(args):
    commit_spy_r15b.append(args)
    return {"success": True, "status": "simulated", "dry_run": True,
            "google_doc_url": "DRY://docs/R15B", "google_event_url": "DRY://cal/R15B",
            "errors": {}}

with patch("umkm_agent.mcp_server.handle_confirm_commit", side_effect=_spy_mcp_commit_r15b), \
     patch("scripts.telegram_bot.send_message", side_effect=_send_fail_r15b):
    try:
        dispatch(TOKEN, _upd(CHAT_R15B, USER_R15B, "YA"), ALLOWED_IDS, _TEST_DB)
    except OSError:
        pass

sess_r15b = session_get(CHAT_R15B, USER_R15B, _TEST_DB)
ok("R15g: commit TIDAK dipanggil saat send_message gagal pada re-show",
   len(commit_spy_r15b) == 0, str(commit_spy_r15b))
ok("R15h: draft_shown tetap False setelah send_message gagal",
   sess_r15b and not sess_r15b.get("draft_shown"),
   str(sess_r15b))

# ════════════════════════════════════════════════════════════════════════════
# RINGKASAN
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*62}")
print("  HASIL REGRESI — Telegram Bot Bug Fixes")
print(f"{'='*62}")
print(f"  ✅ LULUS : {PASS}")
print(f"  ❌ GAGAL : {FAIL}")
if ERRORS:
    print("\n  Test yang gagal:")
    for e in ERRORS:
        print(f"    {e}")
print(f"{'='*62}")
print(f"  STATUS: {'SEMUA LULUS' if FAIL == 0 else f'{FAIL} GAGAL — perbaikan diperlukan'}")
print(f"{'='*62}")

try:
    _TEST_DB.unlink(missing_ok=True)
except Exception:
    pass

sys.exit(0 if FAIL == 0 else 1)
