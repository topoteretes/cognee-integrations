"""The knowledge-graph view: what the tenant's memory actually looks like.

Primary path: relay cognee's own visualization page (``GET /api/v1/visualize``
on the server/tenant — the official product rendering), one dataset at a
time, with a small switcher bar injected so the presenter can flip between
memory layers. The multi-dataset endpoint is superuser-only, hence per-dataset.

Fallback path: a hand-rolled canvas force layout over the merged dataset
graphs — zero external assets, kept for when the visualize route is
unavailable (older servers, degraded tenant).
"""

from __future__ import annotations

import json
from typing import Any, Optional

MAX_NODES_DEFAULT = 350

# dataset name -> (layer key, display name, color)
LAYERS = {
    "files": ("files", "Your files", "#8B5CFF"),
    "handover": ("handover", "Team handovers", "#FF5CA8"),
    "agent": ("agent", "Agent sessions", "#37D2C4"),
    "docs": ("docs", "Documents", "#FFB020"),
}


def classify_dataset(name: str, own: str = "") -> str:
    if name.startswith("handover-") or name.startswith("team-") or name == "org-memory":
        return "handover"
    if "agent" in name or "session" in name:
        return "agent"
    if name == (own or "main"):
        return "files"
    return "docs"


async def native_visualization(
    adapter: Any,
    dataset: str = "",
    max_nodes: int = MAX_NODES_DEFAULT,
    query: str = "",
    exclude: Any = None,
) -> Optional[str]:
    """cognee's own visualization HTML for one dataset, with a layer-switcher
    bar injected. ``query`` focuses the view on that topic's neighborhood —
    essential for large layers (agent sessions run to thousands of nodes, and
    an unfocused view is a 350-node random sample). Returns None when the
    server can't provide it."""
    try:
        response = await adapter._request("GET", "/api/v1/datasets")
        if response.status_code >= 400:
            return None
        datasets = response.json()
        if not isinstance(datasets, list) or not datasets:
            return None
        by_name = {
            str(d.get("name", "")): str(d.get("id", ""))
            for d in datasets
            if str(d.get("name", "")) not in set(exclude or ())
        }
        if not by_name:
            return None
        own = str(getattr(adapter, "dataset", "") or "")
        target = dataset if dataset in by_name else (own if own in by_name else next(iter(by_name)))
        url = f"/api/v1/visualize?dataset_id={by_name[target]}&max_nodes={max_nodes}"
        if query:
            from urllib.parse import quote

            url += f"&query={quote(query)}"
        viz = await adapter._request("GET", url)
        if viz.status_code >= 400 or "<html" not in viz.text[:200].lower():
            return None
        return _inject_switcher(viz.text, sorted(by_name), target, query)
    except Exception:
        return None


def _inject_switcher(html: str, names: list[str], active: str, query: str = "") -> str:
    """A floating bar linking each dataset's graph — the memory-layer story.

    The host page (cognee's viz app) installs global click handlers and
    ``preventDefault``s liberally, so plain anchors never navigate. A
    capture-phase listener on the window fires before the app's handlers and
    navigates explicitly; the ``href`` stays for middle-click/new-tab.
    """
    from urllib.parse import quote

    suffix = f"&query={quote(query)}" if query else ""
    links = " ".join(
        f'<a href="/graph?dataset={name}{suffix}" data-graph-link="1" '
        f'style="color:{"#fff" if name == active else "#b7b1cf"};'
        f"text-decoration:none;font-weight:{600 if name == active else 400};"
        'pointer-events:auto;cursor:pointer">'
        f"{name}</a>"
        for name in names
    )
    bar = (
        '<div id="cg-layer-switch" style="position:fixed;top:56px;right:14px;'
        "background:#1d1930e6;border:1px solid #3a3357;border-radius:10px;"
        "padding:8px 14px;font:12px -apple-system,sans-serif;z-index:2147483647;"
        'pointer-events:auto;display:flex;gap:12px;align-items:center">'
        '<span style="color:#6f6890">memory layer:</span>' + links + "</div>"
        "<script>window.addEventListener('click',function(e){"
        "var a=e.target&&e.target.closest&&e.target.closest('[data-graph-link]');"
        "if(a){e.stopImmediatePropagation();e.preventDefault();"
        "window.location.assign(a.getAttribute('href'));}},true);</script>"
    )
    return html.replace("</body>", bar + "</body>") if "</body>" in html else html + bar


