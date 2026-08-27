# Secret hygiene

This repository is public. Everything tracked therefore uses placeholders, and
the real values live in exactly two places on disk, neither of them tracked.

## Where the real values live

| What | Where | Tracked? |
|---|---|---|
| IBKR username + password | `~/ibc/config.ini` (mode 600) | no — outside the repo entirely |
| Port, account id, data paths | `.env` | no — `.gitignore` |
| Account id, usernames, prod endpoints | `.secrets/IDENTIFIERS.md` | no — `.gitignore` |
| Scanner blocklist | `.secrets/patterns.txt` | no — `.gitignore` |

Placeholders used in tracked files: `agent-user`, `human-user`, `U1234567`,
`<prod-host>:<port>`. A reader who needs the real value looks in `.secrets/`.

Note what is *not* here: no password ever reaches this project. `ib_agent`
connects to an already-authenticated Gateway over a local socket, so the only
credential in the system belongs to IBC, in a file this code never reads.

## The rule, and why it is enforced rather than remembered

Placeholders are easy to maintain and easy to forget at 2am. So
`scripts/pre-commit-secret-scan.sh` refuses any commit whose *added* lines match
a pattern in `.secrets/patterns.txt`:

```bash
./scripts/install-hooks.sh     # once per clone; hooks are not tracked
```

The patterns are deliberately not in the tracked hook — a tracked blocklist of
secrets is just a slower leak. If `.secrets/patterns.txt` is absent the hook says
so and passes, rather than pretending to protect a fresh clone.

It is a safety net, not an authority: `--no-verify` bypasses it. On a public
repository that is how an account id ends up in history forever.

## If a secret does get committed

Editing the file in a later commit is not enough — the value stays in history and
on any fork or cached view. What actually works:

1. **Not yet pushed, and it is the most recent commit.** `git commit --amend`
   after fixing the files, then expire the reflog and garbage-collect so the old
   commit is not merely unreachable but gone:
   ```bash
   git reflog expire --expire=now --all && git gc --prune=now --aggressive
   ```
   This is what was done here on 2026-08-27, before the first push.
2. **Not yet pushed, but further back.** `git rebase -i` to fix the offending
   commit, or `git filter-repo --replace-text` for a value spread over many.
3. **Already pushed.** Assume it is public and act accordingly: rotate the
   secret first — new password, revoked token, changed id where possible — and
   only then rewrite history. `git filter-repo`, force-push, and ask the host to
   drop cached views (GitHub keeps unreachable commits visible by SHA until
   asked). Rewriting without rotating is theatre.

The ordering matters more than the tooling: a leaked credential is compromised
the moment it is published, so rotation comes first and cleanup second.

## What deliberately stays in the repository

Design and failure detail with no secret in it: how the 2FA login is driven,
which IBKR calls are used, why order placement is excluded, what the read-only
gates are. Hiding architecture buys nothing here — the security of this setup
rests on IBKR account rights, `ReadOnlyApi=yes` and a password in a 600 file, not
on nobody knowing the layout.

One exception worth keeping an eye on: `docs/OH-INTEGRATION-PLAN.md` §5 records
that option_harvester's write routes have no authentication. The host and port
are redacted, but the finding itself is a pointer. It should be fixed in that
project rather than merely unmentioned in this one.
