import fcntl
import signal
import struct
import termios
from typing import List
import asyncio
from websockets import WebSocketServerProtocol
import collections
import pty
import os

class Terminal:
    cmdline: str = "/bin/bash"
    process: asyncio.subprocess.Process = None

    master_fd: int
    slave_fd: int

    subscribers: List[WebSocketServerProtocol] = []
    buffer: collections.deque = None

    cols: int = 80
    rows: int = 24
    
    def __init__(self, cmdline: str):
        if cmdline is not None:
            self.cmdline = cmdline
        self.buffer = collections.deque([], maxlen=4096)

    # SERIALIZE ============================================
    def serialize(self) -> dict:
        data = dict(
            is_started=self._is_process_started(),
            is_completed=self._is_process_completed(),
        )

        if self.process is not None:
            data['pid'] = self.process.pid

            if self.process.returncode is not None:
                data['exitcode'] = self.process.returncode

        return data

    # CONTROL ==============================================
    async def start(self):
        await self._start_process()

    async def shutdown(self):
        self._kill_process()
        await self.close_subscribers()
        self.subscribers = []

    async def change_window_size(self, rows: int, cols: int):
        if self._is_process_alive():
            await self._change_pty_size(rows, cols)
            await self.process.send_signal(signal.SIGWINCH)
            await self._write_stdin(b'\x1b[8;%d;%dt' % (rows, cols))
    
    async def _change_pty_size(self, rows: int, cols: int):
        self.rows = rows
        self.cols = cols

        if self.master_fd is not None:
            new_size = struct.pack('HHHH', rows, cols, 0, 0)
            await self._run_async(fcntl.ioctl, self.master_fd, termios.TIOCSWINSZ, new_size)
            

    # WORKERS ==============================================
    async def _process_subscriber(self, ws: WebSocketServerProtocol):
        await ws.send(bytes(self.buffer))
        if not self._is_process_started():
            await self.start()

        while not ws.closed:
            try:
                data = await ws.recv()
                if type(data) == str:
                    data = bytes(data, 'utf-8')

                await self._write_stdin(data)
            except Exception as e:
                print('Exception', e)

            await asyncio.sleep(0)

    # PROCESS CONTROL =======================================
    def get_terminal_env(self):
        result = dict(**os.environ)
        result["TERM"] = "xterm-256color"
        result["PWD"] = result["HOME"]
        result["SSH_TTY"] = os.ttyname(self.slave_fd)
        result["LINES"] = str(self.rows)
        result["COLUMNS"] = str(self.cols)

        return result


    async def _start_process(self):
        self.master_fd, self.slave_fd = pty.openpty()

        await self._change_pty_size(self.rows, self.cols)
        self.process = await asyncio.create_subprocess_shell(
            self.cmdline,
            stdout=self.slave_fd,
            stderr=self.slave_fd,
            stdin=self.slave_fd,
            env=self.get_terminal_env(),
        )

        asyncio.ensure_future(
            self._read_output_loop()
        )

    def _kill_process(self):
        if self._is_process_alive():
            self.process.kill()
        
        try:
            os.close(self.master_fd)
            os.close(self.slave_fd)
        except Exception as e:
            print(e)
        

    # WEBSOCKET =============================================
    def add_subscriber(self, ws: WebSocketServerProtocol):
        if not self.is_subscriber(ws):
            self.subscribers.append(ws)
            asyncio.ensure_future(self._process_subscriber(ws))

    def is_subscriber(self, ws: WebSocketServerProtocol):
        try:
            self.subscribers.index(ws)
            return True
        except ValueError:
            return False
    
    # WEBSOCKET - INTERNAL ==================================
    def _remove_subscriber(self, ws: WebSocketServerProtocol):
        # Internal only!
        if self.is_subscriber(ws):
            self.subscribers.remove(ws)
            if not ws.closed:
                ws.close()

    # BROADCAST =============================================
    async def broadcast_subscribers(self, data: bytes):
        closed = []
        for ws in self.subscribers:
            if ws.closed:
                closed.append(ws)
                continue
            await ws.send(data)

        for ws in closed:
            self._remove_subscriber(ws)

    async def close_subscribers(self):
        for ws in self.subscribers:
            if not ws.closed:
                ws.close()

    # IS ALIVE ==============================================
    def _is_process_started(self):
        if self.process is None:
            return False
        
        return True
    
    def _is_process_alive(self):
        if not self._is_process_started():
            return False

        if self.process.returncode is not None:
            return False
        
        return True
    
    def _is_process_completed(self):
        return not self._is_process_alive() and self._is_process_started()
        
    # PROCESS CONTROL =======================================
    async def _write_stdin(self, input: bytes):
        await self._run_async(os.write, self.master_fd, input)
        await self._run_async(os.fsync, self.master_fd)
    
    async def _read_output(self) -> bytes:
        output = await self._run_async(os.read, self.master_fd, 50)
        if len(output) > 0:
            self._put_buffer(output)
            await self.broadcast_subscribers(output)
            return output

    async def _read_output_loop(self):
        while self._is_process_alive():
            try:
                await self._read_output()
            except Exception as e:
                print(e)
                pass

            await asyncio.sleep(0)
    
    def _put_buffer(self, chars: bytes):
        for i in chars:
            self.buffer.append(i)

    async def _run_async(self, fun, *args):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fun, *args)

        

