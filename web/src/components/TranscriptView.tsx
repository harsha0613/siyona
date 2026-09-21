import type { Call } from "../types";

interface Props {
  call: Call;
}

export function TranscriptView({ call }: Props) {
  return (
    <section className="transcript">
      <header>
        <h3>{call.calleeNumber}</h3>
        <span className={`badge badge--${call.status}`}>{call.status}</span>
        {call.medianTurnLatencyMs !== null && (
          <span className="latency">median {call.medianTurnLatencyMs} ms</span>
        )}
      </header>
      <ol>
        {call.transcripts.map((turn) => (
          <li key={turn.turnIndex} className={`turn turn--${turn.speaker}`}>
            <span className="speaker">{turn.speaker}</span>
            <span className="text">{turn.text}</span>
            {turn.latencyMs !== null && <span className="latency">{turn.latencyMs} ms</span>}
          </li>
        ))}
      </ol>
    </section>
  );
}
