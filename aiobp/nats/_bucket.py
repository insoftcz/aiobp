"""JetStream key-value bucket with typed keys and values"""

from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import msgspec
from nats.errors import ConnectionClosedError
from nats.js import errors as js_errors
from nats.js.kv import KeyValue

from aiobp.nats._common import encoder

K = TypeVar("K")
V = TypeVar("V")


@dataclass
class BucketEntry(Generic[V]):
    """A bucket value together with its revision, for `Bucket.update()`'s optimistic concurrency check"""

    value: V
    revision: int | None


class Bucket(Generic[K, V]):
    """JetStream key-value bucket with JSON encoded values of type `V`, keyed by `K`

    The underlying nats-py `KeyValue` is available as `kv` for anything not covered here.
    """

    def __init__(self, kv: KeyValue, *, value_type: type[V] | None = None, key_type: type[K] | None = None) -> None:
        self.kv: KeyValue = kv
        self._decoder: msgspec.json.Decoder = msgspec.json.Decoder(type=value_type if value_type is not None else Any)
        self._key_type: type[K] = key_type if key_type is not None else str  # type: ignore[assignment]

    def _encode_key(self, key: K) -> str:
        # str.__str__, not str(key): a str subclass (e.g. a str Enum) can override __str__ to
        # return something other than its own value ("Room.LOBBY" instead of "lobby") — nats-py
        # builds the wire subject via string formatting, so that override would leak onto the wire.
        return str.__str__(key) if isinstance(key, str) else str(key)

    def _decode_key(self, key: str) -> K:
        return msgspec.convert(key, type=self._key_type, strict=False)

    async def _get(self, key: K) -> KeyValue.Entry | None:
        """Return raw entry of `key`, None when there is none or it was deleted"""
        try:
            entry = await self.kv.get(self._encode_key(key))
        except js_errors.KeyNotFoundError:
            return None
        return None if entry.value is None else entry

    async def get(self, key: K, default: V | None = None) -> V | None:
        """Return value stored under `key`, or `default` when there is none"""
        entry = await self._get(key)
        if entry is None or entry.value is None:
            return default
        return self._decoder.decode(entry.value)

    async def get_entry(self, key: K) -> BucketEntry[V] | None:
        """Return `key`'s value together with its revision, or None when there is none

        Use this instead of `get()` when you intend to `update()` the value
        afterwards — `update()` needs the revision to detect a concurrent change.
        """
        entry = await self._get(key)
        if entry is None or entry.value is None:
            return None
        return BucketEntry(value=self._decoder.decode(entry.value), revision=entry.revision)

    async def put(self, key: K, value: V) -> int:
        """Store `value` under `key` and return the new revision"""
        return await self.kv.put(self._encode_key(key), encoder.encode(value))

    async def update(self, key: K, entry: BucketEntry[V]) -> int:
        """Store `entry.value` under `key`, but only if it's still at `entry.revision`; return the new revision

        `entry` is normally the one you got from `get_entry()`, its `value`
        mutated in place — that's what couples the write to the read it's
        based on. Raises nats-py's `KeyWrongLastSequenceError` if `key` was
        changed by someone else since `entry.revision` was read — call
        `get_entry()` again and retry your change against the fresh value.
        """
        return await self.kv.update(self._encode_key(key), encoder.encode(entry.value), last=entry.revision)

    async def delete(self, key: K) -> None:
        """Remove `key` from the bucket"""
        await self.kv.delete(self._encode_key(key))

    async def keys(self) -> list[K]:
        """List keys in the bucket"""
        try:
            raw_keys = await self.kv.keys()
        except js_errors.NoKeysError:
            return []
        return [self._decode_key(key) for key in raw_keys]

    async def watch(
        self,
        keys: str = ">",
        *,
        include_history: bool = False,
    ) -> AsyncIterator[tuple[K, V | None, int]]:
        """Async-iterate over `(key, value, revision)` for every change matching `keys`

        `keys` is a raw NATS subject pattern (e.g. `"user.*"`), not a `K` — it
        may contain wildcards, which a single key never does. `value` is None
        when the change is a deletion. Runs until the caller stops iterating
        (e.g. its task is cancelled) or the server closes the watch
        subscription; either way, the underlying watcher is stopped properly
        before this returns.
        """
        watcher = await self.kv.watch(keys, include_history=include_history)
        try:
            async for entry in watcher:
                if entry is None:  # internal marker: history replay caught up, not a real change
                    continue
                # a deletion carries operation="DEL" with an *empty* (not None) value
                value = self._decoder.decode(entry.value) if entry.value else None
                yield self._decode_key(entry.key), value, entry.revision
        finally:
            # the runner may disconnect NATS before cancelling tasks, the subscription is gone with
            # the connection — raising here would replace CancelledError being propagated
            with suppress(ConnectionClosedError):
                await watcher.stop()
