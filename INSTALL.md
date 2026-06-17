# Installing the Tableau → Fabric skills

This is the **single source of truth** for installing, verifying, and (see
[`UNINSTALL.md`](UNINSTALL.md)) removing the three skills
(`tableau-datasource-profiler`, `tableau-mcp-landing-zone`, `tableau-migration`).

The reliable way to make these skills load is to register them as a **plugin** — current GitHub
Copilot CLI loads skills from built-in directories and from installed plugins. It does **not**
auto-scan a personal `~/.copilot/skills/` folder, so copying files there silently does nothing.

## Recommended — self-verifying installer

Clone the repo, then run the installer for your shell. It registers the marketplace, installs the
plugin, **and proves the plugin loaded** (it exits non-zero if not, so it can't fail silently):

```powershell
git clone https://github.com/Yarbrdab000/tableau-fabric-skills.git
cd tableau-fabric-skills
./install.ps1
```

```bash
git clone https://github.com/Yarbrdab000/tableau-fabric-skills.git
cd tableau-fabric-skills
./install.sh
```

## Manual — the same two commands

If you'd rather run the commands yourself, in a Copilot CLI session enter:

```text
/plugin marketplace add Yarbrdab000/tableau-fabric-skills
/plugin install tableau-fabric-skills@tableau-collection
```

…or from your terminal:

```bash
copilot plugin marketplace add Yarbrdab000/tableau-fabric-skills
copilot plugin install tableau-fabric-skills@tableau-collection
```

`tableau-fabric-skills` is the plugin; `tableau-collection` is the marketplace. Installing the
plugin installs all three skills.

## Verify it loaded (machine-checkable)

**Skills load at session start**, so start a **new** session, then confirm — don't ask the agent
"what skills do you have?" (that can't fail loud). Run:

```text
/plugin list     → expect "tableau-fabric-skills" in the list
/skills list     → expect tableau-datasource-profiler, tableau-mcp-landing-zone, tableau-migration
```

If `tableau-fabric-skills` is not in `/plugin list`, the install didn't take — re-run the
installer or the two commands above.

## Where each surface loads skills from

| Surface | Loads skills from | Notes |
|---|---|---|
| **Terminal GitHub Copilot CLI** | built-in dirs + installed **plugins**; repo `.github/skills/` when you're working inside that repo | Use the plugin path above. Skills load at **session start** — restart after installing. |
| **Desktop app — general chat** | built-in + **plugin** skills | The general-chat surface may expose only built-in/plugin skills, so the plugin install is what makes these available. |
| **VS Code Copilot** | installed **plugins** (and repo-scoped config) | Same plugin install; restart the session/window so they load. |

> ⚠️ **Folder-copy does not register a skill on current GitHub Copilot CLI.** Copying
> `skills/*` into `~/.copilot/skills/` (or `~/.claude/skills/`) is **not** a load path the
> current CLI scans — it produces no error and the skills never appear. This is the trap the
> plugin path above avoids. A repo-scoped `.github/skills/<name>/` committed into a repository
> *is* picked up while you work in that repo, but it is scoped to that single repository.

### Manual folder copy (older clients only)

Only if your client is too old to support `/plugin` (and you accept the caveat above). You are
copying **folders** — the three subfolders of `skills/` — into the folder your agent scans:

```powershell
git clone https://github.com/Yarbrdab000/tableau-fabric-skills.git
Copy-Item .\tableau-fabric-skills\skills\* "$env:USERPROFILE\.copilot\skills\" -Recurse -Force
```

```bash
git clone https://github.com/Yarbrdab000/tableau-fabric-skills.git
mkdir -p ~/.copilot/skills
cp -R tableau-fabric-skills/skills/* ~/.copilot/skills/
```

Destinations by agent and scope (restart your agent afterward):

| Agent | Personal (every chat) | Project (one repo only) |
|---|---|---|
| GitHub Copilot CLI / VS Code | `~/.copilot/skills/` | `<your-repo>/.github/skills/` |
| Claude Code | `~/.claude/skills/` | `<your-repo>/.claude/skills/` |

## Requirements

Python **3.11+**. `tableau-migration` is standard-library only; `tableau-datasource-profiler`
needs `requests` (`pip install -r skills/tableau-datasource-profiler/requirements.txt`);
`tableau-mcp-landing-zone` deploys with the Azure CLI / Docker.
