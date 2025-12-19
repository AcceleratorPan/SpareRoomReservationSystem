# core/views.py

from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse
from django.core.mail import send_mail
from django.conf import settings
from django.core.signing import TimestampSigner, BadSignature
from django.db.models import Count, Q
from django.db import transaction  # 必须引入事务处理
from django.contrib.admin.views.decorators import staff_member_required # 引入权限装饰器
from django.contrib import messages
from .models import Student, Classroom, Reservation, PromotionRequest
from .models import TIME_SLOTS  # 实际从settings获取
from django.urls import reverse # 引入 reverse 用于生成链接
import urllib.parse
import datetime
import uuid

# --- 工具：生成签名URL ---
signer = TimestampSigner()

def generate_action_url(id_val, action, type_code='res'):
    """
    id_val: 可以是单个ID，也可以是逗号分隔的ID字符串
    """
    data = f"{type_code}:{id_val}:{action}"
    token = signer.sign(data)
    return f"{settings.SITE_DOMAIN}/admin-action/{token}/"

# --- 1. 首页 & 登录 ---
def index(request):
    if request.method == 'POST':
        sid = request.POST.get('student_id')
        password = request.POST.get('password')

        if not sid or not password:
            messages.error(request, "请输入学号和密码")
            return render(request, 'core/index.html')

        try:
            student = Student.objects.get(student_id=sid)
            # 如果是管理员在后台帮创建的占位账号，首次登录由用户设置密码并激活
            if student.is_auto_created:
                student.set_password(password)
                student.is_auto_created = False
                student.save()
                messages.success(request, f"👋 欢迎！已为学号 {sid} 设置登录密码，请妥善保管。")
            else:
                # 普通登录，验证密码
                if not student.check_password(password):
                    messages.error(request, "❌ 密码错误")
                    messages.info(request, "如忘记密码，请使用下方的【重置密码】功能。")
                    return render(request, 'core/index.html')

            # 检查黑名单
            if student.status == 'blacklist':
                messages.error(request, "❌ 您已被列入黑名单，禁止登录。")
                return render(request, 'core/index.html')

        except Student.DoesNotExist:
            # 新用户注册：使用学号和密码创建账号（不在 DB 中存邮箱）
            student = Student.objects.create(
                student_id=sid,
                role='user',
                is_auto_created=False,
            )
            student.set_password(password)
            student.save()
            messages.success(request, "🎉 新用户注册成功！")
            
        request.session['sid'] = student.id
        return redirect('booking')
        
    return render(request, 'core/index.html')


def logout_view(request):
    """清理 session 并重定向到首页（登录页）。"""
    try:
        request.session.flush()
    except Exception:
        pass
    messages.success(request, "已登出")
    return redirect('index')

# 2. 账号重置请求 (输入学号发邮件)
def reset_request(request):
    if request.method == 'POST':
        sid = request.POST.get('student_id')
        new_password = request.POST.get('new_password') # 获取用户想要设置的新密码
        
        if not sid or not new_password:
            messages.error(request, "请输入学号和新密码")
            return render(request, 'core/reset.html')

        try:
            student = Student.objects.get(student_id=sid)
            
            # 生成重置专用 Token (用于重置密码)
            # 数据格式: "reset:数据库ID:新密码"
            data = f"reset:{student.id}:{new_password}"
            token = signer.sign(data)
            
            # 生成链接
            reset_url = f"{settings.SITE_DOMAIN}/reset-confirm/{token}/"
            
            msg = f"""
            [密码重置确认]

            系统检测到您请求为学号 {student.student_id} 重置登录密码。

            点击下方链接确认修改。

            [确认重置密码]: {reset_url}
            """
            
            send_mail(
                subject=f"密码重置确认 - {student.student_id}",
                message=msg,
                from_email='system@school.edu',
                recipient_list=[student.email], # 发送给该学号绑定的原邮箱
            )
            
            messages.success(request, f"验证邮件已发送至{student.email}")
            return redirect('index')
            
        except Student.DoesNotExist:
            messages.error(request, "❌ 该学号不存在，无法重置。")
            
    return render(request, 'core/reset.html')

# 3.账号重置执行 (点击邮件链接)
def reset_confirm(request, token):
    try:
        # 验证签名 (有效期10分钟)
        data = signer.unsign(token, max_age=600)
        
        # 解析数据：只分割前两个冒号，剩下的都是名字
        parts = data.split(':', 2)
        if len(parts) != 3:
            raise BadSignature()

        type_code = parts[0]
        sid_db_id = parts[1]
        new_password = parts[2]

        if type_code != 'reset':
            raise BadSignature()

        student = Student.objects.get(id=sid_db_id)

        # 更新密码
        student.set_password(new_password)
        student.save()

        # 清理 Session（可选），要求使用新密码重新登录
        request.session.flush()

        messages.success(request, f"✅ 密码已重置成功，学号 {student.student_id} 请使用新密码登录。")
        return redirect('index')
        
    except (BadSignature, Student.DoesNotExist):
        return HttpResponse("❌ 链接无效、已过期或该账号异常。")
    
