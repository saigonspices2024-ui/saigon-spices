# Deploy Délice KDS lên cloud

App đã đóng gói Docker, chạy được trên Render / Railway / Fly. Hướng dẫn dùng **Render** (miễn phí, deploy qua trình duyệt).

## Cần 2 tài khoản (miễn phí)
- **GitHub** — chứa mã nguồn.
- **Render** — chạy server (đăng nhập bằng GitHub cho nhanh).

## Các bước
1. **Đưa mã lên GitHub**
   - Tạo repo mới (vd `delice-kds`), private.
   - Đẩy code (đã commit sẵn ở `kds/`):
     ```
     git remote add origin https://github.com/<user>/delice-kds.git
     git push -u origin main
     ```
2. **Deploy trên Render**
   - New → Blueprint → chọn repo `delice-kds` (Render đọc `render.yaml`).
   - Hoặc New → Web Service → Runtime **Docker** → chọn repo.
3. **Đặt token Square** (bí mật)
   - Trong Render → Environment → thêm biến `SQUARE_ACCESS_TOKEN` = token của m.
   - `SQUARE_ENV` = `sandbox` (đổi `production` khi go-live).
   - ⚠️ Trên cloud dùng biến môi trường này, KHÔNG dùng file `.env` (container không giữ file).
4. **Xong** → Render cho một URL `https://delice-kds.onrender.com`.
   - Mở URL đó trên 2 tablet → `/kitchen` và `/expo`.

## Thêm vào màn hình chính (PWA — dùng như app thật)
Mỗi tablet mở đúng trang trạm của nó, rồi thêm ra màn hình chính. Sau đó bấm
icon là mở **toàn màn hình** (ẩn thanh địa chỉ), phân biệt bằng icon: **B** = Bếp
(viền cam), **E** = Expo (viền xanh).

- **iPad (Safari):** mở trang → nút Chia sẻ ⬆️ → *Thêm vào MH chính* → *Thêm*.
  Tên hiện sẵn "Bếp" / "Expo".
- **Android (Chrome):** mở trang → menu ⋮ → *Thêm vào Màn hình chính* (hoặc
  *Cài đặt ứng dụng*) → *Cài đặt*.

Máy Bếp mở `…/kitchen`, máy Expo mở `…/expo` trước khi thêm — icon sẽ mở lại đúng
trạm đó. Dùng LAN thì đường dẫn là `http://<IP máy server>:5252/kitchen`.
> Lưu ý LAN: iOS đôi khi cần HTTPS để "cài" thành app đầy đủ; qua `http://<IP>` vẫn
> thêm được lối tắt màn hình chính và mở toàn màn hình bình thường.

## Webhook real-time (tuỳ chọn, sau khi có URL)
- Square Developer → Webhooks → Subscriptions → thêm endpoint
  `https://<app>.onrender.com/webhooks/square`, chọn sự kiện
  `order.created`, `order.updated`, `order.fulfillment.updated`.
- Poll vẫn chạy song song làm lớp chống rớt đơn.

## Lưu ý gói Free của Render
- Dịch vụ free "ngủ" sau ~15 phút KHÔNG có kết nối. Khi tablet mở `/kitchen`,
  kết nối SSE giữ cho server thức suốt giờ mở cửa. Sáng mở máy lần đầu có thể
  chờ ~30–60 giây khởi động. Muốn không bao giờ ngủ: nâng gói ~$7/tháng.
