import type { Task } from "./types";

/**
 * Fixture data so the dashboard renders before the API service exists.
 * Swap the body for `fetch("/api/tasks")` once /api/tasks is live.
 */
export async function fetchTasks(): Promise<Task[]> {
  return FIXTURE_TASKS;
}

const FIXTURE_TASKS: Task[] = [
  {
    id: "3f1c9d2e-0000-4000-8000-000000000001",
    userPhone: "+15551230000",
    instruction: "Book a table for four at Bella Vista tonight at 7pm.",
    status: "completed",
    createdAt: "2026-09-20T18:04:11Z",
    outcomeSummary: "Table for four confirmed at 7:00pm under the name Siyona.",
    calls: [
      {
        id: "call-1",
        calleeNumber: "+15551234567",
        status: "completed",
        medianTurnLatencyMs: 268,
        transcripts: [
          { turnIndex: 0, speaker: "callee", text: "Bella Vista, for reservations press one.", latencyMs: null },
          { turnIndex: 1, speaker: "agent", text: "Selecting reservations.", latencyMs: 231 },
          { turnIndex: 2, speaker: "callee", text: "Reservations, how many in your party?", latencyMs: null },
          { turnIndex: 3, speaker: "agent", text: "A table for four at seven, please.", latencyMs: 268 },
          { turnIndex: 4, speaker: "callee", text: "You are all set.", latencyMs: null },
        ],
      },
    ],
  },
  {
    id: "3f1c9d2e-0000-4000-8000-000000000002",
    userPhone: "+15551230000",
    instruction: "Ask the dentist for the first opening next week.",
    status: "in_progress",
    createdAt: "2026-09-20T18:22:40Z",
    outcomeSummary: null,
    calls: [
      {
        id: "call-2",
        calleeNumber: "+15559876543",
        status: "in_ivr",
        medianTurnLatencyMs: 294,
        transcripts: [
          { turnIndex: 0, speaker: "system", text: "Dialing +1 555 987 6543.", latencyMs: null },
        ],
      },
    ],
  },
];
