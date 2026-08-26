# Project — ChatGPT–Codex Bridge

## Objetivo

Construir un Bridge local, auditable y de mínimo privilegio que coordine tareas técnicas entre ChatGPT y Codex local, preserve la evidencia de ejecución y mantenga separadas las autoridades de cada sistema.

El repositorio formal pasa a ser la fuente de verdad viva del proyecto una vez aprobado este baseline.

## Roles y autoridades

- **Usuario:** autoridad final para alcance, autorizaciones y aprobación.
- **ChatGPT:** director técnico, arquitecto y auditor.
- **Codex:** executor técnico local.
- **Bridge:** coordinación, transporte, estado propio, seguridad y observabilidad.

## Alcance MVP v0.1

- Proyectos y tareas con estados explícitos.
- Un `CodexExecutor` detrás de un contrato de executor.
- Inicio controlado de `codex app-server` por `stdio://`.
- Correlación de project, task, thread y turn.
- Política explícita de workspace, sandbox y aprobación.
- Contrato para eventos operativos y evidencia sin secretos; el event journal completo queda en la Etapa 1D.
- Fallback manual cuando el transporte automático no esté disponible.

## Fuera de alcance

- Implementación de una GUI.
- Telegram, GitHub, releases o despliegues remotos.
- Modificación o reutilización del ChatGPT–OpenCode Bridge existente.
- Lectura, copia o almacenamiento de credenciales Codex.
- API paga obligatoria o proveedor alternativo obligatorio.
- Multi-executor antes de validar el MVP con Codex.

## Metodología

```text
problema
→ inspección
→ diagnóstico
→ diseño
→ implementación
→ pruebas
→ evidencia
→ auditoría
→ aprobación
→ commit
```

No se considera una implementación terminada hasta que exista evidencia reproducible y aprobación explícita.

## Fallback manual

Si el Bridge o el transporte no están disponibles, el usuario puede ejecutar la tarea mediante Codex de forma manual y devolver al proyecto la evidencia y el resultado para auditoría.

## Restricción económica

El MVP debe funcionar con la sesión ChatGPT existente, el cupo disponible de Codex y herramientas locales. No debe exigir una API paga para su funcionamiento básico.
