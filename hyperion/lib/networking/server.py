import socket
import select
import time
import logging
import logging.handlers
import struct
import threading
import hyperion.manager
from signal import *
import hyperion.lib.util.depTree
import hyperion.lib.util.actionSerializer as actionSerializer
import hyperion.lib.util.exception as exceptions
import hyperion.lib.util.events as events
import hyperion.lib.util.config as config
import libtmux

from psutil import Process, NoSuchProcess
from subprocess import Popen, PIPE

import queue as queue
import selectors


def recvall(connection, n):
    """Helper function to recv n bytes or return None if EOF is hit

    To read a message with an expected size and combine it to one object, even if it was split into more than one
    packets.

    Parameters
    ----------
    connection : socket.socket
        Connection to a socket to read from.
    n : int
        Size of the message to read in bytes.

    Returns
    -------
    bytes
        Expected message combined into one string.
    """

    data = b""
    while len(data) < n:
        packet = connection.recv(n - len(data))
        data += packet
    return data


class BaseServer(object):
    """Base class for servers."""

    def __init__(self):
        # self.port: int
        self.sel = selectors.DefaultSelector()
        self.keep_running = True
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(config.DEFAULT_LOG_LEVEL)
        self.send_queues = {}
        signal(SIGINT, self._handle_sigint)

    def accept(self, sock, mask):
        """Callback for new connections"""
        new_connection, addr = sock.accept()
        self.logger.debug("accept({})".format(addr))
        new_connection.setblocking(False)
        self.send_queues[new_connection] = queue.Queue()
        self.sel.register(new_connection, selectors.EVENT_READ | selectors.EVENT_WRITE)

    def _interpret_message(self, action, args, connection):
        raise NotImplementedError

    def write(self, connection):
        """Callback for write events"""
        send_queue = self.send_queues.get(connection)
        if send_queue and not send_queue.empty() and self.keep_running:
            # Messages available
            next_msg = send_queue.get()
            try:
                connection.sendall(next_msg)
            except socket.error as err:
                self.logger.error("Error while writing message to socket: %s" % err)

    def read(self, connection):
        raise NotImplementedError

    def _handle_sigint(self, signum, frame):
        self.logger.debug("Received C-c")
        self._quit()

    def _quit(self):
        self.logger.debug(
            "Sending all pending messages to slave clients before quitting server..."
        )
        for sub in self.send_queues.values():
            while sub.empty():
                time.sleep(0.5)
        self.logger.debug("... All pending messages sent to slave clients!")
        self.send_queues = {}
        self.keep_running = False


