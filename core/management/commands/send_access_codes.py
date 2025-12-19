# core/management/commands/send_access_codes.py
"""
门禁密码发送命令：在时间段开始前 N 分钟发送门禁密码邮件给所有该时段有预约的用户
"""
from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.conf import settings
from django.db import transaction
from core.models import Reservation, Classroom, AccessCode
from django.conf import settings
TIME_SLOTS = settings.TIME_SLOTS
import datetime
import random
import string


def generate_access_code(length=6):
    """生成门禁密码：优先使用配置的固定密码，否则随机生成6位数字"""
    fixed_code = getattr(settings, 'ACCESS_CODE_FIXED', None)
    if fixed_code:
        return str(fixed_code)
    return ''.join(random.choices(string.digits, k=length))


def get_slot_start_time(date_obj, slot_id):
    """根据日期和时间段ID获取开始时间的datetime对象"""
    slot_label = dict(TIME_SLOTS).get(slot_id, "")
    if not slot_label:
        return None
    start_str = slot_label.split('-')[0].strip()
    try:
        h, m = map(int, start_str.split(':'))
        return datetime.datetime.combine(date_obj, datetime.time(h, m))
    except Exception:
        return None


class Command(BaseCommand):
    help = '检查即将开始的时间段，发送门禁密码给所有该时段有预约的用户'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='试运行模式，只显示将要发送的邮件，不实际发送',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        now = datetime.datetime.now()
        today = now.date()
        
        # 从配置获取提前通知时间（分钟）
        notify_minutes = getattr(settings, 'ACCESS_CODE_NOTIFY_MINUTES', 15)
        
        self.stdout.write(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 检查即将开始的时间段...")
        
        total_sent = 0
        
        # 遍历所有时间段
        for slot_id, slot_label in TIME_SLOTS:
            slot_start = get_slot_start_time(today, slot_id)
            if not slot_start:
                continue
            
            # 计算通知时间窗口：开始前 notify_minutes 分钟
            notify_time = slot_start - datetime.timedelta(minutes=notify_minutes)
            
            # 检查是否在通知时间窗口内（notify_time <= now < slot_start）
            # 并且距离 notify_time 不超过 5 分钟（避免重复发送）
            if notify_time <= now < slot_start:
                # 检查是否已发送过
                # 对每个教室单独处理
                classrooms = Classroom.objects.filter(is_active=True)
                
                for classroom in classrooms:
                    # 查找该教室、日期、时间段的门禁密码记录
                    access_code_obj, created = AccessCode.objects.get_or_create(
                        classroom=classroom,
                        date=today,
                        time_slot=slot_id,
                        defaults={'code': generate_access_code(), 'notified': False}
                    )
                    
                    # 如果已通知过，跳过
                    if access_code_obj.notified:
                        continue
                    
                    # 获取该时段所有已通过的预约
                    approved_reservations = Reservation.objects.filter(
                        classroom=classroom,
                        date=today,
                        time_slot=slot_id,
                        status='approved'
                    ).select_related('student')
                    
                    if not approved_reservations.exists():
                        # 没有预约，标记为已通知（避免重复处理）
                        access_code_obj.notified = True
                        access_code_obj.save()
                        continue
                    
                    # 按学生分组座位
                    student_seats = {}
                    for res in approved_reservations:
                        stu = res.student
                        if stu.id not in student_seats:
                            student_seats[stu.id] = {
                                'student': stu,
                                'seats': []
                            }
                        student_seats[stu.id]['seats'].append(f"{res.seat_row + 1}行{res.seat_col + 1}列")
                    
                    # 发送邮件给每个学生
                    for stu_id, data in student_seats.items():
                        stu = data['student']
                        seats_str = '、'.join(data['seats'])
                        
                        email_subject = f"【门禁密码】{classroom.name} - {slot_label}"
                        email_body = f"""
您好，{stu.student_id}！

您在 {classroom.name} 的预约即将开始，请查收门禁密码：

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 教室：{classroom.name}
📅 日期：{today.strftime('%Y年%m月%d日')}
⏰ 时间段：{slot_label}
💺 座位：{seats_str}
🔑 门禁密码：{access_code_obj.code}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

请在规定时间内使用此密码进入教室。

注意事项：
1. 此密码仅在该时间段内有效
2. 请勿将密码分享给他人
3. 请按时到达，逾期座位可能被释放

祝学习愉快！
——智能教室预约系统
"""
                        
                        if dry_run:
                            self.stdout.write(self.style.WARNING(
                                f"[试运行] 将发送给 {stu.email}:\n  教室={classroom.name}, 时段={slot_label}, 座位={seats_str}, 密码={access_code_obj.code}"
                            ))
                        else:
                            try:
                                send_mail(
                                    subject=email_subject,
                                    message=email_body,
                                    from_email='system@school.edu',
                                    recipient_list=[stu.email],
                                )
                                self.stdout.write(self.style.SUCCESS(
                                    f"✅ 已发送门禁密码给 {stu.email} ({classroom.name}, {slot_label})"
                                ))
                                total_sent += 1
                            except Exception as e:
                                self.stderr.write(self.style.ERROR(
                                    f"❌ 发送失败 {stu.email}: {e}"
                                ))
                    
                    # 标记为已通知
                    if not dry_run:
                        access_code_obj.notified = True
                        access_code_obj.save()
        
        if dry_run:
            self.stdout.write(self.style.NOTICE("试运行完成，未实际发送邮件"))
        else:
            self.stdout.write(self.style.SUCCESS(f"门禁密码发送完成，共发送 {total_sent} 封邮件"))
