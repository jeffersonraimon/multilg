import asyncio
import html
import ipaddress
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin

import httpx
import telnetlib3

from .schemas import (
    HttpConfig,
    HyperglassConfig,
    HyperglassDiscoveryInput,
    Operation,
    Protocol,
    QueryResult,
    ResultStatus,
    TelnetConfig,
)

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CLI_ERROR_RE = re.compile(
    r"(?im)^\s*%\s*(invalid input|unknown command|unrecognized command|incomplete command)"
)
PROMPT_SETTLE_SECONDS = 0.1
HYPERGLASS_QUERY_TYPE_CACHE: dict[tuple[str, str, Operation], str] = {}


def validate_target(target: str, operation: Operation) -> str:
    value = target.strip()
    if operation in (Operation.bgp_community, Operation.bgp_aspath):
        if not value:
            raise ValueError("Informe o valor da consulta BGP")
        if any(ord(character) < 32 for character in value):
            raise ValueError("A consulta BGP não pode conter caracteres de controle")
        return value
    try:
        parsed = ipaddress.ip_network(value, strict=False) if "/" in value else ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("Informe um endereço IP ou prefixo CIDR válido") from exc
    if operation != Operation.bgp and isinstance(parsed, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
        raise ValueError("Ping e traceroute aceitam um endereço IP, não um prefixo")
    return str(parsed)


def render(value: Any, target: str, operation: Operation) -> Any:
    if isinstance(value, str):
        return value.replace("{target}", target).replace("{operation}", operation.value)
    if isinstance(value, dict):
        return {key: render(item, target, operation) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, target, operation) for item in value]
    return value


def json_path(data: Any, path: str | None) -> Any:
    if not path:
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(part)
    return current


async def execute_http(config_data: dict, operation: Operation, target: str) -> str:
    config = HttpConfig.model_validate(config_data)
    request_config = config.requests.get(operation)
    if not request_config:
        raise NotImplementedError(f"{operation.value} não está configurado neste LG")
    url = render(request_config.url, target, operation)
    query = render(request_config.query, target, operation)
    headers = render(request_config.headers, target, operation)
    body = render(request_config.body, target, operation)
    async with httpx.AsyncClient(
        timeout=config.timeout,
        verify=config.verify_tls,
        follow_redirects=True,
        trust_env=False,
        headers={"User-Agent": "MultiLG/1.0", **headers},
    ) as client:
        kwargs: dict[str, Any] = {"params": query}
        if request_config.method == "POST":
            if request_config.body_type == "json":
                kwargs["json"] = body
            elif request_config.body_type == "form":
                kwargs["data"] = body
            elif body is not None:
                kwargs["content"] = body
        response = await client.request(request_config.method, url, **kwargs)
        response.raise_for_status()
        if request_config.response_type == "json":
            extracted = json_path(response.json(), request_config.json_path)
            output = extracted if isinstance(extracted, str) else json.dumps(extracted, indent=2, ensure_ascii=False)
        else:
            output = response.text
        if request_config.output_regex:
            match = re.search(request_config.output_regex, output, re.DOTALL)
            if not match:
                raise ValueError("A expressão de extração não encontrou conteúdo na resposta")
            output = match.group(1) if match.lastindex else match.group(0)
        return output.strip()


def _hyperglass_error(response: httpx.Response, data: Any | None = None) -> str:
    if isinstance(data, dict):
        detail = data.get("output") or data.get("detail") or data.get("message")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    text = response.text.strip()
    return text or f"Erro HTTP {response.status_code}"


def _balanced_array(value: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(value)):
        character = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return value[start : index + 1]
    return None


