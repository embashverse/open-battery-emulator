# Engineering

For people who want to understand, check or change the emulator itself.

| Page | For |
|---|---|
| [Requirements](requirements.md) | every requirement, with its ID |
| [Design](design.md) | the three levels, the tick, the state machine, the contracts |
| [Verification](verification.md) | how each requirement is checked, manual checks with dates |
| [Milestones and Later](milestones.md) | the plan and ideas for later |
| [How it was built](how-it-was-built.md) | the V-model and the AI assisted workflow |

## Working on these docs

The pages are Markdown in `docs/`, built with MkDocs and the Material theme. They sit in a
separate `docs` dependency group, so the Pi does not install them to run the emulator.

| To | Run |
|---|---|
| preview locally (reloads on save), open `http://127.0.0.1:8000` | `uv run --group docs mkdocs serve` |
| check like CI: fails on a broken link, a missing heading or a page not in the `nav` | `uv run --group docs mkdocs build --strict` |
