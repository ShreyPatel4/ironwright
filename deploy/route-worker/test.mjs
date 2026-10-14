// Run: node deploy/route-worker/test.mjs  (mocks the upstream, checks prefix, slash and redirect handling)
import worker from "./src/index.js";
const env={ORIGIN:"https://ironwright.pages.dev",PREFIX:"/ironwright"};
globalThis.fetch=async (req)=>{const u=new URL(req.url);
  if(u.pathname==="/demo") return new Response(null,{status:308,headers:{location:"/demo/"}});
  return new Response("upstream "+u.href,{status:200});};
const cases=["https://coconutlabs.org/ironwright","https://coconutlabs.org/ironwright/","https://coconutlabs.org/ironwright/demo","https://coconutlabs.org/ironwright/films/?x=1","https://coconutlabs.org/other"];
for(const c of cases){const r=await worker.fetch(new Request(c),env);console.log(c,"->",r.status,r.headers.get("location")||await r.text());}
