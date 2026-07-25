# FoldOS Agent UI

This folder embeds [Agno's open-source Agent UI](https://github.com/agno-agi/agent-ui) as a submodule. It gives FoldOS a production-grade chat/management interface for the AgentOS backend.

## What it provides

- Agent chat with streaming responses
- Session history and management
- Tool-call visualization
- Traces and run inspection
- Reasoning steps and multi-modal rendering

## How to run

1. Start the FoldOS backend:
   ```bash
   cd ..
   python -m uvicorn foldos.app:create_app --factory
   ```

2. Install the UI dependencies:
   ```bash
   cd ui/agent-ui
   pnpm install
   ```

3. Start the UI dev server:
   ```bash
   pnpm dev
   ```

4. Open [http://localhost:3000](http://localhost:3000).

5. In the left sidebar, set the AgentOS endpoint to:
   ```text
   http://localhost:7777
   ```
   Then click **Connect**.

The UI will list the `analyst` agent and let you chat with it. CORS is already enabled on the FoldOS backend for `http://localhost:3000`.

## Note

`ui/agent-ui` is a Git submodule. If you cloned the repo without `--recurse-submodules`, run:

```bash
git submodule update --init --recursive
```
