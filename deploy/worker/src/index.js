// Narration endpoint. It only writes the explanation; decisions stay in the rules engine.
// POST { system, prompt } -> { text, model }
const MODEL = "@cf/meta/llama-3.1-8b-instruct";

export default {
  async fetch(req, env) {
    const cors = { "access-control-allow-origin": "*", "access-control-allow-headers": "content-type" };
    if (req.method === "OPTIONS") return new Response(null, { headers: cors });
    if (req.method !== "POST") return new Response("POST { system, prompt }", { status: 405, headers: cors });
    const { system = "", prompt = "" } = await req.json().catch(() => ({}));
    if (!prompt || prompt.length > 8000) return Response.json({ error: "prompt missing or too long" }, { status: 400, headers: cors });
    const out = await env.AI.run(MODEL, {
      messages: [{ role: "system", content: system }, { role: "user", content: prompt }],
      max_tokens: 400,
    });
    return Response.json({ text: out.response, model: MODEL }, { headers: cors });
  },
};
