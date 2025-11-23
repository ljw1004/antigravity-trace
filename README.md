# Antigravity-trace

This project lets you see the raw LLM calls made by Antigravity.

## Usage

- Setup: `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`. On subsequent use, just `source venv/bin/activate`
- Install: `./antigravity-trace.py [--verbose]`
- Uninstall: `./antigravity-trace.py --uninstall`

This sets up hooks to capture Antigravity's activity. It writes logs in ~/antigravity-trace. The logs are standalone HTML files so you can view in a normal browser and share them, but they're also JSONL in so you can process them with tools. The `--verbose` flag captures additional activity (LLM calls for next-edit-prediction, integration between core and VSCode, stderr).

When a new version of Antigravity is released, this extension will deliberately break to let you know something's wrong; you'll have to reinstall or uninstall.


## Overview

Antigravity is a fork of VSCode with two main components:
1. A bundled extension /Applications/Antigravity.app/Contents/Resources/app/extensions/antigravity written in typescript which does all the VSCode integration. (There are a few other bunded extensions too, for browser, code-executre, remoting).
2. A core agent binary, written in Go, which does the actual agentic work. This is similar to how Claude and Codex also have IDEs that shell out to their corresponding CLI binary. The agent also includes a language server for next-edit-prediction. The agent calls Google endpoints to make its LLM calls (for both agentic chat and next-edit-prediction), and also calls into services provided by the VSCode extension e.g. LaunchBrowser, InsertCodeAtCursor.

When you install this extension, it hooks into the core agent and intercepts all its communications. This is what is put into the log.