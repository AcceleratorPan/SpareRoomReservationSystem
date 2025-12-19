# core/models.py
from django.db import models
from django.conf import settings
import uuid
from django.contrib.auth.hashers import make_password, check_password

# 从settings导入TIME_SLOTS
TIME_SLOTS = settings.TIME_SLOTS

class Student(models.Model):
    STATUS_CHOICES = (('normal', '正常'), ('blacklist', '黑名单'), ('whitelist', '白名单'))
    ROLE_CHOICES = (('user', '普通学生'), ('manager', '负责人/VIP'))
    
    student_id = models.CharField(max_length=20, unique=True, verbose_name="学号")
    # 不在数据库中存储邮箱，邮件地址由学号动态生成
    # 存储密码（已哈希）
    password = models.CharField(max_length=128, verbose_name="密码", blank=True, default='')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='normal')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='user', verbose_name="角色")
    violation_count = models.IntegerField(default=0)
    
    # --- 新增字段：标记是否为系统自动创建 ---
    is_auto_created = models.BooleanField(default=False, verbose_name="自动创建")

    def set_password(self, raw_password):
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        if not self.password:
            return False
        return check_password(raw_password, self.password)

    def __str__(self): return f"{self.student_id}"

    @property
    def email(self):
        """动态生成邮件地址：学号 + @hust.edu.cn（不在 DB 中保存）"""
        return f"{self.student_id}@hust.edu.cn"

class Classroom(models.Model):
    name = models.CharField(max_length=50)
    # 使用文本来绘制布局，1=座位，0=过道
    # 例如：
    # 11011
    # 11011
    layout = models.TextField(verbose_name="布局图(1座0空)", help_text="用1代表座位，0代表过道，换行代表下一行", default="11111\n11111")
    is_active = models.BooleanField(default=True)

    def __str__(self): return self.name

class Reservation(models.Model):
    STATUS_CHOICES = (
        ('pending', '待审核'), ('approved', '已通过'),
        ('rejected', '已拒绝'), ('expired', '已过期'), ('cancelled', '已取消')
    )
    
    # 新增：批次ID，用于将一次提交的多个座位关联起来
    batch_id = models.UUIDField(default=uuid.uuid4, editable=False)
    
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    classroom = models.ForeignKey(Classroom, on_delete=models.CASCADE)
    seat_row = models.IntegerField()
    seat_col = models.IntegerField()
    date = models.DateField()
    time_slot = models.IntegerField(choices=TIME_SLOTS, verbose_name="时间段")
    
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    is_admin_action = models.BooleanField(default=False, verbose_name="管理员操作")
    # 用于存储管理员批量取消的多个座位信息（JSON格式）
    cancelled_seats_info = models.TextField(blank=True, default='', verbose_name="取消座位信息")

    def __str__(self):
        return f"{self.student.student_id} - {self.date}"


class PromotionRequest(models.Model):
    STATUS_CHOICES = (
        ('pending', '申请中'),
        ('approved', '通过'),
        ('rejected', '拒绝'),
    )

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='promotion_requests')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewer = models.CharField(max_length=100, blank=True, default='')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"PromotionRequest({self.student.student_id}, {self.status})"


class AccessCode(models.Model):
    """门禁密码：按教室+日期+时间段生成，用于入场验证"""
    classroom = models.ForeignKey(Classroom, on_delete=models.CASCADE)
    date = models.DateField()
    time_slot = models.IntegerField(choices=TIME_SLOTS, verbose_name="时间段")
    code = models.CharField(max_length=10, verbose_name="门禁密码")
    created_at = models.DateTimeField(auto_now_add=True)
    notified = models.BooleanField(default=False, verbose_name="已通知")

    class Meta:
        unique_together = ('classroom', 'date', 'time_slot')
        ordering = ['-date', 'time_slot']

    def __str__(self):
        return f"{self.classroom.name} - {self.date} - 时段{self.time_slot} - {self.code}"