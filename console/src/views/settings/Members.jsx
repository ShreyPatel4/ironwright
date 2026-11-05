import { useCallback, useEffect, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";

const ROLES = ["viewer", "member", "admin", "owner"];

export default function Members({ slug, me }) {
  const [got, setGot] = useState(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [link, setLink] = useState(null);
  const [said, setSaid] = useState("");
  const [err, setErr] = useState(null);

  const load = useCallback(() => api.members(slug).then(setGot).catch(setErr), [slug]);
  useEffect(() => { load(); }, [load]);

  const isOwner = me.role === "owner";
  const grantable = isOwner ? ROLES : ROLES.filter((r) => r !== "owner");

  async function act(work, message) {
    setErr(null); setSaid("");
    try { await work(); setSaid(message); }
    catch (e) { setErr(e); }
    await load();
  }

  async function invite(e) {
    e.preventDefault();
    setErr(null); setSaid(""); setLink(null);
    try {
      const made = await api.invite(slug, email, role);
      setLink(`${location.origin}${location.pathname}#invite=${made.token}`);
      setSaid(`Invited ${made.email}. Send them the link. It works once.`);
      setEmail("");
    } catch (e2) { setErr(e2); }
    await load();
  }

  return (
    <>
      <p className="sub">Who is in this org and what they can do. A change applies at once,
        and a removed person's session ends right away.</p>
      <form className="row" onSubmit={invite}>
        <input aria-label="Email to invite" onChange={(e) => setEmail(e.target.value)}
               placeholder="name@company.com" style={{ width: 240 }} type="text" value={email} />
        <select aria-label="Role for the invite" onChange={(e) => setRole(e.target.value)} value={role}>
          {grantable.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <button className="btn" disabled={!email.trim()} type="submit">Invite</button>
        <button className="btn ghost" onClick={load} type="button">Refresh</button>
      </form>
      {link && (
        <div className="row">
          <label className="note" htmlFor="invite-link">Invite link</label>
          <input id="invite-link" readOnly style={{ width: 460 }} type="text" value={link} />
        </div>
      )}
      <p aria-live="polite" className="note" role="status">{said}</p>
      <Err e={err} />
      <div className="scroll"><table>
        <thead><tr><th>email</th><th>role</th><th></th></tr></thead>
        <tbody>
          {got?.members.map((m) => {
            const locked = m.role === "owner" && !isOwner;
            return (
              <tr key={m.user_id}>
                <td className="mono">{m.email}{m.user_id === me.user_id ? " (you)" : ""}</td>
                <td>
                  <select aria-label={`Role of ${m.email}`} disabled={locked}
                          onChange={(e) => {
                            const to = e.target.value;
                            act(() => api.setMemberRole(slug, m.user_id, to), `${m.email} is now ${to}`);
                          }}
                          value={m.role}>
                    {(locked ? ROLES : grantable).map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                </td>
                <td>
                  <button className="btn danger" disabled={locked}
                          onClick={() => act(() => api.removeMember(slug, m.user_id), `Removed ${m.email}`)}>
                    Remove
                  </button>
                </td>
              </tr>
            );
          })}
          {got?.invites.map((i) => (
            <tr key={i.email}>
              <td className="mono">{i.email}</td>
              <td><span className="badge hold">invited as {i.role}</span></td>
              <td className="note">until {new Date(i.expires_at * 1000).toLocaleDateString()}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
    </>
  );
}
