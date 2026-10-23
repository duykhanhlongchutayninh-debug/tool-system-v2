"""Deterministic Python UI operator: research -> observe -> rule -> act -> verify.

A single safe browser operator works against the owner's existing Chrome for
Gemini, Meta AI and Google Flow. It chooses fresh DOM controls with bounded
Python rules and checks each click plus the resulting visible state change.
Ambiguous controls are rejected rather than guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import json
import os
import re
import time

from .core import GateError
from .tool_research import GuideResearch

# Generic role + visual control listing, limited to VISIBLE elements on the
# currently open provider tab. No cookies, credentials or hidden DOM dump.
OBSERVE_JS = r"""() => {
  // Read through OPEN Shadow DOM used by Angular/web components. This is
  // live DOM inspection, never a guessed provider-specific button selector.
  const deep=(selector)=>{
    const roots=[document],seen=new Set(),elements=[];
    let iter=0;
    while(roots.length&&iter++<90) {
      const root=roots.shift();
      if(seen.has(root))continue;
      seen.add(root);
      for(const e of root.querySelectorAll(selector))elements.push(e);
      for(const host of root.querySelectorAll('*')){
        if(host.shadowRoot&&roots.length<90)roots.push(host.shadowRoot);
      }
    }
    return elements;
  };
  const editor=deep(
      '[contenteditable="true"],[contenteditable="plaintext-only"],textarea,'+
      'input:not([type=hidden]):not([type=file]):not([type=password]),[role=textbox]')
    .filter(e=>{const r=e.getBoundingClientRect(),cs=getComputedStyle(e);
      const tag=e.tagName.toLowerCase();
      const editable=e.isContentEditable||tag==='textarea'||
        (tag==='input'&&!e.readOnly&&!e.disabled);
      return editable&&r.width>40&&r.height>12&&r.bottom>0&&
        r.top<innerHeight&&cs.visibility!=='hidden'&&cs.display!=='none';}).at(-1);
  const er=editor?.getBoundingClientRect();
  const selectors='button,[role="button"],[role="menuitem"],[aria-haspopup],'+
    '[tabindex],[class*="upload"],[class*="attach"],[class*="add-file"]';
  const named=deep(selectors);
  // UI libraries sometimes wrap the + icon in an unlabeled, pointer-cursor
  // div/span/SVG. Discover these dynamically ONLY near the current composer.
  const iconWrappers=[];
  if(er)for(const e of deep('div,span,svg,mat-icon')){
    const z=e.getBoundingClientRect();
    if(z.width<12||z.height<12||z.width>95||z.height>95)continue;
    const mx=z.x+z.width/2,my=z.y+z.height/2;
    if(mx<er.x-245||mx>er.right+105||
       my<er.y-185||my>er.bottom+175)continue;
    if(getComputedStyle(e).cursor==='pointer')iconWrappers.push(e);
    if(iconWrappers.length>=65)break;
  }
  const candidates=[...new Set([...named,...iconWrappers])];
  const items=[];
  for(const e of candidates) {
    if(items.length>420)break;
    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    if(r.width<8||r.height<8||r.width>350||r.height>140 ||
       s.visibility==='hidden'||s.display==='none' ||
       r.bottom<0||r.top>innerHeight||r.right<0||r.left>innerWidth)continue;
    const text=(e.getAttribute('aria-label')||e.getAttribute('title')||
      e.getAttribute('data-testid')||e.innerText||'').slice(0,95);
    const icon=(e.querySelector('mat-icon,[data-icon]')?.textContent||'').slice(0,35);
    const css=(typeof e.className==='string'?e.className:'').slice(0,60);
    const svg=(e.querySelector('svg')?.getAttribute('aria-label')||'').slice(0,50);
    const hint=(text+' '+icon+' '+svg+' '+css).trim().slice(0,150);
    const x=r.x+r.width/2,y=r.y+r.height/2;
    const near=!!er&&x>er.x-210&&x<er.right+100&&
      y>er.y-190&&y<er.bottom+185;
    // Never assign persistent selectors or mutate the webpage.
    items.push({x,y,width:r.width,height:r.height,
      role:e.getAttribute('role')||e.tagName.toLowerCase(),
      hint,near,disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true'});
  }
  // Sidebar history can put many controls before the composer. Give
  // Material icon names explicit priority before applying the 90-control cap.
  const uploadPriority=/upload|attach(?:ment|[_ -]?files?)?|add[_ -]?(?:circle|files?|2|outline)|\badd\b|\bplus\b|paperclip|tải tệp/i;
  items.sort((a,b)=>Number(b.near)-Number(a.near) ||
    Number(uploadPriority.test(b.hint))-Number(uploadPriority.test(a.hint)));
  const found=items.slice(0,90).map((item,id)=>({...item,id}));
  return {url:location.origin,editor:er?
    {x:er.x,y:er.y,width:er.width,height:er.height}:null,
    controls:found,
    body:(document.body?.innerText||'').slice(0,900)};
}"""

# Python selects a candidate from the fresh DOM snapshot. The actual hit element
# must still be live and clickable; the operator cannot use arbitrary CSS.
SAFE_CLICK_JS = r"""({x,y,allowMenu,expected})=>{
  let node=document.elementFromPoint(x,y);
  if(!node)return {ok:false,reason:'NO_VISIBLE_HIT'};
  // document.elementFromPoint returns a shadow HOST, not the actual button.
  // Descend only through open shadow roots; do not invent a coordinate.
  for(let d=0;node?.shadowRoot&&d<8;d++){
    const inside=node.shadowRoot.elementFromPoint(x,y);
    if(!inside||inside===node)break;
    node=inside;
  }
  const forbidden=/publish|post now|delete|remove account|payment|checkout|new chat|send|submit|share|logout|log.?in|sign.?in|sign.?up|accept|agree|consent|subscribe|gửi|đăng|xóa|xoá|đồng ý/i;
  const deep=(selector)=>{
    const roots=[document],seen=new Set(),items=[];
    let count=0;
    while(roots.length&&count++<90){
      const root=roots.shift();
      if(seen.has(root))continue;
      seen.add(root);
      for(const e of root.querySelectorAll(selector))items.push(e);
      for(const e of root.querySelectorAll('*')){
        if(e.shadowRoot&&roots.length<90)roots.push(e.shadowRoot);
      }
    }
    return items;
  };
  const docs=deep(
      '[contenteditable="true"],[contenteditable="plaintext-only"],textarea,'+
      'input:not([type=hidden]):not([type=file]):not([type=password]),[role=textbox]')
    .filter(e=>{const t=e.tagName.toLowerCase(),r=e.getBoundingClientRect();
      return (e.isContentEditable||t==='textarea'||
        (t==='input'&&!e.disabled&&!e.readOnly))&&r.width>40&&r.height>12;});
  const editor=docs.at(-1);
  const r=editor?.getBoundingClientRect();
  if(!r)return {ok:false,reason:'NO_COMPOSER'};
  for(let i=0;node&&i<8;
      node=node.parentElement||(node.getRootNode()?.host||null),i++){
    const b=node.getBoundingClientRect(),cs=getComputedStyle(node);
    const semantic=node.matches('button,[role="button"],[role="menuitem"],'+
      '[aria-haspopup],[class*="upload"],[class*="attach"]');
    if(!semantic&&cs.cursor!=='pointer')continue;
    if(b.width<10||b.height<10||b.width>350||b.height>140)continue;
    const label=(node.getAttribute('aria-label')||'')+' '+
      (node.getAttribute('title')||'')+' '+
      (node.innerText||'').slice(0,50);
    if(forbidden.test(label))return {ok:false,reason:'DANGEROUS_CONTROL'};
    const want=String(expected||'').toLowerCase().trim();
    const actual=label.toLowerCase().trim();
    if(want.length>3&&actual.length>3 &&
       !actual.includes(want.slice(0,28))&&!want.includes(actual.slice(0,28)))
      continue; // Hit a different control than the observed proposed ID.
    if(node.disabled||node.getAttribute('aria-disabled')==='true')
      return {ok:false,reason:'CONTROL_DISABLED'};
    const cx=b.x+b.width/2,cy=b.y+b.height/2;
    const near=cx>r.x-210&&cx<r.right+100&&cy>r.y-190&&cy<r.bottom+185;
    const menu=node.getAttribute('role')==='menuitem'||/upload files|tải tệp|files/i.test(label);
    if(!near&&!(allowMenu&&menu))continue;
    return {ok:true,reason:'GROUNDED'};
  }
  return {ok:false,reason:'NO_SAFE_INTERACTIVE_ANCESTOR'};
}"""


@dataclass(frozen=True)
class BrowserDecision:
    control_id: int
    reason: str
    source: str


def _bad(hint: str) -> bool:
    return bool(re.search(r"send|submit|delete|publish|post now|share|"
                          r"new chat|microphone|voice|log.?out|log.?in|"
                          r"sign.?in|sign.?up|accept|agree|consent|payment|"
                          r"checkout|subscribe|gửi|đăng|xóa|xoá|đồng ý",
                          hint,re.I))


_UPLOAD_CONTROL = re.compile(
    r"upload[ _-]?files?|file[ _-]?upload|from computer|browse files|"
    r"attachment|\battach(?:ment|[ _-]?files?)\b|"
    r"\badd(?:[ _-](?:circle|files?|2|outline))?\b|"
    r"\badd_circle\b|\badd-circle\b|\battach_file\b|"
    r"\bfile_upload\b|\bplus(?:[ _-]?(?:circle|icon))?\b|"
    r"thêm tệp|thêm file|thêm tập tin|đính kèm|tải tệp|\btệp\b",
    re.I,
)


def _score(control: dict, *, stage: str, guidance: str) -> int:
    hint = str(control.get("hint", ""))
    if control.get("disabled") or _bad(hint):
        return -100
    # A broad Gemini composer wrapper may inherit "add/upload" CSS/text.
    # Never mistake its CENTER for the small clickable plus icon.
    if stage == "toolbar" and (
            float(control.get("width", 0)) > 110 or
            float(control.get("height", 0)) > 85):
        return -100
    label_match = bool(_UPLOAD_CONTROL.search(hint))
    if stage == "menu":
        menu_pattern = re.compile(
            r"upload[ _-]?files?|file[ _-]?upload|browse files|from computer|"
            r"tải tệp|thêm tệp", re.I)
        exact = bool(menu_pattern.search(hint))
    else:
        exact = label_match
    value = 9 if exact else 0
    if stage == "menu" and "upload" in guidance.casefold() and "file" in hint.casefold():
        value += 3
    if control.get("near"):
        value += 4
    if float(control.get("width", 100)) > 110 and stage != "menu":
        value -= 5
    # Material/Angular icon text is sometimes "add_2", "attach_file" or
    # "add_circle" rather than the accessible phrase "Add files". These are
    # grounded labels in the current DOM, not guessed screen coordinates.
    return value


def candidate_ids(observation: dict, *, stage: str, guidance: str,
                  forbidden: set[int] | None = None) -> list[int]:
    ban = forbidden or set()
    threshold = 7 if stage == "toolbar" else 8
    ranked = sorted(
        ((int(c["id"]), _score(c, stage=stage, guidance=guidance))
         for c in observation.get("controls", [])
         if int(c["id"]) not in ban),
        key=lambda pair: pair[1], reverse=True)
    # A completely unlabeled toolbar is NOT enough evidence for a blind click.
    return [idx for idx, score in ranked if score >= threshold]


ATTACHMENT_PREVIEW_EVIDENCE_JS = r"""({filename,stem}) => {
  const norm=s=>(s||"").replace(/\s+/g," ").trim().toLocaleLowerCase();
  const wanted=norm(filename), shortStem=norm(stem);
  const roots=[document],seen=new Set();
  const visible=e=>{
    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&
      r.top<innerHeight&&r.left<innerWidth&&s.display!=="none"&&
      s.visibility!=="hidden"&&Number(s.opacity||1)>0.02;
  };
  const editors=[];
  for(let i=0;i<roots.length&&i<140;i++){
    const root=roots[i];
    if(seen.has(root))continue;
    seen.add(root);
    for(const host of root.querySelectorAll("*"))
      if(host.shadowRoot&&roots.length<140)roots.push(host.shadowRoot);
    for(const el of root.querySelectorAll(
      'rich-textarea [contenteditable="true"],[contenteditable="true"],'+
      '[contenteditable="plaintext-only"],textarea,[role="textbox"]')){
      if(!visible(el)||el.disabled||el.readOnly)continue;
      const r=el.getBoundingClientRect();
      const isEditable=el.isContentEditable||el.tagName.toLowerCase()==="textarea";
      if(isEditable&&r.width>=80&&r.height>=12)
        editors.push({el,r,area:r.width*r.height});
    }
  }
  editors.sort((a,b)=>b.area-a.area);
  if(!editors.length)return {attached:false,busy:false,error:false,reason:"NO_CURRENT_COMPOSER"};
  const editor=editors[0].r;
  let best=null;
  for(const root of roots){
    for(const node of root.querySelectorAll("*")){
      if(!visible(node)||node.matches("body,html,textarea,input,[contenteditable]"))continue;
      const text=norm(node.innerText||node.textContent||"");
      if(text.length<3||text.length>260)continue;
      if(!(wanted&&text.includes(wanted))&&
         !(shortStem&&shortStem.length>=8&&text.includes(shortStem)))continue;
      const r=node.getBoundingClientRect();
      if(r.width<18||r.width>900||r.height<12||r.height>260)continue;
      const cx=r.x+r.width/2,cy=r.y+r.height/2;
      const editorCx=editor.x+editor.width/2,editorCy=editor.y+editor.height/2;
      const nearX=Math.abs(cx-editorCx)<=740;
      const nearY=Math.abs(cy-editorCy)<=380;
      if(!nearX||!nearY)continue;
      // A filename in chat history or a sidebar is not an active attachment.
      // Require a compact, currently visible chip/card near the active composer.
      let context=node,contextText=text,busy=false,error=false;
      for(let depth=0;context&&depth<6;depth++){
        if(context.getAttribute?.("aria-busy")==="true")busy=true;
        const cs=getComputedStyle(context),cr=context.getBoundingClientRect();
        if(Number(cs.opacity||1)<=0.02)break;
        const expanded=norm(context.innerText||context.textContent||"");
        if(expanded.length<=650&&cr.width<=1050&&cr.height<=330){
          contextText=expanded;
          if(/uploading|preparing|processing video|loading attachment|đang tải lên|đang xử lý|đang tải tệp/i.test(expanded))busy=true;
          if(/upload failed|could not upload|failed to attach|tải lên thất bại|không thể tải/i.test(expanded))error=true;
        }
        context=context.parentElement||(context.getRootNode?.().host||null);
      }
      const signature=wanted+"|"+Math.round(cx/40)+"|"+Math.round(cy/40);
      const row={attached:true,busy,error,signature,reason:"ACTIVE_COMPOSER_ATTACHMENT"};
      if(!best||Math.abs(cy-editorCy)<best.distance)
        best={distance:Math.abs(cy-editorCy),row};
    }
  }
  return best?best.row:{attached:false,busy:false,error:false,reason:"NO_NEAR_COMPOSER_FILENAME"};
}""";

class AdaptiveUploader:
    def __init__(self, *, researcher: Any = None,
                 remember: Callable | None = None, lessons: list[dict] | None = None):
        self.researcher=researcher or GuideResearch()
        self.remember=remember
        self.lessons=lessons or []

    def _pick(self, *, observation: dict, docs: str, stage: str,
              rejected: set[int], recent: list[dict]) -> BrowserDecision | None:
        """Choose a unique best live DOM control; abstain on real ambiguity."""
        threshold = 7 if stage == "toolbar" else 8
        ranked = []
        for control in observation.get("controls", []):
            control_id = int(control.get("id", -1))
            if control_id in rejected:
                continue
            score = _score(control, stage=stage, guidance=docs)
            if score < threshold:
                continue
            role = str(control.get("role", "")).casefold()
            # Prefer the actual interactive ancestor over a nested icon when
            # both represent the same physical button and share its center.
            semantic_rank = 0 if role in {"button", "menuitem"} else 1
            ranked.append((score, semantic_rank, control_id, control))
        if not ranked:
            return None
        ranked.sort(key=lambda row: (-row[0], row[1]))
        best_score, _, best_id, best_control = ranked[0]
        best_x = float(best_control.get("x", 0))
        best_y = float(best_control.get("y", 0))
        for score, _, other_id, other in ranked[1:]:
            dx = abs(float(other.get("x", 0)) - best_x)
            dy = abs(float(other.get("y", 0)) - best_y)
            if dx <= 10 and dy <= 10:
                # Nested icon wrappers for the same on-screen target are one
                # target, not an ambiguity.
                continue
            if best_score - score < 3:
                print("TOOL_SYSTEM_AGENT_CONTROL=AMBIGUOUS_UPLOAD_TARGETS", flush=True)
                return None
            break
        return BrowserDecision(
            best_id, "Current live DOM label + bounded Python rules",
            "PYTHON_RULES_LIVE_DOM")

    @staticmethod
    def _verify_attachment(page, filename: str, seconds: int = 100) -> bool:
        """Require the MP4 name to be visible beside the CURRENT live composer.

        A filename anywhere in body text may belong to history/Notebook output
        and is not proof of an active attachment. Require two consecutive,
        stable, visible composer-adjacent observations and no upload/error state.
        """
        deadline=time.monotonic()+seconds
        stem=filename.rsplit(".",1)[0][:14]
        last_signature=None
        stable=0
        while time.monotonic()<deadline:
            try:
                evidence=page.evaluate(
                    ATTACHMENT_PREVIEW_EVIDENCE_JS,
                    {"filename":filename,"stem":stem})
            except Exception:
                evidence={}
            if (isinstance(evidence,dict) and evidence.get("attached")
                    and not evidence.get("busy") and not evidence.get("error")):
                signature=str(evidence.get("signature",""))
                if signature and signature==last_signature:
                    stable+=1
                else:
                    stable=0
                    last_signature=signature
                if stable>=1:
                    print("TOOL_SYSTEM_AGENT_ATTACHMENT=ACTIVE_COMPOSER_PREVIEW_VERIFIED",
                          flush=True)
                    return True
            else:
                stable=0
                last_signature=None
                if isinstance(evidence,dict) and evidence.get("error"):
                    print("TOOL_SYSTEM_AGENT_ATTACHMENT=UPLOAD_ERROR_VISIBLE",flush=True)
                    return False
            page.wait_for_timeout(1000)
        print("TOOL_SYSTEM_AGENT_ATTACHMENT=ACTIVE_COMPOSER_PREVIEW_NOT_VERIFIED",
              flush=True)
        return False

    @staticmethod
    def _input_upload(page, file: Path) -> bool:
        inputs=page.locator('input[type="file"]')
        if inputs.count():
            inputs.last.set_input_files(str(file),timeout=30000)
            print("TOOL_SYSTEM_AGENT_ACTION=FILE_INPUT_POPULATED",flush=True)
            return True
        return False

    def upload(self, page, *, provider: str, file: Path) -> None:
        if not file.is_file() or file.suffix.lower()!=".mp4":
            raise GateError("AGENT_SOURCE_MP4_NOT_VERIFIED")
        try:
            guide=self.researcher.learn(provider,"upload MP4 video from computer")
            docs=guide.excerpt
            print("TOOL_SYSTEM_AGENT_RESEARCH_URL="+guide.url,flush=True)
        except (RuntimeError,ValueError):
            docs=""
            print("TOOL_SYSTEM_AGENT_RESEARCH=UNAVAILABLE_NO_INVENTED_GUIDE",flush=True)
        mode="PYTHON_RULES_ONLY"
        print("MONEY_TOOL_BROWSER_MODE="+mode,flush=True)

        previous=[]
        attempted=set()
        last_ui_signature=None
        last_control_id=None
        rejected_by_state={}
        research_again=0
        self.requested_research=""
        self.path_hints=[]
        # Do not populate a pre-existing hidden input on the landing page.
        # Gemini may keep an inert file input in the DOM before the user opens
        # Add files -> Upload files. First observe and select the real UI path.
        file_input_attempted = False
        fallback_attempted_states: set[tuple] = set()

        # Every cycle observes the ACTUAL current page after the last action;
        # never replay stale control IDs from prior screenshots.
        for step in range(1,9):
            obs=page.evaluate(OBSERVE_JS)
            if not obs.get("editor") or obs.get("url") not in {
                    "https://gemini.google.com", "https://meta.ai",
                    "https://www.meta.ai", "https://labs.google",
                    "https://labs.google.com"}:
                raise GateError("AGENT_UNEXPECTED_TOOL_PAGE")
            # A menu is identified from LIVE visible interactive controls.
            # Never infer that a menu is open merely because page body text
            # mentions the words "upload files" in documentation/history.
            menu_open=any(
                re.search(r"upload[ _-]?files?|file[ _-]?upload|tải tệp|"
                          r"browse files|from computer|upload from|files from",
                          str(v.get("hint","")), re.I) and
                (v.get("role") == "menuitem" or v.get("near"))
                for v in obs.get("controls", []))
            stage="menu" if menu_open else "toolbar"
            current_signature=tuple(
                (str(v.get("hint",""))[:40],round(float(v.get("x",0))/8),
                 round(float(v.get("y",0))/8))
                for v in obs.get("controls",[])[:20])
            # Persist rejected controls PER OBSERVED UI STATE across
            # re-planning. A previously denied click is never retried just
            # because the next cycle reassigned the same temporary ID.
            state_key=(stage,current_signature)
            rejected_id=set(rejected_by_state.get(state_key,set()))
            if (current_signature==last_ui_signature and
                    last_control_id is not None):
                rejected_id.add(last_control_id)
            # A hidden file input is eligible only AFTER the live page
            # shows that the attachment menu is open. This prevents an inert
            # input on Gemini's home page from short-circuiting visual planning.
            if (stage == "menu" and not file_input_attempted
                    and self._input_upload(page,file)):
                file_input_attempted = True
                if self._verify_attachment(page,file.name,seconds=12):
                    self._learn(provider,file.name,docs,"MENU_SCOPED_FILE_INPUT")
                    return
                previous.append({
                    "step": step, "result": "MENU_FILE_INPUT_NO_VISIBLE_ATTACHMENT",
                    "next": "reobserve menu and plan a different grounded action",
                })
                print("TOOL_SYSTEM_AGENT_ATTACHMENT=INPUT_NOT_PROVEN_REPLAN",
                      flush=True)
                # No success claim and no repeated injection into this input.
                # Let the fresh live menu observation/Agent choose the next step.
            proposal=self._pick(observation=obs,docs=docs,stage=stage,
                                rejected=rejected_id,recent=previous)
            if self.requested_research and research_again<1:
                research_again+=1
                question=self.requested_research
                self.requested_research=""
                try:
                    guide=self.researcher.learn(provider,question,
                                                prefer_search=True)
                    docs=guide.excerpt
                    print("TOOL_SYSTEM_AGENT_RESEARCH=FOLLOW_UP_SEARCH_READ",flush=True)
                    previous.append({"step":step,"research_url":guide.url,
                                     "new_evidence":docs[:500]})
                    continue  # Plan AGAIN from the newly observed real UI
                except (RuntimeError,ValueError):
                    print("TOOL_SYSTEM_AGENT_RESEARCH=FOLLOW_UP_UNAVAILABLE",flush=True)
            if proposal is None:
                # The page may show an icon-only control. Use local Python
                # pixel recognition only as a secondary candidate source; the
                # same live-DOM hit-test must still approve the click.
                from .visual_ui import click_add_files
                if stage == "toolbar":
                    if state_key in fallback_attempted_states:
                        print("TOOL_SYSTEM_AGENT_LOOP=NO_PROGRESS_AFTER_SCREENSHOT_ACTION",
                              flush=True)
                        raise GateError("AGENT_NO_PROGRESS_AFTER_SCREENSHOT_ACTION")
                    try:
                        click_add_files(page)
                    except GateError as exc:
                        fallback_attempted_states.add(state_key)
                        raw_reason = str(exc).split(":", 1)[0].strip()
                        safe_reason = (raw_reason if re.fullmatch(
                            r"[A-Z][A-Z0-9_]{3,88}", raw_reason) else "UNKNOWN")
                        print("TOOL_SYSTEM_AGENT_LOCAL_UPLOAD_FALLBACK=BLOCKED_" +
                              safe_reason, flush=True)
                        raise GateError(
                            "AGENT_NO_GROUNDED_UPLOAD_CONTROL_" + safe_reason
                        ) from None
                    fallback_attempted_states.add(state_key)
                    page.wait_for_timeout(650)
                    previous.append({"step": step, "source": "PYTHON_LOCAL_PIXELS",
                                     "result": "TOOLBAR_OPENED_NOT_YET_VERIFIED"})
                    print("TOOL_SYSTEM_AGENT_LOOP=SCREENSHOT_ACTION_REOBSERVE",
                          flush=True)
                    continue
                raise GateError("AGENT_NO_GROUNDED_UPLOAD_CONTROL_MENU")
            control=next((c for c in obs["controls"]
                         if int(c["id"])==proposal.control_id),None)
            if control is None or _bad(str(control.get("hint",""))):
                raise GateError("AGENT_UNSAFE_CONTROL_CHOICE")
            x,y=float(control["x"]),float(control["y"])
            check=page.evaluate(SAFE_CLICK_JS,{"x":x,"y":y,
                                             "allowMenu":stage=="menu",
                                             "expected":str(control.get("hint",""))[:70]})
            if not check.get("ok"):
                previous.append({"step":step,"id":proposal.control_id,
                                 "result":"REJECTED_"+str(check.get("reason",""))[:45]})
                # Re-observation next cycle; a rejected hit does not justify
                # clicking it through JS or sending a direct CDP command.
                attempted.add((str(control.get("hint","")),round(x),round(y)))
                rejected_by_state.setdefault(state_key,set()).add(
                    proposal.control_id)
                if len(attempted)>=5:
                    raise GateError("AGENT_SAFETY_REJECTED_ALL_TARGETS")
                # Avoid indefinitely selecting one rejected control: if
                # a matching, safe alternative exists use it immediately.
                alt=[i for i in candidate_ids(obs,stage=stage,guidance=docs,
                                               forbidden={proposal.control_id})]
                if not alt:
                    # Recover from a false positive in the semantic DOM
                    # (the real + button may be custom/nested). Check the
                    # SAME live screenshot and the independent hit guard
                    # before giving up. Never disable the guard.
                    if stage=="toolbar":
                        if state_key in fallback_attempted_states:
                            print("TOOL_SYSTEM_AGENT_LOOP=REPEATED_FALLBACK_BLOCKED",
                                  flush=True)
                            raise GateError("AGENT_NO_PROGRESS_AFTER_SCREENSHOT_ACTION")
                        from .visual_ui import click_add_files
                        try:
                            click_add_files(page)
                            fallback_attempted_states.add(state_key)
                            last_ui_signature=current_signature
                            last_control_id=int(proposal.control_id)
                            previous.append({"step":step,
                                             "result":"VISUAL_RECOVERY_CLICKED"})
                            print("TOOL_SYSTEM_AGENT_RECOVERY=SCREENSHOT_GUIDED",flush=True)
                            page.wait_for_timeout(450)
                            continue
                        except GateError:
                            fallback_attempted_states.add(state_key)
                    # No safe option in this snapshot. Let the Python
                    # operator re-observe and try one more guide/visual
                    # alternative instead of terminating on the first bad hit.
                    print("TOOL_SYSTEM_AGENT_RECOVERY=REOBSERVE_AFTER_DENIED_HIT",
                          flush=True)
                    page.wait_for_timeout(450)
                    continue
                other=next(c for c in obs["controls"] if c["id"]==alt[0])
                othercheck=page.evaluate(SAFE_CLICK_JS,{"x":other["x"],
                          "y":other["y"],"allowMenu":stage=="menu",
                          "expected":str(other.get("hint",""))[:70]})
                if not othercheck.get("ok"):
                    raise GateError("AGENT_ALTERNATIVE_NOT_GROUNDED")
                x,y=other["x"],other["y"]
                control=other
            self.path_hints.append(str(control.get("hint",""))[:60])
            last_ui_signature=current_signature
            last_control_id=int(control.get("id",-1))
            chooser=None
            try:
                with page.expect_file_chooser(timeout=1700) as selected:
                    page.mouse.click(x,y)
                chooser=selected.value
            except Exception:
                pass  # Opening an upload menu usually doesn't trigger chooser
            if chooser is not None:
                chooser.set_files(str(file))
                if self._verify_attachment(page,file.name):
                    self._learn(provider,file.name,docs,"FILE_CHOOSER")
                    return
                raise GateError("AGENT_ATTACHMENT_NOT_VISIBLE")
            page.wait_for_timeout(450)
            previous.append({"step":step,"source":proposal.source,
                             "control":str(control.get("hint",""))[:70],
                             "result":"CLICKED_UI_REOBSERVE"})
            print("TOOL_SYSTEM_AGENT_STEP="+str(step)+"_ACTED_AND_REOBSERVED",flush=True)
        raise GateError("AGENT_UPLOAD_MAX_OBSERVATIONS_EXCEEDED")

    def _learn(self,provider: str,filename: str,docs: str,method: str):
        print("TOOL_SYSTEM_AGENT_UPLOAD=VISIBLE_FILE_VERIFIED",flush=True)
        if self.remember:
            # Store accepts lessons ONLY after a source-file attachment is
            # observed, and DurableStore backs up to EXISTING Supabase.
            from hashlib import sha256
            proof=sha256(filename.encode()).hexdigest()[:16]
            self.remember(provider+"-attachment", "Successful provider UI upload",
                          method+"; action hints="+
                          " -> ".join(getattr(self,"path_hints",[])[-5:])+
                          "; visible filename; "+docs[:120],
                          "VISIBLE_MP4_NAME_SHA16="+proof)
            print("TOOL_SYSTEM_AGENT_LEARNING=VERIFIED_LESSON_SAVED",flush=True)
