from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from core.models import Reservation
from django.conf import settings
TIME_SLOTS = settings.TIME_SLOTS
import datetime

class Command(BaseCommand):
    help = '自动释放过期或超时的预约'

    def handle(self, *args, **options):
        now = datetime.datetime.now()
        today = datetime.date.today()
        
        # 0. 释放“操作截止时间已过”的 pending 预约
        # 在时间段开始前 N 分钟截止操作
        deadline_minutes = getattr(settings, 'RESERVATION_BOOKING_WINDOW_MINUTES', 30)
        deadline_expired = 0
        
        # 获取所有今天及以后的 pending 预约
        pending_reservations = Reservation.objects.filter(
            status='pending',
            date__gte=today
        )
        
        for res in pending_reservations:
            slot_label = dict(TIME_SLOTS).get(res.time_slot, "")
            if slot_label:
                start_str = slot_label.split('-')[0].strip()
                try:
                    h, m = map(int, start_str.split(':'))
                    slot_start = datetime.datetime.combine(res.date, datetime.time(h, m))
                    deadline = slot_start - datetime.timedelta(minutes=deadline_minutes)
                    
                    if now >= deadline:
                        res.status = 'expired'
                        res.save()
                        deadline_expired += 1
                except Exception:
                    pass
        
        # 1. 释放“超时未审核”的 (例如提交了 24 小时还没人管)
        # 注意：这里仅作演示，实际可能需要更长的时间
        timeout_hours = getattr(settings, 'RESERVATION_TIMEOUT_HOURS', 24)
        timeout_threshold = now - datetime.timedelta(hours=timeout_hours)
        expired_pending = Reservation.objects.filter(
            status='pending', 
            created_at__lt=timeout_threshold
        ).update(status='expired')

        # 2. 释放“已过期”的 (预约日期已过)
        # 假设 end_time 结合 date 小于当前时间
        # 这里简化处理：只看日期，根据配置决定多少天以前视为过期
        expire_days = getattr(settings, 'RESERVATION_EXPIRE_DAYS', 1)
        cutoff_date = datetime.date.today() - datetime.timedelta(days=expire_days)
        expired_date = Reservation.objects.filter(
            status='pending',
            date__lte=cutoff_date
        ).update(status='expired')
        
        self.stdout.write(self.style.SUCCESS(
            f'清理完成。截止时间过期: {deadline_expired}, 超时释放: {expired_pending}, 日期过期: {expired_date}'
        ))