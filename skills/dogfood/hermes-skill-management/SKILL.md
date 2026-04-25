---
name: hermes-skill-management
description: Complete guide to the Hermes skill lifecycle — how skills work, creating, editing, promoting to bundled, auditing, trimming, consolidating, and maintaining a healthy skill inventory. Load this when managing skills in any capacity.
version: 1.0.0
author: PMA
tags: [skills, management, lifecycle, audit, bundled, sync, optimization, hermes]
---

# Hermes Skill Management Guide

Use this when: **any** skill-related work — creating, editing, deleting, promoting, auditing, trimming, consolidating skills, diagnosing why a skill isn't showing up, or understanding how the skill system works internally.

## Part 1: How Skills Work

### The Skill Pipeline (3 Gates)

Skills pass through three gates before reaching the system prompt, all in `_find_all_skills()` (`tools/skills_tool.py`):

1. **`platforms:` frontmatter gate** — `agent/skill_utils.py::skill_matches_platform()`. Skills declare `platforms: [macos]` in YAML frontmatter; absent = all platforms. Checks `sys.platform`.
2. **`skills.disabled` config list** — `agent/skill_utils.py::get_disabled_skill_names()`. Reads from profile or global `config.yaml`. Supports per-platform disable via `skills.platform_disabled.<platform>`.
3. **Prompt injection** — `agent/prompt_builder.py::_build_skills_prompt()`. Builds the `<available_skills>` block injected into every turn's system prompt.

### Directory Resolution (Critical)

```
SKILLS_DIR = HERMES_HOME / "skills"
```

Where `HERMES_HOME` resolves differently depending on mode:

| Mode | HERMES_HOME | SKILLS_DIR (scanned) |
|------|-------------|---------------------|
| Profile mode | `~/.hermes/profiles/<name>` | `~/.hermes/profiles/<name>/skills/` |
| Global mode (no profile) | `~/.hermes` | `~/.hermes/skills/` |

**Each profile gets its own independent skill tree.** There is no automatic inheritance between profiles. A skill in `~/.hermes/skills/` is NOT visible to a profile-mode session.

### Key Source Files

| File | Purpose |
|------|---------|
| `tools/skills_tool.py` | `_find_all_skills()`, scanning logic, skill_view/create/edit/delete |
| `agent/skill_utils.py` | Platform matching, disabled set resolution, external dirs |
| `agent/prompt_builder.py` | Prompt injection (~line 807) |
| `tools/skills_sync.py` | Bundled skill sync (repo → profile) |
| `gateway/run.py:11059` | Triggers `sync_skills()` on gateway startup |

## Part 2: The Bundled Sync System

### How It Works

The repo's `hermes-agent/skills/` directory is the **bundled source of truth**. On every gateway startup, `tools/skills_sync.py::sync_skills()` runs automatically:

```
Repo: hermes-agent/skills/           ← Edit here to update globally
  └── dogfood/hermes-skill-mgmt/     ← Bundled skill source
      └── SKILL.md
         │  sync_skills() on gateway startup
         │  (tools/skills_sync.py)
         ▼
Profile: ~/.hermes/profiles/<name>/skills/  ← Active skills (per profile)
  └── dogfood/hermes-skill-mgmt/
      └── SKILL.md                          ← Auto-copied from bundled
```

### Manifest-Based Tracking

`.bundled_manifest` lives at `SKILLS_DIR/.bundled_manifest` and tracks each bundled skill as `name:md5_hash`.

| Case | Behavior |
|------|----------|
| **New** in repo, not in manifest | Copies to profile as new skill |
| **Existing**, user hasn't edited (hash matches origin) | Updates if repo version changed |
| **Existing**, user edited it (hash differs) | **Skips** — your edits are sacred |
| **User deleted** a bundled skill | Respected — won't re-add |
| **Removed** from repo entirely | Cleaned from manifest |

### Key Commands

| Command | Purpose |
|---------|---------|
| *(automatic)* | Sync runs on every gateway startup |
| `hermes skills reset <name>` | Un-stick a skill flagged as user-modified |
| `hermes skills reset <name> --restore` | Delete user copy + re-copy from bundled |
| `hermes skills config` | Interactive UI for disable/enable per platform |
| `hermes skills list` | Show all installed skills |
| `hermes skills check` | Check for updates from bundled/hub |

### Adding a Skill Globally

To make a skill available to every Hermes instance:

