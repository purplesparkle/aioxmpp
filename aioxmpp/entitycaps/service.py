########################################################################
# File name: service.py
# This file is part of: aioxmpp
#
# LICENSE
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/>.
#
########################################################################
import asyncio
import copy
import functools
import logging
import os
import tempfile
import urllib.parse

import aioxmpp.callbacks
import aioxmpp.disco as disco
import aioxmpp.service
import aioxmpp.xml
import aioxmpp.xso

from . import xso as my_xso
from . import caps115


logger = logging.getLogger("aioxmpp.entitycaps")


class Cache:
    """
    This provides a two-level cache for entity capabilities information. The
    idea is to have a trusted database, e.g. installed system-wide or shipped
    with :mod:`aioxmpp` and in addition a user-level database which is
    automatically filled with hashes which have been found by the
    :class:`Service`.

    The trusted database is taken as read-only and overrides the user-collected
    database. When a hash is in both databases, it is removed from the
    user-collected database (to save space).

    In addition to serving the databases, it provides deduplication for queries
    by holding a cache of futures looking up the same hash.

    Database management (user API):

    .. automethod:: set_system_db_path

    .. automethod:: set_user_db_path

    Queries (API intended for :class:`Service`):

    .. automethod:: create_query_future

    .. automethod:: lookup_in_database

    .. automethod:: lookup
    """

    def __init__(self):
        self._lookup_cache = {}
        self._memory_overlay = {}
        self._system_db_path = None
        self._user_db_path = None

    def _erase_future(self, for_hash, for_node, fut):
        try:
            existing = self._lookup_cache[for_hash, for_node]
        except KeyError:
            pass
        else:
            if existing is fut:
                del self._lookup_cache[for_hash, for_node]

    def set_system_db_path(self, path):
        self._system_db_path = path

    def set_user_db_path(self, path):
        self._user_db_path = path

    def lookup_in_database(self, hash_, node):
        try:
            result = self._memory_overlay[hash_, node]
        except KeyError:
            pass
        else:
            logger.debug("memory cache hit: %s %r", hash_, node)
            return result

        quoted = urllib.parse.quote(node, safe="")
        if self._system_db_path is not None:
            try:
                f = (
                    self._system_db_path / "{}_{}.xml".format(hash_, quoted)
                ).open("rb")
            except OSError:
                pass
            else:
                logger.debug("system db hit: %s %r", hash_, node)
                with f:
                    return aioxmpp.xml.read_single_xso(f, disco.xso.InfoQuery)

        if self._user_db_path is not None:
            try:
                f = (
                    self._user_db_path / "{}_{}.xml".format(hash_, quoted)
                ).open("rb")
            except OSError:
                pass
            else:
                logger.debug("user db hit: %s %r", hash_, node)
                with f:
                    return aioxmpp.xml.read_single_xso(f, disco.xso.InfoQuery)

        raise KeyError(node)

    @asyncio.coroutine
    def lookup(self, hash_, node):
        """
        Look up the given `node` URL using the given `hash_` first in the
        database and then by waiting on the futures created with
        :meth:`create_query_future` for that node URL and hash.

        If the hash is not in the database, :meth:`lookup` iterates as long as
        there are pending futures for the given `hash_` and `node`. If there
        are no pending futures, :class:`KeyError` is raised. If a future raises
        a :class:`ValueError`, it is ignored. If the future returns a value, it
        is used as the result.
        """
        try:
            result = self.lookup_in_database(hash_, node)
        except KeyError:
            pass
        else:
            return result

        while True:
            fut = self._lookup_cache[hash_, node]
            try:
                result = yield from fut
            except ValueError:
                continue
            else:
                return result

    def create_query_future(self, hash_, node):
        """
        Create and return a :class:`asyncio.Future` for the given `hash_`
        function and `node` URL. The future is referenced internally and used
        by any calls to :meth:`lookup` which are made while the future is
        pending. The future is removed from the internal storage automatically
        when a result or exception is set for it.

        This allows for deduplication of queries for the same hash.
        """
        fut = asyncio.Future()
        fut.add_done_callback(
            functools.partial(self._erase_future, hash_, node)
        )
        self._lookup_cache[hash_, node] = fut
        return fut

    def add_cache_entry(self, hash_, node, entry):
        """
        Add the given `entry` (which must be a :class:`~.disco.xso.InfoQuery`
        instance) to the user-level database keyed with the hash function type
        `hash_` and the `node` URL. The `entry` is **not** validated to
        actually map to `node` with the given `hash_` function, it is expected
        that the caller perfoms the validation.
        """
        copied_entry = copy.copy(entry)
        copied_entry.node = node
        self._memory_overlay[hash_, node] = copied_entry
        if self._user_db_path is not None:
            asyncio.async(asyncio.get_event_loop().run_in_executor(
                None,
                writeback,
                self._user_db_path,
                hash_,
                node,
                entry.captured_events))


