from __future__ import annotations

import asyncio
import random
import re
from contextlib import asynccontextmanager
from typing import List, Sized, Union

from loguru import logger
from pyrogram import filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UsernameNotOccupied, UserNotParticipant
from pyrogram.handlers import EditedMessageHandler, MessageHandler
from pyrogram.types import Message

from ...utils import to_iterable, truncate_str
from ..tele import Client

__ignore__ = True


class Session:
    def __init__(self, reply, follows=None, delays=None):
        self.reply = reply
        self.follows = follows
        self.delays = delays
        self.lock = asyncio.Lock()
        self.delayed = asyncio.Event()
        self.followed = asyncio.Event()
        self.canceled = asyncio.Event()
        if not self.follows:
            return self.followed.set()

    async def delay(self):
        if not self.delayed:
            return self.delayed.set()
        if isinstance(self.delays, Sized) and len(self.delays) == 2:
            time = random.uniform(*self.delays)
        else:
            time = self.delays
        await asyncio.sleep(time)
        self.delayed.set()

    async def follow(self):
        async with self.lock:
            self.follows -= 1
            if self.follows <= 0:
                self.followed.set()
            return self.follows

    async def wait(self, timeout=240):
        asyncio.create_task(self.delay())
        try:
            await asyncio.wait_for(asyncio.gather(self.delayed.wait(), self.followed.wait()), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        else:
            return not self.canceled.is_set()

    async def cancel(self):
        self.canceled.set()
        self.delayed.set()
        self.followed.set()


class Monitor:
    name = __name__
    chat_name = None
    chat_allow_outgoing = False
    chat_user = []
    chat_keyword = []
    chat_probability = 1.0
    chat_delay = 0
    chat_follow_user = 0
    chat_reply = None

    def __init__(self, client: Client, nofail=True):
        self.client = client
        self.nofail = nofail
        self.log = logger.bind(scheme="telemonitor", name=self.name, username=self.client.me.first_name)
        self.session = None
        self.failed = asyncio.Event()

    @asynccontextmanager
    async def listener(self):
        filter = filters.caption | filters.text
        if self.chat_name:
            filter = filter & filters.chat(self.chat_name)
        if not self.chat_allow_outgoing:
            filter = filter & (~filters.outgoing)

        handlers = [
            MessageHandler(self._message_handler, filter),
            EditedMessageHandler(self._message_handler, filter),
        ]
        for h in handlers:
            self.client.add_handler(h)
        yield
        for h in handlers:
            try:
                self.client.remove_handler(h)
            except ValueError:
                pass

    async def _start(self):
        try:
            return await self.start()
        except Exception as e:
            if self.nofail:
                self.log.opt(exception=e).warning(f"初始化错误:")
                return False
            else:
                raise

    async def start(self):
        try:
            chat = await self.client.get_chat(self.chat_name)
            self.chat_name = chat.id
        except UsernameNotOccupied:
            self.log.warning(f'初始化错误: 群组 "{self.chat_name}" 不存在.')
            return False
        try:
            me = await chat.get_member("me")
        except UserNotParticipant:
            self.log.warning(f'初始化错误: 尚未加入群组 "{chat.title}".')
            return False
        if me.status in (ChatMemberStatus.LEFT, ChatMemberStatus.RESTRICTED):
            self.log.warning(f'初始化错误: 被群组 "{chat.title}" 禁言.')
            return False
        spec = f"[green]{chat.title}[/] [gray50](@{chat.username})[/]"
        self.log.info(f"开始监视: {spec}.")
        async with self.listener():
            await self.failed.wait()
            self.log.error(f"发生错误, 不再监视: {spec}.")
            return False

    def get_key(self, message: Message):
        sender = message.from_user
        if self.chat_user and not any(i in to_iterable(self.chat_user) for i in (sender.id, sender.username)):
            return False
        text = message.text or message.caption
        if self.chat_keyword:
            for k in to_iterable(self.chat_keyword):
                match = re.search(k, text, re.IGNORECASE)
                if match:
                    return match.groups() or match.group(0)
            else:
                return False
        else:
            return text

    async def get_reply(self, message: Message, keys: str):
        if callable(self.chat_reply):
            result = self.chat_reply(message, keys)
            if asyncio.iscoroutinefunction(self.chat_reply):
                return await result
            else:
                return result
        else:
            return self.chat_reply

    @staticmethod
    def get_spec(keys):
        if isinstance(keys, str):
            return truncate_str(keys.replace("\n", " "), 30)
        else:
            return ", ".join(keys)

    async def _message_handler(self, client: Client, message: Message):
        try:
            await self.message_handler(client, message)
        except OSError as e:
            self.log.info(f'发生错误: "{e}", 忽略.')
        except Exception as e:
            self.failed.set()
            if self.nofail:
                self.log.opt(exception=e).warning(f"发生错误:")
            else:
                raise
        finally:
            message.continue_propagation()

    async def message_handler(self, client: Client, message: Message):
        keys = self.get_key(message)
        if keys:
            spec = self.get_spec(keys)
            self.log.info(f'监听到关键信息: "{spec}".')
            if random.random() >= self.chat_probability:
                self.log.info(f'由于概率设置, 不予回应: "{spec}".')
                return False
            reply = await self.get_reply(message, keys)
            if self.session:
                await self.session.cancel()
            if self.chat_follow_user:
                self.log.info(f"将等待{self.chat_follow_user}个人回复: {reply}")
            self.session = Session(reply, follows=self.chat_follow_user, delays=self.chat_delay)
            if await self.session.wait():
                self.session = None
                self.log.debug(f'执行监听响应: "{spec}".')
                await self.on_trigger(message, keys, reply)
        else:
            if self.session and not self.session.followed.is_set():
                text = message.text or message.caption
                if self.session.reply == text:
                    now = await self.session.follow()
                    self.log.debug(
                        f'从众计数 ({self.chat_follow_user - now}/{self.chat_follow_user}): "{message.from_user.first_name}"'
                    )

    async def on_trigger(self, message: Message, keys: Union[List[str], str], reply: str):
        if reply:
            return await self.client.send_message(message.chat.id, reply)