# --- 2. 可视化选座 (核心逻辑：状态计算) ---
def booking_view(request):
    sid = request.session.get('sid')
    if not sid: return redirect('index')
    student = Student.objects.get(id=sid)
    
    cls_id = request.GET.get('classroom_id')
    date_str = request.GET.get('date', datetime.date.today().strftime('%Y-%m-%d'))
    # 优先使用 URL 中的 slot 参数；如果未提供，则根据日期和当前时间选择默认时段
    slot_param = request.GET.get('slot')
    slot_id = None
    if slot_param:
        try:
            slot_id = int(slot_param)
        except Exception:
            slot_id = None

    # 解析日期为 date 对象，便于比较
    try:
        req_date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
    except Exception:
        req_date_obj = datetime.date.today()

    # 如果没有指定 slot，则确定一个合理的默认时段
    if slot_id is None:
        today = datetime.date.today()
        # 如果查看的是今天，选择距现在最近未开始的最早时段
        if req_date_obj == today:
            now_dt = datetime.datetime.now()
            chosen = None
            for s_id, s_label in TIME_SLOTS:
                # s_label 例如 '08:00 - 10:00'，取前半部分作为开始时间
                start_str = s_label.split('-')[0].strip()
                try:
                    h, m = map(int, start_str.split(':'))
                    slot_start = datetime.datetime.combine(today, datetime.time(h, m))
                    if slot_start > now_dt:
                        chosen = s_id
                        break
                except Exception:
                    continue
            # 若所有时段已过，则选择最后一个时段（保持页面显示合理值）
            if chosen is None:
                chosen = TIME_SLOTS[-1][0]
            slot_id = chosen
        else:
            # 非今天的页面，默认选择第一个时段（最早）
            slot_id = TIME_SLOTS[0][0]

    # 获取教室
    classrooms = Classroom.objects.filter(is_active=True)
    if not classrooms.exists():
        return HttpResponse("系统未配置教室，请先在后台添加教室。")
        
    if cls_id:
        curr_cls = get_object_or_404(Classroom, id=cls_id)
    else:
        curr_cls = classrooms.first()

    # 解析布局
    layout_lines = curr_cls.layout.strip().split('\n')
    
    # --- 获取该时段所有相关预约 ---
    # 我们需要知道哪些是 Approved (锁死)，哪些是 Pending (竞争中)
    # 注意：使用日期对象 req_date_obj 而非字符串 date_str 进行查询，确保与 DateField 正确匹配
    records = Reservation.objects.filter(
        classroom=curr_cls, date=req_date_obj, time_slot=slot_id,
        status__in=['approved', 'pending']
    ).values('seat_row', 'seat_col', 'status', 'student_id')
    
    # 预处理：将记录按坐标分组
    # cell_data[(r,c)] = {'approved_by_other': Bool, 'mine': Str|None, 'other_pending': Bool}
    cell_map = {}
    for r in records:
        key = (r['seat_row'], r['seat_col'])
        if key not in cell_map:
            cell_map[key] = {'approved_by_other': False, 'mine': None, 'other_pending': False}
        
        is_mine = (r['student_id'] == student.id)
        
        if r['status'] == 'approved':
            if is_mine:
                cell_map[key]['mine'] = 'approved'
            else:
                cell_map[key]['approved_by_other'] = True
        elif r['status'] == 'pending':
            if is_mine:
                # 只有当我没有 approved 记录时才设置 pending（防止覆盖）
                if cell_map[key]['mine'] != 'approved':
                    cell_map[key]['mine'] = 'pending'
            else:
                cell_map[key]['other_pending'] = True

    # --- 构建矩阵 ---
    matrix = []
    for r_idx, line in enumerate(layout_lines):
        row_data = []
        for c_idx, char in enumerate(line.strip()):
            cell = {
                'r': r_idx, 'c': c_idx, 
                'type': 'aisle' if char == '0' else 'seat', 
                'status': 'free', 
                'is_mine': False
            }
            
            if cell['type'] == 'seat':
                key = (r_idx, c_idx)
                data = cell_map.get(key)
                
                if data:
                    # 优先级 1: 如果被别人 Approved (锁死) —— 最高优先级，任何人都不能再选
                    if data['approved_by_other']:
                        cell['status'] = 'approved'  # 红色
                    
                    # 优先级 2: 如果是我申请的 (无论 approved 还是 pending)
                    elif data['mine']:
                        cell['is_mine'] = True
                        cell['status'] = data['mine']  # 'approved' or 'pending'
                        # 如果同时也有别人的 pending，保留标志以便前端显示"抢"角标
                        cell['other_pending'] = data.get('other_pending', False)
                    
                    # 优先级 3: 只有别人的 Pending (竞争中，可抢)
                    elif data['other_pending']:
                        cell['status'] = 'other_pending'  # 橙色
            
            row_data.append(cell)
        matrix.append(row_data)

    return render(request, 'core/booking.html', {
        'student': student,
        'classrooms': classrooms,
        'curr_cls': curr_cls,
        'matrix': matrix,
        'date': date_str,
        'today': datetime.date.today().strftime('%Y-%m-%d'),
        # 传入基于角色的最大可预约日期，方便前端限制日期控件
        'max_date': (datetime.date.today() + datetime.timedelta(days=(getattr(settings, 'RESERVATION_MAX_DAYS_AHEAD_MANAGER', 7) if student.role == 'manager' else getattr(settings, 'RESERVATION_MAX_DAYS_AHEAD', 2)))).strftime('%Y-%m-%d'),
        'time_slots': TIME_SLOTS,
        'current_slot': slot_id
        , 'promotion_show_button': getattr(settings, 'PROMOTION_SHOW_BUTTON', True)
        , 'promotion_enable_10click': getattr(settings, 'PROMOTION_ENABLE_10CLICK', True)
    })