class Server(BaseServer):
    def __init__(
        self,
        port,
        cc,
        loop_in_thread=False,
    ):
        BaseServer.__init__(self)
        self.port = port
        self.cc = cc  # type: hyperion.ControlCenter
        self.event_queue = queue.Queue()
        self.cc.add_subscriber(self.event_queue)

        server_address = ("localhost", port)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.setblocking(False)
        try:
            server.bind(server_address)
            self.logger.debug(f"Starting server on localhost:{server.getsockname()[1]}")
        except socket.error as e:
            if e.errno == 98:
                self.logger.critical(
                    f"Server adress '{server_address}' is already in use! Try waiting a few seconds if you are sure "
                    "there is no other instance running"
                )
                # Simulate sigint
                self._handle_sigint(SIGINT, None)
            else:
                self.logger.critical(f"Error while trying to bind server adress: {e}")
                self._handle_sigint(SIGINT, None)
        self.logger.info("Hyperion server up and running")
        server.listen(5)

        self.function_mapping = {
            "start_all": self.cc.start_all,
            "start": self._start_component_wrapper,
            "check": self._check_component_wrapper,
            "stop_all": self.cc.stop_all,
            "stop": self._stop_component_wrapper,
            "get_conf": self._send_config,
            "get_host_states": self._send_host_states,
            "get_host_stats": self._send_host_stats,
            "quit": self.cc.cleanup,
            "reconnect_with_host": self.cc.reconnect_with_host,
            "unsubscribe": None,
            "reload_config": self.cc.reload_config,
            "start_clone_session": self._handle_start_clone_session,
        }

        self.receiver_mapping = {
            "get_conf": "single",
            "get_host_states": "single",
            "get_host_stats": "single",
        }

        self.sel.register(server, selectors.EVENT_READ, self.accept)

        if not loop_in_thread:
            self._loop()
        else:
            self.worker = worker = threading.Thread(target=self._loop)
            worker.start()

    def _loop(self):
        while self.keep_running:
            try:
                for key, mask in self.sel.select(timeout=1):
                    connection = key.fileobj  # type: ignore[assignment]
                    if key.data and self.keep_running:
                        callback = key.data
                        callback(connection, mask)

                    else:
                        if mask & selectors.EVENT_READ:
                            self.read(connection)
                        if mask & selectors.EVENT_WRITE:
                            self.write(connection)
                self._process_events()
                time.sleep(0.3)
            except OSError:
                self.logger.error(
                    "Caught timeout exception while reading from/writing to ui clients. "
                    "If this error occured during shutdown, everything is in order!"
                )
                pass

        self.logger.debug("Exited messaging loop")
        self.sel.close()

    def read(self, connection):
        """Callback for read events"""
        try:
            raw_msglen = connection.recv(4)
            if raw_msglen:
                # A readable client socket has data
                msglen = struct.unpack(">I", raw_msglen)[0]
                data = recvall(connection, msglen)
                self.logger.debug("Received message")
                action, args = actionSerializer.deserialize(data)

                if action:
                    worker = threading.Thread(
                        target=self._interpret_message, args=(action, args, connection)
                    )
                    worker.start()

                    if action == "quit":
                        worker.join()
                        self._quit()
            else:
                # Handle uncontrolled connection loss
                self.send_queues.pop(connection)
                self.sel.unregister(connection)
                self.logger.debug(
                    f"Connection to client on {connection.getpeername()[1]} was lost!"
                )
                connection.close()
        except socket.error as e:
            self.logger.error(
                "Something went wrong while receiving a message. Check debug for more information"
            )
            self.logger.debug(f"Socket excpetion: {e}")
            self.send_queues.pop(connection)
            self.sel.unregister(connection)
            connection.close()

    def _interpret_message(self, action, args, connection):
        self.logger.debug(f"Action: {action}, args: {args}")
        func = self.function_mapping.get(action)

        if action == "unsubscribe":
            self.send_queues.pop(connection)
            self.sel.unregister(connection)
            self.logger.debug(f"Client {connection.getpeername()[0]} unsubscribed")
            connection.close()
            return
        assert func is not None

        response_type = self.receiver_mapping.get(action)
        if response_type:
            try:
                ret = func(*args)
            except TypeError:
                self.logger.error(f"Ignoring unrecognized action '{action}'")
                return
            action = f"{action}_response"
            message = actionSerializer.serialize_request(action, [ret])
            if response_type == "all":
                for message_queue in self.send_queues.values():
                    message_queue.put(message)
            elif response_type == "single":
                self.send_queues[connection].put(message)

        else:
            try:
                func(*args)
            except TypeError as e:
                self.logger.error(f"Ignoring unrecognized action '{action}': {e}")
                return

    def _process_events(self):
        """Process events enqueued by the manager and send them to connected clients if necessary."""
        # Put events received by slave manager into event queue to forward to clients
        assert self.cc.slave_server is not None
        while not self.cc.slave_server.notify_queue.empty():
            event = self.cc.slave_server.notify_queue.get_nowait()
            self.event_queue.put(event)

        while not self.event_queue.empty():
            event = self.event_queue.get_nowait()
            message = actionSerializer.serialize_request("queue_event", [event])
            for message_queue in self.send_queues.values():
                message_queue.put(message)

            if isinstance(event, events.DisconnectEvent):
                self.cc.host_states[event.host_name] = (
                    0,
                    config.HostConnectionState.DISCONNECTED,
                )

    def _start_component_wrapper(self, comp_id, force_mode=False):
        try:
            comp = self.cc.get_component_by_id(comp_id)
            self.cc.start_component(comp, force_mode)
        except exceptions.ComponentNotFoundException as e:
            self.logger.error(e.message)

    def _check_component_wrapper(self, comp_id):
        try:
            comp = self.cc.get_component_by_id(comp_id)
            self.cc.check_component(comp)
        except exceptions.ComponentNotFoundException as e:
            self.logger.error(e.message)

    def _stop_component_wrapper(self, comp_id):
        try:
            comp = self.cc.get_component_by_id(comp_id)
            self.cc.stop_component(comp)
        except exceptions.ComponentNotFoundException as e:
            self.logger.error(e.message)

    def _handle_start_clone_session(self, comp_id):
        comp = self.cc.get_component_by_id(comp_id)

        if self.cc.run_on_localhost(comp):
            self.cc.start_local_clone_session(comp)
        else:
            self.cc.start_remote_clone_session(comp)

    def _send_config(self):
        return self.cc.config

    def _send_host_states(self):
        return self.cc.host_states

    def _send_host_stats(self):
        return self.cc.host_stats

    def _handle_sigint(self, signum, frame):
        self.logger.debug("Received C-c")
        self._quit()
        worker = threading.Thread(target=self.cc.cleanup, args=[True])
        worker.start()
        worker.join()

    def _quit(self):
        self.logger.debug("Stopping Server...")
        self.send_queues = {}
        self.keep_running = False


