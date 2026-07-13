# Design: Upload paperconan to Xiaohongshu SkillHub

Date: 2026-06-13
Status: Approved (pending spec review)

## Goal

Publish paperconan to Xiaohongshu (小红书) SkillHub via the official
`@xhs/skillhub-upload` CLI, **preserving the tool's core functionality** (the
numeric-forensics detectors) while respecting the platform's restrictions.

## Platform constraints (discovered from the CLI source, authoritative)

The public `uploader.md` is thin; the real rules live in the
`@xhs/skillhub-upload` npm package the SkillHub agent drives. From its source:

- **No binary files.** Rejected by *both* file extension *and* magic-byte
  sniffing (`pack.mjs` `BINARY_EXTENSIONS` + `hasForbiddenSignature`). Banned
  includes `.xlsx .xls .docx .doc .pdf .png .jpg .gif .svg .zip .gz .so .dylib
  .exe`. Plain text (`.md .csv .tsv .json .html .py`) is fine.
- **Size:** ≤ 10 MB per file, ≤ 30 MB total.
- **No symlinks.** `.git/`, `node_modules/`, `__MACOSX/`, `.DS_Store` are
  auto-excluded; everything else in the directory is zipped.
- **Input is a directory**, never a user-made zip. The CLI validates the dir,
  then builds the zip itself. Root must contain `SKILL.md`.
- **Metadata** parsed from `SKILL.md` frontmatter. `name` → permanent,
  immutable Skill ID via `deriveIdentifier` (`paperconan`). `version` is
  optional (defaults to `1.0.0`).
- **`skill_md_content` (the SKILL.md *body*, shown as "Skill介绍") is capped at
  ~10,000 chars and truncated past that** — discovered post-submit on
  2026-06-13 (our body was 15,026 chars; the cut dropped the scan.json schema
  and the entire "CRITICAL: signal, not verdict" section). Keep the SKILL.md
  body well under 10k and push verbose reference material into bundled
  `references/` files (which ride along in the zip and aren't subject to this
  cap). The CLI does **not** warn about this — only the platform truncates.
- **Publish flow:** device-code login via the XHS mobile app → `--dry-run`
  payload preview → pick `原创/转载` + ≥1 live **content tag** → explicit submit
  → uploads to Tencent COS → **XHS moderation review**.

### Consequence
The existing build artifact `paperconan-skill.zip` would be **rejected** (it
bundles a demo `.xlsx` and a `.png`). But the source directory
`skills/paperconan/` (SKILL.md + `references/detectors.md` +
`references/interpretation.md`) is **already all-text and upload-clean**. We
upload that directory; the demo example is referenced via GitHub links instead.

## The core-functionality risk

The uploaded skill is **instructions an XHS-side agent reads**, not a sandboxed
program we ship. paperconan's detectors need **Python + numpy/scipy/openpyxl at
runtime**. We cannot bundle those (compiled `.so`/`.dylib` are banned; 30 MB
cap). So the detectors actually execute **only if the XHS agent runtime has
Python and can `pip install paperconan`** (confirmed on PyPI at 0.5.0; repo is
at 0.6.0, so a release is pending). If XHS's agent is chat-only, the math can't
run there.

## Decisions (agreed with user)

1. **Packaging = A+C hybrid.** Primary path runs the real tool; if no
   Python/network, degrade **honestly** — say so, tell the user to run
   `paperconan` locally, and (optionally) do a *clearly-labeled* manual
   heuristic look using `references/`. Never present eyeballed guesses as tool
   output.
2. **Keep `fetch` as secondary.** One line noting it needs outbound network and
   may be unavailable in restricted runtimes.
3. **Framing = technical, with strengthened guardrails.** Keep the forensic
   framing; surface the existing "signal, not verdict / 不是最终结论" red-lines near
   the top to stay review-friendly.
4. **Edit `skills/paperconan/SKILL.md` in place** (single source of truth; the
   changes improve the skill for every consumer). An isolated copy under
   `dist/skillhub/` remains available as a fallback if the user later wants to
   keep the canonical skill untouched.
5. **CLI install:** the sandbox auto-classifier blocked the global
   `npm install -g <tarball>`. User approved a **one-off permission/allowlist**
   for that install; apply it at execution time, remove afterward.

## Concrete edits to `skills/paperconan/SKILL.md`

- Add `version: 0.6.0` to frontmatter (provenance; matches the tool).
- Add a **"Runtime & fallback"** section encoding the A+C hybrid behavior.
- Add a one-line network caveat to the `fetch` section.
- Lift the "signal, not verdict" red-lines nearer the top.
- Leave `references/` and the GitHub-linked `examples/` as-is.

## Upload procedure (guided; user stays in control)

1. Install/enable `skillhub-upload` (one-off allowlist for the install).
2. `skillhub-upload whoami`; if unauthorized → `login --agent`, relay auth
   link + `XXXX-XXXX` code; **user** authorizes in the XHS mobile app.
3. `skillhub-upload publish skills/paperconan --dry-run --agent --source
   original --tag <中文标签>` → show the user the payload.
4. User picks 原创 + content tag(s) from the live list; review.
5. On the user's explicit "submit", do the real submit; relay the receipt or
   rejection verbatim.

## Non-goals

- No invented or eyeballed results passed off as real tool output.
- No repo-root or pre-made-zip upload; no bypassing the binary rules.
- No auto-submit without the user's explicit go.
- Not bundling Python deps (impossible under the binary ban anyway).

## Open items / risks

- Whether the XHS agent runtime can execute Python / reach the network is
  unknown — the A+C hybrid is the mitigation.
- PyPI lagged the repo (was 0.5.0 while the repo carried unreleased post-tag
  changes). Resolved 2026-06-13: bumped to **0.7.0** (public `audit_dir()` +
  fetch-hardening + oversized-guard), tagged `v0.7.0`, and published to PyPI so
  `pip install paperconan` matches the skill's documented version.
- XHS moderation may reject the academic-integrity framing; guardrails-forward
  wording is the mitigation.
