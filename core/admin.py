# core/admin.py

from django import forms
from django.contrib import admin
from django.shortcuts import redirect
from django.core.exceptions import ValidationError
from django.db import transaction
from .models import Student, Classroom, Reservation, AccessCode
from .models import PromotionRequest
from .models import TIME_SLOTS  # 实际从settings获取
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings as django_settings

# --- 1. 自定义预约表单 (处理坐标转换和校验) ---
class ReservationAdminForm(forms.ModelForm):
    # 定义两个虚拟字段，用于接收 1-based 的输入
    row_input = forms.IntegerField(label="行号 (1-based)", min_value=1, help_text="请输入第几行（从1开始）")
    col_input = forms.IntegerField(label="列号 (1-based)", min_value=1, help_text="请输入第几列（从1开始）")

    class Meta:
        model = Reservation
        fields = '__all__'
        # 隐藏数据库真实的 0-based 字段
        exclude = ('seat_row', 'seat_col', 'batch_id', 'created_at')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 如果是编辑现有记录，将数据库的 0-based 转为 1-based 显示
        if self.instance and self.instance.pk:
            self.fields['row_input'].initial = self.instance.seat_row + 1
            self.fields['col_input'].initial = self.instance.seat_col + 1

    def clean(self):
        cleaned_data = super().clean()
        classroom = cleaned_data.get('classroom')
        # 获取输入的 1-based 坐标，转为 0-based
        r_in = cleaned_data.get('row_input')
        c_in = cleaned_data.get('col_input')
        
        if not classroom or r_in is None or c_in is None:
            return cleaned_data

        real_r = r_in - 1
        real_c = c_in - 1

        # 1. 校验坐标是否存在 (解析布局图)
        layout_lines = classroom.layout.strip().split('\n')
        if real_r >= len(layout_lines) or real_r < 0:
            raise ValidationError(f"行号超出范围，该教室最大行数为 {len(layout_lines)}")
        
        row_str = layout_lines[real_r].strip()
        if real_c >= len(row_str) or real_c < 0:
            raise ValidationError(f"列号超出范围，该行最大列数为 {len(row_str)}")

        # 2. 校验是否为过道
        if row_str[real_c] == '0':
            raise ValidationError("该位置是过道 (0)，不是座位，无法预约。")

        # 3. 校验冲突
        date = cleaned_data.get('date')
        time_slot = cleaned_data.get('time_slot')
        
        # 查询该位置是否有【其他】有效预约
        # 注意：要排除自己 (self.instance.id)，否则修改其他字段时会报错
        conflicts = Reservation.objects.filter(
            classroom=classroom,
            seat_row=real_r,
            seat_col=real_c,
            date=date,
            time_slot=time_slot,
            status__in=['approved', 'pending']
        ).exclude(id=self.instance.id)

        # A. 如果有 Approved (硬锁)，直接报错
        if conflicts.filter(status='approved').exists():
            taken_by = conflicts.filter(status='approved').first().student.student_id
            raise ValidationError(f"该座位已被 [{taken_by}] 预约成功，无法覆盖。")

        # B. 如果有 Pending (软锁)，允许通过，但要在 save 中处理
        # 这里不做拦截，把 conflicts 存起来给 save 用
        self.pending_conflicts = conflicts.filter(status='pending')
        
        # 将转换后的坐标存回 cleaned_data 供模型保存
        cleaned_data['seat_row'] = real_r
        cleaned_data['seat_col'] = real_c
        
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        # 从 cleaned_data 获取转换后的坐标
        instance.seat_row = self.cleaned_data['seat_row']
        instance.seat_col = self.cleaned_data['seat_col']
        # 标记为管理员操作
        instance.is_admin_action = True
        
        if commit:
            with transaction.atomic():
                instance.save()
                # 4. 核心逻辑：踢掉 Pending 的竞争者
                if hasattr(self, 'pending_conflicts') and self.pending_conflicts.exists():
                    count = self.pending_conflicts.update(status='rejected')
                    # 这里无法直接给 Admin 发 message，但在逻辑上已经实现了“抢占”
        return instance


# --- 2. 注册 Admin ---
@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ('student_id', 'role', 'status', 'is_auto_created')
    list_editable = ('status', 'role')
    search_fields = ('student_id',)

@admin.register(Classroom)
class ClassroomAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active')

