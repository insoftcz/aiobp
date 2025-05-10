"""Test config loaders"""

import unittest
from dataclasses import dataclass, field

from aiobp.config.conf import loader as conf_loader
from aiobp.config.yaml import loader as json_loader
from aiobp.config.yaml import loader as yaml_loader


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

class TestConfigLoaders(unittest.TestCase):
    def test_conf_loader(self) -> None:
        config = conf_loader(Config, "tests/config.conf")
        assert config == expected

    def test_yaml_loader(self) -> None:
        config = yaml_loader(Config, "tests/config.yaml")
        assert config == expected

    def test_json_loader(self) -> None:
        config = json_loader(Config, "tests/config.json")
        assert config == expected

    def test_conf_defaults(self) -> None:
        config = conf_loader(Config)
        assert config == defaults

    def test_json_defaults(self) -> None:
        config = json_loader(Config)
        assert config == defaults

    def test_yaml_defaults(self) -> None:
        config = yaml_loader(Config)
        assert config == defaults

if __name__ == "__main__":
    unittest.main()
