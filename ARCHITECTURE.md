# Architecture — ChatGPT–Codex Bridge v0.1

## Arquitectura aprobada

```text
MCP Adapter
    ↓
Bridge Core
    ├─ Projects
    ├─ Tasks
    ├─ Policy / Authorizations
    ├─ Audit state
    └─ Observability
    ↓
Executor Contract
    ↓
CodexExecutor
    ↓
codex app-server
```

## Responsabilidades

### MCP Adapter

Expone el punto de integración del Bridge. No conoce el protocolo Codex ni contiene reglas específicas del executor.

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

- **Transporte v0.1:** `stdio://`.
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

### Correlación de identidades

```text
Bridge Task
    ├─ thread_id → referencia Codex
    └─ turn_id   → referencia Codex
```

`task_id`, `thread_id` y `turn_id` son identidades distintas y pertenecen a dominios diferentes.