def _extract_device_query_types(bundle_text: str, device_id: str) -> dict[str, str]:
    marker = f'"id":"{device_id}"'
    marker_position = bundle_text.find(marker)
    while marker_position >= 0:
        directives_position = bundle_text.find(
            '"directives":[', marker_position, marker_position + 12000
        )
        if directives_position >= 0:
            array_start = directives_position + len('"directives":')
            directives_array = _balanced_array(bundle_text, array_start)
            if directives_array:
                query_types: dict[str, str] = {}
                for match in re.finditer(
                    r'\{"id":"(?P<id>[^"\\]+)","name":"(?P<name>[^"\\]+)"',
                    directives_array,
                ):
                    query_type = match.group("id")
                    name = match.group("name").lower()
                    if "ping" in name:
                        query_types[Operation.ping.value] = query_type
                    elif "trace" in name:
                        query_types[Operation.traceroute.value] = query_type
                    elif "communit" in name:
                        query_types[Operation.bgp_community.value] = query_type
                    elif "as path" in name or "aspath" in name:
                        query_types[Operation.bgp_aspath.value] = query_type
                    elif "bgp" in name or "route" in name:
                        query_types[Operation.bgp.value] = query_type
                if query_types:
                    return query_types
        marker_position = bundle_text.find(marker, marker_position + len(marker))
    return {}


def _query_operation(name: str, query_id: str = "") -> Operation | None:
    normalized = f"{name} {query_id}".lower().replace("-", "_")
    if "ping" in normalized:
        return Operation.ping
    if "trace" in normalized:
        return Operation.traceroute
    if "communit" in normalized:
        return Operation.bgp_community
    if "as path" in normalized or "aspath" in normalized or "as_path" in normalized:
        return Operation.bgp_aspath
    if "bgp" in normalized or "route" in normalized:
        return Operation.bgp
    return None


def _hyperglass_v1_metadata(page_text: str) -> dict[str, Any] | None:
    match = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(?P<data>.*?)</script>',
        page_text,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        next_data = json.loads(match.group("data"))
        config = next_data["props"]["appProps"]["config"]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(config, dict) or not isinstance(config.get("networks"), list):
        return None

    query_types: dict[str, str] = {}
    query_labels: list[str] = []
    raw_queries = config.get("queries", {}).get("list", [])
    for query in raw_queries if isinstance(raw_queries, list) else []:
        if not isinstance(query, dict) or query.get("enable") is False:
            continue
        query_id = str(query.get("name") or "")
        label = str(query.get("display_name") or query_id)
        operation = _query_operation(label, query_id)
        if operation and query_id:
            query_types[operation.value] = query_id
            query_labels.append(label)

    devices: list[dict[str, Any]] = []
    for network in config["networks"]:
        if not isinstance(network, dict):
            continue
        group = network.get("display_name")
        for location in network.get("locations", []):
            if not isinstance(location, dict) or not location.get("_id"):
                continue
            vrfs = location.get("vrfs", [])
            selected_vrf = next(
                (
                    str(vrf["_id"])
                    for vrf in vrfs
                    if isinstance(vrf, dict) and vrf.get("_id") and vrf.get("default")
                ),
                None,
            )
            if selected_vrf is None:
                selected_vrf = next(
                    (
                        str(vrf["_id"])
                        for vrf in vrfs
                        if isinstance(vrf, dict) and vrf.get("_id")
                    ),
                    "default",
                )
            devices.append(
                {
                    "id": str(location["_id"]),
                    "name": str(location.get("name") or location["_id"]),
                    "group": group,
                    "query_types": dict(query_types),
                    "vrf": selected_vrf,
                }
            )
    if not devices:
        return None
    return {
        "devices": devices,
        "queries": query_labels,
        "api_format": "v1",
        "version": str(config.get("hyperglass_version") or "1.x"),
        "request_timeout": config.get("request_timeout"),
    }


