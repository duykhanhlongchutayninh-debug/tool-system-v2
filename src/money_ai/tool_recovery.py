"""Bounded Python-rule recovery for a missing web-tool chat editor.

Used before attachment. It inspects current DOM controls, reads cached or
official help where available, permits only low-risk navigation, then
reobserves and validates a genuinely visible, editable browser element.

Permission contract: only reversible chat navigation and one safe same-page
reload; never click consent/login/account/payment/publish/send controls.
Never replay stale IDs, inject selectors/JS, or claim recovery without proof.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Any

from .core import GateError
from .tool_research import GuideResearch
from .adaptive_browser import OBSERVE_JS

# A recovery click is not the same as an upload click: there is NO composer
# to anchor to yet. Permit only obviously reversible chat navigation.
_OPEN_CHAT = re.compile(
    r"\b(?:new chat|new conversation|start chat|start a chat|"
    r"start chatting|open chat|try gemini|chat with gemini|"
    r"retry|try again)\b|trò chuyện mới|cuộc trò chuyện mới|thử lại",
    re.I)
_PROHIBITED = re.compile(
    r"login|log in|sign in|sign up|đăng nhập|accept|agree|consent|"
    r"continue with google|đồng ý|chấp nhận|payment|checkout|buy|"
    r"purchase|subscribe|upgrade|publish|post now|delete|remove|"
    r"send|submit|share|upload|attach|file|gửi|đăng|xóa|xoá",
    re.I)

REVEAL_SAFE_JS = r"""({x,y,expected})=>{
  let e=document.elementFromPoint(x,y);
  if(!e)return {ok:false,reason:'NO_LIVE_ELEMENT'};
  for(let j=0;j<8&&e?.shadowRoot;j++){
    const child=e.shadowRoot.elementFromPoint(x,y);
    if(!child||child===e)break;
    e=child;
  }
  const good=/\b(new chat|new conversation|start chat|start a chat|start chatting|open chat|try gemini|chat with gemini|retry|try again)\b|trò chuyện mới|cuộc trò chuyện mới|thử lại/i;
  const bad=/sign.?in|log.?in|sign.?up|accept|agree|consent|payment|checkout|purchase|subscribe|upgrade|publish|delete|send|submit|share|upload|attach|gửi|đăng|xóa|xoá|đồng ý|chấp nhận/i;
  for(let i=0;i<8&&e;i++,e=e.parentElement||(e.getRootNode()?.host||null)){
    const r=e.getBoundingClientRect();
    if(r.width<12||r.height<12||r.width>360||r.height>160)continue;
    if(r.right<=0||r.bottom<=0||r.left>=innerWidth||r.top>=innerHeight)continue;
    const style=getComputedStyle(e);
    const interactive=e.matches('button,a,[role=button],[role=link]')||
        style.cursor==='pointer';
    if(!interactive||e.disabled||e.getAttribute('aria-disabled')==='true')continue;
    const label=(e.getAttribute('aria-label')||'')+' '+
      (e.getAttribute('title')||'')+' '+(e.innerText||'').slice(0,85);
    if(bad.test(label))return {ok:false,reason:'FORBIDDEN_ACTION'};
    const wanted=String(expected||'').trim().toLowerCase();
    if(wanted.length>3&&!label.toLowerCase().includes(wanted.slice(0,35))&&
       !wanted.includes(label.toLowerCase().trim().slice(0,35)))continue;
    if(!good.test(label))continue;
    return {ok:true,reason:'BENIGN_NAVIGATION_GROUNDED'};
  }
  return {ok:false,reason:'NO_SAFE_RECOVERY_CONTROL'};
}"""


# A visually drawn input may not yet be contenteditable until focused.
# Candidate collection is generic HTML semantics, not Gemini CSS selectors.
# Actual keyboard use remains prohibited until the external probe validates
# the focused element is truly editable.
FOCUS_CANDIDATES_JS = r"""() => {
  const roots=[document],found=[],seen=new Set();let pass=0;
  const selector='textarea,input[type=text],input[type=search],input:not([type]),'+
    '[role=textbox],[placeholder],[contenteditable],'+
    '[class*="composer"],[class*="prompt-input"],[class*="text-input"]';
  while(roots.length&&pass++<65&&found.length<110) {
    const root=roots.shift();
    if(seen.has(root))continue;
    seen.add(root);
    for(const node of root.querySelectorAll(selector)){
      const r=node.getBoundingClientRect(),css=getComputedStyle(node);
      if(r.width<130||r.height<18||r.height>180||r.width>innerWidth||
         r.right<=0||r.bottom<=0||r.left>=innerWidth||r.top>=innerHeight||
         css.display==='none'||css.visibility==='hidden')continue;
      if(node.matches('button,a,[role=button],[role=link]') ||
         node.getAttribute('aria-disabled')==='true'||node.disabled)continue;
      const hint=(node.getAttribute('aria-label')||
        node.getAttribute('placeholder')||
        node.getAttribute('data-placeholder')||
        node.getAttribute('role')||'').slice(0,100);
      found.push({x:r.x+r.width/2,y:r.y+r.height/2,width:r.width,
          height:r.height,near:true,disabled:false,
          hint,role:node.tagName.toLowerCase()});
    }
    for(const node of root.querySelectorAll('*')){
      if(node.shadowRoot&&roots.length<65)roots.push(node.shadowRoot);
    }
  }
  found.sort((a,b)=>{
    const center=(v)=>Math.abs(v.x-innerWidth/2)+Math.abs(v.y-innerHeight*.56);
    return center(a)-center(b);
  });
  return found.slice(0,12).map((v,id)=>({...v,id}));
}"""

FOCUS_SAFE_JS = r"""({x,y}) => {
  let node=document.elementFromPoint(x,y);
  if(!node)return {ok:false,reason:'NO_LIVE_HIT'};
  for(let i=0;node?.shadowRoot&&i<8;i++){
    const child=node.shadowRoot.elementFromPoint(x,y);
    if(!child||child===node)break;
    node=child;
  }
  const bad=/sign.?in|log.?in|consent|accept|agree|payment|checkout|send|submit|publish|share|delete|đăng nhập|đồng ý|gửi|đăng|xoá/i;
  for(let i=0;node&&i<8;i++,
      node=node.parentElement||(node.getRootNode()?.host||null)) {
    const rect=node.getBoundingClientRect();
    if(rect.width<130||rect.height<18||rect.height>180)continue;
    // If the click hits any actual link/button, never reinterpret its
    // large parent as a text field. Otherwise a button overlay could be
    // clicked merely because an underlying wrapper says "composer".
    if(node.matches('button,a,[role=button],[role=link]'))
      return {ok:false,reason:'INTERACTIVE_NON_INPUT_TARGET'};
    if(node.disabled||node.readOnly||node.getAttribute('aria-disabled')==='true')
      return {ok:false,reason:'INPUT_DISABLED_OR_READONLY'};
    const label=(node.getAttribute('aria-label')||'')+' '+
      (node.getAttribute('placeholder')||'')+' '+
      (node.getAttribute('data-placeholder')||'');
    if(bad.test(label))return {ok:false,reason:'FORBIDDEN_INPUT'};
    const role=node.getAttribute('role')||'';
    const cls=(typeof node.className==='string'?node.className:'').toLowerCase();
    const inputType=(node.getAttribute('type')||'text').toLowerCase();
    if(node.tagName==='INPUT'&&!['text','search'].includes(inputType))
      return {ok:false,reason:'NON_TEXT_INPUT'};
    const candidate=node.tagName==='TEXTAREA'||
      (node.tagName==='INPUT'&&['text','search'].includes(inputType))||
      role==='textbox'||node.hasAttribute('placeholder')||
      node.hasAttribute('contenteditable')||
      /composer|prompt-input|text-input/.test(cls);
    if(candidate)return {ok:true,reason:'VISIBLE_INPUT_REGION'};
  }
  return {ok:false,reason:'NOT_AN_INPUT_REGION'};
}"""


def _safe_options(observation: dict, tried: set[tuple]) -> list[dict]:
    """Model sees ONLY safe, current, untried, small clickable controls."""
    result=[]
    for control in observation.get("controls",[]):
        label=str(control.get("hint",""))
        width=float(control.get("width",999))
        height=float(control.get("height",999))
        coord=(label[:60],round(float(control.get("x",0))/8),
               round(float(control.get("y",0))/8))
        if (coord in tried or control.get("disabled") or
                _PROHIBITED.search(label) or not _OPEN_CHAT.search(label) or
                not 12<=width<=360 or not 12<=height<=160):
            continue
        result.append(control)
    return result


def recover_editor(
    page, *, probe: Callable[[Any],Any],
    classify: Callable[[Any],str],
    researcher=None,
    remember: Callable|None=None,
    lessons: list[dict]|None=None,
    max_actions: int=5,
    provider: str="gemini.google.com",
    error_prefix: str="GEMINI",
):
    """Recover the real tool input, or STOP with a machine-readable reason.

    This recovery uses deterministic Python rules and fresh live DOM evidence.
    It never invokes an AI model or inference server.
    """
    # Provider URL must match the fixed allowlisted research catalog.
    from urllib.parse import urlparse
    provider=provider.lower().removeprefix("www.")
    if not re.fullmatch(r"[a-z0-9.-]{4,90}",provider):
        raise GateError("AGENT_RECOVERY_PROVIDER_INVALID")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,30}",error_prefix):
        raise GateError("AGENT_RECOVERY_ERROR_PREFIX_INVALID")
    research=researcher or GuideResearch()
    context=[]
    tried=set()
    print("MONEY_TOOL_RECOVERY_MODE=PYTHON_RULES_ONLY", flush=True)
    reloads=0
    successful_path=[]
    docs=""
    try:
        guide=research.learn(provider,"how to open the chat input on web")
        docs=guide.excerpt[:1400]
        print("MONEY_TOOL_RECOVERY_RESEARCH="+guide.source,flush=True)
    except (RuntimeError,ValueError):
        print("MONEY_TOOL_RECOVERY_RESEARCH=UNAVAILABLE",flush=True)

    for attempt in range(max_actions+1):
        try:
            editor=probe(page)
        except GateError:
            editor=None
        if editor is not None:
            print("MONEY_TOOL_RECOVERY=LIVE_EDITOR_VERIFIED",flush=True)
            if successful_path and remember is not None:
                # Only write long-term knowledge AFTER verified recovery,
                # never store a failed click as a proven fix.
                proof="LIVE_VISIBLE_EDITABLE_EDITOR_AFTER_"+successful_path[-1]
                remember(provider+"-composer-recovery",
                         "Missing/undiscoverable live chat editor",
                         "; ".join(successful_path)[-400:],proof)
                print("MONEY_TOOL_RECOVERY=LESSON_BACKED_UP",flush=True)
            return editor

        state=classify(page)
        if state in ("OWNER_LOGIN_REQUIRED","CONSENT_REQUIRED",
                     "UNEXPECTED_REDIRECT"):
            raise GateError(error_prefix+"_"+state)
        if attempt>=max_actions:
            break

        observation=page.evaluate(OBSERVE_JS)
        actual=(observation.get("url") or "").lower().removesuffix("/")
        if actual not in {"https://"+provider,"https://www."+provider}:
            raise GateError(error_prefix+"_RECOVERY_UNEXPECTED_ORIGIN")
        options=_safe_options(observation,tried)
        context.append({"cycle":attempt,"state":state,
                        "safe_controls":[{"id":v["id"],"hint":v["hint"][:80]}
                                         for v in options[:15]]})
        proposal = None
        if options:
            def _safe_navigation_rank(item):
                label = str(item.get("hint", "")).casefold()
                if re.search(r"new chat|new conversation|trò chuyện mới|cuộc trò chuyện mới",
                             label):
                    return 3
                if re.search(r"try again|retry|thử lại", label):
                    return 2
                return 1
            # _safe_options only returns reversible, current DOM navigation.
            selected_option = max(options, key=_safe_navigation_rank)
            proposal = {"action": "click", "name": str(selected_option["id"])}
            print("MONEY_TOOL_RECOVERY=PYTHON_SAFE_NAVIGATION", flush=True)
        action = proposal.get("action", "") if isinstance(proposal, dict) else ""
        if action == "click" and isinstance(proposal, dict):
            chosen_id = int(proposal["name"])
            target = next((v for v in options if int(v["id"]) == chosen_id), None)
            if target is not None:
                x, y = float(target["x"]), float(target["y"])
                key = (str(target["hint"])[:60], round(x/8), round(y/8))
                tried.add(key)
                verified = page.evaluate(REVEAL_SAFE_JS, {
                    "x": x, "y": y, "expected": str(target["hint"])[:60]})
                if verified.get("ok"):
                    page.mouse.click(x, y)
                    successful_path.append("PYTHON_SAFE_NAVIGATION_CONTROL_" + str(chosen_id))
                    print("MONEY_TOOL_RECOVERY=PYTHON_SAFE_NAVIGATION_CLICK", flush=True)
                    page.wait_for_timeout(650)
                    continue
                context.append({"action": "click", "id": chosen_id,
                                "denied": str(verified.get("reason", ""))[:50]})
                print("MONEY_TOOL_RECOVERY=CLICK_DENIED", flush=True)
                continue

        if reloads<1:
            # A reversible, bounded recovery for a stuck SPA. It does not
            # open another browser, login or activate any provider action.
            reloads+=1
            page.reload(wait_until="domcontentloaded",timeout=30000)
            successful_path.append("SAME_PAGE_RELOAD")
            print("MONEY_TOOL_RECOVERY=SAFE_SPA_RELOAD",flush=True)
            page.wait_for_timeout(1000)
            continue

        page.wait_for_timeout(1000)
        print("MONEY_TOOL_RECOVERY=OBSERVE_AGAIN",flush=True)

    raise GateError(error_prefix+"_EDITOR_SELF_RECOVERY_EXHAUSTED")
