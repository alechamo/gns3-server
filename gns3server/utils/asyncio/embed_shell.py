#!/usr/bin/env python
#
# Copyright (C) 2017 GNS3 Technologies Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import sys
import asyncio
import inspect
import io

from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.contrib.completers import WordCompleter
from prompt_toolkit.enums import DEFAULT_BUFFER
from prompt_toolkit.eventloop.base import EventLoop
from prompt_toolkit.interface import CommandLineInterface
from prompt_toolkit.layout.screen import Size
from prompt_toolkit.shortcuts import create_prompt_application, create_asyncio_eventloop
from prompt_toolkit.terminal.vt100_output import Vt100_Output
from prompt_toolkit.input import StdinInput

from .telnet_server import AsyncioTelnetServer, TelnetConnection
from .input_stream import InputStream


class EmbedShell:
    """
    An asynchronous shell use for stuff like EthernetSwitch console
    or built in VPCS
    """

    def __init__(self, reader=None, writer=None, loop=None, welcome_message=None):
        self._loop = loop
        self._reader = reader
        self._writer = writer
        self._prompt = '> '
        self._welcome_message = welcome_message

    @property
    def writer(self):
        return self._writer

    @writer.setter
    def writer(self, val):
        self._writer = val

    @property
    def reader(self):
        return self._reader

    @reader.setter
    def reader(self, val):
        self._reader = val

    @property
    def prompt(self):
        return self._prompt

    @prompt.setter
    def prompt(self, val):
        self._prompt = val

    @property
    def welcome_message(self):
        return self._welcome_message

    @welcome_message.setter
    def welcome_message(self, welcome_message):
        self._welcome_message = welcome_message

    @asyncio.coroutine
    def help(self, *args):
        """
        Show help
        """
        res = ''
        if len(args) == 0:
            res = 'Help:\n'
        for name, value in inspect.getmembers(self):
            if not inspect.isgeneratorfunction(value):
                continue
            if name.startswith('_') or (len(args) and name != args[0]) or name == 'run':
                continue
            doc = inspect.getdoc(value)
            res += name
            if len(args) and doc:
                res += ': ' + doc
            elif doc:
                res += ': ' + doc.split('\n')[0]
            res += '\n'
        if len(args) == 0:
            res += '\nhelp command for details about a command\n'
        return res

    @asyncio.coroutine
    def _parse_command(self, text):
        cmd = text.split(' ')
        found = False
        if cmd[0] == '?':
            cmd[0] = 'help'

        # when there is no command specified just return empty result
        if not cmd[0].strip():
            return ""

        for (name, meth) in inspect.getmembers(self):
            if name == cmd[0]:
                cmd.pop(0)
                res = yield from meth(*cmd)
                found = True
                break
        if not found:
            res = ('Command not found {}\n'.format(cmd[0]) + (yield from self.help()))
        return res

    @asyncio.coroutine
    def run(self):
        if self._welcome_message:
            self._writer.feed_data(self._welcome_message.encode())
        while True:
            self._writer.feed_data(self._prompt.encode())
            result = yield from self._reader.readline()
            result = result.decode().strip('\n')
            res = yield from self._parse_command(result)
            self._writer.feed_data(res.encode())

    def get_commands(self):
        """
        Returns commands available to execute
        :return: list of (name, doc) tuples
        """
        commands = []
        for name, value in inspect.getmembers(self):
            if not inspect.isgeneratorfunction(value):
                continue
            if name.startswith('_') or name == 'run':
                continue
            doc = inspect.getdoc(value)
            commands.append((name, doc))
        return commands


class PatchedStdinInput(StdinInput):
    """
    `prompt_toolkit.input.StdinInput` checks whether stdin is tty or not, we don't need do that.
    Fixes issue when PyCharm runs own terminal without emulation.
    https://github.com/GNS3/gns3-server/issues/1172
    """
    def __init__(self, stdin=None):
        self.stdin = stdin or sys.stdin
        try:
            self.stdin.fileno()
        except io.UnsupportedOperation:
            if 'idlelib.run' in sys.modules:
                raise io.UnsupportedOperation(
                    'Stdin is not a terminal. Running from Idle is not supported.')
            else:
                raise io.UnsupportedOperation('Stdin is not a terminal.')


class UnstoppableEventLoop(EventLoop):
    """
    Partially fake event loop which cannot be stopped by CommandLineInterface
    """
    def __init__(self, loop):
        self._loop = loop

    def close(self):
        " Ignore. "

    def stop(self):
        " Ignore. "

    def run_in_executor(self, *args, **kwargs):
        return self._loop.run_in_executor(*args, **kwargs)

    def call_from_executor(self, callback, **kwargs):
        self._loop.call_from_executor(callback, **kwargs)

    def add_reader(self, fd, callback):
        raise NotImplementedError

    def remove_reader(self, fd):
        raise NotImplementedError


