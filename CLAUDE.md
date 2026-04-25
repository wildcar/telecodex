# CLAUDE.md

This compatibility entrypoint is for Claude-style agents. The primary repository instructions live in `AGENTS.md`.

## Required Reading Order

1. `AGENTS.md`
2. `AGENTS-TECHSPEC.md`
3. `AGENTS-SUMMARY.md`
4. Latest entries in `AGENTS-HISTORY.md` if present
5. Relevant `AGENTS-SPECIFICS-*.md` files for the task area

## Key Rules

- Treat `AGENTS-TECHSPEC.md` as the functional specification.
- Update `AGENTS-TECHSPEC.md` before code or behavior changes when the requested task changes the functional contract.
- Add a short `AGENTS-HISTORY.md` entry before non-trivial code or documentation changes.
- Commit and push to GitHub after successful verification.
- Keep source code, technical docs, and comments in English.
- Reply to the user in Russian.
