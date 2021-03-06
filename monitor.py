import sys
import time
import threading
import logging
import re
import copy
from tasks.worker import app
from utils import get_config_values
from statsd import StatsClient
from prometheus_client import start_http_server
from prometheus_client import Counter, Gauge, Summary, Info, Enum
from prometheus_client import CollectorRegistry, push_to_gateway
from prometheus_client.exposition import basic_auth_handler
from celery.events import EventReceiver
from kombu import Connection as BrokerConnection

# logging message
logging.basicConfig(level = logging.INFO,format ='[%(asctime)s : %(levelname)s/%(processName)s] %(message)s')
logger = logging.getLogger(__name__)

class StatsdMonitor(object):
    def __init__(self, broker, interval=1):
        # self.interval = interval
        self.state = app.events.State()
        self.statsd_conn = StatsClient(host='localhost', port=8125)
        self.broker_conn = BrokerConnection(broker)
        self.timers_list = []

    # monitor the task and status of worker with functions
    def run_loop(self):
        while True:
            try:
                with self.broker_conn as conn:
                    recv = EventReceiver(conn, handlers={
                        'task-sent': self.on_task_sent,
                        'task-failed': self.on_task_failed,
                        'task-retried': self.on_task_retried,
                        'task-started': self.on_task_started,
                        'task-succeeded': self.on_task_succeeded,
                        'task-received': self.on_task_received,
                        'task-rejected': self.on_task_rejected,
                        'task-revoked': self.on_task_revoked,
                        'worker-online': self.on_worker_online,
                        'worker-heartbeat': self.on_worker_heartbeat,
                        'worker-offline': self.on_worker_offline,
                    })
                    recv.capture(limit=None, timeout=None, wakeup=True)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                raise
            # time.sleep(self.interval)

    # all about the tasks

    def on_task_sent(self, event): # TODO
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])
        self.statsd_conn.incr('tasks.sent')

    def on_task_received(self, event): # TODO
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])
        self.statsd_conn.incr('tasks.received')

    def on_task_started(self, event):
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])
        logger.info('Task {}[{}] started'.format(task.name, task.uuid))
        self.statsd_conn.incr('tasks.started')
        mark = 'task.{}.recorder'.format(task.uuid)
        self.timer_start(mark)

    def on_task_succeeded(self, event):
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])
        logger.info('Task {}[{}] succeeded'.format(task.name, task.uuid))
        self.statsd_conn.incr('tasks.succeeded')
        mark = 'task.{}.recorder'.format(task.uuid)
        self.timer_stop(mark)

    def on_task_failed(self, event): # TODO
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])
        logger.warning('Task {}[{}] failed'.format(task.name, task.uuid))
        self.statsd_conn.incr('tasks.failed')

    def on_task_retried(self, event): # TODO
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])
        logger.warning('Task {}[{}] retried'.format(task.name, task.uuid))
        self.statsd_conn.incr('tasks.retried')

    def on_task_rejected(self, event): # TODO
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])

    def on_task_revoked(self, event): # TODO
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])

    # all about the status of the workers

    def on_worker_online(self, event): # TODO
        self.state.event(event)
        worker = self.state.workers.get(event['hostname'])
        mark = 'worker.{}.recorder'.format(worker.hostname)
        self.timer_start(mark)

    def on_worker_heartbeat(self, event):
        self.state.event(event)
        worker = self.state.workers.get(event['hostname'])
        key_pro = 'worker.{}.processed'.format(worker.hostname)
        key_act = 'worker.{}.active'.format(worker.hostname)
        if worker.processed is None: worker.processed = 0
        if worker.active is None: worker.active = 0
        self.statsd_conn.gauge(key_pro, worker.processed)
        self.statsd_conn.gauge(key_act, worker.active)

    def on_worker_offline(self, event): # TODO
        self.state.event(event)
        worker = self.state.workers.get(event['hostname'])
        mark = 'worker.{}.recorder'.format(worker.hostname)
        self.timer_stop(mark)

    # statsd timer record start
    def timer_start(self, mark):
        timer = self.statsd_conn.timer(mark)
        timer.start()
        self.timers_list.append(timer)

    # statsd timer record stop
    def timer_stop(self, mark):
        for timer in self.timers_list:
            if timer.stat == mark:
                timer.stop()
                self.timers_list.remove(timer)