class ShellConnection(TelnetConnection):
    def __init__(self, reader, writer, shell, loop):
        super(ShellConnection, self).__init__(reader, writer)
        self._shell = shell
        self._loop = loop
        self._cli = None
        self._cb = None
        self._size = Size(rows=40, columns=79)
        self.encoding = 'utf-8'


    @asyncio.coroutine
    def connected(self):
        # prompt_toolkit internally checks if it's on windows during output rendering but
        # we need to force that we use Vt100_Output not Win32_Output
        from prompt_toolkit import renderer
        renderer.is_windows = lambda: False

        def get_size():
            return self._size

        self._cli = CommandLineInterface(
            application=create_prompt_application(self._shell.prompt),
            eventloop=UnstoppableEventLoop(create_asyncio_eventloop(self._loop)),
            input=PatchedStdinInput(sys.stdin),
            output=Vt100_Output(self, get_size))

        self._cb = self._cli.create_eventloop_callbacks()
        self._inputstream = InputStream(self._cb.feed_key)
        # Taken from prompt_toolkit telnet server
        # https://github.com/jonathanslenders/python-prompt-toolkit/blob/99fa7fae61c9b4ed9767ead3b4f9b1318cfa875d/prompt_toolkit/contrib/telnet/server.py#L165
        self._cli._is_running = True

        if self._shell.welcome_message is not None:
            self.send(self._shell.welcome_message.encode())

        self._cli._redraw()

    @asyncio.coroutine
    def disconnected(self):
        pass

    def window_size_changed(self, columns, rows):
        self._size = Size(rows=rows, columns=columns)
        self._cb.terminal_size_changed()

    @asyncio.coroutine
    def feed(self, data):
        data = data.decode()
        self._inputstream.feed(data)
        self._cli._redraw()

        # Prompt toolkit has returned the command
        if self._cli.is_returning:
            try:
                returned_value = self._cli.return_value()
            except (EOFError, KeyboardInterrupt) as e:
                # don't close terminal, just keep it alive
                self.close()
                return

            command = returned_value.text

            res = yield from self._shell._parse_command(command)
            self.send(res.encode())
            self.reset()

    def reset(self):
        """ Resets terminal screen"""
        self._cli.reset()
        self._cli.buffers[DEFAULT_BUFFER].reset()
        self._cli.renderer.request_absolute_cursor_position()
        self._cli._redraw()

    def write(self, data):
        """ Compat with CLI"""
        self.send(data)

    def flush(self):
        """ Compat with CLI"""
        pass


def create_telnet_shell(shell, loop=None):
    """
    Run a shell application with a telnet frontend
    :param application: An EmbedShell instance
    :param loop: The event loop
    :returns: Telnet server
    """

    if loop is None:
        loop = asyncio.get_event_loop()

    def factory(reader, writer):
        return ShellConnection(reader, writer, shell, loop)

    return AsyncioTelnetServer(binary=True, echo=True, naws=True, connection_factory=factory)


def create_stdin_shell(shell, loop=None):
    """
    Run a shell application with a stdin frontend

    :param application: An EmbedShell instance
    :param loop: The event loop
    :returns: Telnet server
    """
    @asyncio.coroutine
    def feed_stdin(loop, reader, shell):
        history = InMemoryHistory()
        completer = WordCompleter([name for name, _ in shell.get_commands()], ignore_case=True)
        while True:
            line = yield from prompt(
                ">", patch_stdout=True, return_asyncio_coroutine=True, history=history, completer=completer)
            line += '\n'
            reader.feed_data(line.encode())

    @asyncio.coroutine
    def read_stdout(writer):
        while True:
            c = yield from writer.read(1)
            print(c.decode(), end='')
            sys.stdout.flush()

    reader = asyncio.StreamReader()
    writer = asyncio.StreamReader()
    shell.reader = reader
    shell.writer = writer
    if loop is None:
        loop = asyncio.get_event_loop()

    reader_task = loop.create_task(feed_stdin(loop, reader, shell))
    writer_task = loop.create_task(read_stdout(writer))
    shell_task = loop.create_task(shell.run())
    return asyncio.gather(shell_task, writer_task, reader_task)

if __name__ == '__main__':
    loop = asyncio.get_event_loop()

    class Demo(EmbedShell):

        @asyncio.coroutine
        def hello(self, *args):
            """
            Hello world

            This command accept arguments: hello tutu will display tutu
            """
            @asyncio.coroutine
            def world():
                yield from asyncio.sleep(2)
                if len(args):
                    return ' '.join(args)
                else:
                    return 'world\n'

            return (yield from world())

    # Demo using telnet
    shell = Demo(welcome_message="Welcome!\n")
    server = create_telnet_shell(shell, loop=loop)
    coro = asyncio.start_server(server.run, '127.0.0.1', 4444, loop=loop)
    s = loop.run_until_complete(coro)
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    # Demo using stdin
    # loop.run_until_complete(create_stdin_shell(Demo()))
    # loop.close()
