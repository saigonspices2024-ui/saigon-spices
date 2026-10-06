#!/usr/bin/env python3
"""
Saigon Spices Order — khách quét QR trên bàn, gọi món VÀ TRẢ TIỀN TRƯỚC trên điện thoại.

Luồng: /?t=21 -> menu thật từ Square (cây menu "QR Order" + "Lunch Special") ->
giỏ -> checkout (Have here / Take away, tên, ghi chú) -> SDK Square trên trình duyệt
sinh mã thẻ dùng 1 lần (thẻ / Apple Pay / Google Pay) -> POST /api/checkout ->
server tạo đơn (ticket_name = "Table 21") rồi thu tiền đơn đó -> báo KDS lên bếp.

Thẻ bị từ chối -> huỷ đơn ngay, KHÔNG có gì lên bếp (KDS cũng chỉ hiện đơn QR đã trả).

Chạy bằng thư viện chuẩn:  python3 order/server.py
"""

import json
import os
import re
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote_plus

import square_api

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Australia/Sydney")
except Exception:
    _TZ = None

PORT = int(os.environ.get("PORT", "5454"))
HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(HERE, "public")

# Cây menu trên Square: "QR Order" (cả ngày) + "Lunch Special" (T2–T6 11–15h).
MENU_ROOT = os.environ.get("ORDER_MENU_ROOT", "CPPDE6XQEG3EZ7T7V4KES56M").strip()
LUNCH_ROOT = os.environ.get("ORDER_LUNCH_ROOT", "YGN5OJG6L57AMEHRYRXBLNKS").strip()
DINING_LIST_NAME = os.environ.get("ORDER_DINING_LIST", "Have here or Take away")

# Giờ nhận đơn QR (giờ Sydney) và giờ bán Lunch Special. Dạng "HH:MM-HH:MM".
OPEN_HOURS = os.environ.get("ORDER_OPEN_HOURS", "11:00-20:30")
LUNCH_HOURS = os.environ.get("ORDER_LUNCH_HOURS", "11:00-15:00")
LUNCH_DAYS = os.environ.get("ORDER_LUNCH_DAYS", "0,1,2,3,4")          # 0 = thứ Hai
ALWAYS_OPEN = os.environ.get("ORDER_ALWAYS_OPEN", "").strip() in ("1", "true", "yes")

# Số bàn theo floor plan POS Saigon: 10–14, 21–24, 31–34. "TA" = QR takeaway quầy.
TABLES_SPEC = os.environ.get("ORDER_TABLES", "10-14,21-24,31-34")
TAKEAWAY_CODES = [s.strip().upper() for s in
                  os.environ.get("ORDER_TAKEAWAY_CODES", "TA,TAKEAWAY").split(",") if s.strip()]

KDS_INGEST_URL = os.environ.get("KDS_INGEST_URL",
                                "https://saigon-kds.onrender.com/api/ingest").strip()
MAX_LINES = 30


def _parse_tables(spec):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                out.extend(str(i) for i in range(int(a), int(b) + 1))
            except ValueError:
                pass
        elif part:
            out.append(part.upper())
    return out


TABLES = _parse_tables(TABLES_SPEC)
TABLE_SET = set(TABLES)


def normalize_table(raw):
    """'21', 'table 21', 'T21' -> '21'; 'ta', 'takeaway' -> 'TA'. Không hợp lệ -> None."""
    if not raw:
        return None
    s = re.sub(r"^(?:table|tbl|t)\s*(?=\d)", "", str(raw).strip(), flags=re.I)
    s = re.sub(r"[\s\-_]", "", s).upper()
    if s in TABLE_SET:
        return s
    if s in TAKEAWAY_CODES or re.match(r"^TA\d*$", s):
        return "TA"
    return None


def _now():
    return datetime.now(_TZ) if _TZ else datetime.now()


def _in_window(spec, now):
    try:
        a, b = spec.split("-")
        ah, am = (int(x) for x in a.split(":"))
        bh, bm = (int(x) for x in b.split(":"))
    except ValueError:
        return True
    m = now.hour * 60 + now.minute
    return ah * 60 + am <= m < bh * 60 + bm


def ordering_open(now=None):
    if ALWAYS_OPEN:
        return True
    return _in_window(OPEN_HOURS, now or _now())


def lunch_open(now=None):
    if ALWAYS_OPEN:
        return True
    now = now or _now()
    days = {int(d) for d in LUNCH_DAYS.split(",") if d.strip().isdigit()}
    return now.weekday() in days and _in_window(LUNCH_HOURS, now)


