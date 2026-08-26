# Decisions — ChatGPT–Codex Bridge

Todas las decisiones siguientes tienen estado `ACTIVE` y forman parte del baseline de la Etapa 1A.

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
