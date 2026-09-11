import asyncio
import ipaddress
import json
import re
import time
from typing import Any

import httpx
import telnetlib3

from .schemas import HttpConfig, Operation, Protocol, QueryResult, ResultStatus, TelnetConfig

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PROMPT_SETTLE_SECONDS = 0.1


def validate_target(target: str, operation: Operation) -> str:
    value = target.strip()
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


async def read_until_regex(
    reader,
    pattern: str,
    timeout: int,
    waiting_for: str = "o prompt esperado",
) -> str:
    output, _ = await read_until_patterns(reader, [pattern], timeout, waiting_for)
    return output


def telnet_command(config: TelnetConfig, operation: Operation, target: str) -> str | None:
    parsed = ipaddress.ip_network(target, strict=False) if "/" in target else ipaddress.ip_address(target)
    if parsed.version == 6:
        return config.commands_v6.get(operation) or config.commands.get(operation)
    return config.commands.get(operation)


async def execute_telnet(config_data: dict, operation: Operation, target: str) -> str:
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
        output = await read_until_regex(
            reader,
            config.prompt_regex,
            config.timeout,
            f'o término do comando "{rendered_command}"',
        )
        if config.quit_command:
            writer.write(config.quit_command + "\n")
        output = ANSI_RE.sub("", output).replace("\r", "")
        lines = output.strip().splitlines()
        if lines and rendered_command.strip() in lines[0]:
            lines = lines[1:]
        if lines and re.search(config.prompt_regex, lines[-1]):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    finally:
        writer.close()


async def execute_one(lg: dict, operation: Operation, target: str) -> QueryResult:
    started = time.perf_counter()
    try:
        if lg["protocol"] == Protocol.http.value:
            output = await execute_http(lg["config"], operation, target)
        else:
            output = await execute_telnet(lg["config"], operation, target)
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