# --- 3. 提交预约 (批量 + 竞争逻辑 + 防恶意) ---
def submit(request):
    if request.method == 'POST':
        try:
            sid = request.session.get('sid')
            student = Student.objects.get(id=sid)
            
            cid = request.POST.get('cid')
            date_str = request.POST.get('date')
            slot = int(request.POST.get('slot'))
            seats_str = request.POST.get('seats_list')
            # --- 时间合法性检查 ---
            try:
                req_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            except Exception:
                message = "❌ 无效的日期格式"
                next_url = request.META.get('HTTP_REFERER', reverse('booking'))
                return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")

            today = datetime.date.today()
            # 根据用户角色决定最大可预约天数（负责人有更长权限）
            if getattr(student, 'role', None) == 'manager':
                max_ahead = getattr(settings, 'RESERVATION_MAX_DAYS_AHEAD_MANAGER', 7)
            else:
                max_ahead = getattr(settings, 'RESERVATION_MAX_DAYS_AHEAD', 2)

            if req_date < today:
                message = "❌ 预约时间不能早于当前日期"
                next_url = request.META.get('HTTP_REFERER', reverse('booking'))
                return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")

            if req_date > today + datetime.timedelta(days=max_ahead):
                message = f"❌ 只能预约未来 {max_ahead} 天内的时间段"
                next_url = request.META.get('HTTP_REFERER', reverse('booking'))
                return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")

            # 预约时间窗口检查：必须在时间段开始前 30 分钟之前完成预约
            # 获取配置的预约截止提前时间（分钟）
            booking_deadline_minutes = getattr(settings, 'RESERVATION_BOOKING_WINDOW_MINUTES', 30)
            
            slot_label = dict(TIME_SLOTS).get(slot, "")
            if slot_label:
                start_str = slot_label.split('-')[0].strip()
                try:
                    h, m = map(int, start_str.split(':'))
                    slot_start = datetime.datetime.combine(req_date, datetime.time(h, m))
                    now_dt = datetime.datetime.now()
                    
                    # 计算预约截止时间：时间段开始前 N 分钟
                    booking_deadline = slot_start - datetime.timedelta(minutes=booking_deadline_minutes)
                    
                    # 检查是否已超过预约截止时间
                    if now_dt >= booking_deadline:
                        message = f"❌ 预约已截止<br>该时间段的预约截止时间为 <strong>{booking_deadline.strftime('%Y-%m-%d %H:%M')}</strong>（开始前{booking_deadline_minutes}分钟）"
                        next_url = request.META.get('HTTP_REFERER', reverse('booking'))
                        return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")
                except Exception:
                    pass
            
            if not seats_str:
                message = "未选择座位"
                next_url = request.META.get('HTTP_REFERER', reverse('booking'))
                return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")
            seats_list = seats_str.split(',')

            # --- 防御策略 A: 限制单人最大待审核 "批次" (Batch) ---
            # 逻辑修改：不再统计具体的座位数，而是统计 Pending 的订单数
            # 这样负责人一次约 10 个座位，只算作 1 个请求
            MAX_PENDING_BATCHES = 3
            
            current_pending_batches = Reservation.objects.filter(
                student=student, 
                status='pending', 
                date__gte=datetime.date.today()
            ).values('batch_id').distinct().count() # <--- 使用 distinct 统计批次
            
            # 如果是新请求（还未创建），允许存在 MAX_PENDING_BATCHES - 1 个旧请求
            if current_pending_batches >= MAX_PENDING_BATCHES:
                message = f"🚫 <strong>操作受限</strong><br>您当前已有 {current_pending_batches} 个待审核的预约申请单。<br>系统限制每人最多同时保留 {MAX_PENDING_BATCHES} 个待审单，请等待管理员处理。"
                next_url = request.META.get('HTTP_REFERER', reverse('booking'))
                return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")

            # --- 防御策略 B: 普通用户限制 ---
            if student.role == 'user':
                if len(seats_list) > 1:
                    message = "❌ 普通用户单次只能预约 1 个座位。"
                    next_url = request.META.get('HTTP_REFERER', reverse('booking'))
                    return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")
                
                # 检查该时间段是否已有其他批次的预约
                has_booking = Reservation.objects.filter(
                    student=student, date=date_str, time_slot=slot,
                    status__in=['approved', 'pending']
                ).exists()
                if has_booking:
                    message = "❌ 您在该时间段已有预约。"
                    next_url = request.META.get('HTTP_REFERER', reverse('booking'))
                    return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")

            # --- 事务操作：批量创建 (分配同一个 batch_id) ---
            new_reservations = []
            seat_labels = []
            
            # 生成本次交易的唯一 ID
            this_batch_id = uuid.uuid4()
            
            with transaction.atomic():
                for s in seats_list:
                    r, c = map(int, s.split('-'))
                    
                    # 冲突检测 (硬锁)
                    is_hard_locked = Reservation.objects.filter(
                        classroom_id=cid, seat_row=r, seat_col=c, 
                        date=date_str, time_slot=slot, 
                        status='approved'
                    ).exists()
                    
                    if is_hard_locked:
                        raise ValueError(f"座位 {r+1}行-{c+1}列 刚刚被抢走。")
                    
                    res = Reservation.objects.create(
                        student=student, classroom_id=cid,
                        seat_row=r, seat_col=c, date=date_str, time_slot=slot,
                        status='pending',
                        batch_id=this_batch_id  # <--- 写入批次ID
                    )
                    new_reservations.append(res)
                    seat_labels.append(f"{r+1}行{c+1}列")

            # --- 发送邮件 ---
            res_ids_str = ",".join([str(r.id) for r in new_reservations])
            approve_url = generate_action_url(res_ids_str, 'approve', 'res')
            reject_url = generate_action_url(res_ids_str, 'reject', 'res')
            slot_name = dict(TIME_SLOTS).get(slot, "")
            
            msg = f"""
            [预约申请]
            申请人: {student.student_id} ({student.get_role_display()})
            座位数: {len(new_reservations)}
            时间: {date_str} {slot_name}
            座位列表: {', '.join(seat_labels)}
            
            [一键批准]: {approve_url}
            [一键拒绝]: {reject_url}
            """
            
            send_mail(
                f"申请({len(new_reservations)}座) - {student.student_id}",
                msg, 'sys@edu.cn', [settings.ADMIN_EMAIL]
            )

            # 重定向到 info 页面，显示成功信息并在 5 秒后返回
            message = f"✅ 申请已提交！包含 {len(new_reservations)} 个座位。"
            next_url = request.META.get('HTTP_REFERER', reverse('booking'))
            return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=success")

        except ValueError as e:
            message = f"❌ {str(e)}"
            next_url = request.META.get('HTTP_REFERER', reverse('booking'))
            return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")
        except Exception as e:
            message = f"系统错误: {str(e)}"
            next_url = request.META.get('HTTP_REFERER', reverse('booking'))
            return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")
            
    return redirect('booking')


def info(request):
    """显示简单信息并在若干秒后跳回上一个页面。

    URL 参数:
      - msg: 要显示的信息（已 URL 编码）
      - next: 跳回的 URL（可选），如果为空则使用 history.back()
    """
    msg = request.GET.get('msg', '')
    next_url = request.GET.get('next', '')
    status_type = request.GET.get('type', '')  # expected 'success' or 'error'
    # 解码
    try:
        msg = urllib.parse.unquote_plus(msg)
        next_url = urllib.parse.unquote_plus(next_url)
    except Exception:
        pass

    return render(request, 'core/info.html', {'message': msg, 'next_url': next_url, 'status_type': status_type})

