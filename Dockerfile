# Délice KDS — image chạy trên cloud (Render/Railway/Fly...)
FROM python:3.12-slim

WORKDIR /app
COPY . /app

# App dùng thư viện chuẩn. Chỉ cài thêm tzdata để zoneinfo tính đúng "hôm nay"
# theo giờ Sydney (kể cả giờ mùa hè) cho màn History — image slim thiếu tzdata.
RUN pip install --no-cache-dir tzdata

# Host tự đặt biến PORT; server.py đã đọc os.environ["PORT"].
CMD ["python", "server.py"]
