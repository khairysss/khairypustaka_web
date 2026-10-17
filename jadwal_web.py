"""Logika timeline mundur /jadwal versi web (murni, tanpa Telegram).

Diekstrak dari _jadwal_flow() di bot.py (Telegram).
Pakai deadline.parse_tanggal + deadline.fmt_id yang sama persis.
"""
from datetime import date, timedelta

import deadline as tenggat

MILESTONES = [
    (60, "Finalisasi proposal / BAB I–III"),
    (45, "Mulai pengumpulan data"),
    (30, "Selesai pengumpulan data, mulai analisis"),
    (21, "BAB IV (hasil & pembahasan) jadi"),
    (14, "BAB V + abstrak jadi"),
    (7, "Revisi akhir + latihan presentasi sidang"),
    (0, "🎓 Hari-H! Semangat!"),
]


def build_timeline(text):
    """Bangun timeline mundur dari tanggal target.

    text: tanggal target (mis. '20/12/2026' atau '20 Des 2026').
    Return (ok, data). ok=True -> data dict {target, target_id, timeline}.
    ok=False -> data string pesan error.
    """
    tgl = tenggat.parse_tanggal(text)
    if not tgl:
        return False, ("Format tanggal tidak dikenal. "
                       "Contoh: 20/12/2026 atau 20 Des 2026.")
    if tgl <= date.today():
        return False, ("Tanggalnya sudah lewat/hari ini. "
                       "Masukkan tanggal ke depan ya.")
    today = date.today()
    items = []
    for d, label in MILESTONES:
        dt = tgl - timedelta(days=d)
        items.append({
            "tanggal": dt.isoformat(),
            "tanggal_id": tenggat.fmt_id(dt.isoformat()),
            "kegiatan": label,
            "terlewat": dt <= today,
            "hari_h": d == 0,
        })
    return True, {
        "target": tgl.isoformat(),
        "target_id": tenggat.fmt_id(tgl.isoformat()),
        "timeline": items,
        "catatan": ("Patokan kasar — sesuaikan dengan kecepatanmu. "
                    "Catat tanggal pentingnya lewat deadline."),
    }
