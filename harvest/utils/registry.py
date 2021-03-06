"""
@author Team 42, Chengdu, China, Qifan Deng, 1077479
"""
import socket
import threading
import json
import queue
import traceback
import os

from cloudant.design_document import DesignDocument, Document
from time import sleep, time, asctime, localtime
from secrets import token_urlsafe
from collections import defaultdict
from utils.config import Config
from utils.database import CouchDB
from utils.logger import get_logger


class Task:
    def __init__(self, task_id, task_type, ids, status='not_assigned'):
        """
        A task object
        :param task_id: task id
        :param task_type: task type
        :param ids: target user ids of this task
        :param status: the task statuses
        """
        self.id = task_id
        self.type = task_type
        self.ids = ids
        self.status = status


class WorkerData:
    def __init__(self, worker_id, receiver_conn, receiver_addr, api_key_hash):
        """
        An object used to store a registered worker information
        :param worker_id: unique worker id
        :param receiver_conn: a tcp connection
        :param receiver_addr: tcp connection address
        :param api_key_hash: api key hash used to manage api key occupations
        """
        self.worker_id = worker_id
        self.sender_conn = None
        self.sender_addr = None
        self.receiver_conn = receiver_conn
        self.receiver_addr = receiver_addr
        self.api_key_hash = api_key_hash
        self.msg_queue = queue.Queue()
        self.active = Status()
        self.threads = queue.Queue()


class Status:
    def __init__(self):
        """
        An object indicates a statuses
        Used to manage a worker status in master's perspective
        """
        self.active = True
        self.lock = threading.Lock()

    def set(self, is_active):
        """
        set status
        :param is_active: bool, is active or not
        """
        self.lock.acquire()
        self.active = is_active
        self.lock.release()

    def is_active(self):
        """
        :return: bool, is active or not
        """
        self.lock.acquire()
        status = self.active
        self.lock.release()
        return status


class RunningTask:
    def __init__(self):
        """
        Protected running task count
        """
        self.count = 0
        self.lock = threading.Lock()

    def get_count(self):
        """
        :return: int, current count
        """
        self.lock.acquire()
        c = self.count
        self.lock.release()
        return c

    def inc(self):
        """
        Increase 1
        """
        self.lock.acquire()
        self.count += 1
        self.lock.release()

    def dec(self):
        """
        Decrease 1
        """
        self.lock.acquire()
        if self.count > 0:
            self.count -= 1

        else:
            self.count = 0
        self.lock.release()


