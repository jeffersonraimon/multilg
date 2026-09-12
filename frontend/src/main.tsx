import React, { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity, ArrowLeftRight, Braces, Check, ChevronDown, CircleAlert, Clock3,
  Copy, Download, Edit3, Globe2, Hash, Loader2, Network, Plus, Radio, Regex, Route, Search,
  RefreshCw, Server, Settings2, Terminal, Trash2, Upload, X
} from "lucide-react";
import "./styles.css";

type Operation = "ping" | "traceroute" | "bgp" | "bgp_community" | "bgp_aspath";
type Protocol = "http" | "telnet";
type RequestConfig = {
  url: string; method: "GET" | "POST"; query: Record<string, string>;
  headers: Record<string, string>; body?: Record<string, unknown> | string | null;
  body_type: "json" | "form" | "text";
  response_type: "text" | "json"; json_path?: string; output_regex?: string;
  queryText?: string; headersText?: string; bodyText?: string;
};
type LookingGlass = {
  id: string; name: string; protocol: Protocol; enabled: boolean;
  config: Record<string, any>; created_at: string; updated_at: string;
};
type QueryResult = {
  looking_glass_id: string; looking_glass_name: string; protocol: Protocol;
  status: "success" | "error" | "unsupported" | "cancelled"; output: string; duration_ms: number;
};
type BatchResult = { target: string; operation: Operation; duration_ms: number; total: number; results: QueryResult[] };
type QueryStreamEvent =
  | { type: "start"; target: string; operation: Operation; looking_glass_ids: string[]; total: number }
  | { type: "partial"; looking_glass_id: string; output: string }
  | { type: "result"; result: QueryResult }
  | { type: "complete"; duration_ms: number };
type HyperglassDevice = {
  id: string; name: string; group?: string | null;
  query_types?: Partial<Record<Operation, string>>;
  vrf?: string;
};
type HyperglassDiscovery = {
  base_url: string; devices: HyperglassDevice[]; queries: string[];
  api_format: "v1" | "v2"; version: string; request_timeout?: number | null;
  bootstrap_session?: boolean;
};
type BgpRoute = {
  prefix: string; active: boolean; age: number; weight: number; med: number;
  local_preference: number; as_path: number[]; communities: string[];
  next_hop: string; source_as: number; source_rid: string; peer_rid: string;
  rpki_state: number;
};
type StructuredBgpOutput = {
  vrf: string; count: number; routes: BgpRoute[]; winning_weight: "low" | "high" | string;
};

const PAGING_PRESETS = [
  { value: "", label: "Não alterar" },
  { value: "terminal length 0", label: "Cisco / FRR / RouteViews — terminal length 0" },
  { value: "set cli screen-length 0", label: "Juniper / Junos — set cli screen-length 0" },
] as const;
const PAGING_COMMANDS = new Set<string>(PAGING_PRESETS.map(option => option.value).filter(Boolean));
const TARGET_HISTORY_KEY = "multilg.recent-targets";
const TARGET_HISTORY_LIMIT = 8;
const RESULTS_PER_PAGE = 4;
const OPERATIONS: Operation[] = ["ping", "traceroute", "bgp", "bgp_community", "bgp_aspath"];
const HYPERGLASS_DEFAULT_QUERY_TYPES: Partial<Record<Operation, string>> = {
  ping: "__hyperglass_juniper_ping__",
  traceroute: "__hyperglass_juniper_traceroute__",
  bgp: "__hyperglass_juniper_bgp_route_table__",
};

const operationLabel = (operation: Operation, compact = false): string => ({
  ping: "PING",
  traceroute: compact ? "TRACE" : "Traceroute",
  bgp: "BGP",
  bgp_community: compact ? "COMM" : "BGP Community",
  bgp_aspath: compact ? "AS PATH" : "BGP AS Path",
})[operation];

const operationIcon = (operation: Operation) => operation === "ping"
  ? <Radio size={17}/>
  : operation === "traceroute"
    ? <Route size={17}/>
    : operation === "bgp_community"
      ? <Hash size={17}/>
      : operation === "bgp_aspath"
        ? <Regex size={17}/>
        : <ArrowLeftRight size={17}/>;

const operationFromQueryLabel = (label: string): Operation | null => {
  const normalized = label.toLowerCase().replaceAll("-", "_");
  if (normalized.includes("ping")) return "ping";
  if (normalized.includes("trace")) return "traceroute";
  if (normalized.includes("communit")) return "bgp_community";
  if (normalized.includes("as path") || normalized.includes("aspath") || normalized.includes("as_path")) return "bgp_aspath";
  if (normalized.includes("bgp") || normalized.includes("route")) return "bgp";
  return null;
};

const parseStructuredBgpOutput = (output: string): StructuredBgpOutput | null => {
  try {
    const data = JSON.parse(output);
    if (!data || typeof data !== "object" || !Array.isArray(data.routes)) return null;
    if (!data.routes.every((route: unknown) => route && typeof route === "object" && typeof (route as BgpRoute).prefix === "string")) return null;
    return data as StructuredBgpOutput;
  } catch {
    return null;
  }
};

const formatRouteAge = (seconds: number): string => {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}min`;
  if (minutes) return `${minutes}min ${Math.floor(seconds % 60)}s`;
  return `${Math.floor(seconds)}s`;
};

const RPKI_STATES: Record<number, { label: string; className: string }> = {
  0: { label: "Inválido", className: "invalid" },
  1: { label: "Válido", className: "valid" },
  2: { label: "Não encontrado", className: "not-found" },
  3: { label: "Desconhecido", className: "unknown" },
};

const parsePreCommands = (value: string): string[] =>
  value.split("\n").map(line => line.trim()).filter(Boolean);

const loadRecentTargets = (): string[] => {
  try {
    const stored = JSON.parse(localStorage.getItem(TARGET_HISTORY_KEY) || "[]");
    return Array.isArray(stored) ? stored.filter(value => typeof value === "string").slice(0, TARGET_HISTORY_LIMIT) : [];
  } catch {
    return [];
  }
};

const api = async <T,>(path: string, options?: RequestInit): Promise<T> => {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail || `Erro HTTP ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
};

