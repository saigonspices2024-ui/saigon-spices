"""
Nối Square Orders API bằng thư viện chuẩn (urllib) — không cần cài SDK.

- Đọc access token từ biến môi trường hoặc file kds/.env (SQUARE_ACCESS_TOKEN).
- Poll các đơn OPEN ở location của seller mỗi vài giây rồi đẩy vào KDS.
- Token bí mật KHÔNG bao giờ bị log ra.
"""

import datetime
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, ".env")
SQUARE_VERSION = "2024-12-18"

BASE = {
    "sandbox": "https://connect.squareupsandbox.com",
    "production": "https://connect.squareup.com",
}

# KDS chỉ đọc đơn OPEN tạo trong ngần này giờ gần đây (bỏ qua đơn cũ chưa đóng
# tồn trong tài khoản). 0 = không lọc (đọc mọi đơn OPEN, kiểu cũ). Mặc định 24h.
def order_window_start():
    try:
        hours = float(os.environ.get("KDS_MAX_ORDER_AGE_HOURS", "24"))
    except ValueError:
        hours = 24.0
    if hours <= 0:
        return None
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Cấu hình (đọc từ .env cục bộ — token không đi qua chat)
# ---------------------------------------------------------------------------
def _read_env_file():
    cfg = {}
    if os.path.isfile(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def get_config():
    cfg = _read_env_file()
    return {
        "token": os.environ.get("SQUARE_ACCESS_TOKEN") or cfg.get("SQUARE_ACCESS_TOKEN", ""),
        "env": (os.environ.get("SQUARE_ENV") or cfg.get("SQUARE_ENV") or "sandbox").lower(),
        # Mã máy Square Terminal đã ghép (để thu thẻ ngay trên Expo). Chủ dán vào
        # Render env; thiếu thì nút Thẻ báo "chưa ghép máy", nút tiền mặt vẫn chạy.
        "device_id": os.environ.get("SQUARE_DEVICE_ID") or cfg.get("SQUARE_DEVICE_ID", ""),
    }


def save_token(token, env="sandbox"):
    """Ghi token vào .env cục bộ. Gọi từ trang /setup do chính chủ nhập."""
    cfg = _read_env_file()
    cfg["SQUARE_ACCESS_TOKEN"] = token.strip()
    cfg["SQUARE_ENV"] = env
    with open(ENV_FILE, "w") as f:
        f.write("# Khóa Square — KHÔNG commit / chia sẻ file này\n")
        for k, v in cfg.items():
            f.write(f"{k}={v}\n")
    os.chmod(ENV_FILE, 0o600)


def is_configured():
    return bool(get_config()["token"])


# ---------------------------------------------------------------------------
# Gọi API
# ---------------------------------------------------------------------------
def _request(method, path, token, env, body=None):
    url = BASE.get(env, BASE["sandbox"]) + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Square-Version", SQUARE_VERSION)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def list_locations(token, env):
    return _request("GET", "/v2/locations", token, env).get("locations", [])


def search_open_orders(token, env, location_ids, since_rfc3339=None):
    """Đơn đang OPEN. `since_rfc3339` != None -> chỉ lấy đơn TẠO từ mốc đó tới nay.

    Tài khoản đang chạy có thể tồn hàng trăm đơn OPEN cũ chưa đóng (thói quen POS
    không đóng bill) -> KDS đọc hết sẽ tràn màn bếp. Lọc theo created_at để chỉ
    hiện đơn của ca hiện tại. Mốc do server tính (xem KDS_MAX_ORDER_AGE_HOURS).
    """
    filt = {"state_filter": {"states": ["OPEN"]}}
    query = {"filter": filt}
    if since_rfc3339:
        filt["date_time_filter"] = {"created_at": {"start_at": since_rfc3339}}
        query["sort"] = {"sort_field": "CREATED_AT", "sort_order": "DESC"}
    body = {"location_ids": location_ids, "query": query, "limit": 100}
    return _request("POST", "/v2/orders/search", token, env, body).get("orders", [])


def search_closed_orders(token, env, location_ids, start_rfc3339):
    """Đơn đã ĐÓNG (COMPLETED/CANCELED) từ mốc thời gian tới nay — cho màn History
    sống qua cả restart/ngủ (nguồn bền = chính Square, không phụ thuộc RAM).

    Chỉ chạy được vì đơn giờ hoàn tất = đã trả tiền + fulfillment COMPLETED ->
    Square chuyển state COMPLETED (trước đây dine-in trả cuối bữa nên đơn ở OPEN,
    query này ra rỗng; nay hết lý do đó)."""
    body = {
        "location_ids": location_ids,
        "query": {
            "filter": {
                "state_filter": {"states": ["COMPLETED", "CANCELED"]},
                "date_time_filter": {"closed_at": {"start_at": start_rfc3339}},
            },
            "sort": {"sort_field": "CLOSED_AT", "sort_order": "DESC"},
        },
        "limit": 200,
    }
    return _request("POST", "/v2/orders/search", token, env, body).get("orders", [])


def search_recent_completed(token, env, location_ids, minutes):
    """Đơn vừa COMPLETED (đã trả tiền + đóng) trong `minutes` phút gần đây.

    ⭐ COUNTER-SERVICE (Saigon): khách trả TẠI POS -> Square ĐÓNG đơn ngay (rời OPEN).
    KDS chỉ đọc đơn OPEN nên đơn trả nhanh KHÔNG kịp bắt -> bếp không thấy. Đọc thêm
    đơn vừa-đóng gần đây làm VÉ NẤU (đơn đã trả nhưng bếp chưa làm). Chỉ COMPLETED,
    KHÔNG lấy CANCELED (đơn huỷ không phải để nấu). Bắt 1 lần là đủ — sau đó vé nằm
    lại nhờ _resolve_vanished giữ, tới khi bếp bấm Done."""
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)
    start = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "location_ids": location_ids,
        "query": {
            "filter": {
                "state_filter": {"states": ["COMPLETED"]},
                "date_time_filter": {"closed_at": {"start_at": start}},
            },
            "sort": {"sort_field": "CLOSED_AT", "sort_order": "DESC"},
        },
        "limit": 100,
    }
    return _request("POST", "/v2/orders/search", token, env, body).get("orders", [])


