# Cognee Memory for Claude Code

Persistent memory for Claude Code, built on Cognee Cloud's knowledge graph.

> This is a probe build used to check the plugin directory's validation. It is not a
> release.

## How it works

The plugin connects Claude Code to Cognee Cloud's MCP server at
`https://mcp.aws.cognee.ai/mcp`. You sign in once: run `/mcp`, pick the Cognee server
and sign in to your Cognee account in the browser. Claude Code keeps the sign-in and
refreshes it. The plugin itself holds no API key and runs no program on your machine.

On every prompt, a hook calls the server's `recall` tool with your prompt and adds what
it finds to Claude's context. Your prompts, the commands and edits Claude makes, and
Claude's answers are sent to the server's `remember` tool, tied to the current session,
so later sessions can recall them. Two skills let Claude search memory on request and
save a fact permanently.

## What is sent where

Everything goes only to Cognee Cloud (`mcp.aws.cognee.ai`), the service this plugin is
for: your prompts, the tool calls Claude makes (Bash, Write, Edit) and Claude's answers.
Nothing is sent before you sign in. See the privacy notice at
https://www.cognee.ai/privacy-notice.

## License

Apache-2.0
