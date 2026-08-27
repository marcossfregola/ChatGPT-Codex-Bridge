# Decisions — ChatGPT–Codex Bridge

Todas las decisiones siguientes tienen estado `ACTIVE` y forman parte del baseline vigente del proyecto.

## D-001 — Codex como executor inicial

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** 0A/0B/0C
- **Decisión:** Codex será el executor inicial.
- **Motivo:** Las pruebas previas verificaron el app-server y sus eventos necesarios.

## D-002 — Executor Contract

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** decisión de arquitectura
- **Decisión:** Codex estará detrás de un Executor Contract.
- **Motivo:** Permite separar el Core del transporte y del proveedor de ejecución.

## D-003 — codex app-server

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** 0B/0C
- **Decisión:** `codex app-server` es la interfaz seleccionada inicialmente.
- **Motivo:** Fue la interfaz local comprobada para initialize, threads, turns y eventos.

## D-004 — Transporte stdio

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** 0B/0C
- **Decisión:** El transporte inicial será `stdio://`.
- **Motivo:** Es local, explícito y suficiente para el MVP.

## D-005 — Luna como modelo habitual

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** 0C
- **Decisión:** Luna será el modelo habitual; Terra queda disponible para tareas más complejas.
- **Motivo:** Luna fue probado en el flujo controlado y cubre el uso habitual previsto.

## D-006 — Sol fuera del uso normal

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** decisión de arquitectura
- **Decisión:** Sol dentro de Codex no se utilizará normalmente.
- **Motivo:** No forma parte del camino habitual aprobado para el MVP.

## D-007 — Ownership de Codex

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** 0C
- **Decisión:** Codex es dueño de autenticación, `CODEX_HOME`, sesiones y rollouts.
- **Motivo:** La sesión existente se reutiliza sin duplicar credenciales ni estado interno.

## D-008 — El Bridge no manipula credenciales

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** 0C
- **Decisión:** El Bridge no manipula credenciales Codex.
- **Motivo:** Reduce exposición y mantiene una sola autoridad de autenticación.

## D-009 — Estados separados

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** decisión de arquitectura
- **Decisión:** El estado de ejecución y el estado de auditoría están separados.
- **Motivo:** `FINISHED` no implica `APPROVED`.

## D-010 — Git sensible requiere autorización

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** decisión de seguridad
- **Decisión:** Una tarea común no autoriza operaciones Git sensibles.
- **Motivo:** Commit, push, reset destructivo y operaciones equivalentes cambian estado durable.

## D-011 — Bridge OpenCode independiente

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** 0A/0B/0C
- **Decisión:** El ChatGPT–OpenCode Bridge v0.4.2 permanece totalmente independiente y protegido.
- **Motivo:** Es infraestructura existente fuera del alcance de este repositorio.

## D-012 — MVP orientado al Orquestador ComfyUI

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** decisión de arquitectura
- **Decisión:** El MVP prioriza arquitectura preparada para crecer, con implementación mínima para desarrollar el Orquestador ComfyUI.
- **Motivo:** Mantiene el primer consumidor concreto sin adelantar complejidad de versiones posteriores.


## D-013 — Python 3.13 como lenguaje inicial

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1B
- **Decisión:** Python 3.13 es el lenguaje inicial del Bridge v0.1.
- **Motivo:** Está disponible localmente y cubre subprocess, stdio JSON, SQLite y testing con biblioteca estándar.

## D-014 — stdlib-first para app-server

- **Fecha:** 2026-08-25
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1B
- **Decisión:** La integración inicial con `app-server` utilizará biblioteca estándar de Python.
- **Motivo:** El spike no demostró una necesidad de dependencias runtime externas.
## D-015 — app-server aislado detrás del Executor

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1C
- **Decisión:** `app-server` es una interfaz que puede evolucionar; sus detalles de protocolo no deben filtrarse al Bridge Core. El `CodexExecutor` funciona como frontera de compatibilidad.
- **Motivo:** Mantiene aislado el transporte y permite evolucionar el dominio Bridge sin acoplarlo al protocolo Codex.