class SlaveManagementServer(BaseServer):
    def __init__(self):
        """Init slave managing socket server."""
        BaseServer.__init__(self)
        self.notify_queue = queue.Queue()  # type: queue.Queue
        self.function_mapping = {
            "queue_event": self._forward_event,
            "auth": None,
            "unsubscribe": None,
        }
        self.check_buffer = {}
        self.slave_log_handlers = {}
        self.port_mapping = {}

        server_address = ("localhost", 0)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setblocking(False)
        try:
            server.bind(server_address)
            self.logger.info(
                "Starting slave management server on localhost:%s"
                % server.getsockname()[1]
            )
            self.port = server.getsockname()[1]
        except socket.error as e:
            if e.errno == 98:
                self.logger.critical(
                    "Server adress is already in use! This is odd, free port should be chosen "
                    "automatically by socket module..."
                )
                self.keep_running = False
            else:
                self.logger.critical("Error while trying to bind server adress: %s" % e)
                self.keep_running = False
        server.listen(5)
        self.sel.register(server, selectors.EVENT_READ, self.accept)

        self.thread = threading.Thread(target=self._run_loop)

    def start(self):
        self.thread.start()

    def kill_slaves(self, full):
        """Send shutdown command to all connected slave client sockets.

        Parameters
        ----------
        full : bool
            Whether the tmux session is killed, too.
        """

        if full:
            action = "quit"
        else:
            action = "suspend"
        payload = []
        message = actionSerializer.serialize_request(action, payload)

        for slave_queue in self.send_queues.values():
            slave_queue.put(message)

    def stop(self):
        self._quit()
        if self.thread.is_alive():
            self.thread.join()
        self.logger.info("Slave server successfully shutdown!")

    def _quit(self):
        self.logger.debug(
            "Sending all pending messages to slave clients before quitting server..."
        )
        send_queues = self.send_queues.copy()
        for sq in send_queues.values():
            while not sq.empty():
                time.sleep(0.5)
        self.logger.debug("... All pending messages sent to slave clients!")
        self.send_queues = {}
        self.keep_running = False

    def _run_loop(self):
        while self.keep_running:
            for key, mask in self.sel.select(timeout=1):
                connection = key.fileobj  # type: ignore[assignment]
                if key.data and self.keep_running:
                    callback = key.data
                    callback(connection, mask)

                else:
                    if mask & selectors.EVENT_READ:
                        self.read(connection)
                    if mask & selectors.EVENT_WRITE:
                        self.write(connection)
            time.sleep(0.3)

        self.sel.close()

    def _forward_event(self, event):
        """Process events enqueued by the manager and send them to connected clients if necessary."""

        # self.logger.debug("Forwarding slave client event: %s" % event)
        self.notify_queue.put(event)

        if isinstance(event, events.CheckEvent):
            self.check_buffer[event.comp_id] = event.check_state

    def _interpret_message(self, action, args, connection):
        # self.logger.debug("Action: %s, args: %s" % (action, args))
        func = self.function_mapping.get(action)

        if action == "unsubscribe":
            self.send_queues.pop(connection)
            self.sel.unregister(connection)
            self.logger.info(f"Client {connection.getpeername()[0]} unsubscribed")
            connection.close()
            return

        if action == "auth":
            hostname = args[0]
            self.port_mapping[connection] = hostname
            return
        assert func is not None

        try:
            func(*args)
        except TypeError:
            self.logger.error(f"Ignoring unrecognized slave action '{action}'")

    def read(self, connection):
        """Callback for read events"""
        try:
            raw_msglen = connection.recv(4)
            if raw_msglen:
                # A readable client socket has data
                msglen = struct.unpack(">I", raw_msglen)[0]
                data = recvall(connection, msglen)
                action, args = actionSerializer.deserialize(data)

                if action is not None:
                    worker = threading.Thread(
                        target=self._interpret_message, args=(action, args, connection)
                    )
                    worker.start()
                else:
                    # Not an action message - trying to decode as log message
                    record = logging.makeLogRecord(args)  # type: ignore[arg-type]
                    try:
                        self.slave_log_handlers[connection.getpeername()[0]].handle(
                            record
                        )
                    except KeyError:
                        self.logger.debug(
                            "Got log message from yet unhandled slave socket logger"
                        )
                        pass
            else:
                # Handle uncontrolled connection loss
                hostname = self.port_mapping[connection]

                self.send_queues.pop(connection)
                self.sel.unregister(connection)
                self.logger.error(f"Connection to client '{hostname}' was lost!")
                self.notify_queue.put(
                    events.SlaveDisconnectEvent(hostname, connection.getpeername()[1])
                )
                connection.close()
        except KeyError:
            self.logger.error(
                f"Could not get hostname of connection '{connection}' where message was read from. This should not happen, dropping connection."
            )
            self.send_queues.pop(connection)
            self.sel.unregister(connection)
            connection.close()
        except socket.error as e:
            self.logger.error(
                "Something went wrong while receiving a message. Check debug for more information"
            )
            self.logger.debug(f"Socket excpetion: {e}")
            self.send_queues.pop(connection)
            self.sel.unregister(connection)
            connection.close()

    def validate_on_slave(
        self,
        remote_hostname,
        local_hostname,
        config_path,
    ):
        """Run validate on slave to test if the config could be loaded successfully.

        Parameters
        ----------
        hostname : str
            Host where the slave is going to be started.
        host_ip : str
            Resolved ip to host `hostname`.
        config_path : str
            Path to the config file on the remote.
        config_name : str
            Name of the configuration (not the file name!).
        window : libtmux.Window
            tmux window of the host connection.

        Returns
        -------
        bool
            True if validation was successful.
        """

        cmd = f"hyperion validate --config {config_path}"

        if config.SLAVE_HYPERION_SOURCE_PATH != None:
            cmd = f"source {config.SLAVE_HYPERION_SOURCE_PATH} && {cmd}"

        forward_cmd = (
            f"ssh -F {config.CUSTOM_SSH_CONFIG_PATH} {remote_hostname} '{cmd}'"
        )
        pipe = Popen(forward_cmd, shell=True, stdout=PIPE, stderr=PIPE)
        stdout, stderr = pipe.communicate()
        if pipe.returncode < 0:
            ret = config.ExitStatus.UNKNOWN_ERROR
        else:
            ret = config.ExitStatus(pipe.returncode)

        if ret != config.ExitStatus.FINE:
            err = stderr.decode("utf-8")
            if len(err) > 0:
                msg = err
            else:
                msg = stdout.decode("utf-8")
            self.logger.error(
                f"validate on remote returned with error {ret.name}: \n#####################\n{msg}\n#####################"
            )
            return False

        self.logger.debug(
            f"validate on remote returned {ret.name}: \n#####################\n{stdout.decode('utf-8')} \n#####################"
        )

        # Checking ssh connection
        forward_cmd = f"ssh -F {config.CUSTOM_SSH_CONFIG_PATH} {remote_hostname} 'ssh {local_hostname} echo test'"
        pipe = Popen(forward_cmd, shell=True, stdout=PIPE, stderr=PIPE)
        stdout, stderr = pipe.communicate()
        if pipe.returncode != 0:
            self.logger.error(
                f"ssh from slave to main failed with error: {pipe.communicate()[1].decode('utf-8')}"
            )
            return False
        return True

    def start_slave(
        self,
        hostname,
        host_ip,
        config_path,
        config_name,
        window,
        custom_messages=None,
    ):
        """Start slave on the remote host.

        Parameters
        ----------
        hostname : str
            Host where the slave is going to be started.
        host_ip : str
            Resolved ip to host `hostname`.
        config_path : str
            Path to the config file on the remote.
        config_name : str
            Name of the configuration (not the file name!).
        window : libtmux.Window
            tmux window of the host connection.
        custom_messages : list[bytes], optional
            Optional custom messages to send on connect (or reconnect), by default None.

        Returns
        -------
        bool
            True if starting slave was successful.
        """

        if not custom_messages:
            custom_messages = []

        for conn, sq in self.send_queues.items():
            if hostname == self.port_mapping.get(conn):
                self.logger.debug(
                    f"Socket to {hostname} already exists! Checking if it is still connected"
                )
                try:
                    select.select([conn], [], [conn], 1)
                    self.logger.debug("Connection still up")
                    self._forward_event(
                        events.SlaveReconnectEvent(hostname, conn.getpeername()[1])
                    )

                    for message in custom_messages:
                        sq.put(message)

                    return True
                except socket.error:
                    self.logger.error(
                        f"Existing connection to {hostname} died. Trying to reconnect..."
                    )

        log_file_path = (
            f"{config.TMP_LOG_PATH}/remote/slave/{config_name}@{hostname}.log"
        )
        slave_log_handler = logging.handlers.RotatingFileHandler(log_file_path)
        hyperion.manager.rotate_log(log_file_path, f"{config_name}@{hostname}")

        slave_log_handler.setFormatter(config.ColorFormatter())
        self.slave_log_handlers[host_ip] = slave_log_handler

        cmd = f"hyperion slave --config {config_path} -H {socket.gethostname()} -p {self.port}"

        if config.SLAVE_HYPERION_SOURCE_PATH != None:
            cmd = f"source {config.SLAVE_HYPERION_SOURCE_PATH} && {cmd}"
        tmux_cmd = f'tmux new -d -s "{config_name}-slave" "{cmd}"'
        self.logger.debug(
            f"Running following command to start slave on remote and conntect to this master: {tmux_cmd}"
        )
        window.cmd("send-keys", tmux_cmd, "Enter")

        self.logger.info(f"Waiting for slave on '{host_ip}' ({hostname}) to connect...")
        end_t = time.time() + 10
        while time.time() < end_t:
            for conn, sq in self.send_queues.items():
                con_host = self.port_mapping.get(conn)
                if con_host:
                    self.logger.debug(f"'{con_host}' is connected")
                if hostname == con_host:
                    self.logger.info("Connection successfully established")

                    for message in custom_messages:
                        sq.put(message)

                    self._forward_event(
                        events.SlaveReconnectEvent(hostname, conn.getpeername()[1])
                    )
                    return True
            time.sleep(0.5)

        self.logger.error("Connection to slave failed!")
        return False

    def kill_slave_on_host(self, hostname):
        """Kill a slave session of the current master session running on the remote host.

        Parameters
        ----------
        hostname : str
            Host to kill the slave on
        """

        for conn, sq in self.send_queues.items():
            if hostname == self.port_mapping.get(conn):
                self.logger.debug(
                    f"Socket to '{hostname}' still exists - Sending shutdown"
                )
                try:
                    # Test if connection still alive
                    select.select([conn], [], [conn], 1)
                    message = actionSerializer.serialize_request("quit", [])
                    sq.put(message)
                except socket.error:
                    self.logger.error(
                        f"Existing connection to '{hostname}' died. Could not send quit command"
                    )

    def start_clone_session(self, comp_id, hostname):
        action = "start_clone_session"
        payload = [comp_id]

        connection_queue = None

        message = actionSerializer.serialize_request(action, payload)

        for connection in self.send_queues:
            if self.port_mapping.get(connection) == hostname:
                connection_queue = self.send_queues.get(connection)
                break

        if connection_queue:
            connection_queue.put(message)
        else:
            raise exceptions.SlaveNotReachableException(
                f"Slave at '{hostname}' is not reachable!"
            )

    def start_component(self, comp_id, hostname):
        action = "start"
        payload = [comp_id]

        connection_queue = None

        message = actionSerializer.serialize_request(action, payload)

        for connection in self.send_queues:
            if self.port_mapping.get(connection) == hostname:
                connection_queue = self.send_queues.get(connection)
                break

        if connection_queue:
            connection_queue.put(message)
        else:
            raise exceptions.SlaveNotReachableException(
                f"Slave at '{hostname}' is not reachable!"
            )

    def stop_component(self, comp_id, hostname):
        action = "stop"
        payload = [comp_id]

        connection_queue = None

        message = actionSerializer.serialize_request(action, payload)

        for connection in self.send_queues:
            if self.port_mapping.get(connection) == hostname:
                connection_queue = self.send_queues.get(connection)
                break

        if connection_queue:
            connection_queue.put(message)
        else:
            raise exceptions.SlaveNotReachableException(
                f"Slave at '{hostname}' is not reachable!"
            )

    def check_component(self, comp_id, hostname, component_wait):
        self.logger.debug(f"Sending '{comp_id}' check request to '{hostname}'")
        action = "check"
        payload = [comp_id]

        connection_queue = None

        message = actionSerializer.serialize_request(action, payload)

        for connection in self.send_queues:
            if self.port_mapping.get(connection) == hostname:
                connection_queue = self.send_queues.get(connection)
                break

        self.check_buffer[comp_id] = None

        if connection_queue:
            connection_queue.put(message)
            end_t = time.time() + component_wait + 1

            self.logger.debug(
                f"Waiting on '{hostname}' response for {component_wait} seconds"
            )
            while end_t > time.time():
                if self.check_buffer[comp_id] is not None:
                    break
                time.sleep(0.5)
        else:
            self.logger.error(f"Slave on '{hostname}' is not connected!")

        ret = self.check_buffer[comp_id]
        if ret is not None:
            self.logger.debug(
                f"Slave answered check request with {config.STATE_DESCRIPTION.get(ret)}"
            )
            return ret
        else:
            self.logger.error("No answer from slave - returning unreachable")
            return config.CheckState.UNREACHABLE
