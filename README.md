Asyncio Service Boilerplate
===========================

This module provides a foundation for building microservices using Python's `asyncio` library. Key features include:

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
| OpenTelemetry logging   | opentelemetry-sdk, opentelemetry-exporter-otlp-proto-grpc     |

To install with OpenTelemetry support:

```bash
pip install aiobp[otel]
```

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

OpenTelemetry Logging
---------------------

aiobp supports exporting logs to OpenTelemetry collectors (SigNoz, Jaeger, etc.).

### Configuration

Add OTEL settings to your `LoggingConfig`:

```ini
[log]
level = DEBUG
filename = service.log
otel_endpoint = http://localhost:4317
otel_export_interval = 5
```

| Option               | Default | Description                                      |
|----------------------|---------|--------------------------------------------------|
| otel_endpoint        | None    | OTLP gRPC endpoint (e.g. http://localhost:4317)  |
| otel_export_interval | 5       | Export interval in seconds (0 = instant export)  |

### Usage

```python
from dataclasses import dataclass
from aiobp.logging import LoggingConfig, setup_logging, log

@dataclass
class Config:
    log: LoggingConfig = None

# ... load config ...

setup_logging("my-service-name", config.log)
log.info("This message goes to console, file, and OTEL collector")
```

### Resource Attributes

To add custom resource attributes (like location, environment, etc.), set the standard OTEL environment variable before calling `setup_logging`:

```python
import os

os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "location=datacenter1,environment=production"
setup_logging("my-service-name", config.log)
```

### Graceful Fallback

If `otel_endpoint` is configured but OpenTelemetry packages are not installed, a warning is logged and the application continues with console/file logging only.


More complex example
--------------------

```python
import asyncio
import aiohttp
import sys
from dataclasses import dataclass

from aiobp import create_task, on_shutdown, runner
from aiobp.config import InvalidConfigFile, sys_argv_or_filenames
from aiobp.config.conf import loader
from aiobp.logging import LoggingConfig, add_devel_log_level, log, setup_logging


@dataclass
class WorkerConfig:
    """Your microservice worker configuration"""

    sleep: int = 5


@dataclass
class Config:
    """Put configurations together"""

    worker: WorkerConfig = None
    log: LoggingConfig = None


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
    log.info("my-service-name", "Using config file: %s", config_filename)

    runner(service(config))


if __name__ == '__main__':
    main()
```
