"""Test config loaders"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import msgspec
import pytest

from aiobp.config.conf import loader as conf_loader
from aiobp.config.json import loader as json_loader
from aiobp.config.yaml import loader as yaml_loader

LOADERS = [conf_loader, json_loader, yaml_loader]


@dataclass
class Server:
    host: str = "10.20.30.40"
    port: int = 65432


@dataclass
class Client:
    enabled: bool = True
    names: list[str] = field(default_factory=list)
    ids: list[int] = field(default_factory=list)
    note: str = "Test"


@dataclass
class Config:
    server: Server = field(default_factory=Server)
    client: Client = field(default_factory=Client)
    users: dict[str, str] = field(default_factory=dict)


expected = Config(
    server=Server(
        host="127.0.0.1",
        port=12345,
    ),
    client=Client(
        enabled=False,
        names=["a", "b", "c"],
        ids=[1, 2, 3],
        note="Testing",
    ),
    users={
        "kenny": "Kenneth McCormick",
        "cartman": "Eric Cartman",
    },
)

defaults = Config(
    server=Server(
        host="10.20.30.40",
        port=65432,
    ),
    client=Client(
        enabled=True,
        names=[],
        ids=[],
        note="Test",
    ),
    users={},
)


def test_conf_loader() -> None:
    config = conf_loader(Config, "tests/config.conf")
    assert config == expected


def test_yaml_loader() -> None:
    config = yaml_loader(Config, "tests/config.yaml")
    assert config == expected


def test_json_loader() -> None:
    config = json_loader(Config, "tests/config.json")
    assert config == expected


def test_conf_defaults() -> None:
    config = conf_loader(Config)
    assert config == defaults


def test_json_defaults() -> None:
    config = json_loader(Config)
    assert config == defaults


def test_yaml_defaults() -> None:
    config = yaml_loader(Config)
    assert config == defaults


# Sections without defaults, like most services declare them


class Log(msgspec.Struct):
    level: str = "DEBUG"
    filename: str | None = None


class Limits(msgspec.Struct):
    size: int = 1024


class Root(msgspec.Struct):
    limits: Limits  # nested section without default
    allow: bool = False


class Required(msgspec.Struct):
    root: Root
    log: Log


required_defaults = Required(root=Root(allow=False, limits=Limits(size=1024)), log=Log(level="DEBUG", filename=None))


@pytest.mark.parametrize("loader", LOADERS)
def test_required_sections_without_filename(loader: Callable[..., Any]) -> None:
    assert loader(Required) == required_defaults


@pytest.mark.parametrize(
    ("loader", "content"),
    [
        (conf_loader, "[root]\nallow = true\n"),
        (json_loader, '{"root": {"allow": true}}'),
        (yaml_loader, "root:\n  allow: true\n"),
    ],
)
def test_missing_required_sections(loader: Callable[..., Any], content: str, tmp_path: Path) -> None:
    filename = tmp_path / "config"
    filename.write_text(content)
    config = loader(Required, filename)
    assert config == Required(root=Root(allow=True, limits=Limits()), log=Log())


def test_conf_missing_file(tmp_path: Path) -> None:
    assert conf_loader(Required, tmp_path / "missing.conf") == required_defaults


def test_yaml_empty_file(tmp_path: Path) -> None:
    filename = tmp_path / "config.yaml"
    filename.touch()
    assert yaml_loader(Required, filename) == required_defaults


@dataclass
class FactoryConfig:
    log: Log = field(default_factory=lambda: Log(level="INFO"))


@pytest.mark.parametrize("loader", LOADERS)
def test_section_default_factory(loader: Callable[..., Any]) -> None:
    assert loader(FactoryConfig).log == Log(level="INFO")


def test_conf_section_default_factory_with_file(tmp_path: Path) -> None:
    filename = tmp_path / "config.conf"
    filename.write_text("[other]\nkey = value\n")
    assert conf_loader(FactoryConfig, filename).log == Log(level="INFO")


@pytest.mark.parametrize(
    ("loader", "filename"),
    [(conf_loader, "tests/config.conf"), (json_loader, "tests/config.json"), (yaml_loader, "tests/config.yaml")],
)
def test_path(loader: Callable[..., Any], filename: str) -> None:
    assert loader(Config, Path(filename)) == expected
