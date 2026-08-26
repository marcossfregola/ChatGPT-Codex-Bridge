# Security — ChatGPT–Codex Bridge

## Infraestructura protegida

El ChatGPT–OpenCode Bridge existente es independiente y protegido. No debe modificarse, detenerse, moverse, copiarse ni reutilizar sus carpetas, locks, PIDs, MCP, túneles o integración de Telegram.

## Principio mínimo

Aplicar el menor privilegio posible para cada operación.

## Default Codex

- `cwd` explícito.
- Workspace explícito.
- `workspace-write` únicamente cuando la tarea requiera escritura.
- `read-only` para inspección.
- `approvalPolicy=on-request`.
- `reviewer=user`.
- Nunca `danger-full-access` por defecto.

## Operaciones no autorizadas por una tarea normal

Las siguientes operaciones requieren autorización expresa cuando correspondan:

- commit;
- push;
- tag;
- release;
- merge;
- rebase;
- reset destructivo;
- force push;
- eliminación destructiva;
- instalación o desinstalación;
- cambios globales sensibles;
- tocar el Bridge OpenCode existente.

## Credenciales

Nunca:

- leer el contenido de `auth.json`;
- copiar `auth.json`;
- registrar credenciales;
- enviar tokens a logs;
- usar una API key como requisito del MVP.

## Logs y evidencia

Los logs y artefactos de evidencia deben redactar secretos, tokens, cookies, credenciales y datos personales innecesarios.
