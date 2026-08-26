# Status — ChatGPT–Codex Bridge

```text
Proyecto: ChatGPT–Codex Bridge
Fase: MCP SDK oficial v2 + Bridge Core + CodexExecutor + journal durable
Última etapa aprobada: 1E-A
Etapa actual: 1E-B-R1
Implementación: MCPServer oficial v2 sobre stdio, MCPAdapter separado, ejecución Codex asíncrona y Event Journal SQLite v2
Repositorio local: creado en esta etapa
GitHub: no creado
Commit de 1A: 25082dd67aecb5e1b58cae4152978025e4cf4fc3
Commit de 1B: 0d0e69a95795266c7256d3c862a072e64326bb0c
Commit de 1C: d5e385f837861002a724519450f439b8b3ba69ab
Commit 1D: 17d19d71ba2227b933266667e031fad4d175e66e
1E-A: completada y sellada en a1c83a972b87d799bcc351d123149fae762d4852
1E-B: implementación local staged; wire manual retirado
1E-B-R1: SDK MCP Python v2 instalado en `.venv`, DB default estable en `%LOCALAPPDATA%\ChatGPTCodexBridge\state\bridge.sqlite3`, fake/real E2E y auditoría pendientes de cierre
```

La dependencia `mcp>=2,<3` se instala únicamente en el `.venv` del proyecto.
El proceso conserva stdio como transporte local y acepta `--db-path` para
tests y laboratorios. El túnel y la conexión con ChatGPT siguen pendientes.

## Descubrimientos 0A–0C

- app-server viable;
- stdio probado;
- Luna probado;
- turns, events, approvals, interrupt y resume probados;
- sesión ChatGPT existente reutilizable;
- sandbox read-only y workspace-write probado.