def _hyperglass_v2_metadata(bundle_text: str) -> dict[str, Any] | None:
    search_position = 0
    while True:
        devices_position = bundle_text.find('"devices":[', search_position)
        if devices_position < 0:
            return None
        array_start = devices_position + len('"devices":')
        devices_array = _balanced_array(bundle_text, array_start)
        search_position = devices_position + len('"devices":')
        if not devices_array:
            continue
        try:
            json_compatible_array = re.sub(
                r"\\x([0-9a-fA-F]{2})",
                lambda match: "\\u00" + match.group(1),
                devices_array,
            )
            raw_groups = json.loads(json_compatible_array)
        except ValueError:
            continue
        if not isinstance(raw_groups, list):
            continue

        devices: list[dict[str, Any]] = []
        query_labels: list[str] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                continue
            group = raw_group.get("group") or raw_group.get("name")
            locations = raw_group.get("locations", [])
            for location in locations if isinstance(locations, list) else []:
                if not isinstance(location, dict) or not location.get("id"):
                    continue
                query_types: dict[str, str] = {}
                directives = location.get("directives", [])
                for directive in directives if isinstance(directives, list) else []:
                    if not isinstance(directive, dict) or not directive.get("id"):
                        continue
                    label = str(directive.get("name") or directive["id"])
                    operation = _query_operation(label, str(directive["id"]))
                    if operation:
                        query_types[operation.value] = str(directive["id"])
                        if label not in query_labels:
                            query_labels.append(label)
                if query_types:
                    devices.append(
                        {
                            "id": str(location["id"]),
                            "name": str(location.get("name") or location["id"]),
                            "group": location.get("group") or group,
                            "query_types": query_types,
                            "vrf": "default",
                        }
                    )
        if devices:
            version_match = re.search(r'"version":"([^"]+)"', bundle_text)
            timeout_match = re.search(r'"requestTimeout":(\d+)', bundle_text)
            return {
                "devices": devices,
                "queries": query_labels,
                "api_format": "v2",
                "version": version_match.group(1) if version_match else "2.x",
                "request_timeout": int(timeout_match.group(1)) if timeout_match else None,
            }


async def _hyperglass_frontend_metadata(
    client: httpx.AsyncClient, base_url: str
) -> dict[str, Any] | None:
    try:
        root_response = await client.get(base_url + "/", headers={"Accept": "text/html"})
        if not root_response.is_success:
            return None
        if legacy_metadata := _hyperglass_v1_metadata(root_response.text):
            legacy_metadata["bootstrap_session"] = "set-cookie" in root_response.headers
            return legacy_metadata
        script_paths = re.findall(r'<script[^>]+src="([^"]+\.js)"', root_response.text)
        script_urls = [
            urljoin(base_url + "/", html.unescape(path))
            for path in script_paths
            if "webpack-" in path or "/pages/_app-" in path or "/pages/index-" in path
        ]
        script_results = await asyncio.gather(
            *(client.get(url, headers={"Accept": "text/javascript"}) for url in script_urls),
            return_exceptions=True,
        )
        bundles: list[str] = []
        runtime_text = ""
        runtime_url = ""
        for url, response in zip(script_urls, script_results):
            if isinstance(response, httpx.Response) and response.is_success:
                bundles.append(response.text)
                if "webpack-" in url:
                    runtime_text = response.text
                    runtime_url = url

        dynamic_ids = {
            match
            for bundle in bundles
            for match in re.findall(r"\.e\((\d+)\)", bundle)
        }
        if runtime_text and dynamic_ids:
            name_block = re.search(r"\(\{(?P<body>.*?)\}\)\[e\]\|\|e", runtime_text)
            name_map = dict(
                re.findall(r'(\d+):"([0-9a-f]+)"', name_block.group("body"))
            ) if name_block else {}
            hash_map = dict(re.findall(r'(\d+):"([0-9a-f]{12,})"', runtime_text))
            dynamic_urls = [
                urljoin(
                    runtime_url,
                    f"{name_map.get(chunk_id, chunk_id)}.{hash_map[chunk_id]}.js",
                )
                for chunk_id in dynamic_ids
                if chunk_id in hash_map
            ]
            dynamic_results = await asyncio.gather(
                *(client.get(url, headers={"Accept": "text/javascript"}) for url in dynamic_urls),
                return_exceptions=True,
            )
            bundles.extend(
                response.text
                for response in dynamic_results
                if isinstance(response, httpx.Response) and response.is_success
            )

        bundle_text = "\n".join(bundles)
        metadata = _hyperglass_v2_metadata(bundle_text)
        if metadata:
            metadata["bootstrap_session"] = "set-cookie" in root_response.headers
        return metadata
    except (httpx.HTTPError, ValueError):
        return None


