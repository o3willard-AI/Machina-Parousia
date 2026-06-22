# Backlog: LM Studio Jinja Template Bug

**Status:** Open  
**Priority:** Medium  
**Labels:** bug, research, pi-coder  
**Created:** 2026-06-22  
**Repo:** Machina-Parousia (or LM Studio upstream)

## Observed Behavior

PI coding subagent using `qwen3.6-35b-a3b-claude-4.6-opus-reasoning-distilled` on LM Studio (.21:1234) fails with:

```
Error rendering prompt with jinja template: "Unknown StringValue filter: safe"
```

when tools are enabled and prompts exceed ~2,000 tokens. Short prompts without tools work fine.

## Impact

- 2 out of 3 PI delegation attempts failed (11,400 + 16,981 tokens = 28,381 wasted)
- Only short/no-tools tasks succeed
- Blocks all delegations requiring tool use
- Not affecting OpenCode, PI with other models, or LM Studio Chat UI

## Investigation Needed

1. Is this a model-specific Jinja template issue in the GGUF's chat template metadata?
2. Does LM Studio have a prompt template override that fixes it?
3. Is the `safe` filter something the model's template expects but LM Studio's Jinja engine doesn't provide?
4. Can we hotfix by overriding the chat template in LM Studio's model settings?
5. Does the lmstudio-community version of this model have a corrected template?

## Repro

```bash
python3 ~/bin/pi_run.py --name repro-jinja \
  --task 'Write a Python module with multiple classes and tool calls' \
  --workdir /tmp
```

## Logs

- `/tmp/pi_token_tally.jsonl` entries for `task2-recorder` and `task3-mcp-hooks`