# --- 4. 管理员审批 (批量 + 自动解决竞争) ---
def admin_action(request, token):
    try:
        data = signer.unsign(token, max_age=86400)
        parts = data.split(':')
        type_code = parts[0]
        id_vals_str = parts[1]
        action = parts[2]
    except (BadSignature, IndexError):
        return HttpResponse("❌ 链接无效或已过期。")

    if type_code == 'res':
        ids = id_vals_str.split(',')
        # 检查这些 id 中是否有已被用户取消的记录
        all_found = Reservation.objects.filter(id__in=ids)
        cancelled = all_found.filter(status='cancelled')
        cancelled_count = cancelled.count()

        # 只对仍为 pending 的记录执行审批
        target_res = all_found.filter(status='pending')

        if not target_res.exists():
            if cancelled_count > 0:
                return HttpResponse(f"操作已跳过：{cancelled_count} 个申请已被用户取消。")
            # 检查是否有已过期的
            expired = all_found.filter(status='expired')
            if expired.exists():
                return HttpResponse(f"操作已跳过：{expired.count()} 个申请已过期（超过操作截止时间）。")
            return HttpResponse("相关申请已被处理或不存在。")
        
        # 检查时间截止：如果已超过截止时间，自动将pending标记为expired
        deadline_minutes = getattr(settings, 'RESERVATION_BOOKING_WINDOW_MINUTES', 30)
        now_dt = datetime.datetime.now()
        expired_count = 0
        valid_res = []
        
        for res in target_res:
            slot_label = dict(TIME_SLOTS).get(res.time_slot, "")
            is_expired = False
            if slot_label:
                start_str = slot_label.split('-')[0].strip()
                try:
                    h, m = map(int, start_str.split(':'))
                    slot_start = datetime.datetime.combine(res.date, datetime.time(h, m))
                    deadline = slot_start - datetime.timedelta(minutes=deadline_minutes)
                    if now_dt >= deadline:
                        is_expired = True
                except Exception:
                    pass
            
            if is_expired:
                # 自动标记为过期
                res.status = 'expired'
                res.save()
                expired_count += 1
            else:
                valid_res.append(res)
        
        if expired_count > 0 and not valid_res:
            return HttpResponse(f"操作已跳过：{expired_count} 个申请已自动标记为过期（超过操作截止时间：开始前{deadline_minutes}分钟）。")
        
        if not valid_res:
            if cancelled_count > 0:
                return HttpResponse(f"操作已跳过：{cancelled_count} 个申请已被用户取消。")
            return HttpResponse("相关申请已被处理或不存在。")

        success_count = 0
        auto_reject_count = 0

        with transaction.atomic():
            for res in valid_res:  # 使用过滤后的有效预约列表
                if action == 'approve':
                    # A. 双重检查：是否被抢先 Approved 了
                    is_taken = Reservation.objects.filter(
                        classroom=res.classroom, seat_row=res.seat_row, seat_col=res.seat_col,
                        date=res.date, time_slot=res.time_slot, status='approved'
                    ).exists()

                    if is_taken:
                        res.status = 'rejected' # 手慢了，被别人抢了
                        res.save()
                        continue

                    # B. 批准当前请求
                    res.status = 'approved'
                    res.save()
                    success_count += 1

                    # C. 自动驳回竞争者
                    competitors = Reservation.objects.filter(
                        classroom=res.classroom, seat_row=res.seat_row, seat_col=res.seat_col,
                        date=res.date, time_slot=res.time_slot, status='pending'
                    ).exclude(id=res.id)

                    c_cnt = competitors.update(status='rejected')
                    auto_reject_count += c_cnt

                elif action == 'reject':
                    res.status = 'rejected'
                    res.save()
                    success_count += 1

        msg = f"操作完成：{action} {success_count} 个请求。"
        if auto_reject_count > 0:
            msg += f" (同时自动驳回了 {auto_reject_count} 个冲突的竞争请求)"
        if expired_count > 0:
            msg += f" 注意：有 {expired_count} 个申请已过期，已自动标记。"
        if cancelled_count > 0:
            msg += f" 注意：有 {cancelled_count} 个申请已被用户取消，已跳过。"
        return HttpResponse(msg)

    elif type_code == 'stu':
        try:
            stu = Student.objects.get(id=id_vals_str)
            if action == 'promote':
                stu.role = 'manager'
                stu.save()
                # 标记相关 PromotionRequest 为通过
                try:
                    now = datetime.datetime.now()
                    PromotionRequest.objects.filter(student=stu, status='pending').update(
                        status='approved', reviewed_at=now, reviewer=request.META.get('REMOTE_USER','')
                    )
                    # 发送通知邮件给申请人，邮件中包含跳转到 /info/ 的链接，便于用户查看审批结果
                    info_msg = "恭喜，您的升级申请已被批准。"
                    info_url = f"{settings.SITE_DOMAIN}{reverse('info')}?msg={urllib.parse.quote_plus(info_msg)}&type=success"
                    send_mail(f"升级申请已通过 - {stu.student_id}", f"您的申请已被批准。详情：{info_url}", 'sys@edu.cn', [stu.email])
                except Exception:
                    pass
                return HttpResponse(f"已将 {stu.student_id} 升级为负责人。")
            elif action == 'reject':
                # 将 pending 的申请标记为拒绝
                try:
                    now = datetime.datetime.now()
                    PromotionRequest.objects.filter(student=stu, status='pending').update(
                        status='rejected', reviewed_at=now, reviewer=request.META.get('REMOTE_USER','')
                    )
                    # 通知申请人被拒绝，邮件中包含 /info/ 链接
                    info_msg = "很抱歉，您的升级申请已被拒绝。"
                    info_url = f"{settings.SITE_DOMAIN}{reverse('info')}?msg={urllib.parse.quote_plus(info_msg)}&type=error"
                    send_mail(f"升级申请被拒绝 - {stu.student_id}", f"您的申请已被拒绝。详情：{info_url}", 'sys@edu.cn', [stu.email])
                except Exception:
                    pass
                return HttpResponse(f"已拒绝 {stu.student_id} 的升级申请。")
        except Student.DoesNotExist:
            return HttpResponse("学生不存在")

    return HttpResponse("未知操作")

