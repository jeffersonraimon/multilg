import asyncio
import json

import httpx
import pytest

from app import connectors
from app.connectors import discover_hyperglass, execute_hyperglass, json_path, read_until_patterns, read_until_regex, render, telnet_command, validate_target
from app.schemas import HyperglassConfig, LookingGlassInput, Operation, Protocol, TelnetConfig


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


class DelayedChunkReader(ChunkReader):
    def __init__(self, chunks: list[str], delay: float):
        super().__init__(chunks, block_after=True)
        self.delay = delay

    async def read(self, size: int) -> str:
        await asyncio.sleep(self.delay)
        return await super().read(size)


def test_target_validation():
    assert validate_target("8.8.8.8", Operation.ping) == "8.8.8.8"
    assert validate_target("2001:db8::/32", Operation.bgp) == "2001:db8::/32"
    assert validate_target("7195:55000", Operation.bgp_community) == "7195:55000"
    assert validate_target("_13335$", Operation.bgp_aspath) == "_13335$"


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


def test_read_timeout_resets_while_output_keeps_arriving():
    reader = DelayedChunkReader(["hop 1\n", "hop 2\n", "router> "], 0.03)
    updates: list[str] = []

    async def run():
        async def record(output: str):
            updates.append(output)

        return await read_until_regex(
            reader,
            r"[>#]\s*$",
            0.05,
            "o término do traceroute",
            on_update=record,
        )

    output = asyncio.run(run())

    assert output.endswith("router> ")
    assert updates == ["hop 1\n", "hop 1\nhop 2\n", "hop 1\nhop 2\nrouter> "]


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


def test_hyperglass_schema_normalizes_api_url():
    config = HyperglassConfig(
        base_url="http://lg.example.net/api/",
        location=" router-01 ",
        query_types={Operation.bgp: " __hyperglass_juniper_bgp_route_table__ "},
    )

    assert config.base_url == "http://lg.example.net"
    assert config.location == "router-01"
    assert config.query_types[Operation.bgp] == "__hyperglass_juniper_bgp_route_table__"


def test_looking_glass_accepts_hyperglass_http_config():
    item = LookingGlassInput(
        name="LG Hyperglass",
        protocol=Protocol.http,
        config={
            "hyperglass_like": True,
            "base_url": "https://lg.example.net",
            "location": "router-01",
            "query_types": {"ping": "__hyperglass_juniper_ping__"},
        },
    )

    assert item.config["hyperglass_like"] is True


def test_execute_hyperglass_posts_expected_payload(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"output": {"count": 1, "routes": [{"prefix": "8.8.4.0/24"}]}, "level": "success"},
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        connectors.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    output = asyncio.run(
        execute_hyperglass(
            {
                "hyperglass_like": True,
                "base_url": "http://lg.example.net/api/",
                "location": "router-01",
                "query_types": {"bgp": "__hyperglass_juniper_bgp_route_table__"},
            },
            Operation.bgp,
            "8.8.4.0/24",
        )
    )

    assert json.loads(requests[0].content) == {
        "queryLocation": "router-01",
        "queryType": "__hyperglass_juniper_bgp_route_table__",
        "queryTarget": ["8.8.4.0/24"],
    }
    assert json.loads(output)["routes"][0]["prefix"] == "8.8.4.0/24"


def test_execute_legacy_hyperglass_posts_v1_payload(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"output": {"count": 0, "routes": []}})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        connectors.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    asyncio.run(
        execute_hyperglass(
            {
                "hyperglass_like": True,
                "base_url": "https://legacy.example.net",
                "location": "sao_paulo",
                "query_types": {"bgp_aspath": "bgp_aspath"},
                "api_format": "v1",
                "vrf": "global",
            },
            Operation.bgp_aspath,
            "_13335$",
        )
    )

    assert requests[0].url.path == "/api/query/"
    assert json.loads(requests[0].content) == {
        "query_location": "sao_paulo",
        "query_type": "bgp_aspath",
        "query_target": "_13335$",
        "query_vrf": "global",
    }


def test_execute_hyperglass_bootstraps_cookie_session(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                text="<html></html>",
                headers={"Set-Cookie": "hg_access=1; Path=/api; HttpOnly; Secure"},
            )
        if "hg_access=1" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text="Forbidden", headers={"Content-Type": "text/html"})
        return httpx.Response(200, json={"output": "consulta concluída", "level": "success"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        connectors.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )

    output = asyncio.run(
        execute_hyperglass(
            {
                "hyperglass_like": True,
                "base_url": "https://protected.example.net",
                "location": "router-01",
                "query_types": {"ping": "Custom_Ping"},
                "bootstrap_session": True,
            },
            Operation.ping,
            "1.1.1.1",
        )
    )

    assert output == "consulta concluída"
    assert [request.method for request in requests] == ["GET", "POST"]
    assert "hg_access=1" in requests[1].headers["cookie"]


def test_execute_hyperglass_marks_router_cli_error(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"output": "  ^\n% Invalid input detected at '^' marker.\n", "level": "success"},
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        connectors.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )

    with pytest.raises(ValueError, match="roteador rejeitou o comando"):
        asyncio.run(
            execute_hyperglass(
                {
                    "hyperglass_like": True,
                    "base_url": "https://lg.example.net",
                    "location": "router-01",
                    "query_types": {"bgp_community": "Custom_Community"},
                },
                Operation.bgp_community,
                "64500:100",
            )
        )


