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
from typing import Optional

from sqlalchemy import Column, String, SmallInteger, and_
from sqlalchemy.engine.result import RowProxy

from mautrix.types import RoomID
from mautrix.bridge.db.base import Base


class Portal(Base):
    __tablename__ = "portal"

    # Hangouts chat information
    gid: str = Column(String(255), primary_key=True)
    conv_type: int = Column(SmallInteger, nullable=False)
    other_user_id: str = Column(String(255), nullable=True)

    # Matrix portal information
    mxid: RoomID = Column(String(255), unique=True, nullable=True)

    # Hangouts chat metadata
    name = Column(String, nullable=True)

    @classmethod
    def scan(cls, row: RowProxy) -> Optional['Portal']:
        gid, conv_type, other_user_id, mxid, name = row
        return cls(gid=gid, conv_type=conv_type, other_user_id=other_user_id, mxid=mxid, name=name)

    @classmethod
    def get_by_gid(cls, gid: str) -> Optional['Portal']:
        return cls._select_one_or_none(cls.c.gid == gid)

    @classmethod
    def get_by_mxid(cls, mxid: RoomID) -> Optional['Portal']:
        return cls._select_one_or_none(cls.c.mxid == mxid)

    @property
    def _edit_identity(self):
        return self.c.gid == self.gid

    def insert(self) -> None:
        with self.db.begin() as conn:
            conn.execute(self.t.insert().values(gid=self.gid, conv_type=self.conv_type,
                                                other_user_id=self.other_user_id, mxid=self.mxid,
                                                name=self.name))