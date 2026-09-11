import asyncio

import pytest

from app.connectors import json_path, read_until_patterns, read_until_regex, render, telnet_command, validate_target
from app.schemas import Operation, TelnetConfig


class ChunkReader:
    def __init__(self, chunks: list[str], block_after: bool = False):
        self.chunks = iter(chunks)
        self.block_after = block_after

    async def read(self, _: int) -> str:
        try:
            return next(self.chunks)
        except StopIteration:
            if self.block_after:
                await asyncio.Event().wait()
            return ""


def test_target_validation():
    assert validate_target("8.8.8.8", Operation.ping) == "8.8.8.8"
    assert validate_target("2001:db8::/32", Operation.bgp) == "2001:db8::/32"


def test_template_rendering():
    value = {"address": "{target}", "kind": "{operation}"}
    assert render(value, "1.1.1.1", Operation.traceroute) == {
        "address": "1.1.1.1",
        "kind": "traceroute",
    }


def test_json_path():
    assert json_path({"data": {"results": ["ok"]}}, "data.results.0") == "ok"


def test_read_until_regex_waits_for_stable_prompt():
    reader = ChunkReader(["route *>  ", "1.1.1.0/24\r\nrouter> "], block_after=True)

    output = asyncio.run(read_until_regex(reader, r"[>#]\s*$", 1))

    assert output.endswith("router> ")


def test_read_until_patterns_accepts_final_prompt_before_login():
    reader = ChunkReader(["Looking Glass\r\nrouter> "], block_after=True)

    output, matched = asyncio.run(
        read_until_patterns(
            reader,
            [r"(?i)(login|username)[: ]*$", r"[>#]\s*$"],
            1,
            "o prompt de usuário ou o prompt final",
        )
    )

    assert output.endswith("router> ")
    assert matched == 1


def test_read_until_regex_has_an_informative_timeout():
    reader = ChunkReader([], block_after=True)

    with pytest.raises(TimeoutError, match="Tempo limite de 0.01s aguardando o prompt final"):
        asyncio.run(
            read_until_patterns(
                reader,
                [r"[>#]\s*$"],
                0.01,
                "o prompt final",
                settle_seconds=0.001,
            )
        )


def test_telnet_uses_specific_ipv6_command_when_configured():
    config = TelnetConfig(
        host="lg.example.net",
        commands={Operation.bgp: "show bgp ipv4 {target}"},
        commands_v6={Operation.bgp: "show bgp ipv6 {target}"},
    )

    assert telnet_command(config, Operation.bgp, "192.0.2.0/24") == "show bgp ipv4 {target}"
    assert telnet_command(config, Operation.bgp, "2001:db8::/32") == "show bgp ipv6 {target}"


def test_telnet_ipv6_falls_back_to_default_command():
    config = TelnetConfig(
        host="lg.example.net",
        commands={Operation.traceroute: "traceroute {target}"},
    )

    assert telnet_command(config, Operation.traceroute, "2001:db8::1") == "traceroute {target}"
