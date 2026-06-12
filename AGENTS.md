# Repository Guidelines

## Project Structure & Module Organization

This repository is an AstrBot plugin for DDL tracking. Source files live at the repository root because AstrBot loads plugins directly from their plugin directory.

- `main.py` registers `DDLTrackerPlugin` and composes the mixins.
- `config.py`, `state_store.py`, `ddl_item.py`, `reminder_rules.py`, `future_task.py`, `extraction.py`, `commands.py`, and `llm_tools.py` contain the plugin behavior by concern.
- `constants.py` and `utils.py` hold shared constants and helpers.
- `_conf_schema.json` defines AstrBot plugin configuration.
- `metadata.yaml` contains plugin metadata.
- `skills/ddl-tracker/SKILL.md` documents the agent-facing workflow.
- `ddl_groups.json` is runtime state data; avoid committing local production state changes unless intentional.

There is currently no dedicated `tests/` directory.

## Build, Test, and Development Commands

- `python -m py_compile main.py commands.py config.py constants.py ddl_item.py extraction.py future_task.py llm_tools.py reminder_rules.py state_store.py utils.py` checks Python syntax without starting AstrBot.
- Copy or symlink this directory into `AstrBot/plugins/astrbot_plugin_ddl_tracker/`, then restart AstrBot to test plugin loading.
- In a target group chat, use `/ddl_on`, `/ddl_extract`, `/ddl_status`, and `/ddl_nearest 5` for smoke testing.

The plugin requires Python 3.10+ and a configured AstrBot LLM provider for extraction features.

## Coding Style & Naming Conventions

Use Python 3.10+ syntax, 4-space indentation, and type hints where practical. Keep mixin methods grouped by feature and prefer private helper names with a leading underscore for internal behavior, matching the existing style (`_load_state`, `_auto_extract_loop`, `_persist`). Use clear snake_case names for files, functions, variables, and JSON keys. Keep user-facing command names short and prefixed with `/ddl_`.

## Testing Guidelines

No formal test framework is configured. Before submitting changes, run the `py_compile` command above and perform an AstrBot smoke test for any touched command, LLM tool, or persistence path. For state changes, test with a temporary or sanitized `ddl_groups.json` and verify old state can still load.

## Commit & Pull Request Guidelines

Recent history uses very short commit subjects such as `Update`, `Test`, and `Initial commit`. Prefer a clearer imperative subject under 72 characters, for example `Fix reminder rule matching` or `Add status output fields`.

Pull requests should include a concise description, affected commands or LLM tools, configuration or state migration notes, and manual test results. Include screenshots or chat transcripts when changing user-visible command output.

## Security & Configuration Tips

Do not commit provider credentials, private chat data, or real group state. Treat `ddl_groups.json` as sensitive runtime data. Keep `_conf_schema.json`, `metadata.yaml`, and README command documentation aligned when adding or renaming configuration or commands.
