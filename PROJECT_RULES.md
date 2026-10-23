# PROJECT_RULES.md — Tool System

## Mục tiêu
Xây dựng Tool System Python tối giản chạy trên Kaggle CPU, tái sử dụng Chrome hiện có qua CDP 9222 để xử lý quy trình video từ nguồn đến Facebook. Đây là hệ thống công cụ, không phải AI Agent.

## Quy tắc bắt buộc
1. Không bịa trạng thái: chỉ báo PASS/VERIFIED/hoàn thành khi có bằng chứng thực tế; phân biệt kiểm tra tĩnh, unit test và runtime.
2. Không lặp vô hạn: giới hạn mọi retry, ghi log lỗi và lưu checkpoint để tiếp tục từ bước cuối đã xác minh.
3. Chống tác dụng phụ trùng lặp theo job/video/trang. Nếu trạng thái đăng Facebook chưa rõ, kiểm tra Facebook trước khi thử đăng lại.
4. Không phá hạ tầng: dùng Chrome hiện có qua CDP 9222; không khởi chạy Chrome thứ hai hoặc đổi hạ tầng ngoài phạm vi đã duyệt.
5. Giữ kiến trúc tối giản; không thêm agent, server, database hay dịch vụ mới nếu chưa cần thiết và chưa được duyệt.
6. Git: làm trực tiếp trên main; không tạo nhánh phụ trừ khi được duyệt rõ.
7. Không commit cookie, token, mật khẩu, khóa API, dữ liệu phiên Chrome hoặc bí mật trong log. Dùng secrets/biến môi trường và không in giá trị bí mật.
8. Chỉ dùng Supabase của Tool System sau khi project mới được xác nhận; không dùng project cũ của Money AI.
9. Mỗi bước có tiêu chí đạt và bằng chứng; chỉ đi tiếp khi bước hiện tại đạt. Khi lỗi, sửa đúng phần lỗi, tránh refactor phần đã đạt nếu không bắt buộc.
10. Trong kiểm thử, không đăng nội dung thật, thay đổi tài khoản, tạo tài nguyên cloud hoặc kích hoạt lịch đăng thật nếu chưa được duyệt.

## Quy trình nghiệp vụ đích
1. Thu thập video nguồn từ nguồn được cấu hình.
2. Xác minh video nguồn và lưu hash.
3. Dùng Gemini Web phân tích video/âm thanh, tạo kịch bản gốc rồi viết lại kịch bản mới.
4. Dùng Meta AI Web tạo video.
5. Tải xuống và xác minh file đầu ra trước khi đánh dấu VERIFIED.
6. Lên lịch đăng Facebook theo từng trang, mặc định 07:30 Asia/Ho_Chi_Minh; vẫn cho phép đăng thủ công bất kỳ lúc nào.
7. Lịch và đăng thủ công có trạng thái/chống trùng riêng; thao tác UI trên cùng Chrome phải được tuần tự hóa.
8. Ghi log, trạng thái và checkpoint để phục hồi khi Kaggle session bị gián đoạn.

## Điều kiện vận hành
- Lịch chạy trong tiến trình Kaggle không hoạt động khi session đã dừng. Không tuyên bố lịch 24/7 nếu chưa có cơ chế chạy liên tục được duyệt và xác minh.
- Không lưu bí mật dạng văn bản thuần trong bảng hoặc log.
- Thao tác Gemini/Meta/Facebook phải kiểm tra UI và kết quả thực tế; lệnh chạy không đồng nghĩa thành công.
- Trạng thái tối thiểu khi cần: PENDING, RUNNING, WAITING, VERIFIED, FAILED; tránh hệ thống trạng thái quá mức.
- Kiểm thử bằng dữ liệu thử nghiệm, không đăng thật. Đăng thật hoặc kích hoạt lịch thật là hành động riêng cần phê duyệt.

## Phạm vi triển khai
Bắt đầu bằng cấu trúc tối giản, chỉ kế thừa có chọn lọc thành phần đã kiểm tra từ Money AI. Không sao chép toàn bộ dự án cũ; CI PASS hoặc tài liệu không thay thế bằng chứng runtime.