async def collect_graph(adapter: Any, max_nodes: int = MAX_NODES_DEFAULT) -> dict[str, Any]:
    """Fetch and merge dataset graphs from the server behind ``adapter``
    (an HttpCogneeAdapter). Caps to the ``max_nodes`` best-connected nodes."""
    response = await adapter._request("GET", "/api/v1/datasets")
    datasets = response.json() if response.status_code < 400 else []
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []
    counts: dict[str, int] = {}

    for ds in datasets if isinstance(datasets, list) else []:
        ds_id, name = str(ds.get("id", "")), str(ds.get("name", ""))
        layer = classify_dataset(name, str(getattr(adapter, "dataset", "") or ""))
        try:
            g = await adapter._request("GET", f"/api/v1/datasets/{ds_id}/graph")
            if g.status_code >= 400:
                continue
            payload = g.json()
        except Exception:
            continue
        for n in payload.get("nodes", []) or []:
            nid = str(n.get("id", ""))
            if not nid or nid in nodes:
                continue
            label = str(n.get("label", "") or n.get("name", "") or nid)[:60]
            # entity labels are readable; internal ids are noise
            if "_" in label and label.split("_")[-1].count("-") == 4:
                label = label.rsplit("_", 1)[0]
            nodes[nid] = {"id": nid, "label": label, "type": str(n.get("type", "")), "layer": layer}
            counts[layer] = counts.get(layer, 0) + 1
        for e in payload.get("edges", []) or []:
            source = str(e.get("source") or e.get("source_node_id") or e.get("from") or "")
            target = str(e.get("target") or e.get("target_node_id") or e.get("to") or "")
            if source and target:
                edges.append({"s": source, "t": target, "l": str(e.get("label", "") or "")[:40]})

    # keep the best-connected nodes; drop dangling edges
    degree: dict[str, int] = {}
    for e in edges:
        degree[e["s"]] = degree.get(e["s"], 0) + 1
        degree[e["t"]] = degree.get(e["t"], 0) + 1
    keep = set(sorted(nodes, key=lambda nid: degree.get(nid, 0), reverse=True)[:max_nodes])
    kept_nodes = [{**nodes[nid], "d": degree.get(nid, 0)} for nid in nodes if nid in keep]
    kept_edges = [e for e in edges if e["s"] in keep and e["t"] in keep]
    return {"nodes": kept_nodes, "edges": kept_edges, "counts": counts}


def render_html(graph: dict[str, Any]) -> str:
    legend = "".join(
        f'<span class="chip"><i style="background:{color}"></i>{title} '
        f"({graph['counts'].get(key, 0)})</span>"
        for key, (key2, title, color) in LAYERS.items()
    )
    colors = {key: color for key, (_, _, color) in LAYERS.items()}
    return (
        _TEMPLATE.replace("__NODES__", json.dumps(graph["nodes"]))
        .replace("__EDGES__", json.dumps(graph["edges"]))
        .replace("__COLORS__", json.dumps(colors))
        .replace("__LEGEND__", legend)
    )


_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Your knowledge graph</title>
<style>
  html,body{margin:0;height:100%;background:#0e0c16;color:#e8e5f4;
    font:13px -apple-system,system-ui,sans-serif;overflow:hidden}
  #bar{position:fixed;top:0;left:0;right:0;padding:14px 18px;display:flex;
    gap:14px;align-items:center;background:linear-gradient(#0e0c16ee,#0e0c1600);z-index:2}
  #bar h1{font-size:15px;font-weight:600;margin:0 14px 0 0}
  .chip{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:#b7b1cf}
  .chip i{width:9px;height:9px;border-radius:50%;display:inline-block}
  #tip{position:fixed;pointer-events:none;background:#1d1930f2;border:1px solid #3a3357;
    border-radius:8px;padding:7px 10px;font-size:12px;display:none;z-index:3;max-width:340px}
  #tip b{color:#c9b8ff}
  canvas{display:block}
  #hint{position:fixed;bottom:12px;left:18px;color:#6f6890;font-size:11px}
</style></head><body>
<div id="bar"><h1>Your knowledge graph</h1>__LEGEND__</div>
<div id="tip"></div>
<div id="hint">drag to pan · scroll to zoom · hover a node</div>
<canvas id="c"></canvas>
<script>
const NODES=__NODES__, EDGES=__EDGES__, COLORS=__COLORS__;
const canvas=document.getElementById('c'), ctx=canvas.getContext('2d');
const tip=document.getElementById('tip');
let W,H,scale=1,ox=0,oy=0,dragging=false,px=0,py=0,hover=null;
function resize(){W=innerWidth;H=innerHeight;canvas.width=W*devicePixelRatio;
  canvas.height=H*devicePixelRatio;canvas.style.width=W+'px';canvas.style.height=H+'px';
  ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);}
