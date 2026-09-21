import type { Task } from "../types";

interface Props {
  tasks: Task[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function TaskList({ tasks, selectedId, onSelect }: Props) {
  return (
    <nav className="task-list">
      <h2>Tasks</h2>
      <ul>
        {tasks.map((task) => (
          <li key={task.id}>
            <button
              type="button"
              className={task.id === selectedId ? "selected" : undefined}
              onClick={() => onSelect(task.id)}
            >
              <span className="instruction">{task.instruction}</span>
              <span className={`badge badge--${task.status}`}>{task.status}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