@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    form = ReservationAdminForm # <--- 挂载自定义表单
    
    list_display = ('student', 'classroom', 'seat_info_display', 'date', 'time_slot', 'status', 'is_admin_action')
    list_filter = ('status', 'date', 'classroom', 'is_admin_action')
    search_fields = ('student__student_id',)
    actions = ['cancel_reservations']  # 添加批量取消操作
    
    # 在列表中显示 1-based 坐标
    def seat_info_display(self, obj):
        return f"{obj.seat_row + 1}行-{obj.seat_col + 1}列"
    seat_info_display.short_description = "座位(1-based)"

    # 拦截 Add 按钮到可视化页面 (保持你之前的逻辑)
    def add_view(self, request, form_url='', extra_context=None):
        return redirect('admin_booking')

    def cancel_reservations(self, request, queryset):
        """管理员批量取消预约：
        - pending状态：直接取消，不检查时间，不发邮件
        - approved状态：检查时间窗口，发送取消通知邮件
        """
        import datetime
        
        # 只处理 pending 和 approved 状态的预约
        valid_reservations = queryset.filter(status__in=['pending', 'approved'])
        
        if not valid_reservations.exists():
            self.message_user(request, "没有可取消的预约（已取消/已拒绝/已过期的预约无法再次取消）", level='warning')
            return
        
        # 获取取消时间窗口配置
        cancel_window_minutes = getattr(django_settings, 'RESERVATION_BOOKING_WINDOW_MINUTES', 30)
        now_dt = datetime.datetime.now()
        
        # 分类处理：pending直接取消，approved需要检查时间
        pending_reservations = []
        approved_can_cancel = []
        approved_cannot_cancel = []
        
        for res in valid_reservations.select_related('student', 'classroom'):
            if res.status == 'pending':
                # pending状态直接取消，不检查时间
                pending_reservations.append(res)
            elif res.status == 'approved':
                # approved状态需要检查时间窗口
                slot_label = dict(TIME_SLOTS).get(res.time_slot, "")
                can_cancel = True
                if slot_label:
                    start_str = slot_label.split('-')[0].strip()
                    try:
                        h, m = map(int, start_str.split(':'))
                        slot_start = datetime.datetime.combine(res.date, datetime.time(h, m))
                        cancel_deadline = slot_start - datetime.timedelta(minutes=cancel_window_minutes)
                        
                        if now_dt >= cancel_deadline:
                            can_cancel = False
                            approved_cannot_cancel.append(f"{res.classroom.name} {res.date} {slot_label} - {res.student.student_id}")
                    except Exception:
                        pass
                
                if can_cancel:
                    approved_can_cancel.append(res)
        
        # 提示无法取消的approved预约
        if approved_cannot_cancel:
            self.message_user(
                request, 
                f"以下 {len(approved_cannot_cancel)} 个【已通过】预约已超过取消时限（需在开始前{cancel_window_minutes}分钟之前）：{'; '.join(approved_cannot_cancel[:3])}{'...' if len(approved_cannot_cancel) > 3 else ''}", 
                level='warning'
            )
        
        # 处理pending预约：找出所有竞争同一座位的待审核申请并取消
        pending_cancelled = 0
        # 收集所有需要取消的座位信息（教室+日期+时段+行+列）
        pending_seats_to_cancel = set()
        for res in pending_reservations:
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
        
        # 处理approved预约：新建取消记录（不修改原记录），发送邮件
        if not approved_can_cancel:
            if pending_cancelled > 0:
                self.message_user(request, f"已取消 {pending_cancelled} 个待审核预约（无需发送邮件）。")
            return
        
        # 按学生分组approved预约，发送邮件
        student_reservations = {}
        for res in approved_can_cancel:
            stu_id = res.student.id
            if stu_id not in student_reservations:
                student_reservations[stu_id] = {
                    'student': res.student,
                    'reservations': []
                }
            student_reservations[stu_id]['reservations'].append(res)
        
        approved_cancelled = 0
        email_sent_count = 0
        
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
                cancelled_items.append(f"  - {res.classroom.name} | {res.date} {slot_name} | 座位: {seat_label}")
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
                classroom=first_res.classroom,  # 用第一个预约的教室
                seat_row=first_res.seat_row,
                seat_col=first_res.seat_col,
                date=first_res.date,
                time_slot=first_res.time_slot,
                status='cancelled',
                is_admin_action=True,
                cancelled_seats_info=json.dumps(seats_info_list, ensure_ascii=False),  # 存储所有座位信息
            )
            
            # 发送邮件通知
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
                self.message_user(request, f"邮件发送失败 ({student.email}): {e}", level='error')
        
        total_cancelled = pending_cancelled + approved_cancelled
        msg = f"已取消 {total_cancelled} 个预约"
        if pending_cancelled > 0:
            msg += f"（其中 {pending_cancelled} 个待审核）"
        if approved_cancelled > 0:
            msg += f"，发送了 {email_sent_count} 封通知邮件"
        self.message_user(request, msg + "。")
    cancel_reservations.short_description = '🚫 取消所选预约（已通过的发通知）'


@admin.register(PromotionRequest)
class PromotionRequestAdmin(admin.ModelAdmin):
    list_display = ('student', 'status', 'created_at', 'reviewed_at', 'reviewer')
    list_filter = ('status', 'created_at', 'reviewed_at')
    search_fields = ('student__student_id',)
    actions = ['approve_requests', 'reject_requests']

    def approve_requests(self, request, queryset):
        # 批量批准申请
        now = timezone.now()
        updated = 0
        for pr in queryset.select_for_update():
            if pr.status != 'pending':
                continue
            pr.status = 'approved'
            pr.reviewed_at = now
            pr.reviewer = request.user
            pr.save()
            # 同步提升 student 的 role
            student = pr.student
            student.role = 'manager'
            student.save()
            updated += 1
        self.message_user(request, f"已批准 {updated} 条申请。")
    approve_requests.short_description = '批准所选的申请'

    def reject_requests(self, request, queryset):
        # 批量拒绝申请
        now = timezone.now()
        updated = 0
        for pr in queryset.select_for_update():
            if pr.status != 'pending':
                continue
            pr.status = 'rejected'
            pr.reviewed_at = now
            pr.reviewer = request.user
            pr.save()
            updated += 1
        self.message_user(request, f"已拒绝 {updated} 条申请。")
    reject_requests.short_description = '拒绝所选的申请'


@admin.register(AccessCode)
class AccessCodeAdmin(admin.ModelAdmin):
    list_display = ('classroom', 'date', 'time_slot', 'code', 'notified', 'created_at')
    list_filter = ('classroom', 'date', 'notified')
    search_fields = ('code', 'classroom__name')
    readonly_fields = ('created_at',)
    ordering = ['-date', 'time_slot']