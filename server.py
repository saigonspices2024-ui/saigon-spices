#!/usr/bin/env python3
"""
Saigon Spices KDS — Kitchen Display System kết nối Square.

MVP chạy bằng Python stdlib (không cần cài thêm gì).
- 2 trạm: Bếp (kitchen) -> Expo (pass), đồng bộ real-time qua SSE.
- Dữ liệu ticket theo đúng hình dạng Square Order object, nên khi nối
  Square sandbox/production thật chỉ cần đẩy webhook vào /webhooks/square.

Máy trạng thái đơn:
    NEW      -> bếp đang nấu (hiện trên màn BẾP)
    READY    -> bếp xong, chờ chạy món (hiện trên màn EXPO)
    COMPLETED-> expo đã giao ra bàn (đẩy fulfillment COMPLETED về Square)
"""

import copy
import datetime
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import square_client

PORT = int(os.environ.get("PORT", "5252"))
HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(HERE, "public")

# ---------------------------------------------------------------------------
# Kho ticket trong bộ nhớ (MVP). Về sau thay bằng Cloudflare D1/KV.
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_tickets = {}          # id -> ticket dict
_pending_vanish = set()  # đơn vừa rời danh sách OPEN, đang hỏi Square huỷ hay đã trả tiền
# Đơn VỪA ĐÓNG bằng nút Thanh toán: Square cập nhật state COMPLETED trễ vài giây
# sau khi ghi fulfillment (async), nên poll trong khoảng đó còn thấy đơn OPEN và
# thêm lại vé -> vé "nhấp nháy" hiện lại rồi mới mất. Khoá id ở đây để poll bỏ qua.
_recently_closed = {}    # order_id -> timestamp
_CLOSED_GRACE_S = 25

# ⭐ COUNTER-SERVICE (Saigon Spices): khách trả tiền TẠI QUẦY/POS TRƯỚC rồi bếp mới
# nấu. KDS là hàng đợi nấu ăn, đơn phải NẰM LẠI tới khi bếp bấm "Done", BẤT KỂ đã
# trả tiền hay chưa. (Model Délice là dine-in: trả cuối bữa, đơn rời màn khi trả +
# giao hết — không hợp Saigon: đơn trả xong ở POS là Square đóng -> KDS xoá luôn.)
# Bật mặc định; tắt bằng env KDS_HOLD_TILL_DONE=0 nếu muốn về model dine-in.
HOLD_TILL_DONE = os.environ.get("KDS_HOLD_TILL_DONE", "1").strip().lower() not in ("0", "false", "no", "off", "")
# Đơn ĐÃ BẤM DONE (bếp nấu xong): chặn BỀN, đừng để poll thêm lại dù Square còn
# báo OPEN (đơn chưa trả ở POS vẫn OPEN). order_id -> timestamp, dọn sau TTL.
_done_ids = {}
_DONE_TTL_S = 12 * 3600
_done_lock = threading.Lock()

# ---------------------------------------------------------------------------
# NHỚ ĐƠN-ĐÃ-NẤU QUA RESTART (Upstash Redis, free) — CHỐNG SÓT ĐƠN TRẢ-NHANH
# ---------------------------------------------------------------------------
# Đơn counter-service trả tiền ngay -> Square đóng liền (COMPLETED). KDS bắt loại
# này bằng cửa sổ "đơn vừa-đóng gần đây", nhưng cửa sổ CO nhỏ lại sau mỗi restart
# (Render free ngủ/restart) -> đơn trả 5-30 phút trước bị SÓT. Nếu có Upstash thì
# _done_ids (đơn bếp đã bấm Done) được LƯU BỀN -> nới rộng cửa sổ quét an toàn (đơn
# cũ đã nấu bị lọc, chỉ đơn thật sự chưa nấu mới hiện). Không cấu hình Upstash ->
# chạy y như cũ (cửa sổ nhỏ), KHÔNG đổi hành vi gì.
_UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
_UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
_UPSTASH_STATE_KEY = os.environ.get("KDS_STATE_KEY", "saigon:kds:state").strip()
_state_save_lock = threading.Lock()
_last_state_save = 0.0
_STATE_SAVE_THROTTLE_S = 12   # ghi tối đa ~1 lần/12s (dưới hạn Upstash free 10k/ngày)


def _upstash_on():
    return bool(_UPSTASH_URL and _UPSTASH_TOKEN)