# ---------------------------------------------------------------------------
# Menu (cache RAM, làm mới mỗi 3 phút)
# ---------------------------------------------------------------------------
_menu_lock = threading.Lock()
_menu = {"groups": [], "dining": {}, "fetched_at": 0, "error": None}
_items = {}        # item_id -> item (đã build) + "lunch": bool
_variations = {}   # variation_id -> (item_id, variation)


def refresh_menu(force=False):
    cfg = square_api.get_config()
    if not cfg["token"]:
        with _menu_lock:
            _menu["error"] = "Square not configured"
        return
    with _menu_lock:
        if not force and time.time() - _menu["fetched_at"] < 60:
            return
    try:
        roots = [(MENU_ROOT, "main")]
        if LUNCH_ROOT:
            roots.insert(0, (LUNCH_ROOT, "lunch"))
        data = square_api.fetch_menu(cfg["token"], cfg["env"], cfg["location_id"],
                                     roots, DINING_LIST_NAME)
    except Exception as e:
        with _menu_lock:
            _menu["error"] = str(e)
        print("[MENU] lỗi đọc catalog: %s" % e, flush=True)
        return
    items, variations = {}, {}
    for g in data["groups"]:
        for it in g["items"]:
            # Món nằm trong nhóm lunch -> khoá theo giờ lunch (trừ khi cũng có ở nhóm thường).
            prev = items.get(it["id"])
            lunch = g["tag"] == "lunch" and (prev is None or prev.get("lunch"))
            items[it["id"]] = dict(it, lunch=lunch)
            for v in it["variations"]:
                variations[v["id"]] = (it["id"], v)
    with _menu_lock:
        _menu.update(groups=data["groups"], dining=data["dining"],
                     fetched_at=time.time(), error=None)
        _items.clear(); _items.update(items)
        _variations.clear(); _variations.update(variations)
    print("[MENU] %d nhóm, %d món" % (len(data["groups"]), len(items)), flush=True)


def menu_snapshot():
    cfg = square_api.get_config()
    with _menu_lock:
        groups, err = _menu["groups"], _menu["error"]
    return {"groups": groups, "error": err, "currency": "AUD",
            "open": ordering_open(), "lunch_open": lunch_open(),
            "open_hours": OPEN_HOURS, "lunch_hours": LUNCH_HOURS,
            "app_id": cfg["app_id"], "location_id": cfg["location_id"],
            "env": cfg["env"]}