async def execute_hyperglass(config_data: dict, operation: Operation, target: str) -> str:
    config = HyperglassConfig.model_validate(config_data)
    cache_key = (config.base_url, config.location, operation)
    query_type = HYPERGLASS_QUERY_TYPE_CACHE.get(cache_key) or config.query_types.get(operation)
    if not query_type:
        raise NotImplementedError(f"{operation.value} não está configurado neste LG")
    try:
        async with httpx.AsyncClient(
            timeout=config.timeout,
            verify=config.verify_tls,
            follow_redirects=True,
            trust_env=False,
            headers={
                "User-Agent": "MultiLG/1.0",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": config.base_url,
                "Referer": config.base_url + "/",
            },
        ) as client:
            async def run_query(selected_query_type: str) -> httpx.Response:
                if config.api_format == "v1":
                    return await client.post(
                        f"{config.base_url}/api/query/",
                        json={
                            "query_location": config.location,
                            "query_type": selected_query_type,
                            "query_target": target,
                            "query_vrf": config.vrf,
                        },
                    )
                return await client.post(
                    f"{config.base_url}/api/query",
                    json={
                        "queryLocation": config.location,
                        "queryType": selected_query_type,
                        "queryTarget": [target],
                    },
                )

            if config.bootstrap_session:
                await client.get(config.base_url + "/", headers={"Accept": "text/html"})
            response = await run_query(query_type)
            if (
                response.status_code == 403
                and not config.bootstrap_session
                and "text/html" in response.headers.get("content-type", "")
            ):
                bootstrap_response = await client.get(
                    config.base_url + "/", headers={"Accept": "text/html"}
                )
                if bootstrap_response.is_success and client.cookies:
                    response = await run_query(query_type)
            try:
                initial_data = response.json()
            except ValueError:
                initial_data = None
            initial_error = _hyperglass_error(response, initial_data)
            if (
                isinstance(initial_data, dict)
                and (
                    not response.is_success
                    or initial_data.get("level") not in (None, "success")
                )
                and "query type" in initial_error.lower()
                and "not found" in initial_error.lower()
            ):
                discovery = await discover_hyperglass(config.model_dump(mode="json"))
                device = next(
                    (item for item in discovery["devices"] if item["id"] == config.location),
                    None,
                )
                discovered_query_types = device.get("query_types", {}) if device else {}
                for discovered_operation, discovered_query_type in discovered_query_types.items():
                    HYPERGLASS_QUERY_TYPE_CACHE[
                        (config.base_url, config.location, Operation(discovered_operation))
                    ] = discovered_query_type
                resolved_query_type = discovered_query_types.get(operation.value)
                if resolved_query_type and resolved_query_type != query_type:
                    query_type = resolved_query_type
                    response = await run_query(query_type)
    except httpx.TimeoutException as exc:
        raise TimeoutError(
            f"Tempo limite de {config.timeout}s aguardando a API Hyperglass"
        ) from exc
    except httpx.HTTPError as exc:
        raise ConnectionError(f"Falha ao conectar à API Hyperglass: {exc}") from exc

    try:
        data = response.json()
    except ValueError:
        data = None
    if not response.is_success:
        raise ValueError(_hyperglass_error(response, data))
    if isinstance(data, dict) and data.get("level") not in (None, "success"):
        raise ValueError(_hyperglass_error(response, data))

    output = data.get("output") if isinstance(data, dict) and "output" in data else data
    if output is None:
        output = response.text
    if isinstance(output, str):
        output = output.strip()
        if match := CLI_ERROR_RE.search(output):
            error_line = output[match.start() :].splitlines()[0].strip()
            raise ValueError(f"O roteador rejeitou o comando gerado pelo Hyperglass: {error_line}")
        return output
    return json.dumps(output, indent=2, ensure_ascii=False)


