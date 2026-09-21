import { useEffect, useState } from "react";
import { fetchTasks } from "./api";
import { TaskList } from "./components/TaskList";
import { TranscriptView } from "./components/TranscriptView";
import type { Task } from "./types";

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchTasks()
      .then((loaded) => {
        setTasks(loaded);
        setSelectedId(loaded[0]?.id ?? null);
      })
      .catch((cause: unknown) => setError(String(cause)));
  }, []);

  const selected = tasks.find((task) => task.id === selectedId) ?? null;

  return (
    <main className="app">
      <h1>Siyona review dashboard</h1>
      {error && <p className="error">Could not load tasks: {error}</p>}
      <div className="layout">
        <TaskList tasks={tasks} selectedId={selectedId} onSelect={setSelectedId} />
        <div className="detail">
          {selected ? (
            <>
              <h2>{selected.instruction}</h2>
              <p className="meta">
                {selected.userPhone} &middot; {new Date(selected.createdAt).toLocaleString()}
              </p>
              <p className="outcome">{selected.outcomeSummary ?? "No outcome reported yet."}</p>
              {selected.calls.map((call) => (
                <TranscriptView key={call.id} call={call} />
              ))}
            </>
          ) : (
            <p>Select a task to review its calls.</p>
          )}
        </div>
      </div>
    </main>
  );
}
