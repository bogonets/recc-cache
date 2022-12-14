# -*- coding: utf-8 -*-

from asyncio import Task, TimeoutError, create_task
from inspect import iscoroutinefunction, isfunction
from typing import Any, Awaitable, Callable, Dict, Final, List, Optional, Set, Union

from aioredis import ConnectionPool, Redis
from aioredis.client import PubSub
from async_timeout import timeout as async_timeout_timeout

DEFAULT_HOST: Final[str] = "localhost"
DEFAULT_PORT: Final[int] = 6379
DEFAULT_PW: Final[str] = ""
DEFAULT_PREFIX: Final[str] = "cache:"

DEFAULT_MAX_CONNECTIONS: Final[int] = 10
EX_KEY_MAX_CONNECTIONS: Final[str] = "max_connections"

# https://redis.io/commands/expire#expire-accuracy
EXPIRE_ACCURACY_SECONDS: Final[int] = 1
CLEAR_SCAN_STEP: Final[int] = 1024

ValueType = Union[bytes, memoryview, str, int, float]
SubscribeAsyncCallable = Callable[[Any], Awaitable[None]]
SubscribeSyncCallable = Callable[[Any], None]
SubscribeCallable = Union[SubscribeAsyncCallable, SubscribeSyncCallable]


class RedisCache:
    """
    Redis cache store class.
    """

    def __init__(
        self,
        host: str,
        port: int,
        pw: Optional[str] = None,
        prefix: Optional[str] = None,
        **kwargs,
    ):
        self._host = host
        self._port = port
        self._pw = pw
        self._prefix = prefix if prefix else str()
        self._kwargs = kwargs
        self._redis: Optional[Redis] = None

        # TODO: Test validation of `self._prefix`

        self._subscribes: Dict[str, Task] = dict()
        self._subscribes_tick = 1.0
        self._subscribes_exit: Set[str] = set()
        self._subscribes_close_timeout = 5.0

    def _get_max_connections(self, default_value=DEFAULT_MAX_CONNECTIONS) -> int:
        try:
            return int(self._kwargs.get(EX_KEY_MAX_CONNECTIONS, default_value))
        except ValueError:
            return default_value

    def is_open(self) -> bool:
        return self._redis is not None

    async def open(self) -> None:
        pool = ConnectionPool.from_url(
            f"redis://{self._host}:{self._port}",
            password=self._pw if self._pw else None,
            max_connections=self._get_max_connections(),
        )
        self._redis = Redis(connection_pool=pool)

    async def close_subscribes(self) -> None:
        try:
            for channel in self._subscribes.keys():
                self._subscribes_exit.add(channel)
            async with async_timeout_timeout(self._subscribes_close_timeout):
                for task in self._subscribes.values():
                    await task
        except TimeoutError:
            for task in self._subscribes.values():
                task.cancel()
        finally:
            self._subscribes.clear()
            self._subscribes_exit.clear()

    async def close(self) -> None:
        assert self._redis
        await self.close_subscribes()
        assert len(self._subscribes) == 0
        await self._redis.close()
        self._redis = None

    @property
    def redis(self) -> Redis:
        assert self._redis is not None
        return self._redis

    async def set(self, key: str, val: ValueType) -> None:
        await self.redis.set(self._prefix + key, val)

    async def mset(self, pairs: Dict[str, ValueType]) -> None:
        await self.redis.mset({self._prefix + k: v for k, v in pairs.items()})

    async def get(self, key: str) -> Optional[bytes]:
        return await self.redis.get(self._prefix + key)

    async def append(self, key: str, val: ValueType) -> None:
        await self.redis.append(self._prefix + key, val)

    async def expire(self, key: str, seconds: int) -> None:
        await self.redis.expire(self._prefix + key, seconds)

    async def delete(self, *keys: str) -> None:
        real_keys = (self._prefix + k for k in keys)
        await self.redis.delete(*real_keys)

    async def exists(self, *keys: str) -> int:
        real_keys = (self._prefix + k for k in keys)
        return await self.redis.exists(*real_keys)

    async def clear(self) -> None:
        while True:
            cursor, keys = await self.redis.scan(
                cursor=0,
                match=f"{self._prefix}*",
                count=CLEAR_SCAN_STEP,
            )

            if keys:
                await self.redis.delete(*keys)
            else:
                break

    async def _subscribe_task(
        self,
        key: str,
        channel: PubSub,
        callback: SubscribeCallable,
        flush_remain_messages=True,
    ) -> None:
        while True:
            message = await channel.get_message(
                ignore_subscribe_messages=True,
                timeout=self._subscribes_tick,
            )
            if message is not None:
                if iscoroutinefunction(callback):
                    await callback(message)  # type: ignore[misc]
                elif isfunction(callback):
                    callback(message)
                else:
                    raise NotImplementedError

            if key in self._subscribes_exit:
                self._subscribes_exit.remove(key)
                break

        while flush_remain_messages:
            message = await channel.get_message(ignore_subscribe_messages=True)
            if message is not None:
                if iscoroutinefunction(callback):
                    await callback(message)  # type: ignore[misc]
                elif isfunction(callback):
                    callback(message)
                else:
                    raise NotImplementedError
            else:
                break

    def get_subscribe_channels(self) -> List[str]:
        return list(self._subscribes.keys())

    def get_subscribe_task(self, channel: str) -> Task:
        return self._subscribes[channel]

    def cancel_subscribe_task(self, channel: str) -> bool:
        return self._subscribes[channel].cancel()

    def exit_flag_subscribe(self, channel: str) -> None:
        self._subscribes_exit.add(channel)

    def exit_flag_subscribes(self) -> None:
        for channel in self._subscribes.keys():
            self._subscribes_exit.add(channel)

    async def wait_subscribe(self, channel: str) -> None:
        await self._subscribes[channel]

    async def subscribe(self, channel: str, callback: SubscribeCallable) -> None:
        pub_sub = self.redis.pubsub()
        await pub_sub.subscribe(channel)
        self._subscribes[channel] = create_task(
            self._subscribe_task(channel, pub_sub, callback)
        )

    async def publish(self, channel: str, value: bytes) -> None:
        await self.redis.publish(channel, value)