async def discover_hyperglass(config_data: dict) -> dict[str, Any]:
    config = HyperglassDiscoveryInput.model_validate(config_data)
    try:
        async with httpx.AsyncClient(
            timeout=config.timeout,
            verify=config.verify_tls,
            follow_redirects=True,
            trust_env=False,
            headers={
                "User-Agent": "MultiLG/1.0",
                "Accept": "application/json",
                "Origin": config.base_url,
                "Referer": config.base_url + "/",
            },
        ) as client:
            frontend_metadata = await _hyperglass_frontend_metadata(client, config.base_url)
            if frontend_metadata:
                return {"base_url": config.base_url, **frontend_metadata}

            devices_response, queries_response = await asyncio.gather(
                client.get(f"{config.base_url}/api/devices/"),
                client.get(f"{config.base_url}/api/queries/"),
            )

            for response in (devices_response, queries_response):
                if not response.is_success:
                    try:
                        data = response.json()
                    except ValueError:
                        data = None
                    raise ValueError(_hyperglass_error(response, data))

            try:
                raw_devices = devices_response.json()
                raw_queries = queries_response.json()
            except ValueError as exc:
                raise ValueError("A API Hyperglass não retornou JSON válido") from exc
            if not isinstance(raw_devices, list):
                raise ValueError("A resposta de /api/devices/ não contém uma lista")
            if not isinstance(raw_queries, list):
                raise ValueError("A resposta de /api/queries/ não contém uma lista")
    except httpx.TimeoutException as exc:
        raise TimeoutError(
            f"Tempo limite de {config.timeout}s ao consultar a API Hyperglass"
        ) from exc
    except httpx.HTTPError as exc:
        raise ConnectionError(f"Falha ao conectar à API Hyperglass: {exc}") from exc

    devices = [
        {
            "id": str(device["id"]),
            "name": str(device.get("name") or device["id"]),
            "group": device.get("group"),
            "query_types": {},
            "vrf": "default",
        }
        for device in raw_devices
        if isinstance(device, dict) and device.get("id")
    ]
    if not devices:
        raise ValueError("Nenhum dispositivo foi encontrado na API Hyperglass")
    return {
        "base_url": config.base_url,
        "devices": devices,
        "queries": [str(query) for query in raw_queries],
        "api_format": "v2",
        "version": "unknown",
        "request_timeout": None,
        "bootstrap_session": False,
    }


def _pattern_at_end(patterns: list[re.Pattern[str]], buffer: str) -> int | None:
    for index, pattern in enumerate(patterns):
        for match in pattern.finditer(buffer):
            if not buffer[match.end() :].strip():
                return index
    return None


async def read_until_patterns(
    reader,
    patterns: list[str],
    timeout: int,
    waiting_for: str,
    settle_seconds: float = PROMPT_SETTLE_SECONDS,
    on_update: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, int]:
    compiled = [re.compile(pattern) for pattern in patterns]
    buffer = ""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(
                f"Tempo limite de {timeout}s aguardando {waiting_for}"
            )
        matched = _pattern_at_end(compiled, buffer)
        read_timeout = min(settle_seconds, remaining) if matched is not None else remaining
        try:
            chunk = await asyncio.wait_for(reader.read(512), timeout=read_timeout)
        except TimeoutError as exc:
            if matched is not None:
                return buffer, matched
            raise TimeoutError(
                f"Tempo limite de {timeout}s aguardando {waiting_for}"
            ) from exc
        if not chunk:
            if matched is not None:
                return buffer, matched
            raise ConnectionError(
                f"A conexão foi encerrada antes de receber {waiting_for}"
            )
        buffer += chunk
        deadline = asyncio.get_running_loop().time() + timeout
        if on_update:
            await on_update(buffer)


