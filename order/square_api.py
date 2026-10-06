"""
Nối Square cho app QR order của Saigon Spices — chỉ dùng thư viện chuẩn.

Khác Délice: khách TRẢ TIỀN TRƯỚC ngay trên điện thoại (Square Web Payments SDK:
thẻ / Apple Pay / Google Pay). Server chỉ nhận MÃ THẺ DÙNG 1 LẦN (source_id) do
SDK của Square sinh — số thẻ không bao giờ đi qua server này.

Việc của module này:
  - đọc menu thật từ Square Catalog theo cây menu "QR Order" (+ "Lunch Special")
    kèm modifier (Soup, add-on, món trong combo…)
  - tạo đơn (có số bàn ở ticket_name) rồi thu tiền đơn đó
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

SQUARE_VERSION = "2024-12-18"
BASE = {
    "sandbox": "https://connect.squareupsandbox.com",
    "production": "https://connect.squareup.com",
}

# Dấu nhận biết đơn do app này tạo — KDS Saigon (square_client.QR_APP_TAG) đọc đúng
# chuỗi này để biết là đơn QR (chỉ hiện lên bếp khi ĐÃ TRẢ TIỀN).
APP_TAG = "saigon-qr"


def get_config():
    return {
        "token": os.environ.get("SQUARE_ACCESS_TOKEN", "").strip(),
        "env": (os.environ.get("SQUARE_ENV") or "production").strip().lower(),
        "app_id": os.environ.get("SQUARE_APP_ID", "").strip(),
        "location_id": os.environ.get("SQUARE_LOCATION_ID", "").strip(),
    }


class SquareError(Exception):
    def __init__(self, message, code=None, errors=None):
        super().__init__(message)
        self.code = code
        self.errors = errors or []


def _request(method, path, token, env, body=None, timeout=15):
    url = BASE.get(env, BASE["production"]) + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Square-Version", SQUARE_VERSION)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()[:2000]
        errs = []
        try:
            errs = json.loads(raw).get("errors") or []
        except ValueError:
            pass
        detail = "; ".join(filter(None, (x.get("detail") or x.get("code") for x in errs))) or raw
        raise SquareError("Square HTTP %s: %s" % (e.code, detail), code=e.code, errors=errs)


# ---------------------------------------------------------------------------
# Catalog -> menu
# ---------------------------------------------------------------------------
def _catalog_list(token, env, obj_type):
    objs, cursor = [], None
    while True:
        path = "/v2/catalog/list?types=" + obj_type
        if cursor:
            path += "&cursor=" + urllib.parse.quote(cursor)
        r = _request("GET", path, token, env, timeout=25)
        objs.extend(r.get("objects", []))
        cursor = r.get("cursor")
        if not cursor:
            return objs


def _sold_out(variation, location_id):
    for ov in (variation.get("item_variation_data") or {}).get("location_overrides") or []:
        if ov.get("location_id") == location_id and ov.get("sold_out"):
            return True
    return False


def fetch_menu(token, env, location_id, root_ids, dining_list_name):
    """Dựng menu từ cây MENU_CATEGORY của Square (đúng menu đang bán trên QR).

    root_ids: [(root_category_id, tag)] — tag "lunch" cho menu Lunch Special để app
    khoá theo giờ. Nhóm con xếp theo ordinal của nhóm; món trong nhóm xếp theo
    ordinal của món trong nhóm đó. Nhóm rỗng (vd "Add-ons") bị bỏ.

    Modifier list tên `dining_list_name` (Have here / Take away) KHÔNG hiện cho khách
    chọn từng món — khách chọn 1 lần ở checkout, server tự gắn vào mọi món.

    Trả {"groups": [...], "dining": {"list_id", "here_id", "away_id"}}.
    """
    cats = {o["id"]: o for o in _catalog_list(token, env, "CATEGORY") if not o.get("is_deleted")}
    images = {o["id"]: (o.get("image_data") or {}).get("url")
              for o in _catalog_list(token, env, "IMAGE")}
    mlists = {o["id"]: o for o in _catalog_list(token, env, "MODIFIER_LIST") if not o.get("is_deleted")}
    items = [o for o in _catalog_list(token, env, "ITEM")
             if not o.get("is_deleted") and not (o.get("item_data") or {}).get("is_archived")]

    dining = {"list_id": None, "here_id": None, "away_id": None}
    for mid, m in mlists.items():
        md = m.get("modifier_list_data") or {}
        if (md.get("name") or "").strip().lower() == dining_list_name.strip().lower():
            dining["list_id"] = mid
            for x in md.get("modifiers") or []:
                n = ((x.get("modifier_data") or {}).get("name") or "").strip().lower()
                if n in ("have here", "dine in", "eat in"):
                    dining["here_id"] = x["id"]
                elif n in ("take away", "takeaway", "to go"):
                    dining["away_id"] = x["id"]

    def item_ord(it, cat_id):
        for c in (it.get("item_data") or {}).get("categories") or []:
            if c.get("id") == cat_id:
                return c.get("ordinal") or 0
        return 0

    def build_item(o):
        idata = o.get("item_data") or {}
        variations = []
        for v in idata.get("variations") or []:
            vdata = v.get("item_variation_data") or {}
            price = (vdata.get("price_money") or {}).get("amount")
            if price is None:          # giá mở: không đặt qua QR được
                continue
            variations.append({"id": v["id"], "name": (vdata.get("name") or "").strip(),
                               "price": int(price),
                               "sold_out": _sold_out(v, location_id)})
        if not variations:
            return None
        mods = []
        infos = sorted(idata.get("modifier_list_info") or [], key=lambda x: x.get("ordinal") or 0)
        for info in infos:
            if info.get("enabled") is False or info.get("hidden_from_customer"):
                continue
            lid = info.get("modifier_list_id")
            if lid == dining["list_id"]:
                continue
            ml = (mlists.get(lid) or {}).get("modifier_list_data") or {}
            options = []
            for x in sorted(ml.get("modifiers") or [],
                            key=lambda x: (x.get("modifier_data") or {}).get("ordinal") or 0):
                md = x.get("modifier_data") or {}
                if x.get("is_deleted"):
                    continue
                options.append({"id": x["id"], "name": (md.get("name") or "").strip(),
                                "price": int((md.get("price_money") or {}).get("amount") or 0)})
            if not options:
                continue
            single = ml.get("selection_type") == "SINGLE"
            mn = info.get("min_selected_modifiers")
            mx = info.get("max_selected_modifiers")
            if mn is None or mn < 0:
                mn = ml.get("min_selected_modifiers")
            if mx is None or mx < 0:
                mx = ml.get("max_selected_modifiers")
            mn = int(mn) if (mn is not None and mn >= 0) else 0
            mx = int(mx) if (mx is not None and mx > 0) else (1 if single else len(options))
            if single:
                mx = 1
            mn = min(mn, mx)
            mods.append({"id": lid, "name": (ml.get("name") or "").strip(),
                         "min": mn, "max": mx, "options": options})
        desc = (idata.get("description_plaintext") or idata.get("description") or "").strip()
        img = next((images.get(i) for i in (idata.get("image_ids") or []) if images.get(i)), None)
        return {"id": o["id"], "name": (idata.get("name") or "—").strip(), "description": desc,
                "image": img, "variations": variations, "modifiers": mods,
                "has_dining": dining["list_id"] in [i.get("modifier_list_id") for i in infos]}

    built = {}
    groups = []
    for root_id, tag in root_ids:
        kids = [c for c in cats.values()
                if ((c.get("category_data") or {}).get("parent_category") or {}).get("id") == root_id]
        kids.sort(key=lambda c: ((c["category_data"].get("parent_category") or {}).get("ordinal") or 0))
        for k in kids:
            members = [it for it in items
                       if any(c.get("id") == k["id"] for c in (it["item_data"].get("categories") or []))]
            members.sort(key=lambda it: item_ord(it, k["id"]))
            out = []
            for it in members:
                if it["id"] not in built:
                    built[it["id"]] = build_item(it)
                if built[it["id"]]:
                    out.append(built[it["id"]])
            if out:
                groups.append({"id": k["id"], "name": k["category_data"].get("name", "").strip(),
                               "tag": tag, "items": out})
    return {"groups": groups, "dining": dining}


# ---------------------------------------------------------------------------
# Đơn + thanh toán
# ---------------------------------------------------------------------------
def create_order(token, env, location_id, ticket_name, line_items, metadata,
                 recipient_name, note, idem):
    """Tạo đơn OPEN có fulfillment PICKUP (Square không cho tạo DINE_IN qua API;
    KDS suy dine-in/takeaway từ ticket_name + modifier Have here/Take away).
    Ghi chú cả đơn -> pickup_details.note (KDS hiện băng ghi chú đầu vé)."""
    pickup = {"recipient": {"display_name": recipient_name[:255] or ticket_name},
              "schedule_type": "ASAP"}
    if note:
        pickup["note"] = note[:500]
    order = {
        "location_id": location_id,
        "ticket_name": ticket_name[:30],
        "line_items": line_items,
        "state": "OPEN",
        "metadata": metadata,
        "fulfillments": [{"type": "PICKUP", "state": "PROPOSED", "pickup_details": pickup}],
    }
    return _request("POST", "/v2/orders", token, env,
                    {"order": order, "idempotency_key": idem}).get("order", {})


def create_payment(token, env, location_id, order_id, amount, currency, source_id,
                   verification_token, idem, note=None, buyer_email=None):
    body = {
        "idempotency_key": idem,
        "source_id": source_id,
        "amount_money": {"amount": int(amount), "currency": currency},
        "order_id": order_id,
        "location_id": location_id,
        "autocomplete": True,
    }
    if verification_token:
        body["verification_token"] = verification_token
    if note:
        body["note"] = note[:500]
    if buyer_email:
        body["buyer_email_address"] = buyer_email[:255]
    return _request("POST", "/v2/payments", token, env, body, timeout=30).get("payment", {})


def retrieve_order(token, env, order_id):
    return _request("GET", "/v2/orders/" + order_id, token, env).get("order", {})


def cancel_order(token, env, order_id):
    """Huỷ đơn chưa trả được tiền (thẻ bị từ chối) — đơn không nằm lì OPEN trên Square."""
    current = retrieve_order(token, env, order_id)
    if current.get("state") != "OPEN":
        return current
    order = {"version": current.get("version"), "state": "CANCELED"}
    ffs = [{"uid": f["uid"], "state": "CANCELED"}
           for f in (current.get("fulfillments") or [])
           if f.get("uid") and f.get("state") not in ("COMPLETED", "CANCELED", "FAILED")]
    if ffs:
        order["fulfillments"] = ffs
    return _request("PUT", "/v2/orders/" + order_id, token, env,
                    {"order": order, "idempotency_key": uuid.uuid4().hex}).get("order", {})


def search_open_qr_orders(token, env, location_id):
    body = {"location_ids": [location_id],
            "query": {"filter": {"state_filter": {"states": ["OPEN"]}},
                      "sort": {"sort_field": "CREATED_AT", "sort_order": "DESC"}},
            "limit": 100}
    orders = _request("POST", "/v2/orders/search", token, env, body).get("orders", [])
    return [o for o in orders if (o.get("metadata") or {}).get("qr_app") == APP_TAG]
