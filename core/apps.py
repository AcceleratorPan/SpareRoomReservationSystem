from django.apps import AppConfig
from django.conf import settings
from django.core.management import call_command
import threading
import time
import sys
import os
import logging

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        # 避免在管理命令（除 runserver）中启动调度器
        if len(sys.argv) > 1 and sys.argv[1] != 'runserver':
            return

        # runserver 会用 auto-reloader 触发两次 ready(); 仅在子进程真正运行时启动
        if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return

        # 避免重复启动
        if getattr(self, '_cleanup_thread_started', False):
            return

        interval_minutes = getattr(settings, 'RESERVATION_CLEANUP_INTERVAL_MINUTES', 30)

        def worker():
            logger.info(f'Cleanup scheduler started, interval={interval_minutes} minutes')
            while True:
                try:
                    call_command('cleanup')
                except Exception:
                    logger.exception('Error running cleanup command')

                try:
                    time.sleep(max(1, int(interval_minutes) * 60))
                except Exception:
                    # 如果 interval 有问题则默认睡 30 分钟
                    time.sleep(30 * 60)

        t = threading.Thread(target=worker, name='cleanup-scheduler-thread', daemon=True)
        t.start()
        self._cleanup_thread_started = True
