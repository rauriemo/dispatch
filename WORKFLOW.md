---
tracker:
  kind: github
  repo: "rauriemo/dispatch"
  labels:
    active: ["todo", "in-progress"]
    terminal: ["done", "canceled"]

polling:
  interval_ms: 10000

workspace:
  root: "./workspaces"

hooks:
  after_create: "git clone {{issue.repo_url}} ."
  before_run: "git pull origin main"

agent:
  command: "claude"
  max_turns: 10
  max_concurrent: 2
  stall_timeout_ms: 300000
  max_retry_backoff_ms: 300000
  permission_mode: "dontAsk"
  allowed_tools:
    - "Read"
    - "Edit"
    - "Grep"
    - "Glob"
    - "Bash(git *)"
    - "Bash(python -m pytest *)"
    - "Bash(python -m dispatch *)"
    - "Bash(pip *)"
  denied_tools:
    - "Bash(git push --force *)"

rules:
  - match:
      labels: ["planning"]
    action: require_approval
    approval_label: "approved"

channels:
  - kind: dispatch
    target: "localhost:8082"
    events: [task.completed, task.failed, wave.completed, maintenance.suggested]
  - kind: prism
    target: "localhost:3104"
    events: [task.completed, task.failed, wave.completed, maintenance.suggested]

system:
  workflow_changes_require_approval: true
  constraints:
    - "Follow the project existing code style and conventions"
    - "Read CLAUDE.md before making changes -- it documents architecture, threading model, and critical implementation details"
    - "Run python -m pytest tests/ before opening a PR"
    - "Keep commits small and focused on a single concern"
    - "Do not modify files outside the project directory"
    - "Never commit secrets, credentials, API keys, or tokens"
    - "Use queue.Queue (stdlib) for frame queues, never asyncio.Queue -- both capture and STT threads are sync contexts"

server:
  port: 8082
---

You are an expert Python software engineer working on **Dispatch**, a voice-first command channel for AI agents.

Repository: {{.issue.repo_url}}
Branch: anthem/{{.issue.identifier}}

## Context
Dispatch is a modular voice pipeline: wake word detection -> speech-to-text -> agent routing -> text-to-speech. It uses asyncio for the main event loop with background threads for audio capture and blocking gRPC. Read CLAUDE.md for the full architecture.

Key tech: Python 3.11+, asyncio, pvrecorder, Google Cloud STT, Edge/OpenAI/Google TTS, websockets, pygame, pynput, pystray.

## Task
{{.issue.body}}

## Rules
- Create a branch named `anthem/{{.issue.identifier}}`
- Make small, focused commits
- Run `python -m pytest tests/ -v` and ensure all tests pass before opening a PR
- When done, open a PR and comment a summary on the issue
