# Architecture — ChatGPT–Codex Bridge v0.1

## Arquitectura aprobada

```text
Official MCP Python SDK v2 (MCPServer / stdio)
    ↓
MCPAdapter
    ↓
Bridge Core
    ↓
Executor Contract
    ↓
CodexExecutor
```

El MCP de 1E-B-R1 usa el SDK oficial MCP Python v2 como dueño del protocolo,
del ciclo de vida, la negociación de versión, JSON-RPC, schemas, framing y
transporte. `MCPAdapter` se conserva como frontera de aplicación sobre Bridge
Core y no conoce el wire protocol de MCP ni el de Codex.

## Responsabilidades

### MCP Adapter

Expone `get_status`, `create_project`, `create_task`, `run_task`, `get_task`,
`get_task_events` y `get_result`. Llama únicamente a Bridge Core y a la
persistencia para lecturas durables. No conoce el protocolo Codex ni contiene
reglas específicas del executor.

### MCP process

`chatgpt-codex-bridge-mcp` construye un único `MCPServer` oficial y lo ejecuta
con transporte local stdio. El SDK escribe exclusivamente el protocolo en
stdout, mantiene los diagnósticos en stderr y gestiona initialize,
notifications, tools, errores, framing y cierre. El wiring del proceso crea
`SQLiteBridgeStore → BridgeCore(SQLiteBridgeStore, CodexExecutor) → MCPAdapter`
→ `MCPServer`. La base por defecto queda en
`%LOCALAPPDATA%\ChatGPTCodexBridge\state\bridge.sqlite3`, independiente del
directorio de trabajo; `--db-path` permite seleccionar una base explícita para
tests y laboratorios.

### Bridge Core

Es autoridad sobre proyectos, tareas, autorizaciones, estado de auditoría y correlación operativa.

### CodexExecutor

Encapsula completamente la interacción con `codex app-server` detrás del Executor Contract. Traduce solicitudes, eventos, aprobaciones, resultados y errores sin filtrar detalles de transporte al MCP Adapter.

### Persistence

Contiene únicamente estado propio del Bridge: proyectos, tareas, correlaciones, timeline, autorizaciones, evidencia y journal de eventos. No replica `CODEX_HOME`, sesiones ni rollouts de Codex.

### Event Journal v0.1

El Bridge mantiene un journal SQLite append-only asociado a cada Task. Cada evento conserva únicamente evidencia que el Bridge decide registrar: source, kind, payload JSON, timestamp UTC y un `event_id` que fija el orden de inserción.

Codex sigue siendo dueño de autenticación, sesiones y rollouts. El journal no copia rollouts, sesiones, `CODEX_HOME` ni credenciales.

### Observability

Registra eventos operativos y evidencia reproducible con secretos redactados.

## Contratos iniciales

- **Transporte v0.1:** MCP oficial sobre stdio, provisto por MCP Python SDK v2.
- **Executor v0.1:** Codex exclusivamente.
- **Autenticación:** Codex administra su propia sesión ChatGPT y `CODEX_HOME`.

El Bridge:

- no lee `auth.json`;
- no copia `auth.json`;
- no parsea `auth.json`;
- no almacena `auth.json` ni tokens Codex.

## Ownership

Codex es dueño de autenticación, `CODEX_HOME`, sesiones, rollouts y estado interno.

Bridge es dueño de project, task, correlación thread/turn, autorizaciones, timeline, evidencia y auditoría.

## Estados de ejecución

```text
QUEUED
RUNNING
WAITING_USER
FINISHED
FAILED
CANCELLED
```

## Estados de auditoría

```text
PENDING
APPROVED
CORRECTION_REQUIRED
```

`FINISHED` no implica `APPROVED`.

Este documento fija responsabilidades y contratos de alto nivel. No diseña clases, paquetes ni APIs.

## Persistence v0.1

El estado propio inicial del Bridge se persiste localmente con SQLite mediante Python stdlib. La persistencia contiene únicamente Project, Task, estados, correlaciones y journal de eventos; no replica sesiones, rollouts ni historial de Codex.

La resolución de ruta default es explícita y estable: cambia el cwd no cambia
`%LOCALAPPDATA%\ChatGPTCodexBridge\state\bridge.sqlite3`. Las pruebas y los
laboratorios pasan `--db-path` para no escribir estado dentro del repositorio.

### Correlación de identidades

```text
Bridge Task
    ├─ thread_id → referencia Codex
    └─ turn_id   → referencia Codex
```

`task_id`, `thread_id` y `turn_id` son identidades distintas y pertenecen a dominios diferentes.