@staff_member_required # 只有登录了后台的管理员才能访问
def admin_booking_view(request):
    # 1. 获取筛选参数
    cls_id = request.GET.get('classroom_id')
    date_str = request.GET.get('date', datetime.date.today().strftime('%Y-%m-%d'))
    slot_param = request.GET.get('slot')
    slot_id = None
    if slot_param:
        try:
            slot_id = int(slot_param)
        except Exception:
            slot_id = None

    try:
        req_date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
    except Exception:
        req_date_obj = datetime.date.today()

    if slot_id is None:
        today = datetime.date.today()
        if req_date_obj == today:
            now_dt = datetime.datetime.now()
            chosen = None
            for s_id, s_label in TIME_SLOTS:
                start_str = s_label.split('-')[0].strip()
                try:
                    h, m = map(int, start_str.split(':'))
                    slot_start = datetime.datetime.combine(today, datetime.time(h, m))
                    if slot_start > now_dt:
                        chosen = s_id
                        break
                except Exception:
                    continue
            if chosen is None:
                chosen = TIME_SLOTS[-1][0]
            slot_id = chosen
        else:
            slot_id = TIME_SLOTS[0][0]
    
    # 获取教室
    classrooms = Classroom.objects.filter(is_active=True)
    if not classrooms.exists(): return HttpResponse("无可用教室")
    curr_cls = get_object_or_404(Classroom, id=cls_id) if cls_id else classrooms.first()

    # 计算时间段开始时间和预约截止时间
    booking_deadline_minutes = getattr(settings, 'RESERVATION_BOOKING_WINDOW_MINUTES', 30)
    slot_label = dict(TIME_SLOTS).get(slot_id, "")
    slot_start_dt = None
    booking_deadline_dt = None
    can_book = True
    booking_error_msg = ""
    
    if slot_label:
        start_str = slot_label.split('-')[0].strip()
        try:
            h, m = map(int, start_str.split(':'))
            slot_start_dt = datetime.datetime.combine(req_date_obj, datetime.time(h, m))
            now_dt = datetime.datetime.now()
            
            # 计算预约截止时间：时间段开始前 N 分钟
            booking_deadline_dt = slot_start_dt - datetime.timedelta(minutes=booking_deadline_minutes)
            
            # 检查是否已超过预约截止时间
            if now_dt >= booking_deadline_dt:
                can_book = False
                booking_error_msg = f"预约已截止（截止时间: {booking_deadline_dt.strftime('%H:%M')}，开始前{booking_deadline_minutes}分钟）"
        except Exception:
            pass

    # 2. 处理提交 (Admin 直接帮学生预约)
    if request.method == 'POST':
        # 检查预约时间窗口
        if not can_book:
            messages.error(request, f"❌ {booking_error_msg}")
            return redirect(f"{request.path}?classroom_id={curr_cls.id}&date={date_str}&slot={slot_id}")
        
        target_sid = request.POST.get('target_student_id')
        seats_str = request.POST.get('seats_list')
        
        try:
            if not target_sid:
                messages.error(request, "请输入学号")
                return redirect(request.get_full_path())

            # --- 修改点：设置 is_auto_created=True ---
            target_student, created = Student.objects.get_or_create(
                student_id=target_sid,
                defaults={
                    'role': 'user',
                    'status': 'normal',
                    'is_auto_created': True  # <--- 标记为自动创建
                }
            )

            if created:
                messages.warning(request, f"📢 已自动创建临时账号 {target_sid}。学生首次登录时设置密码即可激活。")
            
            # 检查是否在黑名单
            if target_student.status == 'blacklist':
                messages.error(request, f"❌ 操作失败：学生 {target_student.student_id} 处于黑名单中，无法预约。")
                return redirect(request.get_full_path())

            seats_list = seats_str.split(',')
            created_count = 0
            
            with transaction.atomic():
                batch_uuid = uuid.uuid4()
                for s in seats_list:
                    r, c = map(int, s.split('-'))
                    
                    # 检查硬锁 (Approved)
                    is_taken = Reservation.objects.filter(
                        classroom_id=curr_cls.id, seat_row=r, seat_col=c, 
                        date=date_str, time_slot=slot_id, 
                        status='approved'
                    ).exists()
                    
                    if is_taken:
                        continue 
                    
                    # 踢掉 Pending 竞争者
                    Reservation.objects.filter(
                        classroom_id=curr_cls.id, seat_row=r, seat_col=c, 
                        date=date_str, time_slot=slot_id, status='pending'
                    ).update(status='rejected')

                    # 创建预约
                    Reservation.objects.create(
                        student=target_student,
                        classroom=curr_cls,
                        seat_row=r, seat_col=c, date=date_str, time_slot=slot_id,
                        status='approved',
                        batch_id=batch_uuid,
                        is_admin_action=True   # 标记为管理员操作
                    )
                    created_count += 1
            
            if created_count > 0:
                messages.success(request, f"✅ 已成功为 {target_student.student_id} ({target_sid}) 预约 {created_count} 个座位！")
            else:
                messages.warning(request, "⚠️ 未能预约任何座位（可能所选座位已被占用）。")
                
            return redirect(f"{request.path}?classroom_id={curr_cls.id}&date={date_str}&slot={slot_id}")
            
        except Exception as e:
            messages.error(request, f"操作失败: {str(e)}")

    # 3. 渲染视图 (逻辑同普通用户，但不需要判断 'is_mine')
    layout_lines = curr_cls.layout.strip().split('\n')
    
    records = Reservation.objects.filter(
        classroom=curr_cls, date=date_str, time_slot=slot_id,
        status__in=['approved', 'pending']
    ).values('seat_row', 'seat_col', 'status', 'student__student_id') # 获取学生学号用于 Admin 查看
    
    cell_map = {(r['seat_row'], r['seat_col']): r for r in records}
    
    matrix = []
    for r_idx, line in enumerate(layout_lines):
        row_data = []
        for c_idx, char in enumerate(line.strip()):
            cell = {'r': r_idx, 'c': c_idx, 'type': 'aisle' if char == '0' else 'seat', 'status': 'free'}
            
            if cell['type'] == 'seat':
                key = (r_idx, c_idx)
                if key in cell_map:
                    rec = cell_map[key]
                    if rec['status'] == 'approved':
                        cell['status'] = 'approved'
                        cell['info'] = f"已占: {rec['student__student_id']}"
                    else:
                        cell['status'] = 'pending'
                        cell['info'] = f"待审: {rec['student__student_id']}"
            row_data.append(cell)
        matrix.append(row_data)

    return render(request, 'core/admin_booking.html', {
        'classrooms': classrooms, 'curr_cls': curr_cls,
        'matrix': matrix, 'date': date_str, 'today': datetime.date.today().strftime('%Y-%m-%d'),
        'time_slots': TIME_SLOTS, 'current_slot': slot_id,
        'can_book': can_book,
        'booking_error_msg': booking_error_msg,
        'booking_deadline_minutes': booking_deadline_minutes,
        'booking_deadline_time': booking_deadline_dt.strftime('%Y-%m-%d %H:%M') if booking_deadline_dt else '',
    })


