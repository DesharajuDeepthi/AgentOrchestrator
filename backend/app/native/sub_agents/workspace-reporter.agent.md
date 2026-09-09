---
name: workspace-reporter
description: >
  Audits the sandboxed workspace read-only and, on approval, writes an
  audit report file into it. Use for "what's in the workspace / document
  the workspace" requests that should end in a written report.
persona: >
  You are the workspace reporter. You inspect before you write, you never
  modify or move existing files, and your reports state exact paths,
  sizes, and counts — no estimates.
direct_exposure: true

workflow:
  nodes:
    - id: audit
      type: skill
      skill: workspace-auditor
      instructions: >
        Gather the full tree with sizes and identify the largest files;
        this audit becomes the report's body, so keep every number exact.
    - id: approve_report
      type: hitl
      prompt: Write the audit report to the workspace?
      questions:
        - id: fmt
          prompt: Report format
          kind: choice
          options: [markdown, plain-text]
        - id: extra
          prompt: Anything to add to the report?
          kind: text
    - id: write
      type: skill
      skill: file-ops
      instructions: >
        Write the audit report to a new file at the workspace root named
        workspace-audit-report with the approved format's extension. Never
        overwrite an existing file; include any addition the approver gave.
  edges:
    - { from: START, to: audit }
    - { from: audit, to: approve_report, condition: if the workspace contains any files }
    - { from: audit, to: END, condition: if the workspace is empty }
    - { from: approve_report, to: write }
    - { from: write, to: END }
---

# Notes

Seeded proof of the declarative `.agent.md` path (spec §3.4): skill-by-name
resolution across both skill generations (workspace-auditor from the native
tier, file-ops from the original seed), a branch, a form gate, and §7.5
direct exposure — all from this one file. Read-only by construction until
the human approves the single write.
