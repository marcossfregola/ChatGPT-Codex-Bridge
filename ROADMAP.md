# Roadmap — ChatGPT–Codex Bridge

El roadmap distingue lo que ya está implementado del trabajo futuro que sólo
se abrirá si aparece una necesidad real.

## Completado MVP

### 1A — Bootstrap

Repositorio, documentación base y límites de ownership.

### 1B — Codex app-server

Executor local por `stdio://`, initialize, account/read, threads, turns,
notificaciones, aprobaciones, interrupt y cierre.

### 1C — Projects/Tasks y SQLite

Modelo persistente, foreign keys, schema versionado y reapertura durable.

### 1D — Event Journal

Journal SQLite append-only con `event_id`, source, kind, payload y timestamp.

### 1E-A — Core y Executor Contract

Bridge Core, contrato async, CodexExecutor, correlación thread/turn y
persistencia de eventos durante la ejecución.

### 1E-B — MCP oficial

MCP Python SDK v2, MCPAdapter separado y exactamente siete tools.

### 1F-B — Secure MCP Tunnel

Runtime independiente bajo `%LOCALAPPDATA%\ChatGPTCodexBridge`, perfil propio,
DPAPI, health `/readyz` y scripts start/stop/doctor.

### 1F-C — Integración ChatGPT E2E

Complemento operativo ChatGPT–Codex Bridge D2 y flujo ChatGPT → túnel → MCP →
Bridge validado externamente.

### 1F-D1 — Lifecycle y recovery

Terminalidad atómica, recuperación de Tasks `RUNNING` huérfanas y
single-instance por SQLite.

### 1F-D2 — AUTONOMOUS_WRITE

Git checkpoint/postflight, protected roots, policy contractual y detección de
cambios de branch/HEAD.

### 1F-D2-CONT / R2 — Continuation

Continuación sólo sobre el estado Git durable y fingerprints exactos, con
rechazo conservador de divergencias.

### 1F-D3 — E2E adaptativa

Cadena externa de tres Tasks, definiendo cada siguiente Task después de la
auditoría de la anterior.

### 1F-D4 — Long-run

Task externa de aproximadamente 75 segundos de espera real y retorno normal.

La evidencia E2E D3/D4 está documentada en `STATUS.md`; no es un test
reproducible de la suite local.

## Cierre documental

1G-B actualiza la documentación viva para reflejar el MVP, sus límites, la
seguridad real y el quickstart operativo. No modifica código, schemas, tools,
runtime ni dependencias.

## Dogfooding siguiente

El siguiente uso real es el desarrollo controlado del **ComfyUI Orchestrator**
en un repositorio independiente. No se crea ni se incorpora ese repositorio
automáticamente.

## Futuro sólo si existe necesidad real

Podrían considerarse, sin compromiso ni fecha:

- `post_audit` y aprobación durable;
- persistent Codex threads;
- dashboard;
- scheduler o notificaciones;
- multi-executor/OpenCode;
- retries;
- rollback explícito;
- métricas avanzadas;
- GitHub automation;
- timeout total de Task;
- E2E adicional de desconexión y crash.

Estas posibilidades no forman parte del MVP ni deben tratarse como
implementadas.