1. Add it to `hermes-agent/skills/<category>/<skill-name>/SKILL.md`
2. Follow the [Skill Format](#part-3-creating--editing-skills) guidelines below
3. Next gateway restart → `sync_skills()` copies it to every profile
4. Users who customize their copy get updates blocked (hash-based protection)

### External Skills Dirs

For skills that shouldn't live in the repo (org-specific, secret, etc.):

```yaml
# config.yaml
skills:
  external_dirs:
    - /path/to/shared/skills     # Extra scan path (after main SKILLS_DIR)
    - ~/org-skills               # ~ and ${VAR} expanded
```

External dirs are scanned after the main `SKILLS_DIR`. Duplicates are skipped. Paths that resolve to the main `SKILLS_DIR` are silently ignored.

## Part 3: Creating & Editing Skills

### SKILL.md Format

Every skill needs a `SKILL.md` file with YAML frontmatter:

```yaml
---
name: your-skill-name                    # Required: unique identifier (hyphens/underscores, max 64 chars)
description: What this skill does.        # Required: shown in <available_skills> block
version: 1.0.0                           # Optional: semver for tracking changes
author: Whoever wrote it                 # Optional
tags: [topic1, topic2]                   # Optional: categorization
platforms: [macos]                       # Optional: gate to specific OS (absent = all platforms)
symptom_index:                           # Recommended for diagnosis/troubleshooting skills
  - symptom: "error / failure"           #   Makes it obvious WHEN to load the skill
    section: "Failure Diagnosis"
---
# Skill Title (human-readable)

Body content in markdown. This loads into context when the agent
calls skill_view(), so keep it focused and actionable.
```

### Rules for Good Skills

- **Name uniqueness**: Must be unique across ALL skills (bundled + hub-installed + external). First match wins.
- **Description**: First 200 chars matter most — shown in system prompt. Make it load-decision-friendly.
- **Size**: Keep bodies under ~15K when possible. Larger skills slow down `skill_view()` loading.
- **Scope**: One concern per skill. If it covers multiple domains, consider splitting.
- **Actionable**: Prefer step-by-step instructions over reference material. Agents follow procedures.
- **When-triggered**: Include clear guidance on *when* to load this skill (first paragraph or `symptom_index`).

### Linked Files

Skills can have companion files in the same directory:

```
your-skill/
├── SKILL.md              # Main content (always required)
├── references/           # Reference docs, API notes
│   ├── api-endpoints.md
│   └── error-codes.md
├── templates/            # Reusable templates
│   └── report-template.md
└── scripts/              # Executable helpers
    └── validate.sh
```

Access linked files via `skill_view(name, file_path="references/api-endpoints.md")`.

### Creating a New Skill

1. **Pick the right category** under `skills/<category>/`:
   - `dogfood/` — Hermes self-care, meta-skills, verification workflows
   - `devops/` — Operations, diagnosis, infrastructure
   - `github/` — GitHub-specific workflows
   - `software-development/` — Coding practices, review, planning
   - `autonomous-ai-agents/` — Agent spawning, delegation
   - (See `ls hermes-agent/skills/` for full category list)

2. **Write SKILL.md** with proper frontmatter and body
3. **Place in repo** at `hermes-agent/skills/<category>/<name>/SKILL.md`
4. **Test locally** by also placing in your profile's `skills/` dir (sync will handle it after restart, but manual placement lets you test immediately)
5. **Restart gateway** (or wait for next restart) → sync distributes it

### Editing an Existing Skill

**Bundled skill (lives in repo):**
- Edit `hermes-agent/skills/<category>/<name>/SKILL.md`
- On next restart, sync propagates to users who haven't customized their local copy
- If you previously edited the local copy, run `hermes skills reset <name>` first so sync doesn't skip it

**Profile-local skill (only in profile tree):**
- Edit directly in `~/.hermes/profiles/<name>/skills/<category>/<name>/SKILL.md`
- Changes take effect on next `/new` or `/restart`
- These won't be overwritten by sync (different hash = user-modified)

### Deleting a Skill

- **From repo**: Remove the directory. Next sync cleans it from manifests.
- **From profile only**: Delete the directory. Sync won't re-add (deletions are respected).
- **Via disabled list**: Add to `skills.disabled` in config.yaml. Non-destructive, easily reversible.

## Part 4: Auditing & Trimming

### When to Audit

- New skills were installed (bulk installs can add 10+ at once)
- A new profile was created (copy relevant disables)
- System prompt feels noisy or model seems distracted
- Every 2-4 weeks as part of workspace hygiene

### Inventory Framework

Call `skills_list(category=None)` to get all skills. Group by relevance tier:

| Tier | Definition | Action |
|------|-----------|--------|
| **Core** | Used regularly in this role | Keep |
| **Occasional** | Useful sometimes | Keep |
| **Never** | Wrong platform / wrong role / novelty | Disable |
| **Fragmented** | Multiple skills doing similar things | Consolidate |

**Rule of thumb:** In any given role, ~15-20 skills carry 95% of value. The rest are candidates for disable or consolidate.

### Elimination Checklist

1. **Platform** — Wrong-OS skills are pure dead weight. Verify `sys.platform` before eliminating.
2. **Role** — Remove categories that don't match how the profile is used. Be conservative; disabling is easy to undo.
3. **Domain** — Deep-domain tooling only matters during work in that domain.
4. **Creative/novelty** — Keep only what serves real workflows.
5. **Fragmentation** — Clusters of narrow skills → consolidation candidates.

### Disabling (No Code Changes)

```yaml
# config.yaml
skills:
  disabled:
    - skill-name-1          # Comment why, for future-you
    - skill-name-2          # Group by reason
```

Takes effect on `/new` or `/restart`. Profile-scoped.

### What NOT To Disable

- Agent spawning skills (codex, opencode, claude-code, hermes-agent)
- Skills actively used day-to-day
- Platform-appropriate skills (Apple on macOS, Linux on Linux)
- Dogfood/meta skills (this skill, setup guides, verification skills)

When uncertain, leave it enabled. Cost of one extra line is low; cost of missing a needed skill mid-workflow is high.

## Part 5: Consolidating Skills

### Avoid Monolithic Merges

❌ Don't merge too many sources into one "mega-skill." Hard to navigate, slow to load, loses specificity.

✅ Aim for cohesive, scannable skills. Each should be loadable as a unit when needed. If a cluster is large enough for one skill to be unwieldy, split into multiple grouped by **operational moment** (when do I reach for this?).

### Grouping Strategy: By Operational Moment

Group by **when** needed, not **what** subsystem covers:

| Bad grouping | Good grouping |
|-------------|--------------|
| All thread-related | Thread diagnosis (when threads fail/stall) |
| All hub-related | Health check (when system hangs/slows) |
| All ticket-related | Ticket recovery (when flows stuck/bloated) |

### `symptom_index` Pattern

Every consolidated diagnosis skill should have this in frontmatter:

```yaml
symptom_index:
  - symptom: "error / failure / crash"
    section: "Failure Diagnosis"
  - symptom: "empty response / no output"
    section: "Empty Response Diagnosis"
```

Makes it obvious *when* to load without reading the full body.

### Consolidation Workflow

1. **Read ALL source skills first** — `skill_view()` on every one. Note sizes.
2. **Create target** in the right category directory
3. **Write consolidated SKILL.md** directly (not via subagents)
4. **Disable old skills** in `config.yaml` with comments mapping old→new
5. **Verify**: new skill loads, old ones disabled, nothing lost

## Part 6: Archive Management

### When to Archive vs. Just Disable

**Just disable** (no archive):
- Pure noise skills (gaming, leisure) — no content worth keeping
- Never-used skills — zero knowledge loss
- Tiny skills (<2K) fully absorbed into consolidated version

**Archive** when:
- Substantial content (10K+ chars) gets compressed during merge
- Contains edge-case details, exhaustive catalogs, deep-dive references
- You might need to look up "what did the old version say about X?" later

### Archive Convention

```
skills/_archive/<date>/<original-skill-name>/
├── SKILL.md              # Original content, untouched
```

Each batch gets an `ARCHIVE_INDEX.md` with a loss-analysis table noting what was preserved vs. compressed away.

### Workflow

1. `mkdir -p skills/_archive/<date>/`
2. `mv skills/<category>/<old-name> skills/_archive/<date>/`
3. Write `ARCHIVE_INDEX.md` with loss analysis
4. Disable old names in `config.yaml` (with old→new comments)
5. Verify count matches expected

## Part 7: Memory & Knowledge Debt Hygiene

Skills aren't the only thing that bloats. Apply same discipline to memory, BRV, and active_context.

### Processing Order

| Priority | Action | Target | Example |
|----------|--------|--------|---------|
| 1 | **Delete** | Stale task progress in memory | "still need to X" for completed work |
| 2 | **Codify into skill** | Reusable operational patterns | Repeated workflow → skill section |
| 3 | **Promote to BRV** | Long-term project knowledge | Architecture decisions, findings |
| 4 | **Prune aggressively** | Active context | Completed flows, archived threads |

### Anti-Patterns

- ❌ Task progress tracking in durable memory → use `todo` tool or `active_context.md`
- ❌ Duplicate entries with incremental updates → replace, don't append
- ❌ Meta-guidance eating budget → codify into a skill instead
- ✅ Facts that prevent future steering errors → user preferences, environment facts, conventions learned hard

### When to Run Memory Hygiene

- Memory usage > 50% (target: under 50%)
- After completing a project whose progress was tracked in memory
- Entries reference "still need to" or "in progress" for done work
- Monthly, paired with skill audit
