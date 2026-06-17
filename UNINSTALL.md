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