## D-016 — Capacidades esenciales sin experimentalApi

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1C
- **Decisión:** El MVP no dependerá de `experimentalApi` para capacidades esenciales. Capacidades experimentales sólo podrán incorporarse posteriormente si existe necesidad concreta y fallback seguro.
- **Motivo:** El flujo esencial de 1B funciona sin solicitar capabilities experimentales.

## D-017 — SQLite para estado propio del MVP

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1C
- **Decisión:** El estado propio inicial del Bridge se persistirá localmente con SQLite mediante Python stdlib.
- **Motivo:** 1C demuestra mediante tests schema versionado, transacciones, foreign keys, roundtrips y reapertura real.

## D-018 — Task y thread/turn son identidades distintas

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1C
- **Decisión:** Task pertenece al dominio Bridge. Thread/Turn pertenecen a Codex y se almacenan sólo como referencias/correlaciones. No se duplican rollouts Codex.
- **Motivo:** Evita confundir el estado propio del Bridge con la identidad y el historial interno del executor.

## D-019 — Journal durable append-only

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1D
- **Decisión:** Los eventos observables propios del Bridge se almacenan inicialmente en un journal SQLite append-only asociado a Task.
- **Motivo:** El journal preserva evidencia estructurada y orden de inserción sin inferir progreso ni duplicar rollouts Codex.

## D-020 — Bridge Core depende del Executor Contract

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1E-A
- **Decisión:** Bridge Core depende únicamente de SQLiteBridgeStore y del Executor Contract, no del wire protocol de Codex.
- **Motivo:** Mantiene el dominio y la orquestación independientes del transporte Codex.

## D-021 — Evidencia Codex se persiste durante ejecución

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1E-A
- **Decisión:** Las notificaciones Codex y la correlación thread/turn se persisten mientras la ejecución está en curso.
- **Motivo:** La evidencia durable debe existir antes de la finalización del turn y permitir auditar el orden real.

## D-022 — Official MCP Python SDK owns the MCP protocol layer

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1E-B-R1
- **Decisión:** El MCP inicial utilizará el MCP Python SDK oficial v2. `MCPServer` será dueño del protocolo MCP, lifecycle, negociación de versión, JSON-RPC, schemas, framing y transporte stdio. `MCPAdapter` seguirá siendo la frontera de aplicación y todas las operaciones de dominio pasarán por Bridge Core.
- **Motivo:** Evita reimplementar wire protocol y conserva el desacoplamiento entre el protocolo MCP, el dominio Bridge y `codex app-server`.

## D-023 — Bridge persistent state uses a stable application-local path

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1E-B-R1
- **Decisión:** La base default del Bridge se resolverá en `%LOCALAPPDATA%\ChatGPTCodexBridge\state\bridge.sqlite3`, independientemente del cwd. `--db-path` quedará disponible para tests y laboratorios.
- **Motivo:** Evita que cambiar el directorio de trabajo cree otra base y mantiene el estado del Bridge separado de `VisorVideosDevBridge`, `ChatGPTOpenCodeBridge` y el repositorio.

## D-024 — Secure MCP Tunnel independiente para ChatGPT–Codex Bridge

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1F-B
- **Decisión:** El runtime del ChatGPT–Codex Bridge vive bajo `%LOCALAPPDATA%\ChatGPTCodexBridge`, con perfil, logs, PID, health y binarios propios. Usa exclusivamente el tunnel ID autorizado `tunnel_6a8ef626bf008191a6294996145747e5` y no reutiliza rutas ni procesos del ChatGPT–OpenCode Bridge.
- **Motivo:** Mantiene dos bridges operativamente aislados y permite validar el camino ChatGPT → Secure MCP Tunnel → MCP oficial → Bridge sin alterar infraestructura existente.