class EntityCapsService(aioxmpp.service.Service):
    """
    This service implements :xep:`0115`, transparently. Besides loading the
    service, no interaction is required to get some of the benefits of
    :xep:`0115`.

    Two additional things need to be done by users to get full support and
    performance:

    1. To make sure that peers are always up-to-date with the current
       capabilities, it is required that users listen on the
       :meth:`on_ver_changed` signal and re-emit their current presence when it
       fires.

       The service takes care of attaching capabilities information on the
       outgoing stanza, using a stanza filter.

    2. Users should use a process-wide :class:`Cache` instance and assign it to
       the :attr:`cache` of each :class:`.entitycaps.Service` they use. This
       improves performance by sharing (verified) hashes among :class:`Service`
       instances.

       In addition, the hashes should be saved and restored on shutdown/start
       of the process. See the :class:`Cache` for details.

    .. signal:: on_ver_changed

       The signal emits whenever the ``ver`` of the local client changes. This
       happens when the set of features or identities announced in the
       :class:`.DiscoServer` changes.

    .. autoattribute:: cache

    .. versionchanged:: 0.8

       This class was formerly known as :class:`aioxmpp.entitycaps.Service`. It
       is still available under that name, but the alias will be removed in
       1.0.

    """

    ORDER_AFTER = {
        disco.DiscoClient,
        disco.DiscoServer,
    }

    NODE = "http://aioxmpp.zombofant.net/"

    on_ver_changed = aioxmpp.callbacks.Signal()

    def __init__(self, node, **kwargs):
        super().__init__(node, **kwargs)

        self.ver = None
        self._cache = Cache()

        self.disco_server = self.dependencies[disco.DiscoServer]
        self.disco_client = self.dependencies[disco.DiscoClient]
        self.disco_server.register_feature(
            "http://jabber.org/protocol/caps"
        )

    @property
    def cache(self):
        """
        The :class:`Cache` instance used for this :class:`Service`. Deleting
        this attribute will automatically create a new :class:`Cache` instance.

        The attribute can be used to share a single :class:`Cache` among
        multiple :class:`Service` instances.
        """
        return self._cache

    @cache.setter
    def cache(self, v):
        self._cache = v

    @cache.deleter
    def cache(self):
        self._cache = Cache()

    @aioxmpp.service.depsignal(
        disco.DiscoServer,
        "on_info_changed")
    def _info_changed(self):
        self.logger.debug("info changed, scheduling re-calculation of version")
        asyncio.get_event_loop().call_soon(
            self.update_hash
        )

    @asyncio.coroutine
    def _shutdown(self):
        self.disco_server.unregister_feature(
            "http://jabber.org/protocol/caps"
        )
        if self.ver is not None:
            self.disco_server.unmount_node(
                self.NODE + "#" + self.ver
            )

    @asyncio.coroutine
    def query_and_cache(self, jid, node, ver, hash_, fut):
        data = yield from self.disco_client.query_info(
            jid,
            node=node+"#"+ver,
            require_fresh=True)

        try:
            expected = caps115.hash_query(data, hash_.replace("-", ""))
        except ValueError as exc:
            fut.set_exception(exc)
        else:
            if expected == ver:
                self.cache.add_cache_entry(hash_, node+"#"+ver, data)
                fut.set_result(data)
            else:
                fut.set_exception(ValueError("hash mismatch"))

        return data

    @asyncio.coroutine
    def lookup_info(self, jid, node, ver, hash_):
        try:
            info = yield from self.cache.lookup(hash_, node+"#"+ver)
        except KeyError:
            pass
        else:
            self.logger.debug("found ver=%r in cache", ver)
            return info

        self.logger.debug("have to query for ver=%r", ver)
        fut = self.cache.create_query_future(hash_, node+"#"+ver)
        info = yield from self.query_and_cache(
            jid, node, ver, hash_,
            fut
        )
        self.logger.debug("ver=%r maps to %r", ver, info)

        return info

    @aioxmpp.service.outbound_presence_filter
    def handle_outbound_presence(self, presence):
        if (self.ver is not None and
                presence.type_ == aioxmpp.structs.PresenceType.AVAILABLE):
            self.logger.debug("injecting capabilities into outbound presence")
            presence.xep0115_caps = my_xso.Caps115(
                self.NODE,
                self.ver,
                "sha-1",
            )

        return presence

    @aioxmpp.service.inbound_presence_filter
    def handle_inbound_presence(self, presence):
        caps = presence.xep0115_caps

        if caps is not None and caps.hash_ is not None:
            self.logger.debug(
                "inbound presence with ver=%r and hash=%r from %s",
                caps.ver, caps.hash_,
                presence.from_)
            task = asyncio.async(
                self.lookup_info(presence.from_,
                                 caps.node,
                                 caps.ver,
                                 caps.hash_)
            )
            self.disco_client.set_info_future(presence.from_, None, task)

        return presence

    def update_hash(self):
        identities = []
        for category, type_, lang, name in self.disco_server.iter_identities():
            identity = disco.xso.Identity(category=category,
                                          type_=type_)
            if lang is not None:
                identity.lang = lang
            if name is not None:
                identity.name = name
            identities.append(identity)

        info = disco.xso.InfoQuery(
            identities=identities,
            features=self.disco_server.iter_features(),
        )

        new_ver = caps115.hash_query(
            info,
            "sha1",
        )

        self.logger.debug("new ver=%r (features=%r)", new_ver, info.features)

        if self.ver != new_ver:
            if self.ver is not None:
                self.disco_server.unmount_node(self.NODE + "#" + self.ver)
            self.ver = new_ver
            self.disco_server.mount_node(self.NODE + "#" + self.ver,
                                         self.disco_server)
            self.on_ver_changed()


def writeback(base_path, hash_, node, captured_events):
    quoted = urllib.parse.quote(node, safe="")
    dest_path = base_path / "{}_{}.xml".format(hash_, quoted)
    with tempfile.NamedTemporaryFile(dir=str(base_path), delete=False) as tmpf:
        try:
            generator = aioxmpp.xml.XMPPXMLGenerator(
                tmpf,
                short_empty_elements=True)
            generator.startDocument()
            aioxmpp.xso.events_to_sax(captured_events, generator)
            generator.endDocument()
        except:
            os.unlink(tmpf.name)
            raise
        os.replace(tmpf.name, str(dest_path))