class Registry:
    def __init__(self, ip, log_level):
        """
        A master
        :param ip: string, ip address for workers to connect
        :param log_level: string, printing log level
        """
        self.logger = get_logger('Registry', level_name=log_level)

        # get config
        self.config = Config(log_level)
        self.pid = None
        # generate a random token used for workers to authorise
        # this token will be updated to database
        self.token = token_urlsafe(13)
        self.ip = ip
        # couchdb
        self.couch = CouchDB(log_level)
        self.client = self.couch.client
        # connection queue that are from workers or others
        # this queue is consumed by a connection consumer
        # which authorise the token sent from connections
        self.conn_queue = queue.Queue()
        # map the worker ids and their message queue
        self.msg_queue_dict = defaultdict(queue.Queue)
        # the lock of msg queue dictionary
        self.lock_msg_queue_dict = threading.Lock()
        # worker count that has connected
        self.worker_count = 0
        # active workers id set
        self.active_workers = set()
        # the api key hashes set, indicates occupied api key
        self.api_using = set()
        # map the worker id and worker data
        self.worker_data = {}
        self.lock_worker = threading.Lock()
        # friend task queue
        self.friends_tasks = queue.Queue()
        # timeline task queue
        self.timeline_tasks = queue.Queue()

        self.generating_friends = threading.Lock()
        # last time when generate friends task
        self.generating_friends_time = 0
        self.generating_timeline = threading.Lock()
        # last time when generate timeline task
        self.generating_timeline_time = 0

    def get_worker_id(self):
        """
        get a unique worker id
        :return: int, a unique id
        """
        self.lock_worker.acquire()
        self.worker_count += 1
        id_tmp = self.worker_count
        self.active_workers.add(id_tmp)
        self.lock_worker.release()
        return id_tmp

    def check_views(self):
        """
        check views' existences, not exists one will be create
        """
        # Make view result ascending
        # https://stackoverflow.com/questions/40463629

        design_doc = Document(self.client['users'], '_design/tasks')
        if not design_doc.exists():
            design_doc = DesignDocument(self.client['users'], '_design/tasks', partitioned=False)
            map_func_friends = 'function(doc) {' \
                               '    var date = new Date();' \
                               '    var timestamp = date.getTime() / 1000;' \
                               '    if (!doc.hasOwnProperty("friends_updated_at")) {doc.friends_updated_at = 0;}' \
                               '    if (!doc.hasOwnProperty("stream_user")) {doc.stream_user = false;}' \
                               '    if (doc.stream_user && timestamp - doc.friends_updated_at > ' \
                               + str(self.config.timeline_updating_window) + \
                               '                                          ) {' \
                               '        emit([doc.friends_updated_at, doc.inserted_time]);}' \
                               '}'
            design_doc.add_view('friends', map_func_friends)

            map_func_timeline = 'function(doc) {' \
                                '    var date = new Date();' \
                                '    var timestamp = date.getTime() / 1000;' \
                                '    if (!doc.hasOwnProperty("timeline_updated_at")) {' \
                                '        doc.timeline_updated_at = 0;' \
                                '    }' \
                                '    if (timestamp - doc.timeline_updated_at > ' \
                                + str(self.config.timeline_updating_window) + \
                                '                                          ) {' \
                                '      if (!doc.hasOwnProperty("stream_user")) {' \
                                '        doc.stream_user = false;' \
                                '      }' \
                                '       if (doc.stream_user) {' \
                                '        emit([doc.timeline_updated_at,0, doc.inserted_time,true],"stream_user");' \
                                '       }else{' \
                                '        emit([doc.timeline_updated_at,1, doc.inserted_time,false],"not_stream");' \
                                '      }' \
                                '    }' \
                                '}'
            design_doc.add_view('timeline', map_func_timeline)
            design_doc.save()

        design_doc = Document(self.client['control'], '_design/api-global')
        if not design_doc.exists():
            design_doc = DesignDocument(self.client['control'], '_design/api-global', partitioned=False)
            map_func_running_worker = 'function(doc) {' \
                                      ' if(doc.is_running){' \
                                      ' emit(doc._id, doc.api_key_hash);' \
                                      ' }' \
                                      '}'
            design_doc.add_view('running-worker', map_func_running_worker)

            design_doc.save()

    def check_db(self, db_name):
        """
        check database existences, will be created if not exist
        :param db_name: str, database name
        """
        if db_name not in self.client.all_dbs():
            partitioned = True
            if db_name in {'control'}:
                partitioned = False
            self.client.create_database(db_name, partitioned=partitioned)
            self.logger.debug("[*] database-{} not in Couch; created.".format(db_name))

    def check_dbs(self):
        """
        check databases in the hardcoded dictionary
        """
        for dn_name in {'statuses', 'users', 'control'}:
            self.check_db(dn_name)

    def tcp_server(self, lock):
        """
        start a tcp server and listen the ip and port
        :param lock: a lock used to make sure this thread starts first
        """
        lock.acquire()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # docker container inner ip is always 0.0.0.0
            s.bind(('0.0.0.0', self.config.registry_port))
            s.listen(1)
            self.logger.info('TCP server started at {}:{}'.format(self.ip, self.config.registry_port))
            lock.release()
            while True:
                conn, addr = s.accept()
                self.conn_queue.put((conn, addr))
        except OSError as e:
            lock.release()
            self.logger.error('[!] Cannot bind to 0.0.0.0:{}.'.format(self.config.registry_port))
            os._exit(0)

    def conn_handler(self):
        """
        handle connections from the tcp address and port
        """
        self.logger.info("ConnectionHandler started.")
        while True:
            (conn, addr) = self.conn_queue.get()
            threading.Thread(target=self.registry_msg_handler, args=(conn, addr,)).start()

    def keep_alive(self, worker_data):
        """
        keep alive function to keep a worker alive using heartbeats
        :param worker_data: worker's data
        """
        while True:
            if not worker_data.active.is_active():
                self.remove_worker(worker_data,
                                   'Lost heartbeat for {} seconds.'.format(self.config.max_heartbeat_lost_time))
                break
            worker_data.active.set(False)
            sleep(self.config.max_heartbeat_lost_time)

    def receiver(self, worker_data):
        """
        A thread that manages a connection with master and handle the messages
        :param worker_data: worker's data
        """
        buffer_data = ''
        keep_alive_thread = threading.Thread(target=self.keep_alive, args=(worker_data,))
        worker_data.threads.put(keep_alive_thread)
        keep_alive_thread.start()

        while True:
            try:
                buffer_data = self.handle_receive_buffer_data(buffer_data, worker_data)
            except socket.error as e:
                self.remove_worker(worker_data, e)
                break
            except json.JSONDecodeError as e:
                self.remove_worker(worker_data, e)
                break
            if not self.is_worker_active(worker_data):
                break
            sleep(1)

    def handle_receive_buffer_data(self, buffer_data, worker_data):
        """
        handle the received data from the buffer
        :param buffer_data:
        :param worker_data:
        :return:
        """
        try:
            buffer_data += worker_data.receiver_conn.recv(1024).decode('utf-8')
            while buffer_data.find('\n') != -1:
                first_pos = buffer_data.find('\n')
                recv_json = json.loads(buffer_data[:first_pos])
                self.logger.debug("Received: {}".format(recv_json))
                if 'token' in recv_json and recv_json['token'] == self.token:
                    worker_data.active.set(True)
                    del recv_json['token']
                    if recv_json['action'] == 'ping':
                        self.handle_action_ping(worker_data)
                    if recv_json['action'] == 'ask_for_task':
                        self.handle_action_ask_for_task(worker_data, recv_json)
                buffer_data = buffer_data[first_pos + 1:]
                buffer_data += worker_data.receiver_conn.recv(1024).decode('utf-8')
            return buffer_data
        except Exception as e:
            self.logger.warning("[{}]".format(worker_data.worker_id, str(e)))

    def handle_action_ping(self, worker_data):
        """
        handle ping message
        :param worker_data: worker's data
        :return:
        """
        msg = {'token': self.token, 'task': 'pong'}
        worker_data.msg_queue.put(json.dumps(msg))

    def handle_action_init_sender(self, recv_json, conn, addr):
        """
        handle message from worker that asks to initialise a sender
        :param recv_json: json, valid received message in json
        :param conn: tcp connection
        :param addr: address
        """
        # Worker send API key hashes to ask which it can use
        valid_api_key_hash = None
        self.lock_worker.acquire()
        for api_key_hash in recv_json['api_keys_hashes']:
            if api_key_hash not in self.api_using:
                valid_api_key_hash = api_key_hash
                break
        self.lock_worker.release()
        if valid_api_key_hash is None:
            msg = {'token': self.token, 'res': 'deny', 'msg': 'no valid api key'}
            conn.send(bytes(json.dumps(msg) + '\n', 'utf-8'))
        else:
            self.start_a_receiver(conn, addr, valid_api_key_hash)

    def start_a_receiver(self, conn, addr, valid_api_key_hash):
        """
        start a receiver for a connection
        :param conn: connection
        :param addr: address on this connection
        :param valid_api_key_hash: str, valid api key hash used to map api details
        """
        worker_id = self.get_worker_id()
        worker_data = WorkerData(worker_id, conn, addr, valid_api_key_hash)
        self.lock_worker.acquire()
        self.worker_data[worker_id] = worker_data
        self.api_using.add(valid_api_key_hash)
        self.lock_worker.release()

        self.lock_msg_queue_dict.acquire()
        self.msg_queue_dict[worker_id] = worker_data.msg_queue
        self.lock_msg_queue_dict.release()

        receiver_thread = threading.Thread(target=self.receiver, args=(worker_data,))
        worker_data.threads.put(receiver_thread)
        receiver_thread.start()
        self.update_worker_info_in_db(worker_id, valid_api_key_hash, True)
        msg = {'token': self.token, 'res': 'use_api_key',
               'api_key_hash': valid_api_key_hash, 'worker_id': worker_id}
        conn.send(bytes(json.dumps(msg) + '\n', 'utf-8'))
        self.logger.debug("Started a receiver")

    def handle_action_init_receiver(self, recv_json, conn, addr):
        """
        handle the message from worker that used to initialise a reveiver
        :param recv_json: json, valid received message in json
        :param conn: connection
        :param addr: address on this connection
        """
        worker_id = recv_json['worker_id']

        self.lock_worker.acquire()
        self.worker_data[worker_id].sender_conn = conn
        self.worker_data[worker_id].sender_addr = addr
        worker_data = self.worker_data[worker_id]
        self.lock_worker.release()

        sender_thread = threading.Thread(target=self.sender, args=(worker_data,))
        worker_data.threads.put(sender_thread)
        sender_thread.start()

    def handle_action_ask_for_task(self, worker_data, recv_json):
        """
        handle the message that asks for a task
        :param worker_data: worker's data
        :param recv_json: json, valid received message in json
        :return:
        """
        self.logger.debug("Got ask for task from Worker-{}: {}".format(recv_json['worker_id'], recv_json))
        if 'friends' in recv_json and 'followers' in recv_json \
                and recv_json['friends'] > 0 and recv_json['followers'] > 0:
            self.handle_task_friends(worker_data)
        if 'timeline' in recv_json and recv_json['timeline'] > 0:
            self.handle_task_timeline(worker_data, recv_json['timeline'])

    def handle_task_friends(self, worker_data):
        """
        handle a task request for friends
        :param worker_data: worker's data
        """
        self.logger.debug("[-] Has {} friends tasks in queue.".format(self.friends_tasks.qsize()))

        try:
            self.generate_friends_task()
            user_id = self.friends_tasks.get(timeout=0.01)
            msg = {'token': self.token, 'task': 'task', 'friends_ids': [user_id]}
            worker_data.msg_queue.put(json.dumps(msg))
            del msg['token']
            self.logger.info("[*] Sent task to Worker-{}: {} ".format(worker_data.worker_id, msg))
        except queue.Empty:
            pass

    def handle_task_timeline(self, worker_data, count):
        """
        handle a task request for user timeline
        :param worker_data: worker's data
        :param count: int, the count num of the user ids of timeline task to request
        """
        self.logger.debug("[-] Has {} timeline tasks in queue.".format(self.timeline_tasks.qsize()))
        try:
            self.generate_timeline_task(count)
            ids = []
            for _ in range(count):
                ids.append(self.timeline_tasks.get(timeout=0.01))
            msg = {'token': self.token, 'task': 'task', 'timeline_ids': ids}
            worker_data.msg_queue.put(json.dumps(msg))
            del msg['token']
            self.logger.info(
                "[*] Sent task to Worker-{}: {} ".format(worker_data.worker_id, msg))
        except queue.Empty:
            pass

    def sender(self, worker_data):
        """
        start a sender connection with the worker
        :param worker_data: worker's data
        """
        self.logger.info('[-] Worker-{} connected.'.format(worker_data.worker_id))
        try:
            self.send_stream_task(worker_data)
            while True:
                if not self.is_worker_active(worker_data):
                    break
                self.send_msg_in_queue(worker_data)
            self.remove_worker(worker_data, 'Worker is not active')

        except Exception as e:
            self.remove_worker(worker_data, e)

    def send_stream_task(self, worker_data):
        """
        send a stream task to a worker
        :param worker_data: worker's data
        """
        msg = {"task": "stream", "token": self.token}
        worker_data.sender_conn.send(bytes(json.dumps(msg) + '\n', 'utf-8'))
        self.logger.debug('[*] Sent stream task to Worker-{}.'.format(worker_data.worker_id))

    # @staticmethod
    def send_msg_in_queue(self, worker_data):
        """
        consume messages to send in the queue
        :param worker_data: worker's data
        """
        msg = worker_data.msg_queue.get()
        try:
            worker_data.sender_conn.send(bytes(msg + '\n', 'utf-8'))
            self.logger.debug("[*] Sent to Worker-{}: {} ".format(worker_data.worker_id, msg))
        except socket.error:
            worker_data.msg_queue.put(msg)

    def is_worker_active(self, worker_data):
        """
         Check if worker in active workers
        :param worker_data: worker's data
        :return: bool the status of the worker, true indicates it is alive
        """
        self.lock_worker.acquire()
        if worker_data.worker_id not in self.active_workers:
            self.lock_worker.release()
            return False
        self.lock_worker.release()
        return True

    def remove_worker_data(self, worker_data):
        """
        remove a worker's data when it disconnected
        :param worker_data: worker's data
        """
        self.lock_worker.acquire()
        if worker_data.worker_id in self.worker_data:
            del self.worker_data[worker_data.worker_id]
        self.lock_worker.release()

    def remove_msg_queue(self, worker_data):
        """
        remove a worker's message queue
        :param worker_data: worker's data
        """
        self.lock_msg_queue_dict.acquire()
        if worker_data.worker_id in self.msg_queue_dict:
            del self.msg_queue_dict[worker_data.worker_id]
        self.lock_msg_queue_dict.release()

    def deactivate_worker(self, worker_data):
        """
        deactive a worker
        :param worker_data: worker's data
        :return: int, the remaining workers count
        """
        self.lock_worker.acquire()
        self.active_workers.remove(worker_data.worker_id)
        self.api_using.remove(worker_data.api_key_hash)
        remaining = [worker_id for worker_id in self.active_workers]
        self.lock_worker.release()
        return remaining

    @staticmethod
    def close_socket_connection(worker_data):
        """
        close a socket connection
        :param worker_data: worker's data
        """
        # https://stackoverflow.com/questions/409783
        if worker_data.sender_conn is not None:
            worker_data.sender_conn.close()
        if worker_data.receiver_conn is not None:
            worker_data.receiver_conn.close()

    def remove_worker(self, worker_data, e):
        """
        remove a worker from the records
        :param worker_data: worker's data
        :param e: exception, indicates the exception for why remove this worker
        """
        # self.terminate_worker_threads(worker_data)
        self.remove_worker_data(worker_data)
        self.remove_msg_queue(worker_data)
        remaining = self.deactivate_worker(worker_data)
        self.close_socket_connection(worker_data)
        self.update_worker_info_in_db(worker_data.worker_id, worker_data.api_key_hash, False)
        self.remove_worker_info_from_db(worker_data)
        self.logger.warning("[-] Worker-{} exit: "
                            "{}(remaining active workers:{})".format(worker_data.worker_id, e, remaining))

    @staticmethod
    def terminate_worker_threads(worker_data):
        """
        terminate a worker's related threads
        :param worker_data: worker's data
        """
        while not worker_data.threads.empty():
            t = worker_data.threads.get()
            t.terminate()

    def remove_worker_info_from_db(self, worker_data):
        """
        remove the worker's status from database
        :param worker_data: worker's data
        """
        try:
            self.client['control']["worker-" + str(worker_data.worker_id)].delete()
        except Exception:
            traceback.format_exc()

    def registry_msg_handler(self, conn, addr):
        """
        the registry entry for connections,
        all the connections should be filtered first here using the random token
        :param conn: connection
        :param addr: address
        """
        self.logger.debug("registry starts msg handler")
        data = ''
        access_time = int(time())
        entries = 100
        while True:
            try:
                data += conn.recv(1024).decode('utf-8')
                while data.find('\n') != -1:
                    entries += 1
                    access_time = int(time())
                    first_pos = data.find('\n')
                    recv_json = json.loads(data[:first_pos])
                    if 'token' in recv_json and recv_json['token'] == self.token:
                        # del recv_json['token']
                        if recv_json['action'] == 'init':
                            if recv_json['role'] == 'sender':
                                self.handle_action_init_sender(recv_json, conn, addr)
                            elif recv_json['role'] == 'receiver':
                                self.handle_action_init_receiver(recv_json, conn, addr)
                        entries -= 100
                entries -= 1
            except json.JSONDecodeError:
                traceback.format_exc()
                break
            except socket.error:
                traceback.format_exc()
                break
            except Exception as e:
                self.logger.warning(e)
                traceback.format_exc()
                break
            if len(data) > 10240:
                data = ''

            if int(time()) - access_time > 60 or not entries:
                break
            else:
                sleep(1)
        self.logger.debug("registry ends msg handler")

    def tasks_generator(self):
        """
        task generator to generate tasks
        """
        self.logger.info("TaskGenerator started.")
        while True:
            self.generate_timeline_task(hard=True)
            self.generate_friends_task(hard=True)
            sleep(120)

    def generate_friends_task(self, hard=False):
        """
        generate friends task
        :param hard: generate friends tasks when ignore other rules
        """
        if hard or (self.friends_tasks.empty() and self.generating_friends.acquire(
                blocking=False) and time() - self.generating_friends_time > 5):
            try:
                self.client.connect()
                count = 0
                result = self.client['users'].get_view_result('_design/tasks', view_name='friends',
                                                              limit=self.config.max_tasks_num, reduce=False).all()
                for doc in result:
                    count += 1
                    self.friends_tasks.put(doc['id'])
                self.generating_friends_time = time()
                self.logger.debug("Generated {} friends tasks".format(count))
            except Exception:
                traceback.format_exc()
            self.generating_friends.release()
        else:
            return

    def generate_timeline_task(self, count=1, hard=False):
        """
        generate timeline task
        :param hard: generate timeline tasks when ignore other rules
        :param count: int, how many use ids in this task
        """
        if hard or (self.timeline_tasks.qsize() < count and self.generating_timeline.acquire(blocking=False)
                    and time() - self.generating_timeline_time > 2):
            try:
                self.client.connect()
                result = self.client['users'].get_view_result('_design/tasks', view_name='timeline',
                                                              limit=self.config.max_tasks_num, reduce=False).all()

                for doc in result:
                    self.timeline_tasks.put([doc['id'], doc['key'][3]])
                self.logger.debug("Generated {} timeline tasks.".format(len(result)))

            except Exception:
                traceback.format_exc()
            self.generating_timeline_time = time()
            self.generating_timeline.release()

        else:
            return

    def update_doc(self, db, key, values):
        """
        update a document in database
        :param db: database object
        :param key: key
        :param values: values
        """
        try:
            if key not in db:
                db.create_document(values)
            else:
                doc = db[key]
                del values['_id']
                for (k, v) in values.items():
                    doc.update_field(action=doc.field_set, field=k, value=v)
            self.logger.debug('Updated {} info in database'.format(key))
        except Exception:
            traceback.format_exc()

    def update_registry_info(self):
        """
        update master's info in database
        :return:
        """
        # update registry info
        values = {
            '_id': 'registry',
            'ip': self.ip,
            'port': self.config.registry_port,
            'token': self.token,
            'updated_at': asctime(localtime(time()))

        }
        self.update_doc(self.client['control'], 'registry', values)

    def remove_all_worker_info(self):
        """
        remove all worker info in database,
        this worker info in database in only for some convenience to watch the status of harvesters
        not directly related with the harvester system
        :return:
        """
        try:
            for doc in self.client['control']:
                if 'worker' in doc['_id']:
                    doc.delete()
        except Exception:
            traceback.format_exc()

    def update_worker_info_in_db(self, worker_id, api_key_hash, is_running):
        """
        update a worker's info in database
        :param worker_id: int, unique worker id
        :param api_key_hash: str, api key hash used to map api key details
        :param is_running: bool, the status of this worker
        """
        values = {
            '_id': 'worker-' + str(worker_id),
            'api_key_hash': api_key_hash,
            'is_running': is_running,
            'updated_at': asctime(localtime(time()))
        }
        self.update_doc(self.client['control'], 'worker-' + str(worker_id), values)

    def run(self):
        """
        run the master
        """
        lock = threading.Lock()
        threading.Thread(target=self.conn_handler).start()
        threading.Thread(target=self.tcp_server, args=(lock,)).start()
        lock.acquire()
        lock.release()
        # self.save_pid()
        self.check_dbs()
        self.update_registry_info()
        self.remove_all_worker_info()
        self.check_views()

        threading.Thread(target=self.tasks_generator).start()
