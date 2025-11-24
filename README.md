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
2. A core agent binary, written in Golang, which does the actual agentic work. This is similar to how Claude and Codex also have IDEs that shell out to their corresponding CLI binary. The agent also includes a language server for next-edit-prediction. The agent calls Google endpoints to make its LLM calls (for both agentic chat and next-edit-prediction), and also calls into services provided by the VSCode extension e.g. LaunchBrowser, InsertCodeAtCursor.

The extension invokes the core agent golang binary like this:
```
language_server_macos_arm \
  --cloud_code_endpoint https://daily-cloudcode-pa.sandbox.googleapis.com \
  --inference_api_server_url http://jetski-server.corp.goog \
  --api_server_url http://jetski-server.corp.goog \
  --analytics_server_url http://jetski-server.corp.goog \
  --parent_pipe_path <UDS> \
  --extension_server_port <EXTENSION> \
  [--server_port <HTTPS>] \
  --enable_lsp [--lsp_port <LSP>] \
  --random_port
```
When you install this extension, it hooks into the various communication channels and writes them into the log.
- **LLM: https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:streamGenerateContent?alt=sse** -- this endpoint is used for making LLM calls, both the regular ones used by the agent, and also "please summarize this conversation" requests
- **CLOUD: https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:tabChat?alt=sse** -- this endpoint is used for making LLM calls to the Windsurf "next edit prediction" service; they're still normal LLM calls with system-prompt and messages, but they look like legacy models
- **CLOUD: https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:...** -- there are several other endpoints that I've seen called so far, including :fetchUserInfo, :loadCodeAssist, :fetchAvailableModels, :recordTrajectoryAnalytics
- **INFERENCE: http://jetski-server.corp.goog** -- I've never seen any traffic on here and don't know what it's for
- **API: http://jetski-server.corp.goog** -- Again I've never seen traffic
- **ANALYTICS: http://jetski-server.corp.goog** -- Again I've never seen traffic. It already sends analytics elsewhere, :recordTrajectoryAnalytics
- **UDS** -- I've never seen traffic on `parent_pipe_path`, a unix domain socket
- **EXTENSION** -- The Golang binary sends requests to the VSCode antigravity extension over this channel, in protobuf binary format. There are about 50 endpoints, e.g. PlaySound, InsertCodeAtCursor, LanguageServerStarted
- **HTTPS server_port** -- The VSCode antigravity extension sends requests to the golang server over this channel, in protobuf binary format. There are about 130 endpoints, e.g. Heartbeat, SetWorkingDirectories, GetUserSettings
- **LSP lsp_port** -- The extension sends LSP requests to the golang server. The only ones I've ever seen it send are didOpen, didClose, didSave, didChangeWatchedFile. It doesn't even send didChange. This must be used solely as a way for golang server to be aware of open files.
- **HTTP random_port** -- I think by reading the source code that the extension sends /unleash requests here (a nodejs library for obtaining feature-enabled settings), and also debug hooks. I didn't bother intercepting this.
- **STDIO** -- The vscode extension sends on small protobuf message on stdin, and there's lots of stderr out. Some error messages on stderr are so frequent that we don't bother logging them, e.g. "could not convert a single message before hitting truncation", "queryText was truncated", "exceeds limit".

Note: strictly speaking, the extension doesn't normally pass `--server_port` nor `--lsp_port`: the golang server picks available ports for these, like it does for `--random_port`, and it communicates its chosen port numbers by sending a LanguageServerStarted(server,lsp,random) request to the extension.

I believe there must be another communication channel that I haven't yet spotted: when the user types a prompt into the agent and hits Submit, then this message must be sent somehow to the golang server, but I've not seen it on any of the above channels.
