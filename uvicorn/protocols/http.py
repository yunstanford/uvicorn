import asyncio
import email
import http
import httptools
import os
import time

from uvicorn.protocols.websocket import websocket_upgrade


def set_time_and_date():
    global CURRENT_TIME
    global DATE_HEADER

    CURRENT_TIME = time.time()
    DATE_HEADER = b''.join([
        b'date: ',
        email.utils.formatdate(CURRENT_TIME, usegmt=True).encode(),
        b'\r\n'
    ])


def get_status_line(status_code):
    try:
        phrase = http.HTTPStatus(status_code).phrase.encode()
    except ValueError:
        phrase = b''
    return b''.join([
        b'HTTP/1.1 ', str(status_code).encode(), b' ', phrase, b'\r\n'
    ])


CURRENT_TIME = 0.0
DATE_HEADER = b''
SERVER_HEADER = b'server: uvicorn\r\n'
STATUS_LINE = {
    status_code: get_status_line(status_code) for status_code in range(100, 600)
}


set_time_and_date()


class BodyChannel(object):
    __slots__ = ['_queue', 'name']

    def __init__(self):
        self._queue = asyncio.Queue()
        self.name = 'body:%d' % id(self)

    async def send(self, message):
        await self._queue.put(message)

    async def recieve(self):
        return await self._queue.get()


class ReplyChannel(object):
    __slots__ = ['_protocol', 'name']

    def __init__(self, protocol):
        self._protocol = protocol
        self.name = 'reply:%d' % id(self)

    async def send(self, message):
        transport = self._protocol.transport

        if transport is None:
            return

        status = message.get('status')
        headers = message.get('headers')
        content = message.get('content')
        more_content = message.get('more_content', False)

        if status is not None:
            response = [
                STATUS_LINE[status],
                SERVER_HEADER,
                DATE_HEADER,
            ]
            transport.write(b''.join(response))

        if headers is not None:
            response = []
            if content is not None and not more_content:
                response = [b'content-length: ', str(len(content)).encode(), b'\r\n']

            for header_name, header_value in headers:
                response.extend([header_name, b': ', header_value, b'\r\n'])
            response.append(b'\r\n')

            transport.write(b''.join(response))

        if content is not None:
            transport.write(content)

        if not more_content and (not status) or (not self._protocol.request_parser.should_keep_alive()):
            transport.close()



class HttpProtocol(asyncio.Protocol):
    __slots__ = [
        'consumer', 'loop', 'request_parser',
        'base_message', 'base_channels',
        'message', 'channels',
        'headers', 'transport',
        'upgrade'
    ]

    def __init__(self, consumer, loop, sock, cfg):
        self.consumer = consumer
        self.loop = loop
        self.request_parser = httptools.HttpRequestParser(self)

        self.base_message = {
            'channel': 'http.request',
            'scheme': 'https' if cfg.is_ssl else 'http',
            'root_path': os.environ.get('SCRIPT_NAME', ''),
            'server': sock.getsockname()
        }
        self.base_channels = {
            'reply': ReplyChannel(self)
        }

        self.transport = None
        self.message = None
        self.headers = None
        self.upgrade = None

    # The asyncio.Protocol hooks...
    def connection_made(self, transport):
        self.transport = transport

    def connection_lost(self, exc):
        self.transport = None

    def eof_received(self):
        pass

    def data_received(self, data):
        try:
            self.request_parser.feed_data(data)
        except httptools.HttpParserUpgrade:
            websocket_upgrade(self)

    # Event hooks called back into by HttpRequestParser...
    def on_message_begin(self):
        self.message = self.base_message.copy()
        self.channels = self.base_channels.copy()
        self.headers = []

    def on_url(self, url):
        parsed = httptools.parse_url(url)
        method = self.request_parser.get_method()
        http_version = self.request_parser.get_http_version()
        self.message.update({
            'http_version': http_version,
            'method': method.decode('ascii'),
            'path': parsed.path.decode('ascii'),
            'query_string': parsed.query if parsed.query else b'',
            'headers': self.headers
        })

    def on_header(self, name: bytes, value: bytes):
        name = name.lower()
        if name == b'upgrade':
            self.upgrade = value
        elif name == b'expect' and value.lower() == b'100-continue':
            self.transport.write(b'HTTP/1.1 100 Continue\r\n\r\n')
        self.headers.append([name, value])

    def on_body(self, body: bytes):
        if 'body' not in self.channels:
            self.channels['body'] = BodyChannel()
            self.loop.create_task(self.consumer(self.message, self.channels))
        message = {
            'content': body,
            'more_content': True
        }
        self.loop.create_task(self.channels['body'].send(message))

    def on_message_complete(self):
        if self.upgrade is not None:
            return

        if 'body' not in self.channels:
            self.loop.create_task(self.consumer(self.message, self.channels))
        else:
            message = {
                'content': b'',
                'more_content': False
            }
            self.loop.create_task(self.channels['body'].send(message))

    def on_chunk_header(self):
        pass

    def on_chunk_complete(self):
        pass
