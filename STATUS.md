# Status — ChatGPT–Codex Bridge

```text
Proyecto: ChatGPT–Codex Bridge
Versión: 0.1.0
Estado: MVP APTO PARA USO REAL CONTROLADO
Rama: main
HEAD técnico base: 389ef55928415470e68309ef01763261439a0cd9
Suite: 125 tests OK
```

## Etapas completadas

- 1A — bootstrap.
- 1B — Codex app-server executor.
- 1C — Projects/Tasks y persistencia SQLite.
- 1D — Event Journal durable.
- 1E-A — Bridge Core, Executor Contract y CodexExecutor.
- 1E-B — MCP Bridge.
- 1F-B — Secure MCP Tunnel independiente.
- 1F-C / E2E real — complemento y flujo ChatGPT → túnel → MCP.
- D1 — lifecycle, recovery y single-instance.
- D2 — `AUTONOMOUS_WRITE` y Git checkpoint/postflight.
- D2-CONT — continuación sobre estado Git verificado.
- D2-CONT-R2 — baseline limpio y fallos terminales.
- D3 — cadena adaptativa multi-Task.
- D4 — long-run controlado.

## MCP vigente

El servidor usa el MCP Python SDK oficial v2 y expone exactamente siete tools:

```text
get_status
create_project
create_task
run_task
get_task
get_task_events
get_result
```

Los modos de Task son:

```text
READ_ONLY
AUTONOMOUS_WRITE
```

No existe `post_audit` ni una tool pública de cancelación. `audit_status` queda
en `PENDING` hasta que exista una auditoría externa o una capacidad futura.

## Evidencia E2E externa

La siguiente evidencia fue obtenida externamente y se documenta aquí para el
cierre; no fue reproducida por la suite unitaria del repositorio.

### D3 adaptativa

Se completaron tres Tasks `AUTONOMOUS_WRITE` en cadena:

```text
Task A → auditoría
Task B definida después de la auditoría A → auditoría
Task C definida después de la auditoría B → auditoría

TASK_B_DEFINED_AFTER_A_AUDIT=true
TASK_C_DEFINED_AFTER_B_AUDIT=true
USER_INTERVENTION_BETWEEN_TASKS=0
TASKS_COMPLETED_AUTONOMOUSLY=3
```

### D4 long-run

```text
task-1f-d4-long-real
espera real: 75.160 s
Task total: aproximadamente 94 s
resultado: D4_LONG_OK
run_task retornó normalmente
USER_INTERVENTION=0
```

## Runtime

El runtime se instala de forma independiente bajo:

```text
%LOCALAPPDATA%\ChatGPTCodexBridge
```

La base default es
`%LOCALAPPDATA%\ChatGPTCodexBridge\state\bridge.sqlite3`. El readiness del
túnel se verifica con `http://127.0.0.1:8877/readyz` HTTP 200. Los scripts
versionados son `scripts/start_mcp_tunnel.ps1`, `scripts/stop_mcp_tunnel.ps1`
y `scripts/doctor_mcp_tunnel.ps1`.

El complemento operativo es **ChatGPT–Codex Bridge D2**. El complemento
original conserva un schema MCP cacheado anterior a `TaskMode`; no debe
borrarse ni modificarse destructivamente.

## Limitaciones MVP

- No hay timeout total de Task; el timeout de turno es de inactividad de 300 s.
- No hay E2E real de desconexión ChatGPT/MCP.
- No se ha probado un crash real durante Luna.
- `audit_status` permanece `PENDING` y no existe `post_audit`.
- El Bridge no puede despertar ChatGPT espontáneamente.
- `AUTONOMOUS_WRITE` usa `approvalPolicy=never` y `sandbox=danger-full-access`.
- Los protected roots son policy, no un sandbox adversarial.
- No hay rollback automático ni retries complejos.
- El stop script conserva un race benigno cuando el proceso ya terminó.
- `WAITING_USER` existe en el modelo, pero no tiene flujo activo.

## Próximo uso

El MVP queda habilitado para dogfooding controlado mediante el desarrollo del
**ComfyUI Orchestrator** en un repositorio separado. El siguiente cambio
previsto es documental y el hardening posterior debe guiarse por incidentes
reales, no por complejidad preventiva.