def _catalog_list(token, env, obj_type):
    """Lấy toàn bộ object 1 loại từ Catalog API (tự lật trang qua cursor)."""
    objs = []
    cursor = None
    while True:
        path = "/v2/catalog/list?types=" + obj_type + (("&cursor=" + cursor) if cursor else "")
        r = _request("GET", path, token, env)
        objs.extend(r.get("objects", []))
        cursor = r.get("cursor")
        if not cursor:
            return objs


def fetch_station_map(token, env, station_names):
    """Xây map catalog_object_id -> tên trạm, dựa trên category của từng món.

    Mỗi món trong Square có thể gắn nhiều category (1 mục menu + 1 trạm bếp).
    Ta tìm category nào của món trùng tên trạm (Larder/Pan/Grill) rồi map cả
    item id lẫn mọi variation id về trạm đó. Khớp tên KHÔNG phân biệt hoa/thường.
    Trả {} nếu lỗi để KDS vẫn chạy (món chưa gắn trạm coi như 'chưa phân trạm').
    """
    stations_lc = {s.strip().lower(): s for s in station_names if s.strip()}
    try:
        cats = {}  # category id -> name
        for o in _catalog_list(token, env, "CATEGORY"):
            if o.get("type") == "CATEGORY":
                cats[o["id"]] = (o.get("category_data") or {}).get("name", "")

        m = {}
        for o in _catalog_list(token, env, "ITEM"):
            if o.get("type") != "ITEM":
                continue
            idata = o.get("item_data") or {}
            cat_ids = []
            for c in (idata.get("categories") or []):
                if c.get("id"):
                    cat_ids.append(c["id"])
            if idata.get("category_id"):                       # kiểu cũ
                cat_ids.append(idata["category_id"])
            rc = (idata.get("reporting_category") or {}).get("id")
            if rc:
                cat_ids.append(rc)

            station = None
            for cid in cat_ids:
                nm = cats.get(cid, "").strip().lower()
                if nm in stations_lc:
                    station = stations_lc[nm]
                    break
            if not station:
                continue
            m[o["id"]] = station
            for v in (idata.get("variations") or []):
                if v.get("id"):
                    m[v["id"]] = station
        return m
    except Exception:
        return {}


