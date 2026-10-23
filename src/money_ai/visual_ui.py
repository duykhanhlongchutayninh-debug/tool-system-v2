"""Deterministic Python recognition of visible controls in the SAME Chrome.

Use live DOM semantics first and CPU-only local pixel checks second. Every
candidate must pass a fresh DOM hit test, including open Shadow DOM boundaries.
No model API, second browser, remote desktop, or arbitrary coordinate click.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


from .core import GateError

# Only provider-controlled UI descriptions are sent; credentials and MP4 never.
_LABEL = re.compile(
    r"\badd(?:[ _-](?:files?|circle|2|outline))?\b|"
    r"\badd_circle\b|\badd-circle\b|\battachment\b|"
    r"\battach(?:ment|[ _-]?files?)\b|\battach_file\b|"
    r"\bupload(?:[ _-]?file)?\b|\bfile[ _-]?upload\b|"
    r"thêm tệp|thêm file|thêm tập tin|đính kèm|tải tệp|\bplus\b",
    re.I,
)
_NO = re.compile(r"send|submit|stop|close|delete|new chat|"
                 r"microphone|record|voice|share|publish|"
                 r"gửi|xóa|xoá|ghi âm|chia sẻ|đăng", re.I)
# Return stable selector indices, geometry and safe control metadata only.
CONTROL_JS = r"""() => {
  const roots=[document],seen=new Set();
  for(let i=0;i<roots.length&&i<120;i++){
    const root=roots[i];
    if(seen.has(root))continue;
    seen.add(root);
    for(const host of root.querySelectorAll('*')){
      if(host.shadowRoot&&roots.length<120)roots.push(host.shadowRoot);
    }
  }
  const deep=selector=>roots.flatMap(root=>[...root.querySelectorAll(selector)]);
  const visible=e=>{
    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>=9&&r.height>=9&&s.display!=='none'&&
      s.visibility!=='hidden'&&r.bottom>0&&r.top<innerHeight&&
      r.right>0&&r.left<innerWidth&&Number(s.opacity||1)>0.02;
  };
  const editors=deep(
    '[contenteditable="true"],[contenteditable="plaintext-only"],textarea'
  ).filter(visible);
  const editor=editors[editors.length-1];
  if(!editor)return {bounds:null,buttons:[]};
  const a=editor.getBoundingClientRect();
  const namedControls=deep(
    'button,[role="button"],[aria-haspopup],[tabindex],'+
    '[class*="add-file"],[class*="upload"],[class*="attach"],'+
    '[data-testid*="upload"],[data-test-id*="upload"]'
  );
  const iconWrappers=[];
  for(const e of deep('div,span,svg,mat-icon')){
    const r=e.getBoundingClientRect();
    if(r.width<12||r.height<12||r.width>90||r.height>90)continue;
    const x=r.x+r.width/2,y=r.y+r.height/2;
    if(x<a.x-215||x>a.x+105||y<a.y-180||y>a.bottom+145)continue;
    if(getComputedStyle(e).cursor!=='pointer')continue;
    iconWrappers.push(e);
    if(iconWrappers.length>=80)break;
  }
  const buttons=[...new Set([...namedControls,...iconWrappers])];
  return {
    bounds:{x:a.x,y:a.y,width:a.width,height:a.height},
    viewport:{width:innerWidth,height:innerHeight},
    buttons:buttons.map((e,i)=>{
      if(!visible(e))return null;
      const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
      if(x<a.x-215||x>a.right+105||y<a.y-180||y>a.bottom+145)return null;
      const fields=['aria-label','title','mattooltip','data-tooltip',
                    'data-testid','data-test-id','aria-haspopup']
                    .map(k=>e.getAttribute(k)||'');
      const cssClass=(typeof e.className==='string'?e.className:'').slice(0,90);
      const icons=[...e.querySelectorAll('mat-icon,[data-icon],svg')]
        .slice(0,3).map(k=>(k.textContent||'')+' '+
             (k.getAttribute('data-icon')||'')+' '+
             (k.getAttribute('aria-label')||'')+' '+
             (k.getAttribute('class')||'').slice(0,80));
      const hint=[...fields,cssClass,...icons,(e.innerText||e.textContent||'').slice(0,40)]
        .join(' ').replace(/\s+/g,' ').trim().slice(0,160);
      let ancestor=editor,common=false;
      for(let d=0;d<8&&ancestor;d++){
        if(ancestor===e||ancestor.contains(e)){common=true;break;}
        ancestor=ancestor.parentElement||(ancestor.getRootNode()?.host||null);
      }
      const left=x<a.x+90&&x>a.x-210&&r.width<90&&r.height<90;
      const role=(e.getAttribute('role')||e.tagName.toLowerCase()).toLowerCase();
      const score=(left?6:0)+(common?2:0);
      return {index:i,hint,x,y,width:r.width,height:r.height,left,score,role};
    }).filter(Boolean)
  };
}"""


@dataclass(frozen=True)
class VisualTarget:
    index: int
    x: float
    y: float
    hint: str
    score: int


def observe(page) -> dict:
    result = page.evaluate(CONTROL_JS)
    if not isinstance(result, dict) or not result.get("bounds"):
        raise GateError("VISUAL_COMPOSER_GEOMETRY_MISSING")
    return result


def candidate_controls(observation: dict) -> list[VisualTarget]:
    out = []
    for item in observation.get("buttons", []):
        hint = str(item.get("hint", ""))
        if _NO.search(hint):
            continue
        score = int(item.get("score", 0)) + (9 if _LABEL.search(hint) else 0)
        if score >= 7 and (item.get("left") or _LABEL.search(hint)):
            out.append(VisualTarget(int(item["index"]),float(item["x"]),
                                    float(item["y"]), hint,score))
    return sorted(out,key=lambda target:target.score,reverse=True)


def resolve_dom_candidate(observation: dict) -> VisualTarget | None:
    choices = candidate_controls(observation)
    if not choices:
        return None
    first = choices[0]
    # Multiple equally plausible icon-only controls = ambiguous. Use vision.
    if len(choices)>1 and first.score-choices[1].score < 3:
        return None
    return first


def _screenshot_clip(observation: dict) -> dict:
    r = observation["bounds"]
    size = observation.get("viewport", {})
    width,height = float(size["width"]),float(size["height"])
    x = max(0.0,min(width-10,float(r["x"])-235))
    y = max(0.0,min(height-10,float(r["y"])-180))
    return {"x":x,"y":y, "width":max(10.0,min(width-x,float(r["width"])+335)),
            "height":max(10.0,min(height-y,float(r["height"])+335))}


def vision_point(page, observation: dict, *, http=None) -> tuple[float, float] | None:
    """No model fallback in Tool Money; control selection is Python-only."""
    return None


def click_grounded(page, observation: dict, point: tuple[float,float]) -> bool:
    """Verify the REAL hit element on the current DOM before clicking.

    The old implementation stopped at e.closest(...). On Gemini, the SVG may
    be inside a LARGE div[tabindex], so closest() picked that wrapper instead
    of its small interactive icon/button and falsely rejected a good + icon.
    Traverse ancestors from inner to outer and accept the first SMALL control
    matching the observed compose-toolbar region; never bypass hit testing.
    """
    x,y=point
    result=page.evaluate(r"""({x,y,bounds})=>{
      const roots=[document],seen=new Set();
      for(let i=0;i<roots.length&&i<120;i++){
        const root=roots[i];
        if(seen.has(root))continue;
        seen.add(root);
        for(const host of root.querySelectorAll('*'))
          if(host.shadowRoot&&roots.length<120)roots.push(host.shadowRoot);
      }
      const deep=selector=>roots.flatMap(root=>[...root.querySelectorAll(selector)]);
      let hit=document.elementFromPoint(x,y);
      if(!hit)return {ok:false,reason:'NO_HIT'};
      for(let depth=0;hit&&depth<10&&hit.shadowRoot;depth++){
        const inner=hit.shadowRoot.elementFromPoint?.(x,y);
        if(!inner||inner===hit)break;
        hit=inner;
      }
      const editors=deep(
        '[contenteditable="true"],[contenteditable="plaintext-only"],textarea'
      ).filter(el=>{
        const r=el.getBoundingClientRect(),s=getComputedStyle(el);
        return r.width>5&&r.height>5&&s.visibility!=='hidden'&&
          s.display!=='none'&&r.bottom>0&&r.top<innerHeight;
      });
      const rect=el=>el.getBoundingClientRect();
      let editor=editors.find(el=>{
        const r=rect(el);
        return Math.abs(r.x-Number(bounds?.x))<5&&
          Math.abs(r.y-Number(bounds?.y))<5&&
          Math.abs(r.width-Number(bounds?.width))<8&&
          Math.abs(r.height-Number(bounds?.height))<8;
      });
      if(!editor)return {ok:false,reason:'COMPOSER_CHANGED'};
      const r=rect(editor);
      const forbidden=/new chat|new conversation|send|submit|voice|microphone|record|stop|close|delete|remove|publish|share|logout|log.?in|sign.?in|sign.?up|accept|agree|consent|payment|checkout|subscribe|\bgửi\b|ghi âm|đăng|xóa|xoá|đồng ý/i;
      const isSemantic=e=>e.matches('button,[role="button"],[role="menuitem"],'+
        '[aria-haspopup],[class*="add-file"],[class*="upload"],[class*="attach"],'+
        '[data-testid*="upload"],[data-test-id*="upload"]');
      const isInteractive=e=>{
        const a=rect(e),semantic=isSemantic(e);
        const custom=getComputedStyle(e).cursor==='pointer'&&
          a.width>=10&&a.height>=10&&a.width<=110&&a.height<=110;
        return semantic||custom;
      };
      const safeLabel=e=>{
        const attrs=['aria-label','title','mattooltip','data-tooltip','data-testid',
                     'data-test-id','role'].map(k=>e.getAttribute?.(k)||'').join(' ');
        const cls=typeof e.className==='string'?e.className:'';
        const a=rect(e);
        // Ignore broad ancestor text (which can include sidebar "New chat")
        // but retain labels/classes on each actual small interactive control.
        const ownText=(a.width<=110&&a.height<=110)
          ?(e.innerText||e.textContent||'').slice(0,80):'';
        const icons=e.querySelectorAll
          ?[...e.querySelectorAll('mat-icon,[data-icon],svg')].slice(0,3)
             .map(k=>(k.textContent||'')+' '+(k.getAttribute('data-icon')||'')+' '+
                        (k.getAttribute('aria-label')||'')).join(' ')
          :'';
        return attrs+' '+cls+' '+ownText+' '+icons;
      };
      const chain=[];
      for(let e=hit;e&&chain.length<14;
          e=e.parentElement||(e.getRootNode?.().host||null))chain.push(e);
      const actionable=chain.filter(isInteractive);
      if(actionable.some(e=>forbidden.test(safeLabel(e))))
        return {ok:false,reason:'DISALLOWED_CONTROL'};
      let reason='NO_CLICKABLE_SMALL_ANCESTOR';
      for(const e of chain){
        const a=rect(e),cx=a.x+a.width/2,cy=a.y+a.height/2;
        if(a.width<10||a.height<10||a.width>90||a.height>90){
          reason='TARGET_TOO_LARGE_OR_SMALL';continue;
        }
        const near=cx>=r.x-215&&cx<r.x+105&&
          Math.abs(cy-(r.y+r.height/2))<180;
        if(!near){reason='TARGET_NOT_NEAR_COMPOSER';continue;}
        if(e.disabled||e.getAttribute('aria-disabled')==='true'){
          reason='DISABLED_CONTROL';continue;
        }
        const semantic=isSemantic(e);
        const custom=getComputedStyle(e).cursor==='pointer';
        if(!semantic&&!custom){reason='NO_CLICKABLE_SEMANTICS';continue;}
        return {ok:true,reason:'HIT_CONFIRMED'};
      }
      return {ok:false,reason};
    }""",{"x":x,"y":y,"bounds":observation["bounds"]})
    if not result.get("ok"):
        reason=result.get("reason","UNKNOWN")
        if not isinstance(reason,str) or not re.fullmatch(r"[A-Z_]{3,48}",reason):
            reason="UNKNOWN"
        print("TOOL_SYSTEM_VISUAL_CLICK=BLOCKED_"+reason,flush=True)
        return False
    page.mouse.click(x,y)
    print("TOOL_SYSTEM_VISUAL_CLICK=COMPOSER_ADD_FILES",flush=True)
    return True


def click_add_files(page) -> None:
    """Observe once; try semantic DOM then CPU pixel recognition, never guess.

    The current hit target and all its ancestors must pass the live DOM safety
    gate. Ambiguous or obstructed controls fail closed.
    """
    observation=observe(page)
    controls=observation.get("buttons",[])
    print("TOOL_SYSTEM_VISUAL_OBSERVATION=CONTROL_COUNT_"+str(min(len(controls),999)),
          flush=True)
    attempts=[]
    candidate=resolve_dom_candidate(observation)
    if candidate is not None and _LABEL.search(candidate.hint):
        print("TOOL_SYSTEM_VISUAL_OBSERVATION=SEMANTIC_DOM_GROUNDED",flush=True)
        point=(candidate.x,candidate.y)
        attempts.append(point)
        if click_grounded(page,observation,point):
            return
        print("TOOL_SYSTEM_VISUAL_OBSERVATION=SEMANTIC_HIT_REJECTED",flush=True)

    from .visual_pixels import locate_visible_plus
    print("TOOL_SYSTEM_VISUAL_OBSERVATION=LOCAL_PIXEL_CHECK",flush=True)
    pixel=locate_visible_plus(page,observation)
    if pixel is not None:
        if all(abs(pixel[0]-x)>8 or abs(pixel[1]-y)>8 for x,y in attempts):
            attempts.append(pixel)
            if click_grounded(page,observation,pixel):
                return
        else:
            print("TOOL_SYSTEM_VISUAL_OBSERVATION=PIXEL_SAME_REJECTED_HIT",flush=True)

    # There is deliberately no model/provider fallback in Tool Money.
    # DOM semantics and the local CPU pixel recognizer are the only candidates.
    print("TOOL_SYSTEM_VISUAL_OBSERVATION=MODEL_FALLBACK_DISABLED", flush=True)
    print("TOOL_SYSTEM_VISUAL_OBSERVATION=ALL_GROUNDED_TARGETS_REJECTED",flush=True)
    raise GateError("GEMINI_VISUAL_ADD_BUTTON_UNRESOLVED")
