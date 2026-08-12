# Délice KDS

Hệ thống màn hình bếp (Kitchen Display System) kết nối **Square**, 2 trạm:
**Bếp → Expo**. Web app chạy trên iPad và Android (Safari/Chrome), 1 codebase.

## Chạy thử (MVP, không cần cài gì)

```bash
python3 kds/server.py        # cần Python 3.7+ (chỉ dùng thư viện chuẩn)
```

Rồi mở:
- Bảng điều khiển + demo: http://localhost:5252/
- Màn Bếp:  http://localhost:5252/kitchen
- Màn Expo: http://localhost:5252/expo

Bấm **“Tạo đơn mẫu”** ở trang chủ để giả lập một đơn Square. Trên tablet,
mở URL rồi “Add to Home Screen” là thành app toàn màn hình.

## Luồng hoạt động

```
Square order ──(webhook)──► /webhooks/square ──► ticket state NEW
   màn BẾP hiển thị NEW  ──[bấm Xong]──►  READY
   màn EXPO hiển thị READY ──[bấm Đã giao]──►  COMPLETED
                                              └─► PATCH fulfillment về Square
```

Đồng bộ real-time giữa các tablet qua **SSE** (`/api/stream`).

## Nối Square thật (Giai đoạn 2)

1. Tạo app trong **Square Developer Dashboard**, lấy khóa **Sandbox** trước.
2. Đăng ký webhook trỏ về `POST /webhooks/square` với các sự kiện:
   `order.created`, `order.updated`, `order.fulfillment.updated`.
3. Dữ liệu đã được parse theo **Square Order object** (`parse_square_order`
   trong `server.py`), nên không phải sửa giao diện.
4. Điền token vào `push_completed_to_square()` để bump ngược trạng thái về Square.
5. Thêm **polling `SearchOrders`** làm lớp dự phòng cho webhook (chống rớt đơn).

## Deploy (rẻ ~$0/tháng)

Chạy được trên bất kỳ host nào có Python, hoặc port sang **Cloudflare Workers +
D1/KV** (khớp hạ tầng Cloudflare đang dùng cho web Délice). Với 1 quán / vài
tablet, nằm gọn trong gói miễn phí — thay cho phí ~$20–40/máy/tháng của Fresh KDS.

## Cấu trúc

```
kds/
  server.py            # HTTP + SSE + máy trạng thái + điểm nhận webhook Square
  public/
    index.html         # trang chủ + nút demo
    kitchen.html       # màn Bếp
    expo.html          # màn Expo
    kds.css            # giao diện tối, nút to cho tablet
    kds.js             # logic client chung (SSE, đồng hồ, âm báo, bump)
```
