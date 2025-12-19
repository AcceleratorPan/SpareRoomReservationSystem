# config/urls.py

"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path
from core import views

urlpatterns = [
    # 2. 首页 (登录页)
    path('', views.index, name='index'),

    # 3. 可视化选座页面
    path('booking/', views.booking_view, name='booking'),

    # 4. 提交预约 (批量处理)
    path('submit/', views.submit, name='submit'),

    # 5. 我的预约记录 (新页面)
    path('my-bookings/', views.my_bookings, name='my_bookings'),
    # 用户取消自己待审核的预约批次
    path('cancel-booking/<uuid:batch_id>/', views.cancel_booking, name='cancel_booking'),

    # 6. 申请升级为负责人
    path('apply-promo/', views.apply_promotion, name='apply_promotion'),
    
    path('reset/', views.reset_request, name='reset_request'),
    
    path('reset-confirm/<str:token>/', views.reset_confirm, name='reset_confirm'),

    # 登出（清理 session）
    path('logout/', views.logout_view, name='logout'),
    # 信息展示页面（显示操作结果并自动返回）
    path('info/', views.info, name='info'),

    # 7. 管理员邮件审批处理 (处理带签名的 Token)
    path('admin-action/<str:token>/', views.admin_action, name='admin_action'),

    # 8. 管理员可视化选座
    path('admin/visual-booking/', views.admin_booking_view, name='admin_booking'),
    
    # 9. 管理员可视化取消预约
    path('admin/visual-cancel/', views.admin_cancel_view, name='admin_cancel'),

    # 1. Django 自带管理员后台
    path('admin/', admin.site.urls),
]