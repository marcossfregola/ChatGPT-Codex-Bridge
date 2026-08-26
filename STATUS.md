# Status — ChatGPT–Codex Bridge

```text
Proyecto: ChatGPT–Codex Bridge
Fase: MCP SDK oficial v2 + Bridge Core + CodexExecutor + journal durable
Última etapa aprobada: 1E-B-R1
Etapa actual: 1F-B — runtime Secure MCP Tunnel independiente preparado
Implementación: MCPServer oficial v2 sobre stdio, MCPAdapter separado, ejecución Codex asíncrona y Event Journal SQLite v2
Repositorio local: creado en esta etapa
GitHub: no creado
Commit de 1A: 25082dd67aecb5e1b58cae4152978025e4cf4fc3
Commit de 1B: 0d0e69a95795266c7256d3c862a072e64326bb0c
Commit de 1C: d5e385f837861002a724519450f439b8b3ba69ab
Commit 1D: 17d19d71ba2227b933266667e031fad4d175e66e
1E-A: completada y sellada en a1c83a972b87d799bcc351d123149fae762d4852
1E-B: completada y sellada; wire manual retirado
1E-B-R1: SDK MCP Python v2 instalado en `.venv`, DB default estable en `%LOCALAPPDATA%\ChatGPTCodexBridge\state\bridge.sqlite3`, fake/real E2E y auditoría cerrados
1F-B: estructura y copias independientes preparadas bajo `%LOCALAPPDATA%\ChatGPTCodexBridge`; perfil con tunnel ID autorizado, MCP stdio, health `127.0.0.1:8877`, readiness `/readyz` y scripts start/stop/doctor agregados al repositorio
```

La dependencia `mcp>=2,<3` se instala únicamente en el `.venv` del proyecto.
El proceso conserva stdio como transporte local y acepta `--db-path` para
tests y laboratorios. El doctor y el arranque real del túnel requieren la
identidad Windows normal que creó la credencial DPAPI y se entregan como
operación manual. Codex no intenta descifrar la credencial bajo su identidad de
sandbox, no inicia el túnel y no crea todavía el complemento ChatGPT.

El runtime 1F-B usa exclusivamente el tunnel ID no secreto
`tunnel_6a8ef626bf008191a6294996145747e5` y permanece separado del
ChatGPT–OpenCode Bridge. La clave de runtime no se documenta ni se incorpora
al repositorio.

## Descubrimientos 0A–0C

- app-server viable;
- stdio probado;
- Luna probado;
- turns, events, approvals, interrupt y resume probados;
- sesión ChatGPT existente reutilizable;
- sandbox read-only y workspace-write probado.