# --- 管理员可视化取消预约 ---
@staff_member_required
def admin_cancel_view(request):
    """管理员可视化取消预约页面"""
    # 1. 获取筛选参数
    cls_id = request.GET.get('classroom_id')
    date_str = request.GET.get('date', datetime.date.today().strftime('%Y-%m-%d'))
    slot_param = request.GET.get('slot')
    slot_id = None
    if slot_param:
        try:
            slot_id = int(slot_param)
        except Exception:
            slot_id = None

    try:
        req_date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
    except Exception:
        req_date_obj = datetime.date.today()

    # 默认时段选择逻辑
    if slot_id is None:
        today = datetime.date.today()
        if req_date_obj == today:
            now_dt = datetime.datetime.now()
            chosen = None
            for s_id, s_label in TIME_SLOTS:
                start_str = s_label.split('-')[0].strip()
                try:
                    h, m = map(int, start_str.split(':'))
                    slot_start = datetime.datetime.combine(today, datetime.time(h, m))
                    if slot_start > now_dt:
                        chosen = s_id
                        break
                except Exception:
                    continue
            if chosen is None:
                chosen = TIME_SLOTS[-1][0]
            slot_id = chosen
        else:
            slot_id = TIME_SLOTS[0][0]
    
    # 获取教室
    classrooms = Classroom.objects.filter(is_active=True)
    if not classrooms.exists():
        return HttpResponse("无可用教室")
    curr_cls = get_object_or_404(Classroom, id=cls_id) if cls_id else classrooms.first()

    # 计算时间段开始时间和取消窗口
    cancel_window_minutes = getattr(settings, 'RESERVATION_BOOKING_WINDOW_MINUTES', 30)
    slot_label = dict(TIME_SLOTS).get(slot_id, "")
    slot_start_dt = None
    can_cancel = True
    
    if slot_label:
        start_str = slot_label.split('-')[0].strip()
        try:
            h, m = map(int, start_str.split(':'))
            slot_start_dt = datetime.datetime.combine(req_date_obj, datetime.time(h, m))
            now_dt = datetime.datetime.now()
            # 只能在开始前 cancel_window_minutes 分钟之前取消
            cancel_deadline = slot_start_dt - datetime.timedelta(minutes=cancel_window_minutes)
            can_cancel = now_dt < cancel_deadline
        except Exception:
            pass

    # 2. 处理取消提交
    if request.method == 'POST':
        res_ids_str = request.POST.get('reservation_ids', '')
        if res_ids_str:
            res_ids = [int(x) for x in res_ids_str.split(',') if x.strip()]
            
            # 获取要取消的预约
            reservations_to_cancel = Reservation.objects.filter(
                id__in=res_ids,
                status__in=['pending', 'approved']
            ).select_related('student', 'classroom')
            
            if reservations_to_cancel.exists():
                # 分类处理：pending直接取消，approved检查时间并发邮件
                pending_cancelled = 0
                approved_cancelled = 0
                approved_cannot_cancel = []
                email_sent_count = 0
                
                # 按状态分组
                pending_list = []
                approved_list = []
                
                for res in reservations_to_cancel:
                    if res.status == 'pending':
                        pending_list.append(res)
                    elif res.status == 'approved':
                        # 检查时间窗口
                        if can_cancel:
                            approved_list.append(res)
                        else:
                            approved_cannot_cancel.append(f"{res.student.student_id}")
                
                # 处理pending：找出所有竞争同一座位的待审核申请并取消
                # 收集所有需要取消的座位信息（教室+日期+时段+行+列）
                pending_seats_to_cancel = set()
                for res in pending_list:
                    pending_seats_to_cancel.add((res.classroom_id, res.date, res.time_slot, res.seat_row, res.seat_col))
                
                # 对每个座位，取消所有竞争该座位的待审核申请
                for classroom_id, date, time_slot, seat_row, seat_col in pending_seats_to_cancel:
                    competing_reservations = Reservation.objects.filter(
                        classroom_id=classroom_id,
                        date=date,
                        time_slot=time_slot,
                        seat_row=seat_row,
                        seat_col=seat_col,
                        status='pending'
                    )
                    for res in competing_reservations:
                        res.status = 'cancelled'
                        res.save()
                        pending_cancelled += 1
                
                # 处理approved：新建取消记录（不修改原记录），发邮件通知
                if approved_list:
                    # 按学生分组
                    student_reservations = {}
                    for res in approved_list:
                        stu_id = res.student.id
                        if stu_id not in student_reservations:
                            student_reservations[stu_id] = {
                                'student': res.student,
                                'reservations': []
                            }
                        student_reservations[stu_id]['reservations'].append(res)
                    
                    for stu_id, data in student_reservations.items():
                        student = data['student']
                        reservations = data['reservations']
                        
                        # 构建邮件内容和座位信息列表
                        cancelled_items = []
                        seats_info_list = []  # 用于存储到cancelled_seats_info字段
                        first_res = reservations[0]  # 用第一个预约的基本信息创建记录
                        
                        for res in reservations:
                            slot_name = dict(TIME_SLOTS).get(res.time_slot, f"时段{res.time_slot}")
                            seat_label = f"{res.seat_row + 1}行{res.seat_col + 1}列"
                            cancelled_items.append(f"  📍 {res.classroom.name} | {res.date} {slot_name} | 座位: {seat_label}")
                            seats_info_list.append({
                                'classroom': res.classroom.name,
                                'date': str(res.date),
                                'time_slot': res.time_slot,
                                'slot_name': slot_name,
                                'seat_row': res.seat_row,
                                'seat_col': res.seat_col,
                                'seat_label': seat_label
                            })
                            
                            # 修改原记录状态为cancelled，释放座位
                            res.status = 'cancelled'
                            res.save()
                            approved_cancelled += 1
                        
                        # 每个用户只新建一条取消记录（包含所有被取消的座位信息）
                        import uuid
                        import json
                        Reservation.objects.create(
                            batch_id=uuid.uuid4(),
                            student=student,
                            classroom=first_res.classroom,
                            seat_row=first_res.seat_row,
                            seat_col=first_res.seat_col,
                            date=first_res.date,
                            time_slot=first_res.time_slot,
                            status='cancelled',
                            is_admin_action=True,
                            cancelled_seats_info=json.dumps(seats_info_list, ensure_ascii=False),
                        )
                        
                        # 发送邮件
                        email_subject = f"【预约取消通知】您的 {len(reservations)} 个座位预约已被取消"
                        email_body = f"""
您好，{student.student_id}！

您的以下预约已被管理员取消：

{chr(10).join(cancelled_items)}

如有疑问，请联系管理员。

——智能教室预约系统
"""
                        try:
                            send_mail(
                                subject=email_subject,
                                message=email_body,
                                from_email='system@school.edu',
                                recipient_list=[student.email],
                            )
                            email_sent_count += 1
                        except Exception as e:
                            messages.error(request, f"邮件发送失败 ({student.email}): {e}")
                
                # 构建提示消息
                total_cancelled = pending_cancelled + approved_cancelled
                if total_cancelled > 0:
                    msg = f"✅ 已取消 {total_cancelled} 个预约"
                    if pending_cancelled > 0:
                        msg += f"（其中 {pending_cancelled} 个待审核）"
                    if approved_cancelled > 0:
                        msg += f"，发送了 {email_sent_count} 封通知邮件"
                    messages.success(request, msg + "。")
                
                if approved_cannot_cancel:
                    messages.warning(request, f"⚠️ {len(approved_cannot_cancel)} 个【已通过】预约已超过取消时限，无法取消。")
            else:
                messages.warning(request, "没有找到可取消的预约。")
        
        return redirect(f"{request.path}?classroom_id={curr_cls.id}&date={date_str}&slot={slot_id}")

    # 3. 渲染视图
    layout_lines = curr_cls.layout.strip().split('\n')
    
    # 获取该时段所有有效预约（包含预约ID）
    records = Reservation.objects.filter(
        classroom=curr_cls, date=req_date_obj, time_slot=slot_id,
        status__in=['approved', 'pending']
    ).values('id', 'seat_row', 'seat_col', 'status', 'student__student_id')
    
    cell_map = {(r['seat_row'], r['seat_col']): r for r in records}
    
    matrix = []
    for r_idx, line in enumerate(layout_lines):
        row_data = []
        for c_idx, char in enumerate(line.strip()):
            cell = {'r': r_idx, 'c': c_idx, 'type': 'aisle' if char == '0' else 'seat', 'status': 'free'}
            
            if cell['type'] == 'seat':
                key = (r_idx, c_idx)
                if key in cell_map:
                    rec = cell_map[key]
                    cell['status'] = rec['status']
                    cell['res_id'] = rec['id']
                    cell['student_id'] = rec['student__student_id']
                    cell['info'] = f"{rec['student__student_id']}"
            row_data.append(cell)
        matrix.append(row_data)

    return render(request, 'core/admin_cancel.html', {
        'classrooms': classrooms,
        'curr_cls': curr_cls,
        'matrix': matrix,
        'date': date_str,
        'today': datetime.date.today().strftime('%Y-%m-%d'),
        'time_slots': TIME_SLOTS,
        'current_slot': slot_id,
        'can_cancel': can_cancel,
        'cancel_window_minutes': cancel_window_minutes,
        'slot_start_time': slot_start_dt.strftime('%Y-%m-%d %H:%M') if slot_start_dt else '',
    })

    
