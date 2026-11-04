## Description: <br>
Cognee Memory System helps agents store, recall, forget, and improve long-term memories using Cognee-backed vector and graph search. <br>

This skill is ready for commercial/non-commercial use. <br>

## Publisher: <br>
[smseow001](https://clawhub.ai/user/smseow001) <br>

### License/Terms of Use: <br>
MIT-0 <br>


## Use Case: <br>
Developers and agent builders use this skill to add persistent memory workflows to assistants, including customer support memory, SQL copilot knowledge distillation, and cross-session user preference recall. <br>

### Deployment Geography for Use: <br>
Global <br>

## Known Risks and Mitigations: <br>
Risk: Automatic memory hooks may persist prompts, tool activity, or session context beyond the immediate conversation. <br>
Mitigation: Enable the skill only with clear retention expectations, avoid secrets or regulated data, and confirm where memories are stored and who can access them. <br>
Risk: Shared vector and graph memory can expose sensitive context across agents or sessions if scoping is unclear. <br>
Mitigation: Use scoped API keys, separate datasets or session scopes, and verify recall, deletion, and recovery behavior before enabling shared memory. <br>
Risk: The workflow depends on external pip and npm packages plus sensitive credentials. <br>
Mitigation: Verify package sources and versions before installation, and use least-privilege credentials for LLM and Cognee service access. <br>


## Reference(s): <br>
- [Cognee website](https://cognee.ai) <br>
- [Cognee GitHub repository](https://github.com/topoteretes/cognee) <br>
- [Cognee LLM provider configuration](https://docs.cognee.ai/setup-configuration/llm-providers) <br>
- [ClawHub skill page](https://clawhub.ai/smseow001/cognee-memory) <br>


## Skill Output: <br>
**Output Type(s):** [text, markdown, code, shell commands, configuration, guidance] <br>
**Output Format:** [Markdown guidance with Python snippets, shell commands, and configuration examples] <br>
**Output Parameters:** [1D] <br>
**Other Properties Related to Output:** [May include API key, service URL, package installation, and OpenClaw hook configuration guidance.] <br>

## Skill Version(s): <br>
1.0.0 (source: frontmatter and server release evidence) <br>

## Ethical Considerations: <br>
Users should evaluate whether this skill is appropriate for their environment, review any generated or modified files before relying on them, and apply their organization's safety, security, and compliance requirements before deployment. <br>
