# Flanx Core Automation Directive

This repository is operated by **Flanx**, a Discord orchestrator. Work is staged here as
task manifests on `flanx-evolution-*` branches and executed by Freebuff/Codebuff agent
sessions. When you are running in this repository, the rules below apply.

## Task manifests

- Task files live in `tasks/*.md` and are created on `flanx-evolution-*` branches; the
  default branch is never pushed to directly.
- Each manifest states the target file, the engineering objective, and a
  "Definition of done" checklist. Treat that objective as the single source of truth for
  the work.
- Multiple manifests may exist on one branch; execute them all or none.

## How to execute a manifest

1. Read the manifest, open the target file, and make the minimal change that satisfies
   the objective.
2. Run the project's build and tests inside the workspace sandbox **before** committing;
   never stage changes that fail validation.
3. Match the file's existing style. Keep existing comments as they are and write any new
   comments and docstrings in the repository's primary language.
4. When validation passes, consolidate the changes and open a Pull Request from the
   `flanx-evolution-*` branch, referencing the manifest ID and pasting the validation log
   into the description.
5. If validation fails, report the failure on the branch (or in the PR description)
   instead of merging broken code.

## Authorship and attribution

- This is a **sole-authored personal project**. All commits are authored as:
  `Flanxiyum001 <pourushnair6@gmail.com>`.
- **Never add AI attribution** to commits or their messages: no
  `Co-Authored-By: ...` trailers, no "Generated with ..." footers, and no
  bot/co-author lines of any kind.
- Keep the repository free of AI-vendor branding in commit history, tags and releases.

## Project conventions

- Python entry point: `main.py`; sandbox + memory helpers: `free_sandbox.py`.
- Dependencies are pinned in `requirements.txt`.
- Environment variables are read from the environment (Railway Variables in production);
  never hard-code or commit secrets.
- Generated artifacts such as `__pycache__/` are not committed.