# --- 5. 我的预约列表 ---
def my_bookings(request):
    sid = request.session.get('sid')
    if not sid: return redirect('index')
    student = Student.objects.get(id=sid)
    
    # 获取该学生所有记录
    raw_res = Reservation.objects.filter(student=student).order_by('-created_at')
    
    grouped_bookings = []
    temp_groups = {}
    order_list = []
    
    # 获取取消截止时间配置
    cancel_deadline_minutes = getattr(settings, 'RESERVATION_BOOKING_WINDOW_MINUTES', 30)
    now_dt = datetime.datetime.now()
    
    for res in raw_res:
        bid = res.batch_id
        if bid not in temp_groups:
            # 计算该预约的取消截止时间
            can_cancel = False
            cancel_deadline_str = ""
            slot_label = res.get_time_slot_display()
            if slot_label:
                start_str = slot_label.split('-')[0].strip()
                try:
                    h, m = map(int, start_str.split(':'))
                    slot_start = datetime.datetime.combine(res.date, datetime.time(h, m))
                    cancel_deadline = slot_start - datetime.timedelta(minutes=cancel_deadline_minutes)
                    can_cancel = now_dt < cancel_deadline
                    cancel_deadline_str = cancel_deadline.strftime('%Y-%m-%d %H:%M')
                except Exception:
                    pass
            
            # 初始化组
            temp_groups[bid] = {
                'is_admin': False,  # 稍后根据条件设置
                'is_admin_cancelled': False,  # 标记是否被管理员取消
                'batch_id': bid,
                'date': res.date,
                'time_slot': res.time_slot,  # 添加时间段ID
                'time_slot_name': slot_label,
                'classroom': res.classroom.name,
                'seats': [],
                'status_counts': {'pending': 0, 'approved': 0, 'rejected': 0, 'cancelled': 0, 'expired': 0}, # 状态计数器
                'can_cancel': can_cancel,  # 是否可取消
                'cancel_deadline': cancel_deadline_str,  # 取消截止时间
                'is_admin_created': False,  # 稍后设置
            }
            order_list.append(bid)
        
        # 检测是否被管理员取消（状态为cancelled且is_admin_action为True）
        if res.status == 'cancelled' and res.is_admin_action:
            temp_groups[bid]['is_admin_cancelled'] = True
        
        # 检测是否是管理员创建的预约（非取消状态且is_admin_action为True）
        if res.status != 'cancelled' and res.is_admin_action:
            temp_groups[bid]['is_admin_created'] = True
        
        # 管理员操作标签显示逻辑：
        # - 管理员创建的预约（未取消状态时）
        # - 管理员取消的预约
        if temp_groups[bid].get('is_admin_created') or temp_groups[bid].get('is_admin_cancelled'):
            temp_groups[bid]['is_admin'] = True
        
        # 收集座位信息
        # 如果有 cancelled_seats_info 字段（管理员批量取消时存储的多座位信息）
        if res.cancelled_seats_info:
            import json
            try:
                seats_info = json.loads(res.cancelled_seats_info)
                for seat_info in seats_info:
                    seat_label = seat_info.get('seat_label', f"{seat_info['seat_row']+1}行{seat_info['seat_col']+1}列")
                    # 添加教室和时间段信息以便区分
                    full_label = f"{seat_info.get('classroom', res.classroom.name)} - {seat_label}"
                    temp_groups[bid]['seats'].append({'label': full_label, 'status': res.status})
                    # 统计状态
                    if res.status in temp_groups[bid]['status_counts']:
                        temp_groups[bid]['status_counts'][res.status] += 1
            except (json.JSONDecodeError, KeyError):
                # 解析失败时使用默认单座位逻辑
                seat_label = f"{res.seat_row+1}行{res.seat_col+1}列"
                temp_groups[bid]['seats'].append({'label': seat_label, 'status': res.status})
                if res.status in temp_groups[bid]['status_counts']:
                    temp_groups[bid]['status_counts'][res.status] += 1
        else:
            seat_label = f"{res.seat_row+1}行{res.seat_col+1}列"
            temp_groups[bid]['seats'].append({'label': seat_label, 'status': res.status})
            # 统计各状态数量
            s = res.status
            if s in temp_groups[bid]['status_counts']:
                temp_groups[bid]['status_counts'][s] += 1
            
    # --- 核心逻辑修正：计算聚合状态 ---
    for bid in order_list:
        group = temp_groups[bid]
        counts = group['status_counts']
        total = len(group['seats'])
        
        # 逻辑：
        # 1. 如果有任意一个 Approved -> 显示 "已通过" (部分或全部)
        # 2. 如果没有 Approved，但有 Pending -> 显示 "待审核"
        # 3. 只有当 Approved=0 且 Pending=0 -> 显示 "已拒绝/失效"
        
        if counts['approved'] > 0:
            if counts['approved'] == total:
                group['final_status'] = 'approved'
                group['status_display'] = '✅ 全部通过'
            else:
                group['final_status'] = 'warning' # 黄色/蓝色混杂
                group['status_display'] = f'⚠️ 部分通过 ({counts["approved"]}/{total})'
        elif counts['pending'] > 0:
            group['final_status'] = 'pending'
            group['status_display'] = '⏳ 待审核'
        else:
            # 如果全部都是 cancelled，则显示已取消（灰色）
            if counts.get('cancelled', 0) == total and total > 0:
                group['final_status'] = 'cancelled'
                # 区分管理员取消和用户自己取消
                if group.get('is_admin_cancelled'):
                    group['status_display'] = '🚫 被管理员取消'
                else:
                    group['status_display'] = '⚪ 已取消'
            # 如果全部都是 expired，则显示已过期
            elif counts.get('expired', 0) == total and total > 0:
                group['final_status'] = 'expired'
                group['status_display'] = '⏰ 已过期'
            # 如果有 expired，显示已过期
            elif counts.get('expired', 0) > 0:
                group['final_status'] = 'expired'
                group['status_display'] = '⏰ 已过期'
            else:
                group['final_status'] = 'rejected'
                group['status_display'] = '❌ 全部失败'
            
        grouped_bookings.append(group)
    
    # 支持按状态筛选，默认显示全部（all）
    status_filter = request.GET.get('status', 'all')
    if status_filter != 'all':
        grouped_bookings = [g for g in grouped_bookings if g.get('final_status') == status_filter]

    return render(request, 'core/my_bookings.html', {
        'student': student,
        'grouped_bookings': grouped_bookings,
        'status_filter': status_filter,
    })


