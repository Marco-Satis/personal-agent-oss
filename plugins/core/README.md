# core

The plugin that ships with personal-agent. It holds three skills for the notes vault:

| Skill | Invoke as | What it does |
|---|---|---|
| organize-inbox | `/core:organize-inbox` | files open `raw/` entries into `wiki/` articles and updates `wiki/_master-index.md` |
| daily-plan | `/core:daily-plan` | picks the top 3 priorities and writes them to `heute.md` |
| capture-note | `/core:capture-note <text>` | appends a note to `raw/inbox-YYYY-MM-DD.md` |

The model can also load them by itself through the Skill tool.

Rules this plugin follows, and every other plugin must follow too (`personal-agent plugins check`):

- Skills only, no hooks, no MCP or language servers, no scripts.
- Each skill is one self-contained `SKILL.md`: the plugin folder lies outside the vault, so
  the assistant cannot read supporting files.
- The skills use Read, Glob, Grep, Write and Edit inside the vault only, and every write goes
  through the approval broker.
- Note text is treated as data, never as instructions.
