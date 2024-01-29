Boilerplate for asyncio service
===============================

This module provides boilerplate for microservices written in asyncio:

  * Runner with task reference handler and graceful shutdown
  * Configuration provider
  * Logger with color support

```python
import asyncio

from aiobp import runner

async def main():
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        print('Saving data...')

runner(main())
```


More complex example
--------------------

```python
import asyncio
import aiohttp  # just for example
import sys

from aiobp import create_task, on_shutdown, runner
from aiobp.config import InvalidConfigFile, sys_argv_or_filenames
from aiobp.config.conf import loader
from aiobp.logging import LoggingConfig, add_devel_log_level, log, setup_logging


class WorkerConfig:
    """Your microservice worker configuration"""

    sleep: int = 5


class Config:
    """Put configurations together"""

    worker: WorkerConfig
    log: LoggingConfig


async def worker(config: WorkerConfig, client_session: aiohttp.ClientSession) -> int:
    """Perform service work"""
    attempts = 0
    try:
        async with client_session.get('http://python.org') as resp:
            assert resp.status == 200
            log.debug('Page length %d', len(await resp.text()))
            attempts += 1
        await asyncio.sleep(config.sleep)
    except asyncio.CancelledError:
        log.info('Doing some shutdown work')
        await client_session.post('http://localhost/service/attempts', data={'attempts': attempts})

    return attempts


async def service(config: Config):
    """Your microservice"""
    client_session = aiohttp.ClientSession()
    on_shutdown(client_session.close, after_tasks_cancel=True)

    create_task(worker(config.worker, client_session), 'PythonFetcher')

    # you can do some monitoring, statistics collection, etc.
    # or just let the method finish and the runner will wait for Ctrl+C or kill


def main():
    """Example microservice"""
    add_devel_log_level()
    try:
        config_filename = sys_argv_or_filenames('service.local.conf', 'service.conf')
        config = loader(config_filename, Config)
    except InvalidConfigFile as error:
        print(f'Invalid configuration: {error}')
        sys.exit(1)

    setup_logging(config.log)
    log.info("Using config file: %s", config_filename)

    runner(service(config))


if __name__ == '__main__':
    main()
```