# ---------------------------------------------------------------------------
# Kiểm giỏ hàng — KHÔNG tin giá/id từ trình duyệt, tính lại theo catalog
# ---------------------------------------------------------------------------
def build_lines(raw, dining):
    """raw: [{variation_id, qty, modifiers:[id], note}] -> (line_items, tổng tiền, lỗi)."""
    if not isinstance(raw, list) or not raw:
        return None, 0, "Your cart is empty."
    if len(raw) > MAX_LINES:
        return None, 0, "Too many items in one order."
    with _menu_lock:
        dining_cfg = dict(_menu["dining"] or {})
        items, variations = dict(_items), dict(_variations)
    dining_mod = dining_cfg.get("away_id" if dining == "away" else "here_id")
    is_lunch = lunch_open()
    lines, total = [], 0
    for r in raw:
        if not isinstance(r, dict):
            return None, 0, "Invalid cart."
        hit = variations.get(r.get("variation_id"))
        if not hit:
            return None, 0, "An item in your cart just changed. Please refresh the page."
        item = items[hit[0]]
        v = hit[1]
        if v.get("sold_out"):
            return None, 0, "%s is sold out, sorry!" % item["name"]
        if item.get("lunch") and not is_lunch:
            return None, 0, "%s is only available Mon–Fri %s." % (item["name"], LUNCH_HOURS)
        try:
            qty = int(r.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1
        if not 1 <= qty <= 20:
            return None, 0, "Quantity must be 1–20."
        chosen = [str(x) for x in (r.get("modifiers") or []) if x]
        unit = v["price"]
        mods = []
        used = set()
        for ml in item["modifiers"]:
            opts = {o["id"]: o for o in ml["options"]}
            picked = [c for c in chosen if c in opts]
            if len(picked) < ml["min"] or len(picked) > ml["max"]:
                return None, 0, "Please choose %s for %s." % (ml["name"], item["name"])
            for c in picked:
                unit += opts[c]["price"]
                mods.append({"catalog_object_id": c, "quantity": "1"})
                used.add(c)
        if set(chosen) - used:
            return None, 0, "An option in your cart just changed. Please refresh the page."
        if item.get("has_dining") and dining_mod:
            mods.insert(0, {"catalog_object_id": dining_mod, "quantity": "1"})
        li = {"catalog_object_id": v["id"], "quantity": str(qty)}
        if mods:
            li["modifiers"] = mods
        note = (r.get("note") or "").strip()[:200]
        if note:
            li["note"] = note
        lines.append(li)
        total += unit * qty
    return lines, total, None


# ---------------------------------------------------------------------------
# Chống spam
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_hits = {}


def rate_ok(key, limit=8, window=60):
    now = time.time()
    with _rate_lock:
        h = [t for t in _hits.get(key, []) if now - t < window]
        if len(h) >= limit:
            _hits[key] = h
            return False
        h.append(now)
        _hits[key] = h
        return True


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------
DECLINE_MSG = {
    "CARD_DECLINED": "Your card was declined. Please try another card.",
    "INSUFFICIENT_FUNDS": "Insufficient funds. Please try another card.",
    "CVV_FAILURE": "The security code (CVV) is incorrect.",
    "ADDRESS_VERIFICATION_FAILURE": "Postcode check failed. Please check your card details.",
    "INVALID_EXPIRATION": "The card expiry date is invalid.",
    "EXPIRATION_FAILURE": "The card has expired.",
    "GENERIC_DECLINE": "Your card was declined. Please try another card.",
    "CARD_DECLINED_VERIFICATION_REQUIRED": "Your bank needs extra verification. Please try again.",
    "INVALID_CARD": "Card details are invalid. Please check and try again.",
}


def _notify_kds(order_id):
    if not KDS_INGEST_URL or not order_id:
        return

    def go():
        try:
            req = urllib.request.Request(KDS_INGEST_URL,
                                         data=json.dumps({"order_id": order_id}).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print("[KDS-PUSH] lỗi (poll KDS sẽ bắt): %s" % e, flush=True)

    threading.Thread(target=go, daemon=True).start()


def checkout(body):
    cfg = square_api.get_config()
    if not (cfg["token"] and cfg["location_id"]):
        return {"ok": False, "message": "Ordering is not set up yet. Please order at the counter."}, 503
    if not ordering_open():
        return {"ok": False, "message": "Sorry, QR ordering is open %s daily." % OPEN_HOURS}, 400

    table = normalize_table(body.get("t"))
    if not table:
        return {"ok": False, "message": "Please rescan the QR code on your table."}, 400
    dining = "away" if (table == "TA" or body.get("dining") == "away") else "here"
    name = re.sub(r"\s+", " ", (body.get("name") or "").strip())[:40]
    if not name:
        return {"ok": False, "message": "Please enter your name."}, 400
    phone = (body.get("phone") or "").strip()[:30]
    email = (body.get("email") or "").strip()[:120]
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        email = ""
    note = (body.get("note") or "").strip()[:300]
    source_id = (body.get("source_id") or "").strip()
    if not source_id:
        return {"ok": False, "message": "Payment details missing."}, 400
    idem = re.sub(r"[^A-Za-z0-9\-]", "", str(body.get("idem") or ""))[:40] or uuid.uuid4().hex

    refresh_menu()
    lines, expected, err = build_lines(body.get("items"), dining)
    if err:
        return {"ok": False, "message": err}, 400
    try:
        client_total = int(body.get("total"))
    except (TypeError, ValueError):
        client_total = -1
    if client_total != expected:
        return {"ok": False, "code": "TOTAL_CHANGED",
                "message": "Prices just changed. Please review your cart."}, 409

    if dining == "here":
        ticket = "Table %s" % table
    else:
        ticket = "Takeaway %s" % name + ("" if table == "TA" else " (T%s)" % table)
    meta = {"qr_app": square_api.APP_TAG, "qr_table": table, "qr_name": name,
            "qr_dining": dining}
    if phone:
        meta["qr_phone"] = phone

    try:
        order = square_api.create_order(cfg["token"], cfg["env"], cfg["location_id"],
                                        ticket, lines, meta, name, note, idem + "-o")
    except square_api.SquareError as e:
        print("[ORDER] tạo đơn lỗi: %s" % e, flush=True)
        return {"ok": False, "message": "Couldn't create your order. Please try again."}, 502
    oid = order.get("id")
    due = int(((order.get("net_amount_due_money") or order.get("total_money") or {})
               .get("amount")) or 0)
    if due != expected:
        # Square tính khác (thuế/giảm giá tự động…) -> không thu số tiền khách chưa thấy.
        print("[ORDER] %s lệch tổng: square=%s app=%s" % (oid, due, expected), flush=True)
        try:
            square_api.cancel_order(cfg["token"], cfg["env"], oid)
        except Exception:
            pass
        return {"ok": False, "code": "TOTAL_CHANGED",
                "message": "Prices just changed. Please review your cart."}, 409

    try:
        pay = square_api.create_payment(
            cfg["token"], cfg["env"], cfg["location_id"], oid, due, "AUD", source_id,
            (body.get("verification_token") or "").strip() or None, idem + "-p",
            note="QR %s - %s" % (ticket, name), buyer_email=email or None)
    except square_api.SquareError as e:
        code = next((x.get("code") for x in e.errors if x.get("code") in DECLINE_MSG), None)
        print("[PAY] %s thất bại: %s" % (oid, e), flush=True)
        try:
            square_api.cancel_order(cfg["token"], cfg["env"], oid)
        except Exception as ce:
            print("[PAY] huỷ đơn %s lỗi: %s" % (oid, ce), flush=True)
        msg = DECLINE_MSG.get(code) or "Payment didn't go through. Please try again."
        return {"ok": False, "code": code or "PAYMENT_FAILED", "message": msg}, 402

    if pay.get("status") not in ("COMPLETED", "APPROVED"):
        try:
            square_api.cancel_order(cfg["token"], cfg["env"], oid)
        except Exception:
            pass
        return {"ok": False, "message": "Payment didn't go through. Please try again."}, 402

    _notify_kds(oid)
    print("[ORDER] ✅ %s | %s | %d dòng | $%.2f | %s" %
          (ticket, name, len(lines), due / 100, oid), flush=True)
    return {"ok": True, "order_id": oid, "ticket": ticket, "table": table,
            "dining": dining, "total": due,
            "receipt_url": pay.get("receipt_url"),
            "receipt_number": pay.get("receipt_number")}, 200


# ---------------------------------------------------------------------------
# Dọn đơn mồ côi: đơn QR đã tạo mà chưa thu được tiền (mất mạng giữa chừng…)
# ---------------------------------------------------------------------------
def _sweep_loop():
    while True:
        time.sleep(300)
        cfg = square_api.get_config()
        if not (cfg["token"] and cfg["location_id"]):
            continue
        try:
            for o in square_api.search_open_qr_orders(cfg["token"], cfg["env"], cfg["location_id"]):
                due = int((o.get("net_amount_due_money") or {}).get("amount") or 0)
                created = o.get("created_at") or ""
                try:
                    age = time.time() - datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S") \
                        .replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
                if due > 0 and age > 900:
                    square_api.cancel_order(cfg["token"], cfg["env"], o["id"])
                    print("[SWEEP] huỷ đơn QR chưa trả %s" % o["id"], flush=True)
        except Exception as e:
            print("[SWEEP] lỗi: %s" % e, flush=True)


def _menu_loop():
    while True:
        try:
            refresh_menu(force=True)
        except Exception as e:
            print("[MENU] lỗi vòng: %s" % e, flush=True)
        time.sleep(180)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon",
                ".webmanifest": "application/manifest+json"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body, code=200, ctype="application/json; charset=utf-8", cache=False):
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400" if cache
                         else "no-cache, must-revalidate")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), code)

    def _file(self, rel):
        full = os.path.normpath(os.path.join(PUBLIC, rel.lstrip("/")))
        if not full.startswith(PUBLIC) or not os.path.isfile(full):
            return self._send(b"Not Found", 404, "text/plain")
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            body = f.read()
        self._send(body, 200, STATIC_TYPES.get(ext, "application/octet-stream"),
                   cache=ext in (".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico"))

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n or n > 100_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._file("index.html")
        if path in ("/healthz", "/api/health"):
            return self._json({"ok": True, "items": len(_items), "open": ordering_open()})
        if path == "/api/menu":
            refresh_menu()
            return self._json(menu_snapshot())
        if path == "/.well-known/apple-developer-merchantid-domain-association":
            # File xác minh tên miền Apple Pay (tải từ Square Developer -> Apple Pay).
            return self._file("apple-developer-merchantid-domain-association")
        return self._file(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/checkout":
            if not rate_ok("ip:" + self._ip(), limit=10):
                return self._json({"ok": False, "message": "Too many attempts. Please wait a minute."}, 429)
            try:
                result, code = checkout(self._body())
            except Exception as e:
                print("[ORDER] lỗi bất ngờ: %r" % e, flush=True)
                result, code = {"ok": False, "message": "Something went wrong. Please try again."}, 500
            return self._json(result, code)
        if path == "/api/refresh-menu":
            refresh_menu(force=True)
            return self._json({"ok": True})
        return self._send(b"Not Found", 404, "text/plain")


class Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


def main():
    threading.Thread(target=_menu_loop, daemon=True).start()
    threading.Thread(target=_sweep_loop, daemon=True).start()
    print("Saigon Spices Order chạy tại http://localhost:%d/?t=%s" % (PORT, TABLES[0]), flush=True)
    print("  • Bàn: %s + TA (takeaway)" % ", ".join(TABLES), flush=True)
    Server(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
