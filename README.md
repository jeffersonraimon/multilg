# MultiLG Client

Cliente web auto-hospedado para executar uma única consulta em vários Looking Glass e comparar os resultados lado a lado. Suporta fontes HTTP/HTTPS e Telnet, com operações de ping, traceroute e consulta BGP configuráveis por provedor.

## Funcionalidades

- Consulta paralela em múltiplos Looking Glass.
- Resultados progressivos: cada LG aparece assim que termina, sem aguardar os demais.
- Exibição de até quatro resultados por página, com navegação anterior/próxima.
- Filtro automático dos LGs conforme a operação selecionada: BGP, ping ou traceroute.
- Histórico local dos oito últimos IPs ou prefixos consultados, com atalhos e opção para limpar.
- Configuração HTTP genérica para requisições GET e POST, respostas em texto ou JSON e extração por regex.
- Sessões Telnet com autenticação opcional, prompts configuráveis e pré-comandos.
- Comandos Telnet IPv4 e IPv6 separados quando o equipamento exigir sintaxes diferentes.
- Opção para desativar a paginação em equipamentos Cisco, FRR e RouteViews.

## Subir com Docker

```bash
cp .env.example .env
# Edite LG_SECRET_KEY no .env e use uma chave longa e aleatória.
docker compose up -d --build
```

Acesse `http://IP-DO-SERVIDOR:8080`. Os cadastros ficam em `./data/multilg.db`.

> Não altere `LG_SECRET_KEY` depois de cadastrar fontes: ela criptografa as configurações e credenciais no banco.

## Executar uma consulta

1. Escolha BGP, ping ou traceroute.
2. Informe um endereço IP; para BGP também é aceito um prefixo CIDR.
3. Selecione os LGs e clique em **Consultar**.

A tela mostra somente os LGs habilitados que oferecem a operação escolhida. Os resultados são apresentados individualmente conforme chegam e divididos em páginas de até quatro cards.

Após uma consulta válida, o destino é salvo no `localStorage` do navegador. O histórico não é enviado nem armazenado no servidor e pode ser removido pelo botão **Limpar**.

## Cadastro HTTP

Cada operação tem sua própria requisição. Use os marcadores `{target}` e `{operation}` na URL, query params, headers ou corpo.

Exemplo de uma API JSON:

- URL: `https://lg.exemplo.net/api/ping`
- Método: `GET`
- Query params: `{ "address": "{target}" }`
- Tipo de resposta: `JSON`
- JSON path: `data.output`

Para uma página HTML, selecione resposta `Texto / HTML` e use uma expressão regular com grupo de captura, por exemplo `<pre[^>]*>([\s\S]*?)</pre>`.

## Cadastro Telnet

Informe host, porta, prompts e os comandos aceitos pelo servidor. Exemplos:

- Ping: `ping {target} count 5`
- Traceroute: `traceroute {target}`
- BGP: `show bgp route {target}`

Ao habilitar uma operação, informe seu comando IPv4/padrão. Use o botão **Usar comando IPv6 diferente** quando a sintaxe para IPv6 não for igual. Sem essa opção, alvos IPv6 reutilizam automaticamente o comando padrão.

Exemplo com comandos separados:

- IPv4: `show bgp ipv4 unicast {target} longer-prefixes`
- IPv6: `show bgp ipv6 unicast {target} longer-prefixes`

Os prompts são expressões regulares. O padrão final `[>#]\s*$` atende CLIs comuns, mas deve ser ajustado ao LG real.

Em Cisco, FRR e RouteViews, marque **Desativar paginação** para executar `terminal length 0` antes da consulta e evitar que a saída pare em `--More--`. Para outras CLIs, use o campo **Outros pré-comandos**, um comando por linha.

Se o host abrir diretamente no prompt final mesmo com credenciais cadastradas, o conector detecta esse prompt e continua sem tentar autenticar. Mensagens de timeout indicam a etapa que não terminou, como conexão, autenticação, pré-comando ou comando principal.

## Limitações importantes

- Looking Glass com CAPTCHA, JavaScript obrigatório, CSRF dinâmico ou autenticação em múltiplas etapas precisa de um adaptador específico; o conector HTTP genérico não contorna essas proteções.
- Telnet transmite dados sem criptografia. Prefira fontes públicas sem senha ou use a aplicação em uma rede de gerenciamento confiável.
- Esta versão não possui login próprio. Restrinja a porta 8080 por firewall, VPN ou reverse proxy com autenticação.
- Cadastre apenas serviços que você tem autorização para consultar e respeite limites de uso dos provedores.

## API

A documentação interativa está disponível em `http://IP-DO-SERVIDOR:8080/docs`.

Endpoints principais:

- `GET /api/looking-glasses`
- `POST /api/looking-glasses`
- `PUT /api/looking-glasses/{id}`
- `DELETE /api/looking-glasses/{id}`
- `POST /api/query`
- `POST /api/query/stream`

`POST /api/query` retorna o lote completo. `POST /api/query/stream` usa NDJSON e envia eventos progressivos dos tipos `start`, `result` e `complete`; é o endpoint utilizado pela interface web.

## Desenvolvimento local

Backend:

```bash
cd backend
export LG_SECRET_KEY='uma-chave-local-com-mais-de-16-caracteres'
export LG_DATABASE_PATH='./multilg.db'
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

O frontend abre em `http://localhost:5173` e já encaminha `/api` para o backend na porta 8080.
