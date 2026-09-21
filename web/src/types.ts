export type TaskStatus =
  | "received"
  | "queued"
  | "in_progress"
  | "needs_user_input"
  | "completed"
  | "failed"
  | "cancelled";

export type Speaker = "agent" | "callee" | "system";

export interface TranscriptTurn {
  turnIndex: number;
  speaker: Speaker;
  text: string;
  latencyMs: number | null;
}

export interface Call {
  id: string;
  calleeNumber: string;
  status: string;
  medianTurnLatencyMs: number | null;
  transcripts: TranscriptTurn[];
}

export interface Task {
  id: string;
  userPhone: string;
  instruction: string;
  status: TaskStatus;
  createdAt: string;
  outcomeSummary: string | null;
  calls: Call[];
}
