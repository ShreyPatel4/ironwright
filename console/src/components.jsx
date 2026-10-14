// Shared pieces. The verification badge is deliberately everywhere a
// workspace's data is shown: the level is derived server-side from artifacts
// on disk and the console never softens it.
export function Badge({ v }) {
  if (!v) return null;
  return (
    <span className={`badge ${v.level}`} title={v.meaning}>
      {v.level} · {v.meaning}
    </span>
  );
}

export function Err({ e }) {
  return e ? <div className="err">{String(e.message || e)}</div> : null;
}

export function Rows({ columns, rows }) {
  return (
    <div className="scroll">
      <table>
        <thead><tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>{r.map((v, j) => <td className="mono" key={j}>{v === null ? "∅" : String(v)}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
