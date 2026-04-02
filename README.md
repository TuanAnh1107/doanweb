# Hệ thống quản lý lớp học thông minh

## Công nghệ sử dụng
- Frontend: HTML, CSS, JavaScript, Bootstrap
- Backend: Python Flask
- Database mặc định: SQLite
- Hỗ trợ Supabase Postgres

## Chạy nhanh bằng SQLite
1. Tạo và kích hoạt môi trường ảo:
   `python -m venv venv`  
   `.\venv\Scripts\Activate.ps1`
2. Tạo file `.env` tối thiểu:

```env
SECRET_KEY=thay_bang_mot_chuoi_bi_mat_dai
FLASK_DEBUG=true
```

3. Cài thư viện:
   `pip install -r requirements.txt`
4. Chạy project:
   `python app.py`

## Tài khoản mẫu
- Giảng viên:
  - Email: `tungnv@hust.edu.vn`
  - Mật khẩu: `tungdzaimica`
- Sinh viên:
  - Email: `anh.nt231556@sis.hust.edu.vn`
  - Hoặc mã sinh viên: `20231556`
  - Mật khẩu: `1`

Hệ thống tự nhận diện vai trò:
- Email đuôi `@sis.hust.edu.vn` hoặc mã số: đăng nhập vai trò sinh viên
- Email còn lại: đăng nhập vai trò giảng viên

## Trình tự demo từ đăng nhập đến thao tác chính
### Luồng giảng viên
1. Mở `/login`.
2. Đăng nhập bằng tài khoản giảng viên.
3. Vào `Dashboard` để xem tổng quan nhanh.
4. Vào `Lớp học`:
   - Tạo lớp mới hoặc mở lớp có sẵn.
   - Vào chi tiết lớp để xem danh sách sinh viên.
   - Thêm sinh viên (bằng mã/email hoặc tạo nhanh tài khoản mới).
5. Vào `Điểm danh`:
   - Tạo buổi học.
   - Vào chi tiết buổi để cập nhật có mặt/muộn/vắng.
   - Đóng buổi khi chốt xong.
6. Vào `Bài tập`:
   - Tạo bài tập và hạn nộp.
   - Vào chi tiết bài để chấm điểm, nhận xét từng sinh viên.
7. (Tuỳ chọn) Xuất Excel ở trang chi tiết lớp, buổi điểm danh, bài tập.
8. Đăng xuất.

### Luồng sinh viên
1. Đăng nhập bằng email hoặc mã sinh viên.
2. Vào `Dashboard sinh viên` để xem:
   - Lớp đang học
   - Bài tập và trạng thái nộp
   - Điểm danh gần đây
3. Đăng xuất.

## Dùng Supabase Postgres
1. Tạo project trên Supabase.
2. Vào `Connect` và copy `Session pooler connection string`.
3. Tạo file `.env`:

```env
SECRET_KEY=thay_bang_mot_chuoi_bi_mat_dai
FLASK_DEBUG=false
DB_ENGINE=postgres
DATABASE_URL=postgresql://postgres.xxxxx:[YOUR-PASSWORD]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
```

4. Mở `SQL Editor` trên Supabase, chạy file `schema.sql`.
5. Chạy ứng dụng: `python app.py`

## Ghi chú
- Nếu không có file `.env`, project sẽ tự fallback về SQLite.
- Nếu password Supabase có ký tự đặc biệt, nên copy nguyên connection string từ dashboard.
- Mật khẩu trong database đã dùng hash, không so sánh plaintext trực tiếp.
- Không chia sẻ `.env`, `SECRET_KEY`, `DATABASE_URL` ra ngoài.