def _upstash_cmd(args, timeout=5):
    """Gọi 1 lệnh Redis qua REST Upstash. Trả field result, None nếu lỗi. Không
    raise: mất mạng thì rơi về hành vi cũ, không làm sập poll/request."""
    try:
        req = urllib.request.Request(
            _UPSTASH_URL, data=json.dumps(args).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + _UPSTASH_TOKEN,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result")
    except (urllib.error.URLError, ValueError, OSError) as e:
        print("[DONE-IDS] Upstash lỗi (%s): %s" % (args[0] if args else "?", e), flush=True)
        return None


def _save_state(force=False):
    """Ghi CẢ BẢNG đơn (đang trên màn) + _done_ids lên Upstash để sống qua restart.
    Lưu nguyên trạng thái (NEW/READY + cờ done từng món) -> restart KHÔNG mất tiến
    độ, đơn đã nấu KHÔNG hiện lại, đơn trả-nhanh bị sót vẫn được cửa-sổ-rộng bắt về.
    Bóp ga tối đa 1 lần/12s (trừ khi force) cho nhẹ Upstash. Lỗi -> bỏ qua (an toàn)."""
    if not _upstash_on():
        return
    global _last_state_save
    now = time.time()
    with _state_save_lock:
        if not force and now - _last_state_save < _STATE_SAVE_THROTTLE_S:
            return
        _last_state_save = now
    with _lock:
        board = json.dumps([t for t in _tickets.values() if t.get("state") != "COMPLETED"])
    with _done_lock:
        done = json.dumps({k: v for k, v in _done_ids.items() if now - v <= _DONE_TTL_S})
    _upstash_cmd(["SET", _UPSTASH_STATE_KEY, '{"tickets":%s,"done":%s}' % (board, done)])


def _load_state():
    """Khôi phục bảng đơn + _done_ids từ Upstash lúc khởi động (sau restart)."""
    if not _upstash_on():
        return
    raw = _upstash_cmd(["GET", _UPSTASH_STATE_KEY])
    if not raw:
        return
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    now = time.time()
    tickets = data.get("tickets") or []
    with _lock:
        for t in tickets:
            if isinstance(t, dict) and t.get("id") and t.get("state") != "COMPLETED":
                _tickets[t["id"]] = t
    with _done_lock:
        for k, v in (data.get("done") or {}).items():
            try:
                if now - float(v) <= _DONE_TTL_S:
                    _done_ids[k] = float(v)
            except (TypeError, ValueError):
                pass
    print("[STATE] khôi phục %d đơn + %d done-id từ Upstash" % (len(tickets), len(_done_ids)), flush=True)


# Counter-service: đọc thêm đơn vừa COMPLETED (đã trả ở POS) trong ngần này phút để
# bắt đơn trả-nhanh (Square đóng đơn ngay -> KDS đọc OPEN không kịp). Bắt 1 lần là
# đủ (sau đó vé nằm lại nhờ _resolve_vanished giữ). 0 = tắt. Chỉ bật khi HOLD_TILL_DONE.
try:
    CLOSED_LOOKBACK_MIN = int(os.environ.get("KDS_CLOSED_LOOKBACK_MIN", "30"))
except ValueError:
    CLOSED_LOOKBACK_MIN = 30
if not HOLD_TILL_DONE:
    CLOSED_LOOKBACK_MIN = 0
# Có Upstash (nhớ đơn-đã-nấu bền) -> nới cửa sổ rộng + BỎ co-window sau restart, để
# bắt lại đơn trả-nhanh bị sót lúc server ngủ/restart. An toàn vì _done_ids bền lọc
# đơn đã nấu. Không có Upstash -> giữ buffer nhỏ 5 phút như cũ (tránh đơn cũ hiện lại).
if _upstash_on() and CLOSED_LOOKBACK_MIN > 0:
    CLOSED_LOOKBACK_MIN = max(CLOSED_LOOKBACK_MIN, 60)
    CLOSED_STARTUP_BUFFER_MIN = CLOSED_LOOKBACK_MIN
else:
    CLOSED_STARTUP_BUFFER_MIN = 5
# Vé chính vừa được GỘP thêm món của vé phụ (lúc thu chung bằng thẻ): poll kế tiếp
# sẽ thấy món lạ nhảy vào -> ĐỪNG kêu chuông "gọi thêm", đồ đã ra bàn rồi. Đánh
# dấu id vé chính tới hạn này để _merge_items bỏ qua cờ added.
_merge_suppress = {}     # ticket_id -> expiry timestamp
_MERGE_SUPPRESS_S = 30
_subscribers = set()   # các Queue của client SSE đang kết nối

# Nhật ký đơn ĐÃ RỜI MÀN (served / cancelled) — cho màn History. Ghi ngay lúc
# Expo bấm Served, KHÔNG chờ Square đóng đơn (đơn dine-in trả tiền cuối bữa nên
# Square còn để OPEN — không thể dựa vào 'đơn đã đóng' của Square). Trong bộ nhớ:
# giờ mở cửa tablet poll liên tục nên server thức, nhật ký còn nguyên; ngủ qua
# đêm thì reset (History chỉ hiện 'hôm nay' nên không sao). Bản lưu vĩnh viễn
# vẫn nằm ở Square Dashboard.
_history = []          # list snapshot đơn, mới thêm ở cuối
_history_lock = threading.Lock()
_HISTORY_MAX = 500

# Các trạm bếp (khớp tên category trong Square). Đổi được qua env KDS_STATIONS.
# Saigon Spices chạy MỘT bếp (không chia trạm) -> mặc định rỗng: màn Kitchen là
# một màn "All" hiện mọi món, khỏi cần category trạm trong Square. Muốn chia trạm
# thì đặt env KDS_STATIONS="Larder,Pan,Grill".
STATIONS = [s.strip() for s in os.environ.get("KDS_STATIONS", "").split(",") if s.strip()]

# Map catalog_object_id -> tên trạm, làm mới định kỳ từ Square Catalog.
_station_map = {}
_station_map_lock = threading.Lock()


def get_station_of(catalog_object_id):
    if not catalog_object_id:
        return None
    with _station_map_lock:
        return _station_map.get(catalog_object_id)


def refresh_station_map():
    if not STATIONS:            # 1 bếp: không phân trạm -> khỏi gọi Catalog
        return
    cfg = square_client.get_config()
    if not cfg["token"]:
        return
    m = square_client.fetch_station_map(cfg["token"], cfg["env"], STATIONS)
    if m:
        with _station_map_lock:
            _station_map.clear()
            _station_map.update(m)


def _now_ms():
    return int(time.time() * 1000)


def _sydney_tz():
    """Múi giờ quán. Ưu tiên zoneinfo (đúng cả giờ mùa hè); container thiếu
    tzdata thì lùi về offset cố định KDS_UTC_OFFSET (mặc định +10 = AEST)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.environ.get("KDS_TZ", "Australia/Sydney"))
    except Exception:
        try:
            off = float(os.environ.get("KDS_UTC_OFFSET", "10"))
        except ValueError:
            off = 10
        return datetime.timezone(datetime.timedelta(hours=off))


def _today_start_ms():
    """Mốc ms đầu 'hôm nay' theo giờ quán — để lọc nhật ký History."""
    tz = _sydney_tz()
    start = datetime.datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _rfc3339_from_ms(ms):
    """ms -> chuỗi RFC3339 UTC cho Square date filter."""
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_from_rfc3339(s):
    """Chuỗi thời gian Square (UTC, có thể kèm mili giây + 'Z') -> ms. Bỏ phần lẻ
    giây cho chắc (Python 3.9 fromisoformat khó tính với số chữ số lẻ)."""
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", s)
    if not m:
        return None
    try:
        dt = datetime.datetime(*[int(x) for x in m.groups()],
                               tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def record_history(ticket, state):
    """Lưu 1 bản chụp đơn vừa rời màn vào nhật ký History (SERVED/CANCELLED).
    Gọi NGOÀI _lock (dùng khoá riêng) để khỏi kẹt khoá."""
    snap = copy.deepcopy(ticket)
    snap["hist_id"] = "h_" + uuid.uuid4().hex[:8]
    snap["orig_id"] = ticket.get("id")
    snap["state"] = state
    snap["closed_at"] = _now_ms()
    with _history_lock:
        _history.append(snap)
        if len(_history) > _HISTORY_MAX:
            del _history[:len(_history) - _HISTORY_MAX]


def redo_from_history(hist_id, uid=None):
    """Đẩy đơn/MÓN trong nhật ký History quay lại màn bếp để LÀM LẠI (vd làm sai
    cho khách). uid có -> chỉ làm lại đúng 1 món đó; không -> cả đơn. Dựng vé mới
    từ bản chụp — KHÔNG gọi Square, KHÔNG tính tiền lại. origin 'redo' nên poller
    không quét mất; cờ redo -> màn hiện badge REDO."""
    with _history_lock:
        snap = next((h for h in _history if h.get("hist_id") == hist_id), None)
    if not snap:
        # Entry từ Square-fallback (hist_id = order_id thật, RAM đã trắng sau
        # restart): đọc lại đơn từ Square rồi làm lại như thường.
        cfg = square_client.get_config()
        if cfg["token"]:
            try:
                o = square_client.retrieve_order(cfg["token"], cfg["env"], hist_id)
                if o and o.get("line_items"):
                    snap = parse_square_order(o)
            except Exception:
                pass
    if not snap:
        return False, "Order not found in history"
    t = copy.deepcopy(snap)
    for k in ("hist_id", "orig_id"):
        t.pop(k, None)
    items = t.get("items", [])
    if uid is not None:
        items = [it for it in items if it.get("uid") == uid]
        if not items:
            return False, "Item not found in order"
    t["items"] = items
    t["id"] = "redo_" + uuid.uuid4().hex[:8]
    t["origin"] = "redo"
    t["received_at"] = _now_ms()
    t["completed_at"] = None
    t["state"] = "NEW"
    t["redo"] = True
    for it in t["items"]:
        it["done"] = False
        for k in ("cancelled", "cancelled_at", "added_at"):
            it.pop(k, None)
    with _lock:
        _tickets[t["id"]] = t
    broadcast()
    # Trả về trạm của (các) món để màn History báo "Sent to Grill" — cook biết
    # ngó màn trạm nào (món redo về đúng trạm, không hiện ở trạm khác).
    stations = sorted({(it.get("station") or "").strip()
                       for it in t["items"] if it.get("station")})
    return True, ", ".join(stations)


TABLE_KEY_HINTS = ("table", "seat", "ban")


def extract_order_note(order):
    """Ghi chú gắn CẢ ĐƠN (khác note từng món) — vd "bàn dị ứng đậu phộng".

    QUAN TRỌNG: Square for Restaurants POS KHÔNG dùng trường `order.note`. Ghi
    chú nhập ở "Name & notes" của check nằm trong
    `fulfillments[].in_store_details.note` (đơn POS về type IN_STORE). Đọc lần
    lượt: order.note (đơn tạo qua API) -> note trong details của fulfillment.
    """
    n = (order.get("note") or "").strip()
    if n:
        return n
    for f in order.get("fulfillments") or []:
        for dk in ("in_store_details", "pickup_details",
                   "delivery_details", "shipment_details"):
            d = (f or {}).get(dk)
            if isinstance(d, dict):
                n = (d.get("note") or "").strip()
                if n:
                    return n
    return None


def extract_table(order):
    """Tìm nhãn trên vé của một Square Order (thường là tên bàn HOẶC tên khách).

    Tài liệu Square mô tả ticket_name có thể chứa số bàn, nhưng Square for
    Restaurants còn có thể nhét nhãn bàn chỗ khác tuỳ cách bấm đơn. Dò lần lượt
    các chỗ hợp lý, chỗ nào có trước lấy trước. Không thấy thì trả None.

    Lưu ý: hàm này chỉ trả CHUỖI THÔ. Có phải tên bàn hay không thì để
    classify_pos_ticket() phân loại.
    """
    name = (order.get("ticket_name") or "").strip()
    if name:
        return name

    for key, val in (order.get("metadata") or {}).items():
        if any(h in key.lower() for h in TABLE_KEY_HINTS) and val:
            return str(val).strip()

    for f in order.get("fulfillments") or []:
        for val in (f or {}).values():
            if not isinstance(val, dict):
                continue
            for key, inner in val.items():
                if any(h in key.lower() for h in TABLE_KEY_HINTS) and inner:
                    return str(inner).strip()

    return None


# --- Dine-in hay Takeaway? --------------------------------------------------
# Square KHÔNG trả "dining option" (Eat in / Takeaway) qua Orders API: mọi đơn
# bấm trên POS đều về fulfillment type "IN_STORE", nên không thể đọc thẳng ra.
# (Square xác nhận trên forum dev, tính năng vẫn nằm ở mục feature request.)
# Cách duy nhất chắc chắn là suy từ TÊN VÉ, vốn do POS/floor plan đặt:
#   - chọn bàn thật trên sơ đồ (L1, R2, O3)      -> dine-in
#   - chọn "bàn" ảo khu Takeaway (TA1..TA5)      -> takeaway  <= chủ chốt cách này
#   - vé chỉ có tên khách ("Hien") hay tiền tố "TA "/"To go" -> takeaway
TABLE_SECTIONS = [s.strip().upper() for s in
                  os.environ.get("KDS_TABLE_SECTIONS", "L,R,O").split(",") if s.strip()]
# Khu bàn ảo dành cho đơn mang về, tạo trong floor plan Square: TA1..TA15.
TAKEAWAY_SECTIONS = [s.strip().upper() for s in
                     os.environ.get("KDS_TAKEAWAY_SECTIONS", "TA,TAKEAWAY,TO GO,TOGO,T/A")
                     .split(",") if s.strip()]
# Saigon Spices bán mang-về là chính: KHÔNG đánh dấu -> mặc định TAKEAWAY.
# Chỉ khi khách ngồi lại nhân viên mới bấm "Dine in". Tắt qua env KDS_DEFAULT_TAKEAWAY=0.
DEFAULT_TAKEAWAY = os.environ.get("KDS_DEFAULT_TAKEAWAY", "1").strip().lower() \
    not in ("0", "false", "no", "")

# Dine in / Takeaway đọc từ SERVICE CHARGE $0 gắn cả đơn (nhân viên bấm
# Actions -> Service charges -> chọn, MỘT LẦN/đơn — riêng biệt với Discounts họ
# hay dùng, không đụng món). Square trả tên service charge ra order.service_charges[].name.
# (Square KHÔNG trả dining option gốc/Fulfilment qua API — đã kiểm chứng nhiều lần.)
DINING_TAKEAWAY_NAMES = {"take away", "takeaway", "take-away", "to go", "togo",
                         "mang về", "mang ve"}
DINING_DINEIN_NAMES = {"dine in", "dine-in", "dinein", "eat in", "eat-in",
                       "have here", "here", "ăn tại chỗ", "tại chỗ"}


def dining_from_service_charge(order):
    """Suy Dine in/Takeaway từ tên service charge gắn cả đơn. None nếu chưa gắn."""
    for sc in (order.get("service_charges") or []):
        nm = (sc.get("name") or "").strip().lower()
        if nm in DINING_TAKEAWAY_NAMES:
            return "TAKEAWAY"
        if nm in DINING_DINEIN_NAMES:
            return "DINE_IN"
    return None


def _section_re(sections):
    """Regex khớp 'L1', 'table l 1', 'TA-3'… theo danh sách mã khu."""
    if not sections:
        return re.compile(r"(?!)")        # không cấu hình khu nào -> không khớp gì
    alt = "|".join(re.escape(s) for s in sorted(sections, key=len, reverse=True))
    return re.compile(r"^(?:table|tbl|bàn|ban)?\s*(%s)\s*[-–]?\s*(\d{1,3})$" % alt,
                      re.IGNORECASE)


_TABLE_RE = _section_re(TABLE_SECTIONS)
_TAKEAWAY_TABLE_RE = _section_re(TAKEAWAY_SECTIONS)
# Saigon Spices: bàn ăn đánh SỐ TRƠN (1..15), khác Délice dùng chữ+số (L1/R2/O3).
# Bật "số trơn = bàn Dine-in" qua env (mặc định BẬT). Khớp "5" / "Table 5" / "bàn 5".
# Khu Takeaway "TAx" xét TRƯỚC nên "TA5" vẫn ra Takeaway, không lọt vào đây.
NUMERIC_TABLES = os.environ.get("KDS_NUMERIC_TABLES", "1").strip().lower() \
    not in ("0", "false", "no", "")
_NUMERIC_TABLE_RE = re.compile(r"^(?:table|tbl|bàn|ban)?\s*(\d{1,3})$", re.IGNORECASE)
# Tiền tố nhân viên có thể gõ để ép thành takeaway (gỡ khỏi tên khách khi hiện).
TAKEAWAY_PREFIXES = ("takeaway", "take away", "take-away", "to go", "togo",
                     "t/a", "ta", "pickup", "pick up", "pu", "mang ve", "mang về", "mv")


def is_table_code(label):
    """Nhãn này có phải tên bàn trong sơ đồ không (L1, R2, O3, 'Table L1')?"""
    return bool(label) and bool(_TABLE_RE.match(label.strip()))


def classify_pos_ticket(label):
    """Đơn bấm trên POS -> (loại đơn, nhãn chỗ, tên khách).

    Không đoán bừa: tên vé trống thì trả UNKNOWN để màn hình khỏi ghi bừa
    cho một đơn takeaway (bếp bày đĩa thay vì đóng hộp là hỏng món).
    """
    label = (label or "").strip()
    if not label:
        # Quán mang-về là chính: không chọn bàn -> Takeaway (không phải "No table").
        return ("TAKEAWAY" if DEFAULT_TAKEAWAY else "UNKNOWN"), None, None

    # Bàn ảo khu Takeaway phải xét TRƯỚC bàn thật, kẻo "TA1" lọt vào nhánh khác.
    m = _TAKEAWAY_TABLE_RE.match(label)
    if m:
        return "TAKEAWAY", "Takeaway " + m.group(2), None

    m = _TABLE_RE.match(label)
    if m:
        # Chuẩn hoá "table l1"/"L 1" -> "L1" cho vé bếp gọn và đồng nhất.
        return "DINE_IN", m.group(1).upper() + m.group(2), None

    # Bàn số trơn (1..15) -> Dine-in "Table 5". Xét SAU khu Takeaway (TAx).
    if NUMERIC_TABLES:
        mn = _NUMERIC_TABLE_RE.match(label)
        if mn:
            return "DINE_IN", mn.group(1), None

    low = label.lower()
    for p in TAKEAWAY_PREFIXES:
        if low.startswith(p) and (len(low) == len(p) or not low[len(p)].isalnum()):
            guest = label[len(p):].strip(" -–:#") or None
            return "TAKEAWAY", None, guest

    # Còn lại: vé có tên người/ghi chú mà không chọn bàn -> quy ước là takeaway.
    return "TAKEAWAY", None, label


def _money(*candidates):
    """Lấy money object Square đầu tiên hợp lệ -> {amount, currency}, hoặc None.

    Square trả tiền dạng đơn vị nhỏ nhất (cent). Giữ nguyên số nguyên đó rồi
    để giao diện tự định dạng theo currency, khỏi lệch làm tròn."""
    for m in candidates:
        if isinstance(m, dict) and m.get("amount") is not None:
            return {"amount": int(m["amount"]), "currency": m.get("currency", "AUD")}
    return None


def _line_price(li, qty):
    """Tiền của một dòng món. total/gross đã nhân số lượng sẵn; base_price là
    ĐƠN GIÁ nên phải tự nhân, không thì đơn 3 phần lại hiện giá 1 phần."""
    p = _money(li.get("total_money"), li.get("gross_sales_money"))
    if p:
        return p
    unit = _money(li.get("base_price_money"))
    if unit:
        return {"amount": unit["amount"] * max(qty, 1), "currency": unit["currency"]}
    return None


def parse_square_order(order):
    """Chuyển một Square Order object -> ticket nội bộ của KDS."""
    fulfillments = order.get("fulfillments") or [{}]
    ftype = (fulfillments[0] or {}).get("type", "PICKUP")
    # Đơn gõ trên máy POS thật LÚC NÀO CŨNG về "IN_STORE", dine-in hay takeaway
    # cũng vậy -> không map cứng thành DINE_IN nữa, phải suy từ tên vé.
    type_map = {"DINE_IN": "DINE_IN", "PICKUP": "TAKEAWAY", "DELIVERY": "DELIVERY"}

    # Saigon Spices dùng FLOOR PLAN: chọn bàn 1..15 -> "Table N" (Dine-in);
    # khu TA1..15 -> "Takeaway N"; KHÔNG chọn bàn -> mặc định Takeaway (quán mang-về).
    label = extract_table(order)
    if ftype == "IN_STORE":
        otype, table, guest = classify_pos_ticket(label)
    elif is_table_code(label) or _TAKEAWAY_TABLE_RE.match((label or "").strip()):
        otype, table, guest = classify_pos_ticket(label)
    else:
        otype = type_map.get(ftype, ftype)
        table = None
        guest = label

    items = []
    for idx, li in enumerate(order.get("line_items", [])):
        try:
            qty = int(float(li.get("quantity", "1")))
        except (TypeError, ValueError):
            qty = 1
        # Trạm bếp: đơn demo có thể ghi thẳng "station"; đơn Square thật tra
        # theo catalog_object_id -> trạm. Không rõ trạm thì để None (chưa phân).
        station = li.get("station") or get_station_of(li.get("catalog_object_id"))
        items.append({
            "uid": li.get("uid") or ("i%d" % idx),   # khóa ổn định để nhớ cờ done
            "name": li.get("name", "—"),
            "qty": qty,
            "variation": li.get("variation_name"),
            "modifiers": [m.get("name") for m in li.get("modifiers", []) if m.get("name")],
            "note": li.get("note"),
            "station": station,                        # trạm nấu (Larder/Pan/Grill)
            "done": False,                             # bếp tick từng món (đã nấu xong)
            "served": False,                           # Expo chạm = đã chạy ra bàn
            # Tiền của cả dòng (đã gồm topping, đã nhân số lượng) để bếp thấy
            # giá trị món mình đang nấu. total_money có thuế; thiếu thì lùi dần
            # về đơn giá rồi tự nhân số lượng.
            "price": _line_price(li, qty),
        })

    oid = order.get("id") or ("sq_" + uuid.uuid4().hex[:8])
    total = _money(order.get("total_money"))
    due = square_client.net_amount_due(order)
    # Đã trả tiền = có tổng > 0 mà số phải thu còn 0 (đơn demo không có tổng nên
    # KHÔNG bị nhận nhầm là đã trả). Đơn QR trả rồi vẫn OPEN tới khi bếp xong, nên
    # phải suy từ net_amount_due chứ không từ state.
    paid = bool(total and total["amount"] > 0 and due["amount"] == 0)
    return {
        "id": oid,
        "table": table,
        "guest": guest,          # tên khách trên vé takeaway (không phải số bàn)
        "type": otype,
        "source": (order.get("source") or {}).get("name", "Square"),
        "total": total,
        # Đơn QR (app order tạo) mới cho thu tiền ngay trên Expo; đơn POS thì POS
        # tự thu. due = số còn phải thu để hiện lên nút Thanh toán.
        "qr": square_client.is_qr_order(order),
        "due": due,
        "paid": paid,
        # Tên + SĐT khách (khách tự nhập ở màn order trước khi gửi bếp). Hiện lên
        # vé để nhân viên biết ai ngồi bàn — răn khách bỏ chạy, gọi được nếu cần.
        "cust_name": (order.get("metadata") or {}).get("qr_name") or None,
        "cust_phone": (order.get("metadata") or {}).get("qr_phone") or None,
        # Note gắn vào CẢ ĐƠN (khác note từng món) — vd "bàn dị ứng đậu phộng",
        # "ra món cùng lúc". Hiện thành băng đỏ cảnh báo ở đầu vé.
        "order_note": extract_order_note(order),
        "items": items,
        "state": "NEW",
        "received_at": _now_ms(),
        "ready_at": None,
        "completed_at": None,
        "origin": "square",
    }


def _merge_items(old_items, new_items, suppress_add=False):
    """Giữ cờ 'done' của món cũ khi Square poll gửi lại danh sách món.

    Món bị void khỏi đơn (Square không trả về nữa) KHÔNG biến mất im lặng:
    giữ lại và đánh dấu cancelled để bếp thấy đỏ trên màn — nấu rồi mà vé tự
    bay mất thì không ai biết đường nào mà lần. Bấm Acknowledge mới gỡ.

    suppress_add: vé chính vừa gộp món của vé phụ (thu chung bằng thẻ). Món lạ
    nhảy vào KHÔNG phải khách gọi thêm — đồ đã ra bàn rồi — nên đánh dấu xong
    luôn (done+served), KHÔNG kêu chuông."""
    done_uids = {it.get("uid") for it in old_items if it.get("done")}
    served_uids = {it.get("uid") for it in old_items if it.get("served")}
    old_uids = {it.get("uid") for it in old_items}
    added_at = {it["uid"]: it["added_at"] for it in old_items
                if it.get("uid") and it.get("added_at")}
    new_uids = {it.get("uid") for it in new_items}
    for it in new_items:
        uid = it.get("uid")
        if uid in done_uids:
            it["done"] = True
        if uid in served_uids:
            it["served"] = True
        # Khách gọi thêm giữa bữa: món mới rơi vào vé ĐANG CÓ trên màn, rất dễ
        # lọt. Đánh dấu để màn bếp kêu chuông và làm nổi món đó. Cờ phải mang
        # sang mỗi vòng poll, không thì nó tắt ngay nhịp sau.
        elif old_uids and uid not in old_uids:
            if suppress_add:
                it["done"] = True
                it["served"] = True
            else:
                it["added_at"] = _now_ms()
        if uid in added_at and not it.get("done"):
            it["added_at"] = added_at[uid]
    gone = []
    for it in old_items:
        # chỉ xét món có uid thật (đơn demo không có uid thì bỏ qua)
        if it.get("uid") and it["uid"] not in new_uids:
            if not it.get("cancelled"):
                it["cancelled"] = True
                it["cancelled_at"] = _now_ms()
            gone.append(it)
    return new_items + gone


def _suppress_add(ticket_id):
    """Vé này có đang trong cửa sổ 'vừa gộp bill' không (bỏ qua chuông gọi thêm)?
    Gọi trong _lock."""
    exp = _merge_suppress.get(ticket_id)
    if not exp:
        return False
    if exp < time.time():
        _merge_suppress.pop(ticket_id, None)
        return False
    return True


def upsert_from_square(order, origin="square"):
    ticket = parse_square_order(order)
    ticket["origin"] = origin
    with _lock:
        # Nếu đơn đã tồn tại, cập nhật món nhưng giữ nguyên trạng thái bump + cờ done.
        existing = _tickets.get(ticket["id"])
        if existing:
            existing["items"] = _merge_items(existing["items"], ticket["items"],
                                             suppress_add=_suppress_add(ticket["id"]))
            existing["table"] = ticket["table"]
            existing["guest"] = ticket["guest"]
            existing["type"] = ticket["type"]
            existing["total"] = ticket["total"]
            existing["order_note"] = ticket["order_note"]
            _apply_pay_fields(existing, ticket)
        else:
            _tickets[ticket["id"]] = ticket
    broadcast()


def sync_from_square_orders(orders):
    """Đồng bộ danh sách đơn OPEN lấy từ Square (poll). Đơn nào Square không
    còn trả về OPEN nữa (đã đóng/hủy) thì gỡ khỏi bảng."""
    open_ids = set()
    now = time.time()
    with _lock:
        for k in [k for k, v in _recently_closed.items() if now - v > _CLOSED_GRACE_S]:
            del _recently_closed[k]
        for k in [k for k, v in _done_ids.items() if now - v > _DONE_TTL_S]:
            del _done_ids[k]
    for o in orders:
        # bỏ qua đơn không có món (chưa lên đơn xong)
        if not o.get("line_items"):
            continue
        t = parse_square_order(o)
        t["origin"] = "square"
        with _lock:
            # Vé vừa đóng bằng nút Thanh toán: đừng thêm lại dù Square còn báo OPEN.
            if t["id"] in _recently_closed:
                continue
            # Đơn đã BẤM DONE (counter-service): chặn bền, đừng thêm lại dù còn OPEN.
            if t["id"] in _done_ids:
                continue
            open_ids.add(t["id"])
            existing = _tickets.get(t["id"])
            if existing:
                existing["items"] = _merge_items(existing["items"], t["items"],
                                                 suppress_add=_suppress_add(t["id"]))
                existing["table"] = t["table"]
                existing["guest"] = t["guest"]
                existing["type"] = t["type"]
                existing["total"] = t["total"]
                existing["order_note"] = t["order_note"]
                _apply_pay_fields(existing, t)
            else:
                _tickets[t["id"]] = t
    # Đơn Square không còn OPEN nữa: có thể đã thanh toán xong (gỡ bình thường)
    # hoặc bị VOID (phải báo bếp). Chưa biết là gì thì chưa gỡ vội — hỏi Square đã.
    with _lock:
        vanished = [k for k, v in _tickets.items()
                    if v.get("origin") == "square" and k not in open_ids
                    and v.get("state") != "CANCELLED" and not v.get("paid")
                    and not v.get("square_closed")   # đã hỏi Square + GIỮ lại (counter-service)
                    and k not in _pending_vanish]
        _pending_vanish.update(vanished)
    if vanished:
        threading.Thread(target=_resolve_vanished, args=(vanished,), daemon=True).start()
    broadcast()
    _save_state()   # lưu bảng đơn qua Upstash (bóp ga 12s) -> sống qua restart


def _resolve_vanished(ids):
    """Hỏi Square xem đơn vừa rời danh sách OPEN là đã trả tiền hay bị huỷ.

    CANCELED  -> giữ vé lại, chuyển sang trạng thái CANCELLED (màn hiện đỏ) để
                 bếp biết mà dừng tay / khỏi cãi nhau món đã nấu đi đâu.
    còn lại   -> đóng bình thường, gỡ khỏi màn như trước.
    Hỏi lỗi (mất mạng…) -> để nguyên, vòng poll sau thử lại."""
    cfg = square_client.get_config()
    for tid in ids:
        state, failed = None, False
        if cfg["token"]:
            try:
                state = (square_client.retrieve_order(cfg["token"], cfg["env"], tid) or {}).get("state")
            except Exception as e:
                failed = True
                print(f"[SQUARE] không đọc được đơn {tid}: {e}", flush=True)
        with _lock:
            _pending_vanish.discard(tid)
            t = _tickets.get(tid)
            if not t or failed:
                continue
            if state == "CANCELED" and t.get("state") != "COMPLETED":
                t["state"] = "CANCELLED"
                t["cancelled_at"] = _now_ms()
                print(f"[KDS] order {tid} cancelled on Square -> alert kitchen", flush=True)
            elif HOLD_TILL_DONE:
                # ⭐ COUNTER-SERVICE (Saigon): đơn rời OPEN = đã TRẢ TIỀN ở POS. KHÔNG
                # xoá — bếp còn phải nấu. GIỮ lại tới khi bấm Done; đánh dấu paid +
                # square_closed để poll không hỏi lại đơn này nữa (khỏi lặp retrieve).
                t["paid"] = True
                t["square_closed"] = True
                cur = (t.get("due") or {}).get("currency", "AUD")
                t["due"] = {"amount": 0, "currency": cur}
            else:
                # Model dine-in (Délice): đơn rời OPEN mà không phải mới-huỷ = đã trả/
                # đóng nơi khác (POS thu xong / Expo bấm Thanh toán) -> gỡ khỏi màn.
                del _tickets[tid]
    broadcast()


def ack_cancel(ticket_id, station=None):
    """Bếp/Expo bấm Acknowledge: đã đọc thông báo huỷ -> dọn khỏi màn.

    station: tên trạm đang xem (Larder/Pan/Grill). Chỉ dọn món huỷ CỦA TRẠM ĐÓ,
    để bếp trạm này xác nhận xong không xoá mất thông báo của trạm khác chưa đọc.
    Bỏ trống (Expo, hoặc bếp đang xem "All") = dọn hết.
    Cả đơn bị huỷ thì gỡ luôn vé — chuyện đó trạm nào cũng thấy như nhau.
    """
    cancelled_snap = None
    with _lock:
        t = _tickets.get(ticket_id)
        if not t:
            return False
        if t.get("state") == "CANCELLED":
            cancelled_snap = t          # cả đơn bị huỷ -> vào History
            del _tickets[ticket_id]
        else:
            def keep(it):
                if not it.get("cancelled"):
                    return True
                return bool(station) and (it.get("station") or "").lower() != station.lower()
            t["items"] = [it for it in t["items"] if keep(it)]
    if cancelled_snap is not None:
        record_history(cancelled_snap, "CANCELLED")
    broadcast()
    return True


def bump(ticket_id, station):
    """Đẩy đơn sang trạng thái kế tiếp theo trạm bấm. Bếp 'Done' -> READY = ĐẨY đơn
    sang màn Expo (KHÔNG gỡ — Expo còn chạy món ra cho khách rồi mới tắt). Counter-
    service (Saigon): KDS chỉ ĐỌC, KHÔNG ghi ngược Square (POS lo vòng đời đơn)."""
    write_back = None  # (order_id, target_fulfillment_state)
    with _lock:
        t = _tickets.get(ticket_id)
        if not t:
            return False
        # HOLD_TILL_DONE (Saigon read-only): không ghi ngược Square.
        is_square = t.get("origin") == "square" and not HOLD_TILL_DONE
        if station == "kitchen" and t["state"] == "NEW":
            t["state"] = "READY"
            t["ready_at"] = _now_ms()
            if is_square:
                write_back = (t["id"], "PREPARED")   # bếp nấu xong
        elif station == "expo" and t["state"] == "READY":
            t["state"] = "COMPLETED"
            t["completed_at"] = _now_ms()
            if is_square:
                write_back = (t["id"], "COMPLETED")  # đã giao khách
    if write_back:
        push_to_square_async(*write_back)
    broadcast()
    return True


def undo(ticket_id, station):
    """Lùi lại 1 bước (bấm nhầm)."""
    with _lock:
        t = _tickets.get(ticket_id)
        if not t:
            return False
        if station == "kitchen" and t["state"] == "READY":
            t["state"] = "NEW"
            t["ready_at"] = None
        elif station == "expo" and t["state"] == "COMPLETED":
            t["state"] = "READY"
            t["completed_at"] = None
    broadcast()
    return True


def _all_served(t):
    """Đã GIAO HẾT món ra bàn chưa (bỏ qua món bị void)."""
    live = [it for it in t["items"] if not it.get("cancelled")]
    return bool(live) and all(it.get("served") for it in live)


def serve(ticket_id):
    """Expo bấm 'Served' = đã GIAO HẾT món ra bàn. Đánh dấu mọi món served rồi
    kiểm tra hoàn tất — đơn CHỈ rời màn khi VỪA giao hết VỪA trả tiền. Chưa trả
    thì giữ vé lại chờ thanh toán (hết cảnh bấm Served là mất vé trước khi thu)."""
    with _lock:
        t = _tickets.get(ticket_id)
        if not t:
            return False
        for it in t["items"]:
            if not it.get("cancelled"):
                it["served"] = True
    broadcast()
    maybe_complete(ticket_id)
    return True


def toggle_item(ticket_id, uid):
    """Bật/tắt cờ 'đã nấu xong' cho một món trong đơn (bếp tick từng món)."""
    found = False
    with _lock:
        t = _tickets.get(ticket_id)
        if t:
            for it in t["items"]:
                if it.get("uid") == uid:
                    it["done"] = not it.get("done")
                    found = True
                    break
    if found:
        broadcast()
    return found


def serve_item(ticket_id, uid):
    """Expo chạm 1 món đang nhấp nháy = ĐÃ CHẠY RA BÀN -> tắt nhấp nháy (gạch mờ).
    Riêng với màn Expo, tách khỏi cờ 'done' của bếp: bếp nấu xong -> món nháy trên
    Expo; người chạy bàn bê ra -> chạm cho hết nháy."""
    found = False
    with _lock:
        t = _tickets.get(ticket_id)
        if t:
            for it in t["items"]:
                if it.get("uid") == uid:
                    it["served"] = not it.get("served")
                    found = True
                    break
    if found:
        broadcast()
        maybe_complete(ticket_id)   # món cuối được giao + đã trả tiền -> hoàn tất
    return found


def _apply_pay_fields(existing, fresh):
    """Cập nhật cờ thanh toán khi poll gửi lại đơn. paid CHỈ đi 1 chiều (False->True):
    Square SearchOrders phản ánh payment trễ hơn retrieve vài giây (eventual
    consistency), không khoá thì vé vừa thu tiền lại chớp về 'chưa trả' 1 nhịp."""
    existing["qr"] = fresh.get("qr", existing.get("qr"))
    existing["paid"] = bool(existing.get("paid") or fresh.get("paid"))
    if existing["paid"]:
        cur = (existing.get("due") or fresh.get("due") or {}).get("currency", "AUD")
        existing["due"] = {"amount": 0, "currency": cur}
    else:
        existing["due"] = fresh.get("due", existing.get("due"))


def _finalize_ticket(ticket_id):
    """Đơn HOÀN TẤT (đã trả tiền + đã giao hết món): ghi History, đẩy fulfillment
    COMPLETED về Square (đơn rời OPEN, poll không thêm lại), gỡ khỏi mọi màn."""
    write_back = None
    snap = None
    added_done = False
    with _lock:
        t = _tickets.get(ticket_id)
        if not t:
            return
        t["state"] = "COMPLETED"
        t["completed_at"] = _now_ms()
        snap = t
        if t.get("origin") == "square":
            _recently_closed[ticket_id] = time.time()   # poll bỏ qua vài giây
            if HOLD_TILL_DONE:
                # counter-service (Saigon): KDS chỉ ĐỌC, KHÔNG ghi ngược Square (POS
                # lo vòng đời đơn). Chặn BỀN để poll không thêm lại — đơn chưa trả ở
                # POS vẫn OPEN, nếu không chặn sẽ hiện lại sau khi bếp đã bấm Done.
                _done_ids[ticket_id] = time.time()
                added_done = True
            else:
                write_back = (t["id"], "COMPLETED")   # dine-in: đẩy Square rời OPEN
        _merge_suppress.pop(ticket_id, None)
        del _tickets[ticket_id]
    if added_done:
        _save_state(force=True)   # lưu bền ngay để đơn này không hiện lại sau restart
    record_history(snap, "SERVED")
    if write_back:
        push_to_square_async(*write_back)
    broadcast()


def mark_paid(ticket_id):
    """Thu tiền xong -> đánh dấu ĐÃ TRẢ. KHÔNG gỡ vé ngay: đơn chỉ hoàn tất khi
    VỪA trả tiền VỪA giao hết món (khách trả trước ăn sau thì vé vẫn nằm ở Bếp +
    Expo tới khi chạy hết món). Đã giao hết rồi thì hoàn tất luôn."""
    with _lock:
        t = _tickets.get(ticket_id)
        if not t:
            return
        t["paid"] = True
        cur = (t.get("due") or {}).get("currency", "AUD")
        t["due"] = {"amount": 0, "currency": cur}
    broadcast()
    maybe_complete(ticket_id)


def maybe_complete(ticket_id):
    """Hoàn tất (tắt) đơn KHI VÀ CHỈ KHI: ĐÃ TRẢ TIỀN + đã giao hết món. Chủ chốt:
    đơn CHƯA thanh toán thì KHÔNG cho tắt (chống cho món ra mà chưa thu tiền) — kể
    cả counter-service (đơn trả ở POS thì paid=True sẵn, đơn OPEN chưa trả thì chặn)."""
    with _lock:
        t = _tickets.get(ticket_id)
        ready = bool(t and _all_served(t) and t.get("paid"))
    if ready:
        _finalize_ticket(ticket_id)


def _square_err(e):
    """Bóc phần người đọc được từ lỗi HTTP của Square."""
    try:
        raw = e.read().decode()[:600]
        errs = json.loads(raw).get("errors") or []
        d = "; ".join(filter(None, (x.get("detail") or x.get("code") for x in errs)))
        return d or ("Square rejected (HTTP %s)" % e.code)
    except Exception:
        return "Square rejected (HTTP %s)" % getattr(e, "code", "?")


def _payable_order(ticket_id):
    """Đọc lại đơn tươi từ Square để lấy số phải thu chính xác. Trả
    (order, due, error_tuple). error_tuple None nếu OK."""
    cfg = square_client.get_config()
    if not cfg["token"]:
        return None, None, ({"ok": False, "message": "Square not connected"}, 503)
    with _lock:
        t = _tickets.get(ticket_id)
    if not t:
        return None, None, ({"ok": False, "message": "Order not found"}, 404)
    if not t.get("qr"):
        return None, None, ({"ok": False, "message": "This order is paid on the POS, not here"}, 400)
    try:
        order = square_client.retrieve_order(cfg["token"], cfg["env"], t["id"])
    except urllib.error.HTTPError as e:
        return None, None, ({"ok": False, "message": _square_err(e)}, 502)
    except Exception as e:
        return None, None, ({"ok": False, "message": "Couldn't read order: %s" % e}, 502)
    due = square_client.net_amount_due(order)
    if due["amount"] <= 0:
        return None, None, ({"ok": False, "message": "This order is already paid"}, 400)
    return order, due, None


def pay_cash(ticket_id, received):
    """Expo bấm thu tiền mặt: ghi payment CASH vào Square, trả tiền thối."""
    order, due, err = _payable_order(ticket_id)
    if err:
        return err
    try:
        recv = int(round(float(received)))
    except (TypeError, ValueError):
        return {"ok": False, "message": "Invalid amount"}, 400
    if recv < due["amount"]:
        return {"ok": False, "message": "Cash received is less than the bill"}, 400
    cfg = square_client.get_config()
    with _lock:
        t = _tickets.get(ticket_id)
        table = (t or {}).get("table") or (t or {}).get("guest") or ""
    try:
        pay = square_client.create_cash_payment(
            cfg["token"], cfg["env"], order.get("location_id"), order.get("id"),
            due, recv, note=("Bàn %s — tiền mặt" % table) if table else "Tiền mặt")
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": _square_err(e)}, 502
    except Exception as e:
        return {"ok": False, "message": str(e)}, 502
    change = (pay.get("cash_details") or {}).get("change_back_money") \
        or {"amount": recv - due["amount"], "currency": due["currency"]}
    mark_paid(ticket_id)
    print("[PAY] %s tiền mặt: thu %d, khách đưa %d, thối %d"
          % (table, due["amount"], recv, change["amount"]), flush=True)
    return {"ok": True, "due": due, "change": change}, 200


def pay_card(ticket_id):
    """Expo bấm thu thẻ: đẩy bill sang máy Terminal đã ghép."""
    cfg = square_client.get_config()
    if not cfg["device_id"]:
        return {"ok": False, "message": "Terminal not paired (set SQUARE_DEVICE_ID)"}, 400
    order, due, err = _payable_order(ticket_id)
    if err:
        return err
    with _lock:
        t = _tickets.get(ticket_id)
        table = (t or {}).get("table") or (t or {}).get("guest") or ""
    try:
        co = square_client.create_terminal_checkout(
            cfg["token"], cfg["env"], cfg["device_id"], order.get("id"), due,
            note=("Bàn %s" % table) if table else None)
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": _square_err(e)}, 502
    except Exception as e:
        return {"ok": False, "message": str(e)}, 502
    print("[PAY] %s -> Terminal (%s)" % (table, co.get("status")), flush=True)
    return {"ok": True, "checkout_id": co.get("id"), "status": co.get("status")}, 200


def pay_card_status(checkout_id, ticket_id=None):
    """Theo dõi máy Terminal. COMPLETED -> đánh dấu vé đã trả ngay."""
    cfg = square_client.get_config()
    if not cfg["token"]:
        return {"ok": False, "message": "Square not connected"}, 503
    try:
        co = square_client.get_terminal_checkout(cfg["token"], cfg["env"], checkout_id)
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": _square_err(e)}, 502
    except Exception as e:
        return {"ok": False, "message": str(e)}, 502
    if co.get("status") == "COMPLETED" and ticket_id:
        mark_paid(ticket_id)
    return {"ok": True, "status": co.get("status"),
            "cancel_reason": co.get("cancel_reason")}, 200


# ---------------------------------------------------------------------------
# THU CHUNG NHIỀU NGƯỜI — một người bao cả bàn, trả 1 lần
# ---------------------------------------------------------------------------
def _collect_payables(ids):
    """Đọc tươi từng vé cần thu. Trả (list[(tid, order, due)], total, currency,
    error_tuple). Dừng ở lỗi đầu tiên."""
    payloads, total, cur = [], 0, "AUD"
    for tid in ids:
        order, due, err = _payable_order(tid)
        if err:
            return None, 0, cur, err
        payloads.append((tid, order, due))
        total += due["amount"]
        cur = due["currency"]
    return payloads, total, cur, None


def pay_cash_together(ids, received):
    """Thu TIỀN MẶT cho nhiều vé cùng lúc (một người bao). Ghi một payment CASH
    cho TỪNG đơn = đúng số phải thu của đơn đó (sổ Square đúng từng người); tiền
    thối tính trên TỔNG. Vé nào ghi xong đóng luôn."""
    ids = [i for i in (ids or []) if i]
    if not ids:
        return {"ok": False, "message": "No orders selected"}, 400
    payloads, total, cur, err = _collect_payables(ids)
    if err:
        return err
    try:
        recv = int(round(float(received)))
    except (TypeError, ValueError):
        return {"ok": False, "message": "Invalid amount"}, 400
    if recv < total:
        return {"ok": False, "message": "Cash received is less than the total bill"}, 400
    cfg = square_client.get_config()
    done = 0
    for tid, order, due in payloads:
        with _lock:
            t = _tickets.get(tid)
            who = (t or {}).get("cust_name") or (t or {}).get("table") or ""
        try:
            # received = đúng số phải thu của đơn này (thu đủ); tiền thối gộp tính
            # trên tổng ở dưới, không tính lẻ từng đơn.
            square_client.create_cash_payment(
                cfg["token"], cfg["env"], order.get("location_id"), order.get("id"),
                due, due["amount"], note=("Thu chung — %s" % who) if who else "Thu chung")
        except urllib.error.HTTPError as e:
            msg = _square_err(e)
            if done:
                msg += " (đã thu %d/%d người, thu nốt số còn lại riêng)" % (done, len(payloads))
            return {"ok": False, "message": msg}, 502
        except Exception as e:
            return {"ok": False, "message": str(e)}, 502
        mark_paid(tid)
        done += 1
    change = {"amount": recv - total, "currency": cur}
    print("[PAY] thu chung tiền mặt: %d người, thu %d, khách đưa %d, thối %d"
          % (len(payloads), total, recv, change["amount"]), flush=True)
    return {"ok": True, "due": {"amount": total, "currency": cur}, "change": change}, 200


def _reconstruct_line_items(order):
    """Dựng payload line_items để nối món của bill phụ sang bill chính khi gộp."""
    out = []
    for li in order.get("line_items", []):
        qty = str(li.get("quantity") or "1")
        if li.get("catalog_object_id"):
            item = {"catalog_object_id": li["catalog_object_id"], "quantity": qty}
        else:
            item = {"name": li.get("name") or "Item", "quantity": qty}
            bp = li.get("base_price_money")
            if isinstance(bp, dict) and bp.get("amount") is not None:
                item["base_price_money"] = {"amount": int(bp["amount"]),
                                            "currency": bp.get("currency", "AUD")}
        if li.get("note"):
            item["note"] = li["note"][:500]
        out.append(item)
    return out


def pay_card_together(ids):
    """Thu THẺ cho nhiều vé bằng MỘT lần chạm thẻ (một người bao cả bàn).

    Square gắn mỗi lần quẹt vào đúng 1 đơn, nên để 1-chạm-cho-nhiều-người mà sổ
    vẫn sạch: GỘP món của các bill phụ sang bill chính, huỷ bill phụ (rỗng), rồi
    đẩy Terminal 1 lần cho tổng. Lúc thu tiền đồ đã ra bàn nên gộp là an toàn."""
    ids = [i for i in (ids or []) if i]
    if not ids:
        return {"ok": False, "message": "No orders selected"}, 400
    cfg = square_client.get_config()
    if not cfg["device_id"]:
        return {"ok": False, "message": "Terminal not paired (set SQUARE_DEVICE_ID)"}, 400
    if len(ids) == 1:
        return pay_card(ids[0])
    payloads, _total, _cur, err = _collect_payables(ids)
    if err:
        return err
    primary_tid = payloads[0][0]        # vé chính = vé đầu (id vé = id đơn Square)
    for tid, order, due in payloads[1:]:
        new_items = _reconstruct_line_items(order)
        try:
            if new_items:
                square_client.append_line_items(cfg["token"], cfg["env"], primary_tid, new_items)
            square_client.cancel_order(cfg["token"], cfg["env"], order.get("id"))
        except urllib.error.HTTPError as e:
            return {"ok": False, "message": _square_err(e)}, 502
        except Exception as e:
            return {"ok": False, "message": str(e)}, 502
        # Gỡ vé phụ khỏi màn NGAY, KHÔNG để nó thành "đơn bị huỷ" báo động bếp:
        # xoá thẳng + khoá poll khỏi thêm lại vài giây.
        with _lock:
            _tickets.pop(tid, None)
            _recently_closed[order.get("id")] = time.time()
    # Chặn chuông "gọi thêm" khi poll thấy món phụ nhảy vào vé chính.
    with _lock:
        _merge_suppress[primary_tid] = time.time() + _MERGE_SUPPRESS_S
        t = _tickets.get(primary_tid)
        table = (t or {}).get("table") or (t or {}).get("guest") or ""
    # Đọc lại tổng phải thu của vé chính (đã gồm hết món vừa gộp).
    try:
        merged = square_client.retrieve_order(cfg["token"], cfg["env"], primary_tid)
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": _square_err(e)}, 502
    except Exception as e:
        return {"ok": False, "message": str(e)}, 502
    due = square_client.net_amount_due(merged)
    try:
        co = square_client.create_terminal_checkout(
            cfg["token"], cfg["env"], cfg["device_id"], primary_tid, due,
            note=("Bàn %s — thu chung" % table) if table else "Thu chung")
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": _square_err(e)}, 502
    except Exception as e:
        return {"ok": False, "message": str(e)}, 502
    broadcast()   # màn cập nhật ngay: các vé phụ đã gộp biến mất
    print("[PAY] thu chung thẻ: %d người -> Terminal (%s)" % (len(payloads), co.get("status")), flush=True)
    return {"ok": True, "checkout_id": co.get("id"), "status": co.get("status"),
            "ticket_id": primary_tid}, 200


def push_to_square_async(order_id, target_state):
    """Ghi ngược trạng thái fulfillment về Square, chạy nền để bump luôn mượt."""
    def run():
        cfg = square_client.get_config()
        if not cfg["token"]:
            return
        res = square_client.advance_fulfillment(cfg["token"], cfg["env"], order_id, target_state)
        mark = "OK" if res.get("ok") else "SKIP"
        print(f"[SQUARE] {order_id} -> {target_state}: {mark} — {res.get('message')}", flush=True)
    threading.Thread(target=run, daemon=True).start()


def snapshot():
    with _lock:
        # bỏ đơn đã hoàn tất khỏi màn hình (giữ lịch sử tối giản)
        return [t for t in _tickets.values() if t["state"] != "COMPLETED"]


# ---------------------------------------------------------------------------
# SSE broadcast
# ---------------------------------------------------------------------------
def broadcast():
    data = json.dumps({"type": "tickets", "tickets": snapshot()})
    dead = []
    for q in list(_subscribers):
        try:
            q.put_nowait(data)
        except Exception:
            dead.append(q)
    for q in dead:
        _subscribers.discard(q)


# ---------------------------------------------------------------------------
# Đơn mẫu để demo (định dạng Square Order thật)
# ---------------------------------------------------------------------------
SAMPLE_ORDERS = [
    {
        "ticket_name": "12", "fulfillments": [{"type": "DINE_IN"}],
        "source": {"name": "Square POS"},
        "note": "PEANUT ALLERGY — whole table",
        "line_items": [
            {"name": "Beef Pho", "quantity": "2", "variation_name": "Large",
             "modifiers": [{"name": "Extra beef"}], "note": "Less onion", "station": "Pan"},
            {"name": "Fresh Spring Rolls", "quantity": "1", "station": "Larder"},
            {"name": "Vietnamese Iced Coffee", "quantity": "2", "station": "Larder"},
        ],
    },
    {
        "ticket_name": None, "fulfillments": [{"type": "PICKUP"}],
        "source": {"name": "Online"},
        "line_items": [
            {"name": "Hanoi Bun Cha", "quantity": "1", "note": "No coriander", "station": "Grill"},
            {"name": "Fried Spring Rolls", "quantity": "3", "station": "Pan"},
        ],
    },
    {
        "ticket_name": "5", "fulfillments": [{"type": "DINE_IN"}],
        "source": {"name": "Square POS"},
        "line_items": [
            {"name": "Broken Rice with Pork Chop", "quantity": "1", "modifiers": [{"name": "Extra egg"}], "station": "Grill"},
            {"name": "Special Banh Mi", "quantity": "2", "station": "Larder"},
            {"name": "Peach Tea", "quantity": "1", "variation_name": "Ice on the side", "station": "Larder"},
        ],
    },
    {
        "ticket_name": None, "fulfillments": [{"type": "DELIVERY"}],
        "source": {"name": "Uber Eats"},
        "line_items": [
            {"name": "Hue Beef Noodle Soup", "quantity": "2", "note": "Medium spicy", "station": "Pan"},
            {"name": "Three-Color Dessert", "quantity": "2", "station": "Larder"},
        ],
    },
]


def make_sample_order():
    import random
    base = dict(random.choice(SAMPLE_ORDERS))
    base["id"] = "sq_" + uuid.uuid4().hex[:8]
    return base


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # tắt log ồn ào

    # -- helpers ----------------------------------------------------------
    # Mọi phản hồi hữu hạn đều đóng kết nối (Connection: close). Lý do:
    # http.server của Python xử lý keep-alive không chắc chắn, và reverse proxy
    # của Render tái dùng kết nối upstream — nếu khung HTTP lệch một nhịp thì các
    # request sau bị 404/lẫn nội dung. Đóng kết nối sau mỗi phản hồi loại bỏ hẳn
    # việc proxy tái dùng kết nối lỗi. SSE (/api/stream) dùng kết nối riêng.
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _order_debug(self):
        """Soi xem Square nhét nhãn bàn vào trường nào (tạm, để dò 1 lần).

        Chỉ trả tên trường + mấy giá trị nghi là nhãn bàn, KHÔNG đổ nguyên đơn
        vì URL này công khai và đơn có thể kèm thông tin khách.
        """
        cfg = square_client.get_config()
        if not cfg["token"]:
            return {"ok": False, "message": "Square not connected"}
        try:
            locs = [l["id"] for l in square_client.list_locations(cfg["token"], cfg["env"])]
            orders = square_client.search_open_orders(cfg["token"], cfg["env"], locs)
        except Exception as e:
            return {"ok": False, "message": str(e)}

        out = []
        for o in orders:
            out.append({
                "id": o.get("id"),
                "top_level_keys": sorted(o.keys()),
                "ticket_name": o.get("ticket_name"),
                "reference_id": o.get("reference_id"),
                "metadata": o.get("metadata"),
                "source": o.get("source"),
                "fulfillments": [
                    {k: (sorted(v.keys()) if isinstance(v, dict) else v) for k, v in (f or {}).items()}
                    for f in (o.get("fulfillments") or [])
                ],
                "label_found": extract_table(o),
                "kds_reads_as": dict(zip(("type", "table", "guest"),
                                         classify_pos_ticket(extract_table(o)))),
            })
        return {"ok": True, "count": len(out), "orders": out}

    def _history(self):
        """Đơn đã Served + đã huỷ TRONG NGÀY. Hai nguồn gộp lại:
          - nhật ký RAM `_history` (nhanh, có redo/sim, ghi ngay lúc hoàn tất);
          - đơn đã ĐÓNG hôm nay hỏi thẳng Square (BỀN: sống qua restart/ngủ khi
            RAM trắng). Dedupe theo order_id. Mới nhất trước.
        """
        start = _today_start_ms()
        with _history_lock:
            out = [h for h in _history if (h.get("closed_at") or 0) >= start]
        seen = {h.get("orig_id") for h in out if h.get("orig_id")}

        cfg = square_client.get_config()
        if cfg["token"]:
            try:
                locs = [l["id"] for l in square_client.list_locations(cfg["token"], cfg["env"])]
                for o in square_client.search_closed_orders(
                        cfg["token"], cfg["env"], locs, _rfc3339_from_ms(start)):
                    oid = o.get("id")
                    if not oid or oid in seen or not o.get("line_items"):
                        continue
                    seen.add(oid)
                    t = parse_square_order(o)
                    for it in t["items"]:          # đơn đã đóng = coi như đã giao hết
                        it["done"] = True
                        it["served"] = True
                    t["state"] = "CANCELLED" if o.get("state") == "CANCELED" else "SERVED"
                    t["closed_at"] = _ms_from_rfc3339(o.get("closed_at")) or start
                    t["orig_id"] = oid
                    t["hist_id"] = oid              # redo đọc lại từ Square theo id này
                    out.append(t)
            except Exception as e:
                print("[HISTORY] Square fallback lỗi: %s" % e, flush=True)

        out.sort(key=lambda h: h.get("closed_at") or 0, reverse=True)
        return {"ok": True, "count": len(out), "orders": out}

    def _send_404(self):
        body = b"Not Found"
        self.close_connection = True
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path):
        full = os.path.normpath(os.path.join(PUBLIC, path.lstrip("/")))
        if not full.startswith(PUBLIC) or not os.path.isfile(full):
            return self._send_404()
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            body = f.read()
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/" or path == "/index.html":
            return self._serve_file("index.html")
        if path == "/kitchen":
            return self._serve_file("kitchen.html")
        if path == "/expo":
            return self._serve_file("expo.html")
        if path == "/history":
            return self._serve_file("history.html")
        if path == "/api/history":
            return self._send_json(self._history())
        if path == "/api/stations":
            return self._send_json({"stations": STATIONS})
        if path == "/setup":
            return self._serve_file("setup.html")
        if path == "/api/tickets":
            return self._send_json({"tickets": snapshot()})
        if path == "/api/square-status":
            cfg = square_client.get_config()
            st = dict(POLLER.status)
            st["configured"] = bool(cfg["token"])
            st["env"] = cfg["env"]
            st["device_paired"] = bool(cfg["device_id"])   # có SQUARE_DEVICE_ID chưa
            return self._send_json(st)
        if path == "/api/order-debug":
            return self._send_json(self._order_debug())
        if path == "/api/pay-card-status":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            cid = (q.get("id") or [""])[0]
            tid = (q.get("t") or [None])[0]
            body, code = pay_card_status(cid, tid)
            return self._send_json(body, code)
        if path == "/api/stream":
            return self._stream()
        # static assets
        return self._serve_file(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/sim":
            upsert_from_square(make_sample_order(), origin="sim")
            return self._send_json({"ok": True})

        if path == "/api/sim-square":
            # Tạo đơn THẬT trong Square sandbox rồi để poller đọc lại về.
            cfg = square_client.get_config()
            if not cfg["token"]:
                return self._send_json({"ok": False, "message": "Square not connected"}, 400)
            sample = make_sample_order()
            line_items = [
                {"name": it["name"], "quantity": str(it["qty"]),
                 **({"note": it["note"]} if it.get("note") else {})}
                for it in parse_square_order(sample)["items"]
            ]
            try:
                square_client.create_test_order(
                    cfg["token"], cfg["env"], line_items,
                    ticket_name=sample.get("ticket_name"))
                POLLER.start()
                return self._send_json({"ok": True, "message": "Order created in Square"})
            except Exception as e:
                return self._send_json({"ok": False, "message": str(e)}, 500)

        if path.startswith("/api/history/") and path.endswith("/redo"):
            hid = path[len("/api/history/"):-len("/redo")]
            body = self._read_body()
            ok, msg = redo_from_history(hid, body.get("uid"))
            return self._send_json({"ok": ok, "message": msg}, 200 if ok else 400)

        if path == "/api/setup":
            # Chủ quán tự dán token ở trang /setup — token vào thẳng server cục bộ.
            body = self._read_body()
            token = (body.get("token") or "").strip()
            env = (body.get("env") or "sandbox").strip()
            if not token:
                return self._send_json({"ok": False, "message": "No token entered"}, 400)
            ok, message = square_client.verify(token, env)
            if ok:
                square_client.save_token(token, env)
                POLLER.start()
                threading.Thread(target=refresh_station_map, daemon=True).start()
            return self._send_json({"ok": ok, "message": message})

        if path == "/webhooks/square":
            # Điểm nhận webhook thật của Square (Giai đoạn 2).
            payload = self._read_body()
            order = (payload.get("data", {}).get("object", {}).get("order")
                     or payload.get("order") or payload)
            if order.get("line_items") or order.get("id"):
                upsert_from_square(order, origin="webhook")
            return self._send_json({"ok": True})

        if path.startswith("/api/tickets/") and path.endswith("/bump"):
            tid = path.split("/")[3]
            body = self._read_body()
            ok = bump(tid, body.get("station", "kitchen"))
            return self._send_json({"ok": ok})

        if path.startswith("/api/tickets/") and path.endswith("/undo"):
            tid = path.split("/")[3]
            body = self._read_body()
            ok = undo(tid, body.get("station", "kitchen"))
            return self._send_json({"ok": ok})

        if path.startswith("/api/tickets/") and path.endswith("/item-toggle"):
            tid = path.split("/")[3]
            body = self._read_body()
            ok = toggle_item(tid, body.get("uid", ""))
            return self._send_json({"ok": ok})

        if path.startswith("/api/tickets/") and path.endswith("/item-serve"):
            tid = path.split("/")[3]
            body = self._read_body()
            ok = serve_item(tid, body.get("uid", ""))
            return self._send_json({"ok": ok})

        if path.startswith("/api/tickets/") and path.endswith("/serve"):
            tid = path.split("/")[3]
            ok = serve(tid)
            return self._send_json({"ok": ok})

        if path == "/api/pay-together/cash":
            body = self._read_body()
            res, code = pay_cash_together(body.get("ids"), body.get("received"))
            return self._send_json(res, code)

        if path == "/api/pay-together/card":
            res, code = pay_card_together(self._read_body().get("ids"))
            return self._send_json(res, code)

        if path.startswith("/api/tickets/") and path.endswith("/pay-cash"):
            tid = path.split("/")[3]
            body, code = pay_cash(tid, self._read_body().get("received"))
            return self._send_json(body, code)

        if path.startswith("/api/tickets/") and path.endswith("/pay-card"):
            tid = path.split("/")[3]
            body, code = pay_card(tid)
            return self._send_json(body, code)

        if path.startswith("/api/tickets/") and path.endswith("/ack-cancel"):
            tid = path.split("/")[3]
            body = self._read_body()
            ok = ack_cancel(tid, (body.get("ack_station") or "").strip() or None)
            return self._send_json({"ok": ok})

        return self._send_404()

    # -- SSE --------------------------------------------------------------
    def _stream(self):
        q = queue.Queue(maxsize=50)
        _subscribers.add(q)
        try:
            # Kết nối SSE là một luồng dài, dùng riêng cho màn này rồi đóng —
            # KHÔNG để proxy của Render gộp lại tái dùng (tránh desync khung HTTP).
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            # gửi trạng thái hiện tại ngay khi kết nối
            init = json.dumps({"type": "tickets", "tickets": snapshot()})
            self.wfile.write(f"data: {init}\n\n".encode("utf-8"))
            self.wfile.flush()

            while True:
                try:
                    data = q.get(timeout=10)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                except queue.Empty:
                    # nhịp tim dạng sự kiện để client biết kết nối còn sống
                    self.wfile.write(b'data: {"type":"ping"}\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _subscribers.discard(q)


POLLER = square_client.Poller(sync_from_square_orders, interval=4,
                              closed_lookback_min=CLOSED_LOOKBACK_MIN,
                              closed_startup_buffer_min=CLOSED_STARTUP_BUFFER_MIN)


def _station_map_loop():
    """Làm mới map món->trạm từ Square Catalog mỗi 5 phút (category ít đổi)."""
    while True:
        try:
            refresh_station_map()
        except Exception:
            pass
        time.sleep(300)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    _load_state()   # khôi phục bảng đơn + đơn đã-nấu từ Upstash (nếu có) TRƯỚC khi poll
    POLLER.start()  # tự poll Square nếu đã cấu hình token trong .env
    threading.Thread(target=_station_map_loop, daemon=True).start()
    print(f"  • Trạm bếp       : {', '.join(STATIONS) if STATIONS else '1 bếp (không phân trạm)'}")
    print(f"Saigon Spices KDS chạy tại http://localhost:{PORT}")
    print(f"  • Bảng điều khiển : http://localhost:{PORT}/")
    print(f"  • Màn Bếp        : http://localhost:{PORT}/kitchen")
    print(f"  • Màn Expo       : http://localhost:{PORT}/expo")
    print(f"  • Kết nối Square : http://localhost:{PORT}/setup")
    square = "đã cấu hình" if square_client.is_configured() else "chưa cấu hình"
    print(f"  • Token Square   : {square}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