## D-025 — Frontera DPAPI bajo identidad propietaria

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1F-B
- **Decisión:** La credencial del runtime se referencia como `env:CONTROL_PLANE_API_KEY`. Los scripts la recuperan mediante `ConvertTo-SecureString` sólo bajo la identidad Windows que creó el archivo DPAPI, la mantienen en memoria y la entregan únicamente al proceso hijo. Codex no intenta descifrarla, no copia la clave y no persiste plaintext.
- **Motivo:** DPAPI CurrentUser es una frontera de seguridad intencional; separar el usuario normal del sandbox evita ampliar privilegios o exponer el secreto.

## D-026 — Readiness local como criterio de arranque

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1F-B
- **Decisión:** El runtime usa health `127.0.0.1:8877`, health URL y PID propios. `start_mcp_tunnel.ps1` sólo informa listo cuando `/readyz` responde HTTP 200; `doctor_mcp_tunnel.ps1` queda como operación manual y `stop_mcp_tunnel.ps1` limita la detención a procesos inequívocamente asociados.
- **Motivo:** Evita declarar disponibilidad por un archivo o proceso aislado y mantiene el ciclo de vida comprobable y reversible.

## D-027 — Complemento ChatGPT posterior al runtime

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1F-B
- **Decisión:** No se crea ni registra todavía ningún complemento ChatGPT. La etapa 1F-C comienza sólo después de validar manualmente doctor, túnel, health/readiness, MCP y aislamiento.
- **Motivo:** La frontera local debe quedar comprobada antes de introducir interacción externa o configuración de ChatGPT.

## D-028 — Continuación AUTONOMOUS_WRITE por baseline Git durable

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1F-D2-CONT
- **Decisión:** Una tarea `AUTONOMOUS_WRITE` sólo puede continuar sobre un worktree
  dirty cuando el último postflight durable del mismo Project pertenece a una
  tarea `FINISHED` sin `policy_violation`, y branch, HEAD, estado staged/unstaged,
  paths untracked, diffs y huellas SHA-256 de contenido para todos los paths
  dirty con contenido coinciden exactamente.
  Un baseline limpio conserva el comportamiento existente.
- **Limitación:** `audit_status` continúa en `PENDING`: no existe una tool de
  auditoría ChatGPT. La autorización práctica para crear la siguiente Task
  sigue siendo la decisión de ChatGPT; el Bridge sólo verifica que el estado
  Git no haya cambiado desde el postflight conocido.
- **Motivo:** Permite encadenar Tasks sin commit, stash, reset, clean ni snapshots
  generales, rechazando cualquier divergencia antes de invocar el executor.

## D-029 — Cierre MVP con AUTONOMOUS_WRITE pragmático

- **Fecha:** 2026-08-26
- **Estado:** `ACTIVE`
- **Origen:** Etapa 1G-A
- **Decisión:** El MVP adopta `AUTONOMOUS_WRITE` pragmático únicamente para
  repositorios locales controlados, con `approvalPolicy=never` y
  `sandbox=danger-full-access`.
- **Motivación:** `workspace-write` con aprobaciones fail-closed impedía la
  programación autónoma; un staged workspace agregaba complejidad sin resolver
  el problema; D3 demostró autonomía real; y Git checkpoint/postflight aporta
  detección y recuperabilidad, no aislamiento.
- **Riesgo aceptado:** Protected roots no son un sandbox fuerte. Una ejecución
  con `danger-full-access` no ofrece aislamiento adversarial del host y exige
  auditoría postflight.
- **Repositorio válido:** Un repositorio local persistente y recuperable, como
  `C:\Codex\ComfyUI-Orchestrator`, es compatible con este modo; no se exige que
  el proyecto real sea descartable, sólo que esté controlado y fuera de los
  protected roots.
- **Veredicto:** `MVP APTO PARA USO REAL CONTROLADO`.
- **No se declara:** autonomía desatendida permanente ni seguridad adversarial.
- **Siguiente paso:** dogfooding con el ComfyUI Orchestrator y hardening guiado
  por incidentes reales.
