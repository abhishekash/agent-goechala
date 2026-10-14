# JavaScript client

The dependency-free client supports Node.js 18+.

From a repository checkout:

```bash
npm install ./clients/javascript
```

Example:

```js
import { Client } from '@agent-observatory/client';

const telemetry = new Client({ service: 'javascript-research-agent' });
await telemetry.trace(
  { task: 'Find current guidance', sessionId: 'thread-42' },
  async run => {
    await run.progress('planning', 'Choosing a search strategy', 10);
    return run.span('search', { attributes: { 'tool.name': 'web_search' } }, async () => {
      return searchWeb();
    });
  },
);
```

The client sends completed spans to the local receiver. By default, delivery is best effort so telemetry failures do not interrupt the agent; enable `strict: true` to raise delivery errors. See [all client options](usage.md).
