# mautrix-hangouts - A Matrix-Hangouts puppeting bridge
# Copyright (C) 2019 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Any, Dict, Iterator, Optional, Iterable, List, Awaitable, Union, TYPE_CHECKING
from concurrent import futures
import datetime
import asyncio
import logging
import time

import hangups
from hangups import Client, UserList, RefreshTokenCache, ConversationEvent, ChatMessageEvent, \
    MembershipChangeEvent
from hangups.conversation import ConversationList, Conversation
from hangups.parsers import TypingStatusMessage
from hangups import hangouts_pb2 as hangouts

from mautrix.types import UserID
from mautrix.appservice import AppService

from .config import Config
from .db import User as DBUser
from .util.hangups_try_auth import try_auth, TryAuthResp
from . import puppet as pu, portal as po

if TYPE_CHECKING:
    from .context import Context

config: Config


class User:
    az: AppService
    loop: asyncio.AbstractEventLoop
    log: logging.Logger = logging.getLogger("mau.user")
    by_mxid: Dict[UserID, 'User'] = {}

    client: Optional[Client]
    command_status: Optional[Dict[str, Any]]
    is_whitelisted: bool
    is_admin: bool
    _db_instance: Optional[DBUser]

    mxid: UserID
    gid: str
    refresh_token: str
    name: Optional[str]
    name_future: asyncio.Future
    connected: bool

    chats: ConversationList
    users: UserList

    def __init__(self, mxid: UserID, gid: str = None, refresh_token: str = None,
                 db_instance: Optional[DBUser] = None) -> None:
        self.mxid = mxid
        self.gid = gid
        self.refresh_token = refresh_token
        self.by_mxid[mxid] = self
        self.command_status = None
        self.is_whitelisted, self.is_admin = config.get_permissions(mxid)
        self._db_instance = db_instance
        self.client = None
        self.name = None
        self.name_future = asyncio.Future()
        self.connected = False

        self.log = self.log.getChild(self.mxid)

    # region Sessions

    def save(self) -> None:
        self.db_instance.edit(refresh_token=self.refresh_token, gid=self.gid)

    @property
    def db_instance(self) -> DBUser:
        if not self._db_instance:
            self._db_instance = DBUser(mxid=self.mxid, refresh_token=self.refresh_token)
        return self._db_instance

    @classmethod
    def from_db(cls, db_user: DBUser) -> 'User':
        return User(mxid=db_user.mxid, refresh_token=db_user.refresh_token, db_instance=db_user)

    @classmethod
    def get_all(cls) -> Iterator['User']:
        for db_user in DBUser.all():
            yield cls.from_db(db_user)

    @classmethod
    def get_by_mxid(cls, mxid: UserID, create: bool = True) -> Optional['User']:
        if pu.Puppet.get_id_from_mxid(mxid) is not None or mxid == cls.az.bot_mxid:
            return None
        try:
            return cls.by_mxid[mxid]
        except KeyError:
            pass

        db_user = DBUser.get_by_mxid(mxid)
        if db_user:
            return cls.from_db(db_user)

        if create:
            user = cls(mxid)
            user.db_instance.insert()
            return user

        return None

    # endregion

    async def is_logged_in(self) -> bool:
        return self.client and self.connected

    @classmethod
    async def init_all(cls) -> None:
        users = [user for user in cls.get_all() if user.refresh_token]

        with futures.ThreadPoolExecutor() as pool:
            auth_resps: List[TryAuthResp] = await asyncio.gather(
                *[cls.loop.run_in_executor(pool, try_auth, user.refresh_token)
                  for user in users],
                loop=cls.loop)
        finish = []
        for user, auth_resp in zip(users, auth_resps):
            if auth_resp.success:
                finish.append(user.login_complete(auth_resp.cookies))
            else:
                user.log.exception("Failed to resume session with stored refresh token",
                                   exc_info=auth_resp.error)
        await asyncio.gather(*finish, loop=cls.loop)

    async def login_complete(self, cookies: dict) -> None:
        self.client = Client(cookies)
        asyncio.ensure_future(self.start(), loop=self.loop)
        self.client.on_connect.add_observer(self.on_connect)
        self.client.on_reconnect.add_observer(self.on_reconnect)
        self.client.on_disconnect.add_observer(self.on_disconnect)

    async def start(self) -> None:
        try:
            await self.client.connect()
            self.log.info("Client connection finished")
        except Exception:
            self.log.exception("Exception in connection")

    async def stop(self) -> None:
        await self.client.disconnect()

    async def on_connect(self) -> None:
        self.connected = True
        asyncio.ensure_future(self.on_connect_later(), loop=self.loop)

    async def on_connect_later(self) -> None:
        try:
            info = await self.client.get_self_info(hangouts.GetSelfInfoRequest(
                request_header=self.client.get_request_header()
            ))
        except Exception:
            self.log.exception("Failed to get_self_info")
            return
        self.gid = info.self_entity.id.gaia_id
        self.name = info.self_entity.properties.display_name
        self.name_future.set_result(self.name)
        self.save()
        try:
            await self.sync()
        except Exception:
            self.log.exception("Failed to sync conversations and users")

    async def on_reconnect(self) -> None:
        self.connected = True

    async def on_disconnect(self) -> None:
        self.connected = False

    async def sync(self) -> None:
        users, chats = await hangups.build_user_conversation_list(self.client)
        await asyncio.gather(*self.sync_users(users), *self.sync_chats(chats), loop=self.loop)

    def sync_users(self, users: UserList) -> Iterable[Awaitable[None]]:
        self.users = users
        return (pu.Puppet.get_by_gid(info.id_.gaia_id, create=True).update_info(self, info)
                for info in users.get_all())

    def sync_chats(self, chats: ConversationList) -> Iterable[Awaitable[None]]:
        self.chats = chats
        self.chats.on_event.add_observer(self.on_event)
        self.chats.on_typing.add_observer(self.on_typing)
        quota = config["bridge.initial_chat_sync"]
        return (po.Portal.get_by_conversation(info).create_matrix_room(self, info)
                for info in self.chats.get_all(include_archived=False)[:quota])

    # region Hangouts event handling

    async def on_event(self, event: ConversationEvent) -> None:
        conv: Conversation = self.chats.get(event.conversation_id)
        portal = po.Portal.get_by_conversation(conv)
        if not portal:
            return

        sender = pu.Puppet.get_by_gid(event.user_id.gaia_id)

        if isinstance(event, ChatMessageEvent):
            await portal.handle_hangouts_message(self, sender, event)
        elif isinstance(event, MembershipChangeEvent):
            self.log.info(
                f"{event.id_} by {event.user_id} in {event.conversation_id} ({conv._conversation.type}): {event.participant_ids} {event.type_}'d")
        else:
            self.log.info(f"Unrecognized event {event}")

    async def on_typing(self, event: TypingStatusMessage):
        portal = po.Portal.get_by_gid(event.conv_id)
        if not portal:
            return False
        self.log.info(
            f"{event.user_id} is typing({event.status}) in {event.conv_id} at {event.timestamp}")

    # endregion
    # region Hangouts API calls

    async def set_typing(self, conversation_id: str, typing: bool) -> None:
        await self.client.set_typing(hangouts.SetTypingRequest(
            request_header=self.client.get_request_header(),
            conversation_id=conversation_id,
            type=hangouts.TYPING_TYPE_STARTED if typing else hangouts.TYPING_TYPE_STOPPED,
        ))

    async def send_emote(self, conversation_id: str, text: str) -> str:
        resp = await self.client.send_chat_message(hangouts.SendChatMessageRequest(
            annotation=[hangouts.EventAnnotation(type=4)],
            event_request_header=hangouts.EventRequestHeader(
                conversation_id=hangouts.ConversationId(
                    id=conversation_id,
                ),
                client_generated_id=self.client.get_client_generated_id(),
            ),
            message_content=hangouts.MessageContent(
                segment=[hangups.ChatMessageSegment(text).serialize()]
            )
        ))
        return resp.created_event.event_id

    async def send_text(self, conversation_id: str, text: str) -> str:
        resp = await self.client.send_chat_message(hangouts.SendChatMessageRequest(
            event_request_header=hangouts.EventRequestHeader(
                conversation_id=hangouts.ConversationId(
                    id=conversation_id,
                ),
                client_generated_id=self.client.get_client_generated_id(),
            ),
            message_content=hangouts.MessageContent(
                segment=[hangups.ChatMessageSegment(text).serialize()]
            )
        ))
        return resp.created_event.event_id

    async def mark_read(self, conversation_id: str,
                        timestamp: Optional[Union[datetime.datetime, int]] = None) -> None:
        if isinstance(timestamp, datetime.datetime):
            timestamp = hangups.parsers.to_timestamp(timestamp)
        elif not timestamp:
            timestamp = int(time.time() * 1_000_000)
        await self.client.update_watermark(hangouts.UpdateWatermarkRequest(
            request_header=self.client.get_request_header(),
            conversation_id=conversation_id,
            last_read_timestamp=timestamp,
        ))

    # endregion


class UserRefreshTokenCache(RefreshTokenCache):
    user: User

    def __init__(self, user: User) -> None:
        self.user = user

    def get(self) -> str:
        return self.user.refresh_token

    def set(self, refresh_token: str) -> None:
        self.user.log.debug("New refresh token: %s", refresh_token)
        self.user.refresh_token = refresh_token
        self.user.save()


def init(context: 'Context') -> Awaitable[None]:
    global config
    User.az, config, User.loop = context.core
    return User.init_all()