resize();addEventListener('resize',resize);
const byId={};
NODES.forEach((n,i)=>{const a=i/NODES.length*Math.PI*2,r=Math.min(innerWidth,innerHeight)*.35;
  n.x=innerWidth/2+r*Math.cos(a)+(Math.random()-.5)*60;
  n.y=innerHeight/2+r*Math.sin(a)+(Math.random()-.5)*60;n.vx=0;n.vy=0;byId[n.id]=n;});
const links=EDGES.map(e=>({a:byId[e.s],b:byId[e.t],l:e.l})).filter(e=>e.a&&e.b);
let alpha=1;
function step(){
  // repulsion
  for(let i=0;i<NODES.length;i++)for(let j=i+1;j<NODES.length;j++){
    const a=NODES[i],b=NODES[j];let dx=a.x-b.x,dy=a.y-b.y;
    let d2=dx*dx+dy*dy;if(d2<1)d2=1;if(d2>90000)continue;
    const f=1400/d2*alpha;const d=Math.sqrt(d2);dx/=d;dy/=d;
    a.vx+=dx*f;a.vy+=dy*f;b.vx-=dx*f;b.vy-=dy*f;}
  // springs
  links.forEach(e=>{let dx=e.b.x-e.a.x,dy=e.b.y-e.a.y;
    const d=Math.sqrt(dx*dx+dy*dy)||1,f=(d-70)*.02*alpha;dx/=d;dy/=d;
    e.a.vx+=dx*f;e.a.vy+=dy*f;e.b.vx-=dx*f;e.b.vy-=dy*f;});
  // centering + integrate
  NODES.forEach(n=>{n.vx+=(innerWidth/2-n.x)*.0015*alpha;
    n.vy+=(innerHeight/2-n.y)*.0015*alpha;
    n.x+=n.vx;n.y+=n.vy;n.vx*=.85;n.vy*=.85;});
  alpha=Math.max(alpha*.995,.02);
}
function draw(){
  ctx.clearRect(0,0,W,H);ctx.save();ctx.translate(ox,oy);ctx.scale(scale,scale);
  ctx.strokeStyle='rgba(140,125,200,.16)';ctx.lineWidth=1/scale;
  links.forEach(e=>{ctx.beginPath();ctx.moveTo(e.a.x,e.a.y);ctx.lineTo(e.b.x,e.b.y);ctx.stroke();});
  NODES.forEach(n=>{const r=Math.min(3+Math.sqrt(n.d||0)*1.6,14);
    ctx.beginPath();ctx.arc(n.x,n.y,r,0,7);
    ctx.fillStyle=COLORS[n.layer]||'#888';
    ctx.globalAlpha=hover&&hover!==n?.45:1;ctx.fill();ctx.globalAlpha=1;
    if(hover===n){ctx.strokeStyle='#fff';ctx.lineWidth=1.5/scale;ctx.stroke();}});
  ctx.restore();
}
function loop(){step();draw();requestAnimationFrame(loop);}loop();
canvas.addEventListener('mousedown',e=>{dragging=true;px=e.clientX;py=e.clientY;});
addEventListener('mouseup',()=>dragging=false);
addEventListener('mousemove',e=>{
  if(dragging){ox+=e.clientX-px;oy+=e.clientY-py;px=e.clientX;py=e.clientY;return;}
  const mx=(e.clientX-ox)/scale,my=(e.clientY-oy)/scale;hover=null;
  for(const n of NODES){const dx=n.x-mx,dy=n.y-my;
    if(dx*dx+dy*dy<200){hover=n;break;}}
  if(hover){tip.style.display='block';tip.style.left=(e.clientX+14)+'px';
    tip.style.top=(e.clientY+10)+'px';
    tip.innerHTML='<b>'+hover.label.replace(/</g,'&lt;')+'</b><br>'+
      hover.type+' · '+(hover.d||0)+' connections';}
  else tip.style.display='none';});
canvas.addEventListener('wheel',e=>{e.preventDefault();
  const f=Math.exp(-e.deltaY*.001);
  ox=e.clientX-(e.clientX-ox)*f;oy=e.clientY-(e.clientY-oy)*f;scale*=f;},{passive:false});
</script></body></html>
"""
