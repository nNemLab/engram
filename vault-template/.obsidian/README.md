# Obsidian setup

After pointing Obsidian at this vault, install these plugins (Settings → Community plugins):

- **Dataview** — query frontmatter from notes
- **Templater** — instantiate the templates in `_templates/`

Recommended Obsidian settings:

- Files & Links → Default location for new notes: `000-inbox/`
- Files & Links → Use [[wikilinks]]: enabled
- Editor → Show frontmatter: enabled (or use Properties view)

The vault is a **projection** of the Engram event log. Manual edits are
captured by the watcher daemon and become authoritative for the affected entry.
The projector won't overwrite your edits — but if you delete a file by hand,
the next render will recreate it from the log. Use `kb.flag_contradiction` or
explicit log events if you need to remove content for real.
