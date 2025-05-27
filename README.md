Asyncio Service Boilerplate
===========================

This module provides a foundation for building microservices using Python’s `asyncio` library. Key features include:

  * A runner with graceful shutdown
  * A task reference management
  * A flexible configuration provider
  * A logger with colorized output

No dependencies are enforced by default, so you only install what you need.
For basic usage, no additional Python modules are required.
The table below summarizes which optional dependencies to install based on the features you want to use:

|     aiobp Feature       | Required Module(s) |
|-------------------------|--------------------|
| config (.conf or .json) | msgspec            |
| config (.yaml)          | msgspec, pyyaml    |


Basic example
-------------

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
import aiohttp
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
        config = loader(Config, config_filename)
    except InvalidConfigFile as error:
        print(f'Invalid configuration: {error}')
        sys.exit(1)

    setup_logging(config.log)
    log.info("Using config file: %s", config_filename)

    runner(service(config))


if __name__ == '__main__':
    main()
```
