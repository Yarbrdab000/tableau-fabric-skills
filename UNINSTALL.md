# Uninstalling the Tableau → Fabric skills

Counterpart to [`INSTALL.md`](INSTALL.md). Use the path that matches how you installed.

## Plugin install (the recommended path)

Remove the plugin, then unregister the marketplace:

```text
/plugin uninstall tableau-fabric-skills
/plugin marketplace remove tableau-collection
```

…or from your terminal:

```bash
copilot plugin uninstall tableau-fabric-skills
copilot plugin marketplace remove tableau-collection
```

> If `marketplace remove` reports that plugins from it are still installed, add `--force` to
> remove the marketplace and uninstall its plugins in one step:
> `copilot plugin marketplace remove tableau-collection --force`.

Start a new session so the change takes effect, then confirm with `/plugin list` (it should no
longer list `tableau-fabric-skills`).

## Manual folder install (older clients only)

If you previously copied the skill folders by hand, delete them:

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.copilot\skills\tableau-datasource-profiler"
Remove-Item -Recurse -Force "$env:USERPROFILE\.copilot\skills\tableau-mcp-landing-zone"
Remove-Item -Recurse -Force "$env:USERPROFILE\.copilot\skills\tableau-migration"
```

```bash
rm -rf ~/.copilot/skills/tableau-datasource-profiler \
       ~/.copilot/skills/tableau-mcp-landing-zone \
       ~/.copilot/skills/tableau-migration
# Claude Code equivalents:
rm -rf ~/.claude/skills/tableau-datasource-profiler \
       ~/.claude/skills/tableau-mcp-landing-zone \
       ~/.claude/skills/tableau-migration
```

Restart your agent. Done.

## Clean up what removal leaves behind

Removing the plugin or the skill folders stops the skills from loading, but two of them create
resources **outside** the skill folder that you must remove yourself.

### `tableau-mcp-landing-zone`

- **Deployed Azure landing zone — keeps billing until removed.** See
  [deploy-azure.md § Tear down](skills/tableau-mcp-landing-zone/resources/deploy-azure.md). If it has
  its own resource group:

  ```powershell
  az group delete --name <your-resource-group> --yes --no-wait
  ```

  That removes the Container App, its `*-env` managed environment, the Log Analytics workspace, and
  any Key Vault the deploy created. If the group also holds resources you keep, delete just the
  Container App and its `*-env` managed environment instead.
- **MCP client wiring.** Removing the deployment doesn't remove the client config pointing at it:
  `claude mcp remove tableau` (Claude Code), or delete the `tableau` server entry from `.mcp.json`
  (project), `~/.claude.json`, or `~/.copilot/mcp-config.json`.
- **Copilot Studio.** In Power Apps, delete the **Tableau MCP** custom connector and its connection.
- **Local dev stack.** If you ran the Docker harness, from
  `skills/tableau-mcp-landing-zone/assets/local/` run `docker compose down -v` and delete the local
  `.env` you filled in.

### `tableau-migration`

These live outside the skill folder, so they survive uninstall — delete them yourself: downloaded
**sensitive** Tableau artifacts (`*.tds` / `*.tdsx` / `*.twb` / `*.twbx` / `*.hyper`), migration
output bundles you generated (`semantic_models/`, `pbip/`, `report.json`, `summary.md`), and any
self-update **backups** (`*.bak-<timestamp>` folders next to a previous install).

### `tableau-datasource-profiler`

Its only non-stdlib dependency is `requests`. Remove it **only** if nothing else needs it:
`pip uninstall requests`.

> **Secret hygiene.** While cleaning up, scrub any real `.env`, Tableau PAT, Connected App secret, or
> sidecar API key, plus the downloaded artifacts above. See [`SECURITY.md`](SECURITY.md).