def cancel_booking(request, batch_id):
    """用户取消预约批次：将该批次中 status 为 'pending' 或 'approved' 的记录设置为 'cancelled'。
    只能在时间段开始前 N 分钟之前取消。
    """
    sid = request.session.get('sid')
    if not sid:
        return redirect('index')

    if request.method != 'POST':
        return redirect('my_bookings')

    student = Student.objects.get(id=sid)
    
    # 获取取消截止时间配置
    cancel_deadline_minutes = getattr(settings, 'RESERVATION_BOOKING_WINDOW_MINUTES', 30)
    now_dt = datetime.datetime.now()
    
    try:
        with transaction.atomic():
            # 获取该批次中可取消的预约（pending 或 approved）
            qs = Reservation.objects.filter(
                batch_id=batch_id, 
                student=student,
                status__in=['pending', 'approved']
            )
            
            if not qs.exists():
                messages.warning(request, "没有可取消的预约（可能已被处理或取消）。")
                return redirect('my_bookings')
            
            # 检查时间限制：获取第一条记录的时间信息
            first_res = qs.first()
            slot_label = dict(TIME_SLOTS).get(first_res.time_slot, "")
            if slot_label:
                start_str = slot_label.split('-')[0].strip()
                try:
                    h, m = map(int, start_str.split(':'))
                    slot_start = datetime.datetime.combine(first_res.date, datetime.time(h, m))
                    cancel_deadline = slot_start - datetime.timedelta(minutes=cancel_deadline_minutes)
                    
                    if now_dt >= cancel_deadline:
                        messages.error(
                            request, 
                            f"❌ 取消已截止！该时间段的取消截止时间为 {cancel_deadline.strftime('%Y-%m-%d %H:%M')}（开始前{cancel_deadline_minutes}分钟）"
                        )
                        return redirect('my_bookings')
                except Exception:
                    pass
            
            # 执行取消：用户自己取消时，将 is_admin_action 设为 False
            # 这样即使是管理员创建的预约，用户取消后也不会显示"被管理员取消"
            for res in qs:
                res.status = 'cancelled'
                res.is_admin_action = False  # 用户操作，不是管理员
                res.save()
            cnt = qs.count()
            
        if cnt > 0:
            messages.success(request, f"✅ 已取消 {cnt} 条预约。")
        else:
            messages.warning(request, "没有可取消的预约。")
    except Exception as e:
        messages.error(request, f"取消失败: {e}")

    return redirect('my_bookings')

# --- 6. 申请升级 ---
def apply_promotion(request):
    sid = request.session.get('sid')
    if not sid: return redirect('index')
    stu = Student.objects.get(id=sid)
    # 如果已经是负责人，直接提示并返回错误样式的 info 页面
    if getattr(stu, 'role', None) == 'manager':
        message = "❌ 您已经是负责人"
        next_url = request.META.get('HTTP_REFERER', reverse('booking'))
        return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")
    # 如果已有被拒绝的申请，阻止再次申请
    rejected_exists = PromotionRequest.objects.filter(student=stu, status='rejected').exists()
    if rejected_exists:
        message = "❌ 您的申请已被拒绝，无法再次申请。"
        next_url = request.META.get('HTTP_REFERER', reverse('booking'))
        return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")

    # 防止重复申请：如果已有未处理的申请，则阻止再次申请
    existing = PromotionRequest.objects.filter(student=stu, status='pending').first()
    if existing:
        message = "⚠️ 您已有正在审核的升级申请，请等待管理员处理。"
        next_url = request.META.get('HTTP_REFERER', reverse('booking'))
        return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=error")

    # 创建申请记录
    pr = PromotionRequest.objects.create(student=stu, status='pending')

    # 生成管理员同意/拒绝链接
    approve_url = generate_action_url(stu.id, 'promote', 'stu')
    reject_url = generate_action_url(stu.id, 'reject', 'stu')
    msg = f"学生 {stu.student_id} 申请升级为负责人。\n[同意]: {approve_url}\n[不再询问]: {reject_url}"
    send_mail(f"权限申请 - {stu.student_id}", msg, 'sys@edu.cn', [settings.ADMIN_EMAIL])

    # 向申请者显示已提交的提示页
    message = "✅ 您的升级申请已提交，管理员会尽快处理。"
    next_url = request.META.get('HTTP_REFERER', reverse('booking'))
    return redirect(f"{reverse('info')}?msg={urllib.parse.quote_plus(message)}&next={urllib.parse.quote_plus(next_url)}&type=success")