async def read_until_regex(
    reader,
    pattern: str,
    timeout: int,
    waiting_for: str = "o prompt esperado",
    on_update: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    output, _ = await read_until_patterns(
        reader,
        [pattern],
        timeout,
        waiting_for,
        on_update=on_update,
    )
    return output


def telnet_command(config: TelnetConfig, operation: Operation, target: str) -> str | None:
    if operation in (Operation.bgp_community, Operation.bgp_aspath):
        return config.commands.get(operation)
    parsed = ipaddress.ip_network(target, strict=False) if "/" in target else ipaddress.ip_address(target)
    if parsed.version == 6:
        return config.commands_v6.get(operation) or config.commands.get(operation)
    return config.commands.get(operation)


def clean_telnet_output(output: str, command: str, prompt_regex: str) -> str:
    output = ANSI_RE.sub("", output).replace("\r", "")
    if "\n" not in output and command.strip().startswith(output.strip()):
        return ""
    lines = output.strip().splitlines()
    if lines and command.strip() in lines[0]:
        lines = lines[1:]
    if lines and re.search(prompt_regex, lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines).strip()


async def execute_telnet(
    config_data: dict,
    operation: Operation,
    target: str,
    on_output: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    config = TelnetConfig.model_validate(config_data)
    command = telnet_command(config, operation, target)
    if not command:
        raise NotImplementedError(f"{operation.value} não está configurado neste LG")
    try:
        reader, writer = await asyncio.wait_for(
            telnetlib3.open_connection(
                host=config.host,
                port=config.port,
                connect_minwait=0,
            ),
            timeout=config.timeout,
        )
    except TimeoutError as exc:
        raise TimeoutError(
            f"Tempo limite de {config.timeout}s ao conectar a "
            f"{config.host}:{config.port}"
        ) from exc
    transcript = ""
    try:
        prompt_ready = False
        if config.username:
            initial, matched = await read_until_patterns(
                reader,
                [config.username_prompt, config.prompt_regex],
                config.timeout,
                "o prompt de usuário ou o prompt final",
            )
            transcript += initial
            if matched == 0:
                writer.write(config.username + "\n")
            else:
                prompt_ready = True
        if config.password and not prompt_ready:
            password_prompt, matched = await read_until_patterns(
                reader,
                [config.password_prompt, config.prompt_regex],
                config.timeout,
                "o prompt de senha ou o prompt final",
            )
            transcript += password_prompt
            if matched == 0:
                writer.write(config.password + "\n")
            else:
                prompt_ready = True
        if not prompt_ready:
            transcript += await read_until_regex(
                reader,
                config.prompt_regex,
                config.timeout,
                "o prompt final após a autenticação",
            )
        for pre_command in config.pre_commands:
            writer.write(render(pre_command, target, operation) + "\n")
            transcript += await read_until_regex(
                reader,
                config.prompt_regex,
                config.timeout,
                f'o término do pré-comando "{pre_command}"',
            )
        rendered_command = render(command, target, operation)
        writer.write(rendered_command + "\n")

        async def report_output(current: str):
            if on_output:
                cleaned = clean_telnet_output(
                    current,
                    rendered_command,
                    config.prompt_regex,
                )
                if cleaned:
                    await on_output(cleaned)

        output = await read_until_regex(
            reader,
            config.prompt_regex,
            config.timeout,
            f'o término do comando "{rendered_command}"',
            on_update=report_output if on_output else None,
        )
        if config.quit_command:
            writer.write(config.quit_command + "\n")
        return clean_telnet_output(output, rendered_command, config.prompt_regex)
    finally:
        writer.close()


async def execute_one(
    lg: dict,
    operation: Operation,
    target: str,
    on_output: Callable[[str], Awaitable[None]] | None = None,
) -> QueryResult:
    started = time.perf_counter()
    try:
        if lg["protocol"] == Protocol.http.value:
            if lg["config"].get("hyperglass_like"):
                output = await execute_hyperglass(lg["config"], operation, target)
            else:
                output = await execute_http(lg["config"], operation, target)
        else:
            output = await execute_telnet(lg["config"], operation, target, on_output)
        status = ResultStatus.success
        output = output or "Consulta concluída sem conteúdo na resposta."
    except NotImplementedError as exc:
        status, output = ResultStatus.unsupported, str(exc)
    except TimeoutError as exc:
        status = ResultStatus.error
        output = str(exc) or "A consulta excedeu o tempo limite configurado."
    except Exception as exc:
        status, output = ResultStatus.error, f"{type(exc).__name__}: {exc}"
    return QueryResult(
        looking_glass_id=lg["id"],
        looking_glass_name=lg["name"],
        protocol=Protocol(lg["protocol"]),
        status=status,
        output=output,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )
