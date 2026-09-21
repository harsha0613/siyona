# Review dashboard

React + TypeScript console for reviewing tasks, calls and transcripts.

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api to the API service
npm run typecheck
```

`src/api.ts` currently serves fixture data so the UI runs with no backend; point
`fetchTasks` at `/api/tasks` once the API service is up.
