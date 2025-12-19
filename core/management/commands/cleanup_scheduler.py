from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.conf import settings
import time
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = '周期性触发 cleanup 和 send_access_codes 管理命令，默认每 5 分钟运行一次。'

    def add_arguments(self, parser):
        parser.add_argument(
            '--run-once',
            action='store_true',
            help='只运行一次所有任务然后退出（用于测试/手动触发）',
        )
        parser.add_argument(
            '--interval',
            type=int,
            default=5,
            help='调度间隔（分钟），默认 5 分钟（门禁密码需要更频繁检查）',
        )

    def handle(self, *args, **options):
        interval = options.get('interval', 5)
        
        try:
            interval = max(1, int(interval))
        except Exception:
            interval = 5

        if options.get('run_once'):
            self.stdout.write(self.style.NOTICE('运行一次所有调度任务...'))
            self._run_all_tasks()
            return

        self.stdout.write(self.style.NOTICE(f'启动调度器，间隔 {interval} 分钟。按 Ctrl+C 停止。'))
        self.stdout.write(self.style.NOTICE('调度任务包括: cleanup（清理过期预约）, send_access_codes（发送门禁密码）'))

        try:
            while True:
                start = time.time()
                self._run_all_tasks()
                elapsed = time.time() - start
                sleep_time = max(0, interval * 60 - elapsed)
                time.sleep(sleep_time)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('调度器已停止'))

    def _run_all_tasks(self):
        """执行所有调度任务"""
        # 任务 1: 清理过期预约
        try:
            call_command('cleanup')
        except Exception as e:
            logger.exception('cleanup 执行失败')
            self.stderr.write(f'cleanup 执行失败: {e}')

        # 任务 2: 发送门禁密码
        try:
            call_command('send_access_codes')
        except Exception as e:
            logger.exception('send_access_codes 执行失败')
            self.stderr.write(f'send_access_codes 执行失败: {e}')