const streamApi = async (
  path: string,
  options: RequestInit,
  onEvent: (event: QueryStreamEvent) => void,
) => {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail || `Erro HTTP ${response.status}`);
  }
  if (!response.body) throw new Error("O navegador não disponibilizou a resposta progressiva");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consumeLines = (finished = false) => {
    const lines = buffer.split("\n");
    const tail = lines.pop() || "";
    buffer = finished ? "" : tail;
    for (const line of lines) if (line.trim()) onEvent(JSON.parse(line));
    if (finished && tail.trim()) onEvent(JSON.parse(tail));
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    consumeLines();
  }
  buffer += decoder.decode();
  consumeLines(true);
};

const emptyHttpRequest = (): RequestConfig => ({
  url: "", method: "GET", query: { target: "{target}" }, headers: {},
  body: {}, body_type: "json", response_type: "text", json_path: "", output_regex: "",
});

const initialForm = () => ({
  name: "", protocol: "http" as Protocol, enabled: true, hyperglass_like: false,
  http: { timeout: 20, verify_tls: true, requests: {} as Partial<Record<Operation, RequestConfig>> },
  hyperglass: {
    base_url: "", location: "", timeout: 30, verify_tls: true,
    api_format: "v2" as "v1" | "v2", vrf: "default", version: "", bootstrap_session: false,
    query_types: { ...HYPERGLASS_DEFAULT_QUERY_TYPES } as Partial<Record<Operation, string>>,
  },
  telnet: {
    host: "", port: 23, username: "", password: "",
    username_prompt: "(?i)(login|username)[: ]*$", password_prompt: "(?i)password[: ]*$",
    prompt_regex: "[>#]\\s*$", pre_commands: [] as string[], pre_commands_text: "", paging_command: "",
    commands: {} as Partial<Record<Operation, string>>,
    commands_v6: {} as Partial<Record<Operation, string>>, quit_command: "exit", timeout: 20,
  },
});

