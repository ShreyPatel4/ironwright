// Path route: coconutlabs.org/ironwright/* -> the Ironwright Pages site.
// The site only uses relative links, so it works under any prefix as long as
// directory URLs end with a slash. This Worker adds that slash and rewrites redirects.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const prefix = env.PREFIX || "/ironwright";
    if (url.pathname === prefix) return Response.redirect(`${url.origin}${prefix}/${url.search}`, 301);
    if (!url.pathname.startsWith(prefix + "/")) return fetch(request);

    const upstream = new URL(url.pathname.slice(prefix.length) + url.search, env.ORIGIN);
    const res = await fetch(new Request(upstream, request), { redirect: "manual" });

    const loc = res.headers.get("location");
    if (loc) {
      const target = new URL(loc, upstream);
      if (target.host === upstream.host) {
        const headers = new Headers(res.headers);
        headers.set("location", `${url.origin}${prefix}${target.pathname}${target.search}`);
        return new Response(res.body, { status: res.status, headers });
      }
    }
    return res;
  },
};