class PrometheusMonitor(object):
    def __init__(self, app, broker, interval=1):
        # self.interval = interval
        start_http_server(8000)
        self.app = app
        self.state = app.events.State()
        self.broker_conn = BrokerConnection(broker)
        self.gateway = 'localhost:9091'
        self.create_metric()

    def create_metric(self):
        # record app conf
        self.conf_info = Info('celery_conf_info','APP_CONF')
        self.conf_info_c = CollectorRegistry()

        # monitor worker info
        self.workers_info = Info('celery_workers_info', 'WORKER_INFO')
        self.workers_info_c = CollectorRegistry()

        # monitor worker info real-time
        self.workers_state = Gauge('celery_workers_state', 'WORKER_STATE', ['worker'])
        self.workers_state_c = CollectorRegistry()
        self.workers_processed = Gauge('celery_processed_tasks_total', 'WORKER_TASKS_PROCESSED', ['worker'])
        self.workers_processed_c = CollectorRegistry()
        self.workers_active = Gauge('celery_active_tasks_total', 'WORKER_TASKS_ACTIVE', ['worker'])
        self.workers_active_c = CollectorRegistry()

        # monitor tasks info
        self.tasks_counter = Counter('celery_tasks_total', 'TASK_COUNT_INFO', ['worker','task','result'])
        self.tasks_counter_c = CollectorRegistry()
        self.tasks_runtime = Summary('celery_tasks_seconds', 'TASK_RUNTIME', ['worker', 'task'])
        self.tasks_runtime_c = CollectorRegistry()
        self.tasks_info = Info('celery_tasks_info', 'TASK_INFO')
        self.tasks_info_c = CollectorRegistry()

    @staticmethod
    def auth_handler(url, method, timeout, headers, data):
        username = get_config_values('pushgateway','username')
        password = get_config_values('pushgateway','password')
        return basic_auth_handler(url, method, timeout, headers, data, username, password)

    # monitor the task and status of worker with functions
    def run_loop(self):

        # self.on_application_conf()

        while True:
            try:
                with self.broker_conn as conn:
                    recv = EventReceiver(conn, handlers={
                        'task-sent': self.on_task_sent,
                        'task-failed': self.on_task_failed,
                        'task-retried': self.on_task_retried,
                        'task-started': self.on_task_started,
                        'task-succeeded': self.on_task_succeeded,
                        'task-received': self.on_task_received,
                        'task-rejected': self.on_task_rejected,
                        'task-revoked': self.on_task_revoked,
                        'worker-online': self.on_worker_online,
                        'worker-heartbeat': self.on_worker_heartbeat,
                        'worker-offline': self.on_worker_offline,
                    })
                    recv.capture(limit=None, timeout=None, wakeup=True)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                raise
            # time.sleep(self.interval)

    # all about configuration

    def on_application_conf(self): # TODO
        conf = {}

        # get the password shielded
        for key in self.app.conf.keys():
            if key.lower() in ['broker_url', 'celery_result_backend']:
                if isinstance(self.app.conf[key], str):
                    uri = re.sub(r':.*?@', ':********@', self.app.conf[key])
                    conf[key] = re.sub(r'@.*?:', '@hostname:', uri)
                else:
                    conf[key] = 'unknown'
            elif bool(re.search(r'password', key.lower())):
                conf[key] = '********' if self.app.conf[key] is not None else None
            else:
                conf[key] = str(self.app.conf[key]) if self.app.conf[key] is not None else 'unknown'

        self.conf_info.info(conf)

    # all about the tasks

    def on_task_sent(self, event): # TODO
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])

    def on_task_received(self, event): # TODO
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])

    def on_task_started(self, event):
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])
        logger.info('Task {}[{}] started'.format(task.name, task.uuid))

    def on_task_succeeded(self, event):
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])
        logger.info('Task {}[{}] succeeded'.format(task.name, task.uuid))

        self.tasks_counter.labels(worker=task.hostname, task=task.name, result='succeeded').inc()
        self.tasks_runtime.labels(worker=task.hostname, task=task.name).observe(task.runtime)
        self.tasks_info.info({'name':task.name,
                              'uuid':task.uuid,
                              'result':'succeeded',
                              'runtime':str(task.runtime),
                              'hostname':task.hostname,
                              'timestamp':str(task.timestamp)})
        push_to_gateway(self.gateway, job='pushgateway', registry=self.tasks_counter_c, handler=self.auth_handler)
        push_to_gateway(self.gateway, job='pushgateway', registry=self.tasks_runtime_c, handler=self.auth_handler)
        push_to_gateway(self.gateway, job='pushgateway', registry=self.tasks_info_c, handler=self.auth_handler)

    def on_task_failed(self, event): # TODO
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])
        logger.warning('Task {}[{}] failed'.format(task.name, task.uuid))

        self.tasks_counter.labels(worker=task.hostname, task=task.name, result='failed').inc()
        self.tasks_info.info({'name':task.name,
                              'uuid':task.uuid,
                              'result':'failed',
                              'exception':task.exception,
                              'traceback':task.traceback,
                              'hostname':task.hostname,
                              'timestamp':str(task.timestamp)})
        push_to_gateway(self.gateway, job='pushgateway', registry=self.tasks_counter_c, handler=self.auth_handler)
        push_to_gateway(self.gateway, job='pushgateway', registry=self.tasks_info_c, handler=self.auth_handler)

    def on_task_retried(self, event): # TODO
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])
        logger.warning('Task {}[{}] retried'.format(task.name, task.uuid))

        self.tasks_counter.labels(worker=task.hostname, task=task.name, result='retried').inc()
        self.tasks_info.info({'name':task.name,
                              'uuid':task.uuid,
                              'result':'retried',
                              'exception':task.exception,
                              'traceback':task.traceback,
                              'hostname':task.hostname,
                              'timestamp':str(task.timestamp)})
        push_to_gateway(self.gateway, job='pushgateway', registry=self.tasks_counter_c, handler=self.auth_handler)
        push_to_gateway(self.gateway, job='pushgateway', registry=self.tasks_info_c, handler=self.auth_handler)

    def on_task_rejected(self, event): # TODO
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])

    def on_task_revoked(self, event): # TODO
        self.state.event(event)
        task = self.state.tasks.get(event['uuid'])

    # all about the status of the workers

    def on_worker_online(self, event): # TODO
        self.state.event(event)
        worker = self.state.workers.get(event['hostname'])

        self.workers_state.labels(worker=worker.hostname).set(1)
        self.workers_info.info({'hostname':worker.hostname,
                                'sw_ident':worker.sw_ident,
                                'sw_ver':worker.sw_ver,
                                'sw_sys':worker.sw_sys})
        push_to_gateway(self.gateway, job='pushgateway', registry=self.workers_state_c, handler=self.auth_handler)
        push_to_gateway(self.gateway, job='pushgateway', registry=self.workers_info_c, handler=self.auth_handler)

    def on_worker_heartbeat(self, event):
        self.state.event(event)
        worker = self.state.workers.get(event['hostname'])

        if worker.processed is None: worker.processed = 0
        if worker.active is None: worker.active = 0
        self.workers_processed.labels(worker=worker.hostname).set(worker.processed)
        self.workers_active.labels(worker=worker.hostname).set(worker.active)
        push_to_gateway(self.gateway, job='pushgateway', registry=self.workers_processed_c, handler=self.auth_handler)
        push_to_gateway(self.gateway, job='pushgateway', registry=self.workers_active_c, handler=self.auth_handler)

        self.workers_state.labels(worker=worker.hostname).set(1)
        push_to_gateway(self.gateway, job='pushgateway', registry=self.workers_state_c, handler=self.auth_handler)

    def on_worker_offline(self, event): # TODO
        self.state.event(event)
        worker = self.state.workers.get(event['hostname'])

        self.workers_state.labels(worker=worker.hostname).set(0)
        push_to_gateway(self.gateway, job='pushgateway', registry=self.workers_state_c, handler=self.auth_handler)