def test_discover_hyperglass_reads_devices_and_queries(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/devices/":
            return httpx.Response(200, json=[{"id": "rta-01", "name": "RTA-01", "group": None}])
        if request.url.path == "/api/queries/":
            return httpx.Response(200, json=["Ping", "Traceroute", "BGP Route"])
        if request.url.path == "/":
            return httpx.Response(
                200,
                text='<script src="/_next/static/chunks/pages/index-main.js"></script>',
            )
        if request.url.path.endswith("/pages/index-main.js"):
            return httpx.Response(
                200,
                text=(
                    '{"version":"2.0.4","devices":[{"group":null,"locations":['
                    '{"id":"rta-01","name":"RTA-01","directives":['
                    '{"id":"Custom_Ping","name":"Ping","groups":[]},'
                    '{"id":"Custom_Trace","name":"Traceroute","groups":[]},'
                    '{"id":"Custom_Community","name":"BGP Community","groups":[]},'
                    '{"id":"Custom_ASPath","name":"BGP AS Path","groups":[]},'
                    '{"id":"Custom_BGP","name":"BGP Route","groups":[]}]}]}]}'
                ),
            )
        return httpx.Response(404)

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        connectors.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    result = asyncio.run(discover_hyperglass({"base_url": "https://lg.example.net"}))

    assert result["devices"][0]["id"] == "rta-01"
    assert result["devices"][0]["query_types"] == {
        "ping": "Custom_Ping",
        "traceroute": "Custom_Trace",
        "bgp_community": "Custom_Community",
        "bgp_aspath": "Custom_ASPath",
        "bgp": "Custom_BGP",
    }
    assert result["queries"] == [
        "Ping",
        "Traceroute",
        "BGP Community",
        "BGP AS Path",
        "BGP Route",
    ]


def test_discover_legacy_hyperglass_from_next_data(monkeypatch):
    legacy_config = {
        "props": {
            "appProps": {
                "config": {
                    "hyperglass_version": "1.0.4",
                    "queries": {
                        "list": [
                            {"name": "bgp_route", "display_name": "BGP Route", "enable": True},
                            {"name": "bgp_community", "display_name": "BGP Community", "enable": True},
                            {"name": "bgp_aspath", "display_name": "BGP AS Path", "enable": True},
                            {"name": "ping", "display_name": "Ping", "enable": True},
                        ]
                    },
                    "networks": [
                        {
                            "display_name": "AS64500",
                            "locations": [
                                {
                                    "_id": "sao_paulo",
                                    "name": "São Paulo",
                                    "vrfs": [
                                        {"_id": "global", "default": True, "ipv4": True, "ipv6": True}
                                    ],
                                }
                            ],
                        }
                    ],
                }
            }
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(
                200,
                text=(
                    '<script id="__NEXT_DATA__" type="application/json">'
                    + json.dumps(legacy_config)
                    + "</script>"
                ),
            )
        return httpx.Response(404)

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        connectors.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )

    result = asyncio.run(discover_hyperglass({"base_url": "https://legacy.example.net"}))

    assert result["api_format"] == "v1"
    assert result["version"] == "1.0.4"
    assert result["devices"][0]["vrf"] == "global"
    assert result["devices"][0]["query_types"]["bgp_community"] == "bgp_community"
    assert result["devices"][0]["query_types"]["bgp_aspath"] == "bgp_aspath"


def test_execute_hyperglass_recovers_from_custom_query_type(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        if payload["queryType"] == "__hyperglass_juniper_ping__":
            return httpx.Response(
                200,
                json={
                    "output": "ValueError: Query Type '__hyperglass_juniper_ping__' not found.",
                    "level": "danger",
                },
            )
        return httpx.Response(200, json={"output": "PING concluído", "level": "success"})

    async def fake_discovery(_config):
        return {
            "devices": [
                {
                    "id": "router-custom",
                    "query_types": {
                        "ping": "Juniper_Ping_RT1",
                        "traceroute": "Juniper_Traceroute_RT1",
                    },
                }
            ]
        }

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        connectors.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    monkeypatch.setattr(connectors, "discover_hyperglass", fake_discovery)
    connectors.HYPERGLASS_QUERY_TYPE_CACHE.clear()

    output = asyncio.run(
        execute_hyperglass(
            {
                "hyperglass_like": True,
                "base_url": "https://custom.example.net",
                "location": "router-custom",
                "query_types": {"ping": "__hyperglass_juniper_ping__"},
            },
            Operation.ping,
            "1.1.1.1",
        )
    )

    assert output == "PING concluído"
    assert [json.loads(request.content)["queryType"] for request in requests] == [
        "__hyperglass_juniper_ping__",
        "Juniper_Ping_RT1",
    ]
    connectors.HYPERGLASS_QUERY_TYPE_CACHE.clear()