function App() {
  const [page, setPage] = useState<"query" | "providers">("query");
  const [items, setItems] = useState<LookingGlass[]>([]);
  const [loadingItems, setLoadingItems] = useState(true);
  const [target, setTarget] = useState(() => loadRecentTargets()[0] || "");
  const [recentTargets, setRecentTargets] = useState<string[]>(loadRecentTargets);
  const [operation, setOperation] = useState<Operation>("ping");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<BatchResult | null>(null);
  const [queryIds, setQueryIds] = useState<string[]>([]);
  const [partialOutputs, setPartialOutputs] = useState<Record<string, string>>({});
  const [resultPage, setResultPage] = useState(0);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [modal, setModal] = useState(false);
  const [editing, setEditing] = useState<LookingGlass | null>(null);
  const queryAbortRef = useRef<AbortController | null>(null);
  const queryStartedAtRef = useRef(0);

  const loadItems = async () => {
    try {
      const data = await api<LookingGlass[]>("/api/looking-glasses");
      setItems(data);
      setSelected(prev => prev.size ? new Set([...prev].filter(id => data.some(item => item.id === id && item.enabled))) : new Set(data.filter(i => i.enabled).map(i => i.id)));
    } catch (e) { setError((e as Error).message); }
    finally { setLoadingItems(false); }
  };
  useEffect(() => { loadItems(); }, []);

  const availableItems = useMemo(
    () => items.filter(item => item.enabled && configuredOperations(item).includes(operation)),
    [items, operation],
  );
  const supportedCount = useMemo(
    () => availableItems.filter(item => selected.has(item.id)).length,
    [availableItems, selected],
  );
  const allAvailableSelected = availableItems.length > 0 && supportedCount === availableItems.length;
  const toggleAll = () => {
    setSelected(previous => {
      const next = new Set(previous);
      for (const item of availableItems) allAvailableSelected ? next.delete(item.id) : next.add(item.id);
      return next;
    });
  };

  const rememberTarget = (value: string) => setRecentTargets(previous => {
    const next = [value, ...previous.filter(item => item !== value)].slice(0, TARGET_HISTORY_LIMIT);
    try { localStorage.setItem(TARGET_HISTORY_KEY, JSON.stringify(next)); } catch { /* armazenamento indisponível */ }
    return next;
  });

  const clearTargetHistory = () => {
    try { localStorage.removeItem(TARGET_HISTORY_KEY); } catch { /* armazenamento indisponível */ }
    setRecentTargets([]);
  };

  const cancelQuery = () => {
    queryAbortRef.current?.abort();
    const duration = Math.round(performance.now() - queryStartedAtRef.current);
    setResult(previous => {
      if (!previous) return previous;
      const completed = new Set(previous.results.map(item => item.looking_glass_id));
      const cancelled: QueryResult[] = queryIds.filter(id => !completed.has(id)).map(id => {
        const source = items.find(item => item.id === id);
        const partial = partialOutputs[id];
        return {
          looking_glass_id: id,
          looking_glass_name: source?.name || "Looking Glass",
          protocol: source?.protocol || "telnet",
          status: "cancelled",
          output: partial ? `${partial}\n\n[Consulta cancelada pelo usuário]` : "Consulta cancelada pelo usuário.",
          duration_ms: duration,
        };
      });
      return { ...previous, duration_ms: duration, results: [...previous.results, ...cancelled] };
    });
    setPartialOutputs({});
    setRunning(false);
  };

  const clearResults = () => {
    setResult(null);
    setQueryIds([]);
    setPartialOutputs({});
    setResultPage(0);
    setError("");
  };

  const run = async (event: FormEvent) => {
    event.preventDefault();
    const selectedIds = availableItems.filter(item => selected.has(item.id)).map(item => item.id);
    const started = performance.now();
    const controller = new AbortController();
    let receivedResults = 0;
    const streamedOutputs: Record<string, string> = {};
    queryAbortRef.current = controller;
    queryStartedAtRef.current = started;
    setError("");
    setRunning(true);
    setQueryIds(selectedIds);
    setPartialOutputs({});
    setResultPage(0);
    setResult({ target, operation, duration_ms: 0, total: selectedIds.length, results: [] });
    try {
      await streamApi(
        "/api/query/stream",
        { method: "POST", body: JSON.stringify({ target, operation, looking_glass_ids: selectedIds }), signal: controller.signal },
        event => {
          if (event.type === "start") {
            setQueryIds(event.looking_glass_ids);
            setTarget(event.target);
            rememberTarget(event.target);
            setResult(previous => ({
              target: event.target,
              operation: event.operation,
              duration_ms: previous?.duration_ms || 0,
              total: event.total,
              results: previous?.results || [],
            }));
          } else if (event.type === "partial") {
            streamedOutputs[event.looking_glass_id] = event.output;
            setPartialOutputs(previous => ({ ...previous, [event.looking_glass_id]: event.output }));
            setResult(previous => previous ? {
              ...previous,
              duration_ms: Math.round(performance.now() - started),
            } : previous);
          } else if (event.type === "result") {
            receivedResults += 1;
            const partial = streamedOutputs[event.result.looking_glass_id];
            const finalResult = event.result.status === "error" && partial
              ? { ...event.result, output: `${partial}\n\n[Interrompido] ${event.result.output}` }
              : event.result;
            setPartialOutputs(previous => {
              const next = { ...previous };
              delete next[event.result.looking_glass_id];
              return next;
            });
            setResult(previous => previous ? {
              ...previous,
              duration_ms: Math.round(performance.now() - started),
              results: [...previous.results.filter(item => item.looking_glass_id !== finalResult.looking_glass_id), finalResult],
            } : previous);
          } else {
            setResult(previous => previous ? { ...previous, duration_ms: event.duration_ms } : previous);
          }
        },
      );
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setError((e as Error).message);
        if (!receivedResults) setResult(null);
      }
    }
    finally {
      if (queryAbortRef.current === controller) queryAbortRef.current = null;
      setRunning(false);
    }
  };

  const remove = async (item: LookingGlass) => {
    if (!confirm(`Remover ${item.name}?`)) return;
    try { await api(`/api/looking-glasses/${item.id}`, { method: "DELETE" }); await loadItems(); }
    catch (e) { setError((e as Error).message); }
  };

  const duplicate = async (item: LookingGlass) => {
    try {
      const copy = await api<LookingGlass>(`/api/looking-glasses/${item.id}/duplicate`, { method: "POST" });
      await loadItems();
      setEditing(copy);
      setModal(true);
    } catch (e) { setError((e as Error).message); }
  };

  const fileInputRef = useRef<HTMLInputElement>(null);

  const exportLgs = async () => {
    try {
      const response = await fetch("/api/looking-glasses/export");
      if (!response.ok) throw new Error("Erro ao exportar Looking Glasses");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `multilg-looking-glasses-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) { setError((e as Error).message); }
  };

  const handleImportFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      let json;
      try {
        json = JSON.parse(text);
      } catch {
        throw new Error("O arquivo selecionado não contém um JSON válido.");
      }
      const result = await api<{ imported: number; created?: number; updated?: number }>("/api/looking-glasses/import", {
        method: "POST",
        body: JSON.stringify(json),
      });
      await loadItems();
      const msg = result.created !== undefined && result.updated !== undefined
        ? `Importação concluída! ${result.created} novo(s), ${result.updated} atualizado(s).`
        : `${result.imported} Looking Glass(es) importado(s) com sucesso!`;
      alert(msg);
    } catch (e) { setError((e as Error).message); }
    finally {
      if (event.target) event.target.value = "";
    }
  };

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Network size={21}/></div><div><strong>MultiLG</strong><span>Looking Glass Client</span><span className="brand-author">by @jeffersonraimon</span></div></div>
      <nav>
        <button className={page === "query" ? "active" : ""} onClick={() => setPage("query")}><Terminal size={19}/> Consultar</button>
        <button className={page === "providers" ? "active" : ""} onClick={() => setPage("providers")}><Server size={19}/> Looking Glasses <span className="nav-count">{items.length}</span></button>
      </nav>
      <div className="sidebar-foot"><span className="status-dot"/> API operacional</div>
    </aside>
    <main>
      <header className="topbar">
        <div><h1>{page === "query" ? "Consulta distribuída" : "Looking Glasses"}</h1><p>{page === "query" ? "Compare a visão da Internet em vários provedores." : "Cadastre e configure as fontes de consulta."}</p></div>
        {page === "providers" && <div className="header-actions">
          <input type="file" ref={fileInputRef} accept=".json" style={{ display: "none" }} onChange={handleImportFile} />
          <button className="secondary" title="Exportar LGs em JSON" onClick={exportLgs}><Download size={18}/> Exportar</button>
          <button className="secondary" title="Importar LGs de arquivo JSON" onClick={() => fileInputRef.current?.click()}><Upload size={18}/> Importar</button>
          <button className="primary" onClick={() => { setEditing(null); setModal(true); }}><Plus size={18}/> Adicionar LG</button>
        </div>}
      </header>

      {error && <div className="alert"><CircleAlert size={18}/><span>{error}</span><button onClick={() => setError("")}><X size={17}/></button></div>}

      {page === "query" ? <section className="query-page">
        <form className="query-panel" onSubmit={run}>
          <div className="operation-tabs">
            {OPERATIONS.map(op => <button type="button" key={op} className={operation === op ? "selected" : ""} onClick={() => setOperation(op)}>
              {operationIcon(op)} {operationLabel(op)}
            </button>)}
          </div>
          <div className="query-row">
            <label><span>{operation === "bgp_community" ? "Community BGP" : operation === "bgp_aspath" ? "Expressão regular de AS Path" : "IP ou prefixo"}</span><div className="input-with-icon"><Globe2 size={19}/><input value={target} onChange={e => setTarget(e.target.value)} placeholder={operation === "bgp" ? "1.1.1.0/24 ou 2001:db8::/32" : operation === "bgp_community" ? "7195:55000 ou large:7195:55:0" : operation === "bgp_aspath" ? "_13335$" : "8.8.8.8"} required autoFocus/></div></label>
            <div className="query-actions">{running && <button type="button" className="cancel-query" onClick={cancelQuery}><X size={18}/> Cancelar</button>}<button className="run-button" disabled={running || !supportedCount}>{running ? <Loader2 className="spin" size={19}/> : <Search size={19}/>} {running ? "Consultando..." : `Consultar ${supportedCount} LG${supportedCount === 1 ? "" : "s"}`}</button></div>
          </div>
          {recentTargets.length > 0 && <div className="target-history"><span><Clock3 size={13}/> Recentes</span><div>{recentTargets.map(value => <button type="button" className={target === value ? "selected" : ""} onClick={() => setTarget(value)} key={value}>{value}</button>)}</div><button type="button" className="clear-history" onClick={clearTargetHistory}>Limpar</button></div>}
          <div className="targets-head"><button type="button" className="link-button" disabled={!availableItems.length} onClick={toggleAll}>{allAvailableSelected ? "Limpar seleção" : "Selecionar todos"}</button><span>{supportedCount} de {availableItems.length} {availableItems.length === 1 ? "disponível" : "disponíveis"} selecionado{supportedCount === 1 ? "" : "s"}</span></div>
          <div className="provider-pills">
            {loadingItems ? <span className="muted">Carregando fontes…</span> : items.length === 0 ? <button type="button" className="empty-inline" onClick={() => { setPage("providers"); setModal(true); }}><Plus size={16}/> Cadastre o primeiro Looking Glass</button> : availableItems.length === 0 ? <span className="muted">Nenhum LG habilitado oferece {operationLabel(operation)}.</span> : availableItems.map(item => <button type="button" className={selected.has(item.id) ? "provider-pill selected" : "provider-pill"} onClick={() => setSelected(prev => { const next = new Set(prev); next.has(item.id) ? next.delete(item.id) : next.add(item.id); return next; })} key={item.id}><span className={`protocol-icon ${item.protocol}`}>{item.protocol === "http" ? <Globe2 size={14}/> : <Terminal size={14}/>}</span>{item.name}{selected.has(item.id) && <Check size={14}/>}</button>)}
          </div>
        </form>

        {!result && !running && <div className="blank-state"><div className="radar"><span/><span/><span/><i/></div><h2>Uma consulta, várias perspectivas</h2><p>Escolha as fontes acima para executar a consulta em paralelo.</p></div>}
        {result && <div className="results-section">
          <div className="results-summary"><div><strong>{result.results.length}/{result.total}</strong><span>LGs finalizados · {result.results.filter(r => r.status === "success").length} com sucesso</span></div><div><Clock3 size={16}/><span>{result.duration_ms} ms {running ? "decorridos" : "no total"}</span>{!running && <button type="button" className="clear-results" onClick={clearResults}><Trash2 size={14}/> Limpar resultados</button>}</div></div>
          <div className="results-grid">{queryIds.slice(resultPage * RESULTS_PER_PAGE, (resultPage + 1) * RESULTS_PER_PAGE).map(id => {
            const item = result.results.find(candidate => candidate.looking_glass_id === id);
            const source = items.find(candidate => candidate.id === id);
            return item ? <ResultCard key={id} item={item}/> : running && partialOutputs[id] && source ? <LiveResultCard key={id} item={source} output={partialOutputs[id]}/> : running ? <div className="result-card skeleton" key={id}><div/><span/><span/><span/></div> : null;
          })}</div>
          {queryIds.length > RESULTS_PER_PAGE && <div className="results-pagination"><button type="button" disabled={resultPage === 0} onClick={() => setResultPage(page => page - 1)}>Anterior</button><span>Página <strong>{resultPage + 1}</strong> de {Math.ceil(queryIds.length / RESULTS_PER_PAGE)}</span><button type="button" disabled={resultPage >= Math.ceil(queryIds.length / RESULTS_PER_PAGE) - 1} onClick={() => setResultPage(page => page + 1)}>Próxima</button></div>}
        </div>}
      </section> : <section className="providers-page">
        {loadingItems ? <div className="muted">Carregando…</div> : items.length === 0 ? <div className="empty-card"><Server size={31}/><h2>Nenhum Looking Glass cadastrado</h2><p>Adicione uma fonte HTTP ou Telnet para começar.</p><button className="primary" onClick={() => setModal(true)}><Plus size={18}/> Adicionar LG</button></div> : <div className="provider-list">{items.map(item => <article className="provider-row" key={item.id}>
          <div className={`big-protocol ${item.protocol}`}>{item.protocol === "http" ? <Globe2 size={22}/> : <Terminal size={22}/>}</div>
          <div className="provider-main"><div><h3>{item.name}</h3><span className={`badge ${item.enabled ? "online" : "off"}`}><i/>{item.enabled ? "Habilitado" : "Desabilitado"}</span></div><p>{describe(item)}</p></div>
          <div className="operation-badges">{configuredOperations(item).map(op => <span key={op}>{operationLabel(op as Operation, true)}</span>)}</div>
          <button className="icon-button" title="Duplicar" onClick={() => duplicate(item)}><Copy size={17}/></button>
          <button className="icon-button" title="Editar" onClick={() => { setEditing(item); setModal(true); }}><Edit3 size={17}/></button>
          <button className="icon-button danger" title="Remover" onClick={() => remove(item)}><Trash2 size={17}/></button>
        </article>)}</div>}
      </section>}
    </main>
    {modal && <ProviderModal item={editing} onClose={() => setModal(false)} onSaved={async () => { setModal(false); await loadItems(); }}/>} 
  </div>;
}

function ResultCard({ item }: { item: QueryResult }) {
  const [copied, setCopied] = useState(false);
  const [showRaw, setShowRaw] = useState(false);
  const structuredOutput = item.status === "success" ? parseStructuredBgpOutput(item.output) : null;
  const copy = async () => { await navigator.clipboard.writeText(item.output); setCopied(true); setTimeout(() => setCopied(false), 1500); };
  return <article className={`result-card ${item.status}`}>
    <header><div className={`protocol-icon ${item.protocol}`}>{item.protocol === "http" ? <Globe2 size={15}/> : <Terminal size={15}/>}</div><div><h3>{item.looking_glass_name}</h3><span>{item.protocol.toUpperCase()}</span></div><span className={`result-status ${item.status}`}>{item.status === "success" ? "Concluído" : item.status === "unsupported" ? "Não suportado" : item.status === "cancelled" ? "Cancelado" : "Erro"}</span><span className="duration">{item.duration_ms} ms</span>{structuredOutput && <button className={`copy raw-toggle ${showRaw ? "selected" : ""}`} title={showRaw ? "Ver rotas formatadas" : "Ver JSON original"} aria-label={showRaw ? "Ver rotas formatadas" : "Ver JSON original"} onClick={() => setShowRaw(value => !value)}><Braces size={16}/></button>}<button className="copy" title="Copiar resultado completo" aria-label="Copiar resultado completo" onClick={copy}>{copied ? <Check size={16}/> : <Copy size={16}/>}</button></header>
    {structuredOutput && !showRaw ? <BgpRoutesOutput data={structuredOutput}/> : <pre>{item.output}</pre>}
  </article>;
}

function BgpRoutesOutput({ data }: { data: StructuredBgpOutput }) {
  return <div className="bgp-output">
    <div className="bgp-overview">
      <div><span>VRF</span><strong>{data.vrf || "default"}</strong></div>
      <div><span>Rotas</span><strong>{data.count ?? data.routes.length}</strong></div>
      <div><span>Preferência de weight</span><strong>{data.winning_weight === "low" ? "Menor" : data.winning_weight === "high" ? "Maior" : data.winning_weight || "—"}</strong></div>
    </div>
    <div className="bgp-route-list">{data.routes.map((route, index) => {
      const rpki = RPKI_STATES[route.rpki_state] || { label: `Estado ${route.rpki_state}`, className: "unknown" };
      return <section className={`bgp-route ${route.active ? "active" : "alternate"}`} key={`${route.prefix}-${route.next_hop}-${index}`}>
        <div className="route-heading">
          <span className={`route-choice ${route.active ? "active" : "alternate"}`}>{route.active ? "Melhor caminho" : "Alternativo"}</span>
          <strong>{route.prefix}</strong>
          <span className={`rpki-state ${rpki.className}`} title={`Estado RPKI ${route.rpki_state}`}>RPKI {rpki.label}</span>
        </div>
        <div className="route-path">
          <div><span>Next-hop</span><strong>{route.next_hop || "—"}</strong></div>
          <div><span>AS Path</span><strong>{route.as_path?.length ? route.as_path.join(" → ") : "Local"}</strong></div>
        </div>
        <div className="route-metrics">
          <div><span>Local-pref</span><strong>{route.local_preference ?? "—"}</strong></div>
          <div><span>MED</span><strong>{route.med ?? "—"}</strong></div>
          <div><span>Weight</span><strong>{route.weight ?? "—"}</strong></div>
          <div><span>Idade</span><strong title={`${route.age} segundos`}>{formatRouteAge(route.age)}</strong></div>
          <div><span>AS de origem</span><strong>{route.source_as || "—"}</strong></div>
          <div><span>Router ID origem</span><strong>{route.source_rid || "—"}</strong></div>
          <div><span>Router ID peer</span><strong>{route.peer_rid || "—"}</strong></div>
        </div>
        <div className="route-communities"><span>Communities</span><div>{route.communities?.length ? route.communities.map(community => <code key={community}>{community}</code>) : <i>Nenhuma</i>}</div></div>
      </section>;
    })}</div>
  </div>;
}

function LiveResultCard({ item, output }: { item: LookingGlass; output: string }) {
  const outputRef = useRef<HTMLPreElement>(null);
  useEffect(() => {
    const element = outputRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [output]);
  return <article className="result-card running">
    <header><div className={`protocol-icon ${item.protocol}`}><Terminal size={15}/></div><div><h3>{item.name}</h3><span>{item.protocol.toUpperCase()}</span></div><span className="result-status running"><Loader2 className="spin" size={12}/> Executando</span></header>
    <pre ref={outputRef} aria-live="polite">{output}</pre>
  </article>;
}

const configuredOperations = (item: LookingGlass): string[] => Object.keys(
  item.protocol === "telnet"
    ? item.config.commands || {}
    : item.config.hyperglass_like
      ? item.config.query_types || {}
      : item.config.requests || {},
);
const describe = (item: LookingGlass) => item.protocol === "telnet"
  ? `${item.config.host}:${item.config.port || 23}`
  : item.config.hyperglass_like
    ? `Hyperglass · ${item.config.location} · ${configuredOperations(item).length} operação(ões)`
    : `${configuredOperations(item).length} endpoint(s) HTTP configurado(s)`;

function ProviderModal({ item, onClose, onSaved }: { item: LookingGlass | null; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState<any>(() => {
    const base = initialForm();
    if (!item) return base;
    return item.protocol === "http" && item.config.hyperglass_like
      ? {
          ...base,
          name: item.name,
          protocol: item.protocol,
          enabled: item.enabled,
          hyperglass_like: true,
          hyperglass: {
            ...base.hyperglass,
            ...item.config,
            query_types: item.config.query_types || {},
          },
        }
      : item.protocol === "http"
      ? { ...base, name: item.name, protocol: item.protocol, enabled: item.enabled, http: { ...base.http, ...item.config } }
      : {
          ...base,
          name: item.name,
          protocol: item.protocol,
          enabled: item.enabled,
          telnet: {
            ...item.config,
            commands_v6: item.config.commands_v6 || {},
            pre_commands_text: (item.config.pre_commands || []).filter((command: string) => !PAGING_COMMANDS.has(command)).join("\n"),
            paging_command: (item.config.pre_commands || []).find((command: string) => PAGING_COMMANDS.has(command)) || "",
          },
        };
  });
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");
  const [discovering, setDiscovering] = useState(false);
  const [hyperglassDevices, setHyperglassDevices] = useState<HyperglassDevice[]>([]);
  const [hyperglassQueries, setHyperglassQueries] = useState<string[]>([]);
  const ops = OPERATIONS;

  const toggleHttpOp = (op: Operation) => setForm((prev: any) => ({ ...prev, http: { ...prev.http, requests: { ...prev.http.requests, [op]: prev.http.requests[op] ? undefined : emptyHttpRequest() } } }));
  const toggleTelnetOp = (op: Operation) => setForm((prev: any) => {
    const enabled = prev.telnet.commands[op] !== undefined;
    return {
      ...prev,
      telnet: {
        ...prev.telnet,
        commands: { ...prev.telnet.commands, [op]: enabled ? undefined : "" },
        commands_v6: { ...prev.telnet.commands_v6, [op]: undefined },
      },
    };
  });
  const toggleTelnetV6 = (op: Operation) => setForm((prev: any) => ({
    ...prev,
    telnet: {
      ...prev.telnet,
      commands_v6: {
        ...prev.telnet.commands_v6,
        [op]: prev.telnet.commands_v6[op] !== undefined ? undefined : "",
      },
    },
  }));
  const toggleHyperglassOp = (op: Operation) => setForm((prev: any) => ({
    ...prev,
    hyperglass: {
      ...prev.hyperglass,
      query_types: {
        ...prev.hyperglass.query_types,
        [op]: prev.hyperglass.query_types[op] !== undefined ? undefined : (HYPERGLASS_DEFAULT_QUERY_TYPES[op] || ""),
      },
    },
  }));
  const discoverHyperglass = async () => {
    setDiscovering(true);
    setFormError("");
    try {
      const discovery = await api<HyperglassDiscovery>("/api/hyperglass/discover", {
        method: "POST",
        body: JSON.stringify({
          base_url: form.hyperglass.base_url,
          timeout: form.hyperglass.timeout,
          verify_tls: form.hyperglass.verify_tls,
        }),
      });
      setHyperglassDevices(discovery.devices);
      setHyperglassQueries(discovery.queries);
      setForm((prev: any) => {
        const currentLocationExists = discovery.devices.some(device => device.id === prev.hyperglass.location);
        const location = currentLocationExists ? prev.hyperglass.location : discovery.devices[0].id;
        const selectedDevice = discovery.devices.find(device => device.id === location) || discovery.devices[0];
        const recognized: Partial<Record<Operation, string>> = {};
        for (const label of discovery.queries) {
          const detectedOperation = operationFromQueryLabel(label);
          if (detectedOperation) {
            recognized[detectedOperation] = prev.hyperglass.query_types[detectedOperation]
              || HYPERGLASS_DEFAULT_QUERY_TYPES[detectedOperation]
              || "";
          }
        }
        return {
          ...prev,
          name: prev.name || discovery.devices[0].name,
          hyperglass: {
            ...prev.hyperglass,
            base_url: discovery.base_url,
            location,
            api_format: discovery.api_format,
            version: discovery.version,
            vrf: selectedDevice.vrf || "default",
            bootstrap_session: discovery.bootstrap_session || false,
            timeout: discovery.request_timeout || prev.hyperglass.timeout,
            query_types: selectedDevice.query_types && Object.keys(selectedDevice.query_types).length
              ? { ...selectedDevice.query_types }
              : Object.keys(recognized).length ? recognized : prev.hyperglass.query_types,
          },
        };
      });
    } catch (e) {
      setFormError((e as Error).message);
    } finally {
      setDiscovering(false);
    }
  };
  const parseJson = (value: string, label: string) => { try { return value.trim() ? JSON.parse(value) : {}; } catch { throw new Error(`${label} deve ser um JSON válido`); } };

  const save = async (event: FormEvent) => {
    event.preventDefault(); setSaving(true); setFormError("");
    try {
      let config: any;
      if (form.protocol === "http" && form.hyperglass_like) {
        const queryTypes: Record<string, string> = {};
        for (const op of ops) if (form.hyperglass.query_types[op] !== undefined) queryTypes[op] = form.hyperglass.query_types[op]!;
        config = {
          hyperglass_like: true,
          base_url: form.hyperglass.base_url,
          location: form.hyperglass.location,
          api_format: form.hyperglass.api_format,
          vrf: form.hyperglass.vrf,
          bootstrap_session: form.hyperglass.bootstrap_session,
          timeout: form.hyperglass.timeout,
          verify_tls: form.hyperglass.verify_tls,
          query_types: queryTypes,
        };
      } else if (form.protocol === "http") {
        const requests: Record<string, any> = {};
        for (const op of ops) {
          const req = form.http.requests[op]; if (!req) continue;
          const { queryText, headersText, bodyText, ...request } = req;
          requests[op] = {
            ...request,
            query: parseJson(queryText ?? JSON.stringify(req.query || {}), "Query params"),
            headers: parseJson(headersText ?? JSON.stringify(req.headers || {}), "Headers"),
            body: req.method === "POST"
              ? (req.body_type === "text" ? (bodyText ?? String(req.body || "")) : parseJson(bodyText ?? JSON.stringify(req.body || {}), "Corpo"))
              : null,
          };
        }
        config = { ...form.http, requests };
      } else {
        const commands: Record<string, string> = {};
        const commandsV6: Record<string, string> = {};
        for (const op of ops) if (form.telnet.commands[op] !== undefined) commands[op] = form.telnet.commands[op]!;
        for (const op of ops) if (form.telnet.commands_v6[op] !== undefined) commandsV6[op] = form.telnet.commands_v6[op]!;
        const { pre_commands_text, paging_command, has_password: _has_password, commands_v6: _commands_v6, ...telnet } = form.telnet;
        config = {
          ...telnet,
          pre_commands: [
            ...(paging_command ? [paging_command] : []),
            ...parsePreCommands(pre_commands_text).filter(command => command !== paging_command),
          ],
          commands,
          commands_v6: commandsV6,
        };
      }
      const payload = { name: form.name, protocol: form.protocol, enabled: form.enabled, config };
      await api(item ? `/api/looking-glasses/${item.id}` : "/api/looking-glasses", { method: item ? "PUT" : "POST", body: JSON.stringify(payload) });
      onSaved();
    } catch (e) { setFormError((e as Error).message); }
    finally { setSaving(false); }
  };

  return <div className="modal-backdrop" onMouseDown={e => e.target === e.currentTarget && onClose()}><div className="modal">
    <header><div><h2>{item ? "Editar Looking Glass" : "Adicionar Looking Glass"}</h2><p>Defina como cada operação será executada.</p></div><button className="icon-button" onClick={onClose}><X size={20}/></button></header>
    <form onSubmit={save}>
      {formError && <div className="alert"><CircleAlert size={17}/>{formError}</div>}
      <div className="form-grid two"><label><span>Nome</span><input required minLength={2} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="LG São Paulo"/></label><label><span>Protocolo</span><div className="select-wrap"><select value={form.protocol} onChange={e => setForm({ ...form, protocol: e.target.value as Protocol })}><option value="http">HTTP / HTTPS</option><option value="telnet">Telnet</option></select><ChevronDown size={16}/></div></label></div>
      <label className="switch-row"><button type="button" role="switch" aria-checked={form.enabled} className={form.enabled ? "switch on" : "switch"} onClick={() => setForm({ ...form, enabled: !form.enabled })}><i/></button><span>Habilitar nas consultas</span></label>
      <div className="divider"/>
      {form.protocol === "http" ? <>
        <label className="hyperglass-option"><input type="checkbox" checked={form.hyperglass_like} onChange={e => setForm({ ...form, hyperglass_like: e.target.checked })}/><span><strong>Hyperglass-like</strong><small>Detectar Hyperglass 1.x/2.x, dispositivos, operações e o formato da API.</small></span></label>
        {form.hyperglass_like ? <>
          <div className="hyperglass-discovery-row"><label><span>URL base do Hyperglass</span><input required value={form.hyperglass.base_url} onChange={e => setForm({ ...form, hyperglass: { ...form.hyperglass, base_url: e.target.value } })} placeholder="http://lg.exemplo.net.br"/></label><button type="button" className="secondary discover-button" disabled={discovering || !form.hyperglass.base_url} onClick={discoverHyperglass}>{discovering ? <Loader2 className="spin" size={16}/> : <RefreshCw size={16}/>} Detectar API</button></div>
          <div className="form-grid three">
            <label><span>Dispositivo / queryLocation</span>{hyperglassDevices.length ? <div className="select-wrap"><select required value={form.hyperglass.location} onChange={e => {
              const location = e.target.value;
              const device = hyperglassDevices.find(item => item.id === location);
              setForm((prev: any) => ({ ...prev, hyperglass: {
                ...prev.hyperglass,
                location,
                vrf: device?.vrf || "default",
                query_types: device?.query_types && Object.keys(device.query_types).length
                  ? { ...device.query_types }
                  : prev.hyperglass.query_types,
              } }));
            }}>{hyperglassDevices.map(device => <option value={device.id} key={device.id}>{device.name} ({device.id}){device.group ? ` · ${device.group}` : ""}</option>)}</select><ChevronDown size={16}/></div> : <input required value={form.hyperglass.location} onChange={e => setForm({ ...form, hyperglass: { ...form.hyperglass, location: e.target.value } })} placeholder="ssaba_-_rta-01"/>}</label>
            <label><span>Timeout (segundos)</span><input type="number" min="2" max="300" value={form.hyperglass.timeout} onChange={e => setForm({ ...form, hyperglass: { ...form.hyperglass, timeout: +e.target.value } })}/></label>
            <label className="check-row"><input type="checkbox" checked={form.hyperglass.verify_tls} onChange={e => setForm({ ...form, hyperglass: { ...form.hyperglass, verify_tls: e.target.checked } })}/> Validar TLS</label>
          </div>
          {hyperglassQueries.length > 0 && <div className="hyperglass-detected"><span>Hyperglass {form.hyperglass.version || "detectado"} · API {form.hyperglass.api_format.toUpperCase()}</span>{hyperglassQueries.map(query => <i key={query}>{query}</i>)}</div>}
          <div className="hyperglass-help"><strong>IDs internos da consulta</strong><span>O MultiLG tenta detectar os <code>queryType</code> específicos de cada dispositivo. Se o LG não publicar esses dados, os padrões Juniper são usados como base e continuam editáveis.</span></div>
          <OperationEditor ops={ops} configured={form.hyperglass.query_types} onToggle={toggleHyperglassOp}>{op => <div className="operation-fields"><label><span>queryType da API</span><input required value={form.hyperglass.query_types[op] || ""} onChange={e => setForm((prev: any) => ({ ...prev, hyperglass: { ...prev.hyperglass, query_types: { ...prev.hyperglass.query_types, [op]: e.target.value } } }))} placeholder={HYPERGLASS_DEFAULT_QUERY_TYPES[op]}/></label></div>}</OperationEditor>
        </> : <>
        <div className="form-grid two"><label><span>Timeout (segundos)</span><input type="number" min="2" max="120" value={form.http.timeout} onChange={e => setForm({ ...form, http: { ...form.http, timeout: +e.target.value } })}/></label><label className="check-row"><input type="checkbox" checked={form.http.verify_tls} onChange={e => setForm({ ...form, http: { ...form.http, verify_tls: e.target.checked } })}/> Validar certificado TLS</label></div>
        <OperationEditor ops={ops} configured={form.http.requests} onToggle={toggleHttpOp}>{op => {
          const req = form.http.requests[op]!;
          const update = (next: Partial<RequestConfig>) => setForm((prev: any) => ({ ...prev, http: { ...prev.http, requests: { ...prev.http.requests, [op]: { ...prev.http.requests[op]!, ...next } } } }));
          return <div className="operation-fields">
            <div className="method-url"><select value={req.method} onChange={e => update({ method: e.target.value as "GET" | "POST" })}><option>GET</option><option>POST</option></select><input required value={req.url} onChange={e => update({ url: e.target.value })} placeholder="https://lg.exemplo.net/api/ping/{target}"/></div>
            <div className="form-grid two">
              <label><span>Query params (JSON)</span><textarea value={req.queryText ?? JSON.stringify(req.query || {}, null, 2)} onChange={e => update({ queryText: e.target.value })} /></label>
              <label><span>Headers (JSON)</span><textarea value={req.headersText ?? JSON.stringify(req.headers || {}, null, 2)} onChange={e => update({ headersText: e.target.value })} /></label>
              <label><span>Tipo de resposta</span><select value={req.response_type} onChange={e => update({ response_type: e.target.value as "text" | "json" })}><option value="text">Texto / HTML</option><option value="json">JSON</option></select></label>
              <label><span>JSON path (opcional)</span><input value={req.json_path || ""} onChange={e => update({ json_path: e.target.value })} placeholder="data.result.output"/></label>
            </div>
            {req.method === "POST" && <div className="form-grid two">
              <label><span>Formato do corpo</span><select value={req.body_type} onChange={e => update({ body_type: e.target.value as "json" | "form" | "text" })}><option value="json">JSON</option><option value="form">Formulário</option><option value="text">Texto</option></select></label>
              <label><span>Corpo {req.body_type === "text" ? "" : "(JSON)"}</span><textarea value={req.bodyText ?? (req.body_type === "text" ? String(req.body || "") : JSON.stringify(req.body || {}, null, 2))} onChange={e => update({ bodyText: e.target.value })} placeholder={req.body_type === "text" ? "target={target}" : '{ "target": "{target}" }'} /></label>
            </div>}
            <label><span>Regex de extração (opcional, grupo 1)</span><input value={req.output_regex || ""} onChange={e => update({ output_regex: e.target.value })} placeholder={'<pre[^>]*>([\\s\\S]*?)</pre>'}/></label>
          </div>;
        }}</OperationEditor>
        </>}
      </> : <>
        <div className="form-grid three"><label><span>Host</span><input required value={form.telnet.host} onChange={e => setForm({ ...form, telnet: { ...form.telnet, host: e.target.value } })} placeholder="lg.exemplo.net"/></label><label><span>Porta</span><input type="number" min="1" max="65535" value={form.telnet.port} onChange={e => setForm({ ...form, telnet: { ...form.telnet, port: +e.target.value } })}/></label><label><span>Timeout</span><input type="number" min="2" max="120" value={form.telnet.timeout} onChange={e => setForm({ ...form, telnet: { ...form.telnet, timeout: +e.target.value } })}/></label></div>
        <div className="form-grid two"><label><span>Usuário (opcional)</span><input value={form.telnet.username} onChange={e => setForm({ ...form, telnet: { ...form.telnet, username: e.target.value } })}/></label><label><span>Senha {form.telnet.has_password ? "(deixe vazio para manter)" : "(opcional)"}</span><input type="password" value={form.telnet.password} onChange={e => setForm({ ...form, telnet: { ...form.telnet, password: e.target.value } })}/></label><label><span>Regex do prompt de usuário</span><input value={form.telnet.username_prompt} onChange={e => setForm({ ...form, telnet: { ...form.telnet, username_prompt: e.target.value } })}/></label><label><span>Regex do prompt de senha</span><input value={form.telnet.password_prompt} onChange={e => setForm({ ...form, telnet: { ...form.telnet, password_prompt: e.target.value } })}/></label><label><span>Regex do prompt final</span><input value={form.telnet.prompt_regex} onChange={e => setForm({ ...form, telnet: { ...form.telnet, prompt_regex: e.target.value } })}/></label><label><span>Outros pré-comandos (um por linha)</span><textarea value={form.telnet.pre_commands_text || ""} onChange={e => setForm({ ...form, telnet: { ...form.telnet, pre_commands_text: e.target.value } })} placeholder="terminal width 0"/></label></div>
        <label className="pagination-option"><span>Desativar paginação</span><div className="select-wrap"><select value={form.telnet.paging_command || ""} onChange={e => setForm({ ...form, telnet: { ...form.telnet, paging_command: e.target.value } })}>{PAGING_PRESETS.map(option => <option value={option.value} key={option.value}>{option.label}</option>)}</select><ChevronDown size={16}/></div><small>Escolha conforme a CLI quando a saída parar em <code>--More--</code> ou <code>---(more)---</code>.</small></label>
        <OperationEditor ops={ops} configured={form.telnet.commands} onToggle={toggleTelnetOp}>{op => <div className="operation-fields">
          <label><span>Comando padrão — use <code>{'{target}'}</code></span><input required value={form.telnet.commands[op] || ""} onChange={e => setForm((prev: any) => ({ ...prev, telnet: { ...prev.telnet, commands: { ...prev.telnet.commands, [op]: e.target.value } } }))} placeholder={op === "ping" ? "ping {target} count 5" : op === "traceroute" ? "traceroute {target}" : op === "bgp_community" ? "show bgp community {target}" : op === "bgp_aspath" ? "show bgp regexp {target}" : "show bgp ipv4 unicast {target}"}/></label>
          {(op === "ping" || op === "traceroute" || op === "bgp") && <button type="button" className={`v6-command-toggle ${form.telnet.commands_v6[op] !== undefined ? "enabled" : ""}`} onClick={() => toggleTelnetV6(op)}>{form.telnet.commands_v6[op] !== undefined ? <Check size={15}/> : <Plus size={15}/>} {form.telnet.commands_v6[op] !== undefined ? "Comando IPv6 habilitado" : "Usar comando IPv6 diferente"}</button>}
          {(op === "ping" || op === "traceroute" || op === "bgp") && form.telnet.commands_v6[op] !== undefined && <label><span>Comando IPv6 — use <code>{'{target}'}</code></span><input required value={form.telnet.commands_v6[op] || ""} onChange={e => setForm((prev: any) => ({ ...prev, telnet: { ...prev.telnet, commands_v6: { ...prev.telnet.commands_v6, [op]: e.target.value } } }))} placeholder={op === "ping" ? "ping ipv6 {target} count 5" : op === "traceroute" ? "traceroute ipv6 {target}" : "show bgp ipv6 unicast {target}"}/></label>}
        </div>}</OperationEditor>
      </>}
      <footer><button type="button" className="secondary" onClick={onClose}>Cancelar</button><button className="primary" disabled={saving}>{saving && <Loader2 className="spin" size={17}/>} Salvar Looking Glass</button></footer>
    </form>
  </div></div>;
}

function OperationEditor({ ops, configured, onToggle, children }: { ops: Operation[]; configured: Partial<Record<Operation, any>>; onToggle: (op: Operation) => void; children: (op: Operation) => React.ReactNode }) {
  return <div className="operations-editor"><div className="section-label">Operações disponíveis</div>{ops.map(op => <div className={`op-editor ${configured[op] !== undefined ? "open" : ""}`} key={op}><button type="button" className="op-toggle" onClick={() => onToggle(op)}><span>{operationIcon(op)} {operationLabel(op)}</span><span className={configured[op] !== undefined ? "configured" : "not-configured"}>{configured[op] !== undefined ? "Configurado" : "Não configurado"}</span></button>{configured[op] !== undefined && <div className="op-content">{children(op)}</div>}</div>)}</div>;
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><App/></React.StrictMode>);