def create_test_order(token, env, line_items, ticket_name=None):
    """Tạo một đơn OPEN thật trong Square (sandbox) để thử vòng chạy KDS."""
    locs = list_locations(token, env)
    if not locs:
        raise RuntimeError("Không có location nào trong tài khoản")
    currency = locs[0].get("currency", "AUD")
    # Square bắt buộc mỗi món ad-hoc phải có giá; thêm giá mẫu nếu thiếu.
    items = []
    for it in line_items:
        it = dict(it)
        it.setdefault("base_price_money", {"amount": 1000, "currency": currency})
        items.append(it)
    pickup_at = (datetime.datetime.utcnow() + datetime.timedelta(minutes=30)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
    order = {
        "location_id": locs[0]["id"],
        "line_items": items,
        "state": "OPEN",
        # kèm fulfillment để KDS đẩy trạng thái bếp (PROPOSED->PREPARED->COMPLETED)
        "fulfillments": [{
            "type": "PICKUP",
            "state": "PROPOSED",
            "pickup_details": {
                "recipient": {"display_name": ticket_name or "Khách"},
                "schedule_type": "ASAP",
                "pickup_at": pickup_at,
            },
        }],
    }
    if ticket_name:
        order["ticket_name"] = ticket_name
    body = {"order": order, "idempotency_key": uuid.uuid4().hex}
    return _request("POST", "/v2/orders", token, env, body).get("order", {})


def advance_fulfillment(token, env, order_id, target_state):
    """Đẩy fulfillment đầu tiên của đơn sang trạng thái mới (PREPARED/COMPLETED).
    Trả dict {ok, message} — không ném lỗi để bump ở KDS luôn mượt."""
    try:
        o = retrieve_order(token, env, order_id)
    except Exception as e:
        return {"ok": False, "message": "couldn't read order: %s" % e}
    ffs = o.get("fulfillments") or []
    if not ffs:
        return {"ok": False, "message": "order has no fulfillment to update"}
    body = {"order": {"version": o.get("version"),
                      "fulfillments": [{"uid": ffs[0]["uid"], "state": target_state}]},
            "idempotency_key": uuid.uuid4().hex}
    try:
        o2 = _request("PUT", "/v2/orders/" + order_id, token, env, body).get("order", {})
        st = (o2.get("fulfillments") or [{}])[0].get("state")
        return {"ok": True, "message": "fulfillment -> %s" % st}
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": "HTTP %s: %s" % (e.code, e.read().decode()[:140])}


def retrieve_order(token, env, order_id):
    return _request("GET", "/v2/orders/" + order_id, token, env).get("order", {})


def complete_order(token, env, order_id):
    """Đánh dấu đơn Square = COMPLETED (khi expo bấm 'Đã giao')."""
    o = retrieve_order(token, env, order_id)
    version = o.get("version")
    body = {"order": {"version": version, "state": "COMPLETED"},
            "idempotency_key": uuid.uuid4().hex}
    return _request("PUT", "/v2/orders/" + order_id, token, env, body).get("order", {})


def append_line_items(token, env, order_id, line_items):
    """Nối thêm món vào một đơn đang mở (dùng khi GỘP bill để thu chung 1 lần).

    UpdateOrder nhận object thưa: chỉ gửi món MỚI, Square tự nối vào danh sách cũ.
    Phải kèm version hiện tại (chống ghi đè khi có sửa song song)."""
    current = retrieve_order(token, env, order_id)
    body = {"order": {"version": current.get("version"), "line_items": line_items},
            "idempotency_key": uuid.uuid4().hex}
    return _request("PUT", "/v2/orders/" + order_id, token, env, body).get("order", {})


def cancel_order(token, env, order_id):
    """Huỷ một đơn Square. Dùng khi GỘP bill: món của bill phụ đã được nối sang
    bill chính, giờ huỷ bill phụ (rỗng) đi cho sổ sạch — 1 giao dịch, 1 đơn.

    Square từ chối đưa đơn về CANCELED nếu còn fulfillment đang treo, nên phải
    huỷ fulfillment TRONG CÙNG lệnh cập nhật."""
    current = retrieve_order(token, env, order_id)
    order = {"version": current.get("version"), "state": "CANCELED"}
    ffs = [{"uid": f["uid"], "state": "CANCELED"}
           for f in (current.get("fulfillments") or [])
           if f.get("uid") and f.get("state") not in ("COMPLETED", "CANCELED", "FAILED")]
    if ffs:
        order["fulfillments"] = ffs
    body = {"order": order, "idempotency_key": uuid.uuid4().hex}
    return _request("PUT", "/v2/orders/" + order_id, token, env, body).get("order", {})


# ---------------------------------------------------------------------------
# Thanh toán ngay trên Expo (đơn QR do app order tạo)
# ---------------------------------------------------------------------------
# Khớp order/square_api.py: đơn QR gắn metadata qr_app="saigon-qr". Chỉ những đơn
# này mới cho thu tiền qua Expo — không đụng đơn bấm trên POS (POS tự thu).
QR_APP_TAG = "saigon-qr"


def is_qr_order(order):
    return (order.get("metadata") or {}).get("qr_app") == QR_APP_TAG


def net_amount_due(order):
    """Còn phải thu bao nhiêu. Đơn trả đủ tiền VẪN ở state OPEN chừng nào
    fulfillment chưa xong, nên không dùng state để biết đã trả — net_amount_due
    mới nói thật. Thiếu trường (Square-Version cũ) thì lùi về tổng đơn."""
    m = order.get("net_amount_due_money")
    if isinstance(m, dict) and m.get("amount") is not None:
        return {"amount": int(m["amount"]), "currency": m.get("currency", "AUD")}
    t = order.get("total_money") or {}
    return {"amount": int(t.get("amount") or 0), "currency": t.get("currency", "AUD")}


def create_cash_payment(token, env, location_id, order_id, amount, received, note=None):
    """Ghi thu TIỀN MẶT vào Square (Payments API, source_id='CASH').

    buyer_supplied_money = tiền khách đưa -> Square tự tính change_back_money
    (tiền thối) và lưu vào sổ. Kèm order_id nên đơn được đánh dấu đã trả
    (net_amount_due -> 0). Hiện thành giao dịch thật ở Dashboard → Transactions.
    """
    cur = amount.get("currency", "AUD")
    payment = {
        "idempotency_key": uuid.uuid4().hex,
        "source_id": "CASH",
        "amount_money": {"amount": int(amount["amount"]), "currency": cur},
        "cash_details": {"buyer_supplied_money": {"amount": int(received), "currency": cur}},
        "order_id": order_id,
        "location_id": location_id,
    }
    if note:
        payment["note"] = note[:500]
    return _request("POST", "/v2/payments", token, env, payment).get("payment", {})


def create_terminal_checkout(token, env, device_id, order_id, amount, note=None):
    """Đẩy bill sang máy Terminal đã ghép để khách chạm thẻ. autocomplete: trả đủ
    là đơn tự khép, doanh thu vào Square như mọi giao dịch khác."""
    checkout = {
        "amount_money": {"amount": int(amount["amount"]), "currency": amount.get("currency", "AUD")},
        "order_id": order_id,
        "device_options": {"device_id": device_id, "show_itemized_cart": True,
                           "skip_receipt_screen": False},
        "payment_options": {"autocomplete": True},
    }
    if note:
        checkout["note"] = note[:500]
    body = {"idempotency_key": uuid.uuid4().hex, "checkout": checkout}
    return _request("POST", "/v2/terminals/checkouts", token, env, body).get("checkout", {})


def get_terminal_checkout(token, env, checkout_id):
    """status: PENDING / IN_PROGRESS / CANCEL_REQUESTED / CANCELED / COMPLETED."""
    return _request("GET", "/v2/terminals/checkouts/" + checkout_id, token, env).get("checkout", {})


def cancel_terminal_checkout(token, env, checkout_id):
    return _request("POST", "/v2/terminals/checkouts/%s/cancel" % checkout_id,
                    token, env, {}).get("checkout", {})


def verify(token, env):
    """Trả (ok, message) — dùng cho trang /setup kiểm tra token ngay."""
    try:
        locs = list_locations(token, env)
        names = ", ".join(l.get("name", l.get("id", "?")) for l in locs) or "(no location yet)"
        return True, f"Connected OK — {len(locs)} location(s): {names}"
    except urllib.error.HTTPError as e:
        return False, f"Token rejected (HTTP {e.code})"
    except Exception as e:
        return False, f"Connection error: {e}"


# ---------------------------------------------------------------------------
# Vòng lặp poll chạy nền
# ---------------------------------------------------------------------------
class Poller:
    def __init__(self, on_orders, interval=4, closed_lookback_min=0):
        self.on_orders = on_orders      # callback(list_of_square_orders)
        self.interval = interval
        # Counter-service: đọc thêm đơn vừa COMPLETED trong ngần này phút (0 = tắt).
        self.closed_lookback_min = closed_lookback_min
        self._stop = threading.Event()
        self._thread = None
        self.status = {"connected": False, "last": None, "error": None, "locations": []}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            cfg = get_config()
            if not cfg["token"]:
                self.status.update(connected=False, error="Chưa cấu hình token")
                self._stop.wait(self.interval)
                continue
            try:
                locs = list_locations(cfg["token"], cfg["env"])
                loc_ids = [l["id"] for l in locs]
                self.status["locations"] = [l.get("name") for l in locs]
                orders = search_open_orders(cfg["token"], cfg["env"], loc_ids,
                                            order_window_start()) if loc_ids else []
                # Counter-service: gộp thêm đơn vừa-trả (COMPLETED) gần đây làm vé nấu.
                if loc_ids and self.closed_lookback_min > 0:
                    try:
                        orders = orders + search_recent_completed(
                            cfg["token"], cfg["env"], loc_ids, self.closed_lookback_min)
                    except Exception as e:
                        print(f"[POLL] đọc đơn đã-trả lỗi (bỏ qua): {e}", flush=True)
                self.on_orders(orders)
                self.status.update(connected=True, last=time.time(), error=None)
            except urllib.error.HTTPError as e:
                self.status.update(connected=False, error=f"HTTP {e.code}")
            except Exception as e:
                self.status.update(connected=False, error=str(e))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
