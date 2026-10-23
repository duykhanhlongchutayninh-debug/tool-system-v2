"""Gemini Web writer for ONE already-verified Facebook source MP4.

Attach to the existing owner's Chrome via CDP 9222, upload the source video,
request visual + audio analysis, then a separate page-specific new screenplay.
A UI appearance, guessed analysis or echoed prompt is NOT evidence of success.
No other browser, headless session, or Gemini API key; Gemini Web is used through the existing signed-in Chrome.
"""
from __future__ import annotations

import os

from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse
import re
import time

from .agent import ScriptResult
from .chrome import Chrome
from .core import GateError, Job, digest_file
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

GEMINI_URL = "https://gemini.google.com/app"
PROFILE = Path("/kaggle/working/tool-system-v2-runtime/chrome_profile")
DOWNLOADS = Path("/kaggle/working/tool-system-v2-runtime/downloads")
SELECTORS = (
    'model-response',
    '[data-test-id="model-response"]',
    '.model-response-text',
    '[data-testid="model-response"]',
)
_MODEL_RESPONSE_SNAPSHOT_JS = r"""() => {
  const roots=[document], visited=new Set(), out=[];
  const rules=[
    ["model-response",120],
    ['[data-test-id="model-response"]',120],
    ['[data-testid="model-response"]',120],
    ['[data-message-author-role="assistant"]',115],
    ["message-content",108],
    ['[data-testid*="response"]',96],
    ['[data-test-id*="response"]',96],
    ['[class*="markdown-main-panel"]',92],
    ['[class*="response-content"]',88],
    ['[class*="response-text"]',84]
  ];
  const visible=e=>{
    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&
      r.top<innerHeight&&r.left<innerWidth&&s.display!=="none"&&
      s.visibility!=="hidden"&&Number(s.opacity||1)>0.02;
  };
  for(let i=0;i<roots.length&&i<140;i++){
    const root=roots[i];
    if(visited.has(root))continue;
    visited.add(root);
    for(const host of root.querySelectorAll("*"))
      if(host.shadowRoot&&roots.length<140)roots.push(host.shadowRoot);
    let localOrder=0;
    for(const [selector,priority] of rules){
      let nodes=[];
      try{nodes=[...root.querySelectorAll(selector)];}catch(_){continue;}
      for(const node of nodes){
        const order=localOrder++;
        if(!visible(node)||node.matches("textarea,input,[contenteditable]"))continue;
        if(node.closest&&node.closest('[data-message-author-role="user"],user-query,[data-testid*="user-message"]'))continue;
        const text=(node.innerText||node.textContent||"").replace(/\s+/g," ").trim();
        if(text.length<25||text.length>50000)continue;
        const rect=node.getBoundingClientRect();
        out.push({text,selector,priority,order:i*100000+order,
                  y:Math.round(rect.y),height:Math.round(rect.height)});
      }
    }
  }
  return {url:location.href,candidates:out};
}""";
_GEMINI_USER_TURNS_JS = r"""() => {
  const roots=[document],seen=new Set(),turns=[];
  const selectors=[
    "user-query",
    '[data-message-author-role="user"]',
    '[data-testid*="user-query"]',
    '[data-test-id*="user-query"]',
    '[class*="user-query"]'
  ];
  const visible=e=>{
    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    // Count offscreen turns in the conversation DOM too; scroll position
    // must not hide prior turns or make repeated identical prompts ambiguous.
    return r.width>0&&r.height>0&&s.display!=="none"&&
      s.visibility!=="hidden"&&Number(s.opacity||1)>0.02;
  };
  for(let i=0;i<roots.length&&i<140;i++){
    const root=roots[i];
    if(seen.has(root))continue;
    seen.add(root);
    for(const host of root.querySelectorAll("*"))
      if(host.shadowRoot&&roots.length<140)roots.push(host.shadowRoot);
    // Use the most semantic available selector per root. This avoids counting
    // nested spans as separate turns while preserving repeated identical prompts.
    for(const selector of selectors){
      let nodes=[];
      try{nodes=[...root.querySelectorAll(selector)];}catch(_){continue;}
      const visibleNodes=[];
      for(const node of nodes){
        if(!visible(node))continue;
        const text=(node.innerText||node.textContent||"").replace(/\s+/g," ").trim();
        if(text.length<5||text.length>12000)continue;
        const rect=node.getBoundingClientRect();
        visibleNodes.push({text,order:i*100000+Math.round(rect.y*10)+visibleNodes.length});
      }
      if(visibleNodes.length){
        turns.push(...visibleNodes);
        break;
      }
    }
  }
  turns.sort((a,b)=>a.order-b.order);
  return {count:turns.length,last_text:turns.length?turns[turns.length-1].text:""};
}""";
COMPOSERS = (
    'rich-textarea [contenteditable="true"]',
    'rich-textarea [contenteditable="plaintext-only"]',
    '.ql-editor[contenteditable]',
    '[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    '[role="textbox"][contenteditable]',
    '[contenteditable="plaintext-only"]',
    'textarea',
    '[role="textbox"]',
    '[role="combobox"][contenteditable]',
    'input:not([type="hidden"]):not([type="file"]):not([type="password"])',
)

ANALYZE = """Hãy xem TRỰC TIẾP TỆP MP4 ĐÍNH KÈM, quan sát hình ảnh và nghe âm thanh,
không đoán dựa trên mô tả hoặc tên file.
1. Chia video thành những cảnh theo mốc thời gian; ai làm gì, đồ vật nào, hành động nào.
2. Chép lời thoại nghe được bằng ngôn ngữ gốc, gắn đúng nhân vật, đánh dấu tiếng
động/âm nhạc. Phân biệt rõ câu nghe không rõ, TUYỆT ĐỐI không tự chế lời thoại.
3. Tóm tắt kịch bản gốc theo diễn tiến nhân-quả và kết thúc/twist.
4. Nêu kỹ thuật hoặc mẹo thật sự có trong video; nêu điểm không thể xác nhận.
Nếu không thực sự xem/nghe được MP4, nói rõ KHÔNG ĐỌC ĐƯỢC VIDEO, không
bịa phân tích. Chỉ trả lời những gì thật sự quan sát hoặc nghe được."""

REWRITE = """Bạn đang là BIÊN KỊCH DUY NHẤT cho Page Facebook 'Mẹo nhỏ mỗi ngày'.
Dựa trực tiếp vào MP4 đã tải lên và bản phân tích bạn vừa viết, tạo KỊCH BẢN
HOÀN TOÀN MỚI (không sao chép y nguyên thoại hay cảnh của video gốc), theo phong cách:
- Mẹo vặt thực tế, chính xác, làm được trong đời sống; có tình huống hài 3D,
  leo thang sự cố, cú chốt bất ngờ rồi giải pháp hữu ích.
- Nhân vật đã khóa là HAI CON THẠCH SÙNG 3D và MỘT CON GIÁN 3D; giữ đồng nhất
  từng nhân vật xuyên suốt toàn bộ cảnh; KHÔNG thêm nhân vật người.
- Thời lượng 30–45 giây, video dọc 9:16. Chia thành các phân đoạn video có
  mốc thời gian rõ (nếu tạo clip 8–10 giây thì nối phân đoạn liên tục).
- Viết rõ từng nhân vật hành động/thoại trực tiếp (nếu có), âm thanh,
  chuyển động, bối cảnh, hành vi vật lý hợp lý và cú chốt cuối.
- Không dẫn truyện ngôi thứ ba nếu video dùng hội thoại nhân vật.
- Mẹo phải an toàn, không bịa tác dụng hoặc chỉ dẫn nguy hiểm.
Đầu ra: TÊN Ý TƯỞNG, ĐỘ DÀI, DANH SÁCH CẢNH THEO GIÂY, HÀNH ĐỘNG,
THOẠI THEO NHÂN VẬT, ÂM THANH, NHẤT QUÁN NHÂN VẬT, CÚ CHỐT.
Chỉ tạo kịch bản sau khi bạn đã phân tích thực sự MP4. Không viết caption
hoặc hashtags vì đây không phải bước đăng Facebook."""


def _visible_composer(page):
    """Discover the current Gemini chat input from LIVE browser semantics.

    A screenshot can show a textbox while the outer host is not itself
    contenteditable. Inspect every real editable candidate, including nested
    open shadow roots and same-provider frames. Never infer success from an
    image or type into an arbitrary noneditable layout element.
    """
    from urllib.parse import urlparse

    scopes=[page]
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            hostname=(urlparse(frame.url).hostname or "").lower()
            if hostname in ("gemini.google.com","www.gemini.google.com"):
                scopes.append(frame)
    except Exception:
        pass
    for scope in scopes:
        for selector in COMPOSERS:
            matches=scope.locator(selector)
            try:
                length=min(matches.count(),48)
            except Exception:
                continue
            for idx in range(length-1,-1,-1):
                candidate=matches.nth(idx)
                try:
                    if not (candidate.is_visible() and candidate.is_enabled()
                            and candidate.is_editable()):
                        continue
                    box=candidate.bounding_box()
                    if box and box["width"]>=80 and box["height"]>=12:
                        return candidate
                except Exception:
                    continue

    # Chromium/Angular can render the editable inside an OPEN shadow root.
    # Use a read-only, live DOM probe, not a hard-coded Gemini CSS selector.
    js=r"""() => {
      const pending=[document];
      let visited=0, found=null;
      while(pending.length && visited++<160) {
        const root=pending.shift();
        const editables=root.querySelectorAll(
          'textarea,input:not([type=hidden]):not([type=file]):not([type=password]),'
          +'[contenteditable],[role=textbox],[role=combobox]');
        for(const e of editables) {
          if(e.shadowRoot)pending.push(e.shadowRoot);
          const r=e.getBoundingClientRect(), style=getComputedStyle(e);
          if(r.width<80 || r.height<12 || style.display==='none' ||
             style.visibility==='hidden' || r.bottom<=0 ||
             r.top>=innerHeight)continue;
          const tag=e.tagName.toLowerCase();
          const editable=tag==='textarea' ||
            (tag==='input' && !e.disabled && !e.readOnly) ||
            e.isContentEditable;
          if(editable && !e.disabled && !e.readOnly)found=e;
        }
        for(const node of root.querySelectorAll('*')) {
          if(node.shadowRoot && pending.length<160)
            pending.push(node.shadowRoot);
        }
      }
      return found;
    }"""
    for scope in scopes:
        try:
            handle=scope.evaluate_handle(js)
            elem=handle.as_element()
            if elem is not None and elem.is_visible() and elem.is_editable():
                bbox=elem.bounding_box()
                if bbox and bbox["width"]>=80 and bbox["height"]>=12:
                    print("TOOL_SYSTEM_GEMINI_COMPOSER=LIVE_SHADOW_DOM_GROUNDED",
                          flush=True)
                    return elem
            handle.dispose()
        except Exception:
            continue
    raise GateError("GEMINI_COMPOSER_NOT_FOUND")


VIDEO_NOTICE_EVIDENCE_JS = r"""() => {
  const norm=s=>(s||"").replace(/\s+/g," ").trim().toLocaleLowerCase();
  const roots=()=>{const out=[document];for(let i=0;i<out.length&&i<100;i++){
    const root=out[i];for(const host of root.querySelectorAll("*"))
      if(host.shadowRoot&&out.length<100)out.push(host.shadowRoot);
  }return out;};
  const allRoots=roots();
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&
      r.left<innerWidth&&s.display!=="none"&&s.visibility!=="hidden"&&
      Number(s.opacity||1)>0.02;};
  const composedText=start=>{const stack=[start],chunks=[];let n=0;
    while(stack.length&&n++<1600){const e=stack.pop();if(!e)continue;
      if(e.nodeType===3){chunks.push(e.nodeValue||"");continue;}
      if(e.nodeType!==1&&e.nodeType!==9&&e.nodeType!==11)continue;
      if(e.shadowRoot)stack.push(e.shadowRoot);
      for(const child of e.childNodes||[])stack.push(child);
    }return norm(chunks.join(" "));};
  const parent=e=>e?.parentElement||(e?.getRootNode?.().host||null);
  const title="lời nhắc về việc tạo video";
  const rights="quyền đối với mọi nội dung mà bạn tải lên";
  const permission="quyền cho phép";
  const titleNodes=[];
  const selectors='h1,h2,h3,h4,p,span,div,strong,button,[role="heading"]';
  for(const root of allRoots)for(const node of root.querySelectorAll(selectors)){
    if(visible(node)&&norm(node.innerText||node.textContent)===title)
      titleNodes.push(node);
  }
  for(const heading of titleNodes){
    for(let panel=heading,depth=0;panel&&depth<16;panel=parent(panel),depth++){
      if(!visible(panel))continue;
      const text=composedText(panel),r=panel.getBoundingClientRect();
      if(text.length<2600&&text.includes(title)&&text.includes(rights)&&
         text.includes(permission)&&r.width>=240&&r.height>=80){
        return {title_visible:true,visible:true,blocking_overlay:true,
          panel:{x:r.x,y:r.y,width:r.width,height:r.height}};
      }
    }
  }
  let blocking=false;
  for(const root of allRoots){
    for(const node of root.querySelectorAll(
      '[role="dialog"],[aria-modal="true"],dialog,[role="alertdialog"],mat-dialog-container')){
      if(!visible(node))continue;
      const r=node.getBoundingClientRect();
      if(r.width>=240&&r.height>=80){blocking=true;break;}
    }
    if(blocking)break;
  }
  return {title_visible:titleNodes.length>0,visible:false,
          blocking_overlay:blocking,panel:null};
}""";


GEMINI_PAGE_UNOBSTRUCTED_JS = r"""() => {
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>=80&&r.height>=12&&r.bottom>0&&r.right>0&&
      r.top<innerHeight&&r.left<innerWidth&&s.display!=="none"&&
      s.visibility!=="hidden"&&Number(s.opacity||1)>0.02;};
  const roots=[document],seen=new Set();
  for(let i=0;i<roots.length&&i<100;i++){
    const root=roots[i];if(seen.has(root))continue;seen.add(root);
    for(const host of root.querySelectorAll("*"))
      if(host.shadowRoot&&roots.length<100)roots.push(host.shadowRoot);
  }
  const selectors='rich-textarea [contenteditable="true"],[contenteditable="true"],'+
    '[contenteditable="plaintext-only"],textarea,[role="textbox"]';
  const candidates=[];
  for(const root of roots)for(const el of root.querySelectorAll(selectors)){
    if(!visible(el)||el.disabled||el.readOnly)continue;
    const r=el.getBoundingClientRect();
    const editable=el.isContentEditable||el.tagName.toLowerCase()==="textarea"||
      (el.tagName.toLowerCase()==="input"&&!el.disabled&&!el.readOnly);
    if(editable)candidates.push({el,area:r.width*r.height,x:r.x+r.width/2,
                                 y:r.y+r.height/2,r});
  }
  candidates.sort((a,b)=>b.area-a.area);
  const composedContains=(ancestor,child)=>{
    for(let n=child,depth=0;n&&depth<24;
        n=n.parentElement||(n.getRootNode?.().host||null),depth++)
      if(n===ancestor)return true;
    return false;
  };
  for(const c of candidates){
    let hit=document.elementFromPoint(c.x,c.y);
    if(!hit)continue;
    for(let depth=0;hit&&depth<12;depth++){
      const shadow=hit.shadowRoot;
      if(!shadow||typeof shadow.elementFromPoint!=="function")break;
      const inner=shadow.elementFromPoint(c.x,c.y);
      if(!inner||inner===hit)break;
      hit=inner;
    }
    const unobstructed=composedContains(c.el,hit)||composedContains(hit,c.el);
    if(unobstructed)return {unobstructed:true,editor_visible:true,
      x:Math.round(c.x),y:Math.round(c.y),hit_tag:hit.tagName||"",
      hit_role:hit.getAttribute?.("role")||""};
  }
  return {unobstructed:false,editor_visible:candidates.length>0,
          candidate_count:candidates.length};
}""";


VIDEO_NOTICE_OBSERVE_JS = r"""() => {
  const norm=s=>(s||"").replace(/\s+/g," ").trim();
  const roots=[document], seenRoots=new Set(), controls=[], seenKeys=new Set();
  for(let idx=0;idx<roots.length && idx<100;idx++){
    const root=roots[idx];
    if(seenRoots.has(root))continue;
    seenRoots.add(root);
    for(const host of root.querySelectorAll("*"))
      if(host.shadowRoot && roots.length<100)roots.push(host.shadowRoot);
    const selector='button,[role="button"],[role="menuitem"],a,[tabindex="0"]';
    for(const el of root.querySelectorAll(selector)){
      if(controls.length>=240)break;
      const r=el.getBoundingClientRect(),cs=getComputedStyle(el);
      if(r.width<18||r.height<14||r.width>500||r.height>180||
         r.bottom<=0||r.top>=innerHeight||r.right<=0||r.left>=innerWidth||
         cs.display==="none"||cs.visibility==="hidden"||Number(cs.opacity||1)<0.02||
         el.disabled||el.getAttribute("aria-disabled")==="true")continue;
      const label=norm(el.innerText||el.textContent||"");
      const aria=norm(el.getAttribute("aria-label")||"");
      const titleText=norm(el.getAttribute("title")||"");
      const testid=norm(el.getAttribute("data-testid")||"");
      const hint=[label,aria,titleText,testid].filter(Boolean).join(" ").slice(0,130);
      const key=[Math.round(r.x),Math.round(r.y),Math.round(r.width),
                 Math.round(r.height),hint].join("|");
      if(seenKeys.has(key))continue;
      seenKeys.add(key);
      const role=(el.getAttribute("role")||el.tagName.toLowerCase()).toLowerCase();
      const x=r.x+r.width/2, y=r.y+r.height/2;
      controls.push({x,y,width:r.width,height:r.height,role,hint,near:true,disabled:false,
                     _priority:/đồng\s+ý/i.test(hint)?0:/hủy|huy|cancel/i.test(hint)?2:1,
                     _center:Math.abs(x-innerWidth/2)+Math.abs(y-innerHeight/2)});
    }
  }
  controls.sort((a,b)=>a._priority-b._priority||a._center-b._center);
  const selected=controls.slice(0,70).map((item,id)=>{
    const out={...item,id};delete out._priority;delete out._center;return out;
  });
  return {url:location.origin,visible:true,controls:selected};
}""";


VIDEO_NOTICE_TARGET_GUARD_JS = r"""({x,y}) => {
  const norm=s=>(s||"").replace(/\s+/g," ").trim().toLocaleLowerCase();
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&
      r.left<innerWidth&&s.display!=="none"&&s.visibility!=="hidden"&&
      Number(s.opacity||1)>0.02;};
  const parent=e=>e?.parentElement||(e?.getRootNode?.().host||null);
  let hit=document.elementFromPoint(x,y);
  if(!hit)return {ok:false,reason:"NO_LIVE_HIT"};
  for(let depth=0;hit&&depth<12;depth++){
    const root=hit.shadowRoot;
    if(!root)break;
    const inner=root.elementFromPoint(x,y);
    if(!inner||inner===hit)break;
    hit=inner;
  }
  let button=null;
  for(let node=hit,depth=0;node&&depth<14;node=parent(node),depth++){
    if(!node.matches)continue;
    const role=(node.getAttribute("role")||node.tagName||"").toLowerCase();
    const interactive=node.matches("button,[role='button'],[role='menuitem'],a")||
      node.tabIndex>=0||getComputedStyle(node).cursor==="pointer";
    if(!interactive)continue;
    const label=norm(node.innerText)||norm(node.textContent)||
      norm(node.getAttribute("aria-label"))||norm(node.getAttribute("title"));
    if(label==="đồng ý"&&(role==="button"||role==="menuitem"||
       node.tagName.toLowerCase()==="button"||node.tabIndex>=0)){
      button=node;break;
    }
  }
  if(!button||!visible(button))
    return {ok:false,reason:"TARGET_NOT_EXACT_APPROVE_BUTTON"};
  // The exact label alone is not sufficient: prove this control belongs to the
  // exact known rights notice, not a background page or unrelated dialog.
  const textOf=start=>{const stack=[start],chunks=[];let n=0;
    while(stack.length&&n++<1800){const e=stack.pop();if(!e)continue;
      if(e.nodeType===3){chunks.push(e.nodeValue||"");continue;}
      if(e.nodeType!==1&&e.nodeType!==9&&e.nodeType!==11)continue;
      if(e.shadowRoot)stack.push(e.shadowRoot);
      for(const child of e.childNodes||[])stack.push(child);
    }return norm(chunks.join(" "));};
  const title="lời nhắc về việc tạo video";
  const rights="quyền đối với mọi nội dung mà bạn tải lên";
  const permission="quyền cho phép";
  let knownPanel=false;
  for(let node=button,depth=0;node&&depth<18;node=parent(node),depth++){
    if(!visible(node))continue;
    const text=textOf(node),r=node.getBoundingClientRect();
    if(text.length<2600&&text.includes(title)&&text.includes(rights)&&
       text.includes(permission)&&r.width>=240&&r.height>=80){
      knownPanel=true;break;
    }
  }
  if(!knownPanel)
    return {ok:false,reason:"APPROVE_BUTTON_NOT_IN_KNOWN_NOTICE"};
  const b=button.getBoundingClientRect();
  if(b.width<18||b.height<14||b.width>420||b.height>140)
    return {ok:false,reason:"APPROVE_BUTTON_GEOMETRY_INVALID"};
  return {ok:true,reason:"VISIBLE_EXACT_APPROVE_BUTTON"};
}""";


def _inspect_gemini_notice_dom(page):
    """Inspect the main tab and visible Google child frames for the known dialog."""
    outcomes = []
    for scope_index, (scope_name, scope, offset_x, offset_y) in enumerate(
            _gemini_notice_scopes(page)):
        try:
            state = scope.evaluate(VIDEO_NOTICE_EVIDENCE_JS)
        except Exception:
            continue
        if not isinstance(state, dict):
            continue
        outcomes.append((scope_index, scope_name, scope, offset_x, offset_y, state))
    # A positive exact match anywhere wins over unrelated overlays elsewhere.
    for item in outcomes:
        if item[-1].get("visible"):
            return "known", item, outcomes
    if any(item[-1].get("title_visible") for item in outcomes):
        return "title_unresolved", None, outcomes
    if any(item[-1].get("blocking_overlay") for item in outcomes):
        return "unknown_modal", None, outcomes
    return "none", None, outcomes


def _observe_gemini_notice_controls(page, scope_outcomes):
    """Merge live controls from each observed frame into main-viewport coords."""
    combined = []
    for scope_index, scope_name, scope, offset_x, offset_y, state in scope_outcomes:
        try:
            result = scope.evaluate(VIDEO_NOTICE_OBSERVE_JS)
        except Exception:
            continue
        if not isinstance(result, dict):
            continue
        for control in result.get("controls", []):
            item = dict(control)
            try:
                item["_scope_index"] = scope_index
                item["_scope_name"] = scope_name
                item["_local_x"] = float(item["x"])
                item["_local_y"] = float(item["y"])
                item["x"] = float(item["x"]) + offset_x
                item["y"] = float(item["y"]) + offset_y
                item["id"] = len(combined)
            except (KeyError, TypeError, ValueError):
                continue
            combined.append(item)
            if len(combined) >= 120:
                break
        if len(combined) >= 120:
            break
    return {"url": page.url, "controls": combined[:120]}


def _ensure_gemini_interactive_before_send(page, *, stage: str,
                                           timeout_seconds: int = 14,
                                           stable_seconds: float = 4.0,
                                           lessons: list[dict] | None = None,
                                           remember=None) -> None:
    """Wait for a stable, actually clickable composer before submitting prompts.

    A single negative modal classification is not enough: video upload can
    reveal the Gemini rights notice asynchronously. Require repeated live
    hit-tests over a quiet interval. If a blocker appears, process the known
    owner-approved notice or fail closed before a prompt is sent.
    """
    deadline = time.monotonic() + max(3, timeout_seconds)
    stable_since = None
    polls = 0
    while time.monotonic() < deadline:
        polls += 1
        kind, known, outcomes = _inspect_gemini_notice_dom(page)
        if kind == "known":
            print("TOOL_SYSTEM_GEMINI_READY=KNOWN_NOTICE_DURING_" + stage.upper(),
                  flush=True)
            _accept_known_video_creation_notice(page, wait_ms=0, lessons=lessons, remember=remember)
            stable_since = None
            page.wait_for_timeout(700)
            continue
        if kind == "title_unresolved":
            raise GateError("GEMINI_VIDEO_NOTICE_RIGHTS_TEXT_UNVERIFIED")
        if kind == "unknown_modal":
            # Let the visual observer distinguish a known screenshot false
            # positive from a genuinely blocking, unrecognized modal.
            _accept_known_video_creation_notice(page, wait_ms=0, lessons=lessons, remember=remember)
            stable_since = None
            page.wait_for_timeout(700)
            continue
        try:
            live = page.evaluate(GEMINI_PAGE_UNOBSTRUCTED_JS)
        except Exception as exc:
            raise GateError("GEMINI_LIVE_COMPOSER_HIT_TEST_FAILED") from exc
        if live.get("unobstructed"):
            if stable_since is None:
                stable_since = time.monotonic()
            stable_for = time.monotonic() - stable_since
            if stable_for >= stable_seconds and polls >= 3:
                print("TOOL_SYSTEM_GEMINI_READY=COMPOSER_UNOBSTRUCTED_STABLE_" +
                      stage.upper(), flush=True)
                return
        else:
            stable_since = None
            # The DOM text detector may not see a visual/iframe overlay. Call the DOM-only blocker handler before any submission.
            _accept_known_video_creation_notice(page, wait_ms=0, lessons=lessons, remember=remember)
        page.wait_for_timeout(750)
    raise GateError("GEMINI_UI_NOT_STABLE_BEFORE_" + stage.upper() + "_SEND")


def _select_or_create_gemini_tab(context):
    """Reuse the owner's already signed-in Gemini tab before creating a new one.

    Step 06 must inspect the existing Gemini tab (including any pending consent
    dialog), and must never close a page it did not create.
    """
    from urllib.parse import urlparse

    try:
        pages = list(context.pages)
    except Exception:
        pages = []
    for page in reversed(pages):
        try:
            if page.is_closed():
                continue
            host = (urlparse(page.url).hostname or "").lower()
            if host in {"gemini.google.com", "www.gemini.google.com"}:
                page.bring_to_front()
                print("TOOL_SYSTEM_GEMINI_TAB=REUSED_EXISTING_TAB", flush=True)
                print("TOOL_SYSTEM_GEMINI_TAB_URL_HOST=" + host, flush=True)
                return page, False
        except Exception:
            continue
    page = context.new_page()
    print("TOOL_SYSTEM_GEMINI_TAB=CREATED_NO_EXISTING_GEMINI_TAB", flush=True)
    return page, True


def _gemini_notice_scopes(page):
    """Return the main page and inspectable Google child frames with offsets."""
    from urllib.parse import urlparse

    scopes = [("main", page, 0.0, 0.0)]
    try:
        frames = list(page.frames)
        main_frame = page.main_frame
    except Exception:
        return scopes
    for index, frame in enumerate(frames):
        if frame == main_frame:
            continue
        try:
            if frame.is_detached():
                continue
            host = (urlparse(frame.url).hostname or "").lower()
            if not host or not (
                host.endswith(".google.com")
                or host.endswith(".googleusercontent.com")
                or host.endswith(".gstatic.com")
            ):
                continue
            rect = frame.frame_element().bounding_box()
            if not rect or rect["width"] < 40 or rect["height"] < 30:
                continue
            scopes.append((
                "frame_" + str(index) + "_" + host,
                frame,
                float(rect["x"]),
                float(rect["y"]),
            ))
        except Exception:
            continue
    return scopes


def _save_verified_notice_lesson(remember, confirmation: str) -> None:
    """Persist a lesson only after a known notice is gone and the state is verified."""
    if not callable(remember):
        return
    try:
        remember(
            "gemini-video-rights-notice",
            "Gemini's known video-rights reminder can block chat input",
            "Click only the exact visible \"Đồng ý\" control after the live DOM "
            "verifies the exact video-rights title, rights text, dialog ancestry, "
            "and hit target. Never click unknown dialogs or reuse stale coordinates.",
            "KNOWN_NOTICE_DOM_VERIFIED; EXACT_APPROVE_TARGET_GUARD; NOTICE_ABSENT; "
            + confirmation,
        )
        print("TOOL_SYSTEM_AGENT_LEARNING=VERIFIED_GEMINI_NOTICE_HANDLING_LESSON_SAVED",
              flush=True)
    except Exception as exc:
        # Learning-store failure must not invalidate already verified live UI state.
        print("TOOL_SYSTEM_AGENT_LEARNING=NOTICE_LESSON_SAVE_FAILED_" +
              type(exc).__name__, flush=True)


def _click_guarded_known_video_notice(page, known_scope=None) -> bool:
    """Click only the exact pre-approved Gemini video-rights notice.

    The target's live hit-test, exact text and containing rights-notice ancestry
    must pass twice. The coarse modal observer may miss a shadow-root variant;
    this helper never clicks unless the independent exact target guard passes.
    """
    def scope_rows():
        if known_scope is not None:
            return [known_scope]
        try:
            live_scopes = _gemini_notice_scopes(page)
        except Exception:
            live_scopes = []
        return [(index, name, scope, offset_x, offset_y, {})
                for index, (name, scope, offset_x, offset_y) in enumerate(live_scopes)]

    def guarded_target(scope):
        try:
            observation = scope.evaluate(VIDEO_NOTICE_OBSERVE_JS)
        except Exception:
            return None
        controls = observation.get("controls", []) if isinstance(observation, dict) else []
        for control in controls:
            hint = " ".join(str(control.get("hint", "")).split()).casefold()
            if "đồng ý" not in hint or any(word in hint for word in ("hủy", "huy", "cancel")):
                continue
            try:
                x, y = float(control["x"]), float(control["y"])
                guard = scope.evaluate(VIDEO_NOTICE_TARGET_GUARD_JS, {"x": x, "y": y})
            except Exception:
                continue
            if isinstance(guard, dict) and guard.get("ok") is True:
                return x, y
        return None

    # First live inspection. No match is a normal "no known consent dialog"
    # result, allowing the caller to distinguish unrelated overlays.
    first_rows = scope_rows()
    if not any(guarded_target(row[2]) is not None for row in first_rows):
        return False

    # Re-observe the current page and recheck the exact target just before click.
    latest_kind, latest_scope, outcomes = _inspect_gemini_notice_dom(page)
    if latest_kind == "known" and latest_scope:
        fresh_rows = [latest_scope]
    else:
        fresh_rows = outcomes or scope_rows()
    target = None
    chosen_row = None
    for row in fresh_rows:
        coords = guarded_target(row[2])
        if coords is not None:
            chosen_row, target = row, coords
            break
    if chosen_row is None or target is None:
        raise GateError("GEMINI_KNOWN_NOTICE_APPROVE_BUTTON_NOT_VERIFIED")

    _, _, _, offset_x, offset_y, _ = chosen_row
    x, y = target
    print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=EXACT_DOM_AND_APPROVE_GUARD_VERIFIED",
          flush=True)
    try:
        page.mouse.click(x + float(offset_x), y + float(offset_y))
    except Exception as exc:
        raise GateError("GEMINI_KNOWN_NOTICE_APPROVE_CLICK_FAILED") from exc
    print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=EXACT_APPROVE_CLICK_SENT", flush=True)
    return True



def _accept_known_video_creation_notice(page, *, wait_ms: int = 900,
                                       lessons: list[dict] | None = None,
                                       remember=None,
                                       owner_wait_seconds: float = 120.0) -> bool:
    """Inspect Gemini blockers; click only the one exact pre-approved rights notice.

    A fresh exact title/rights/target guard is required twice before one click.
    Every other or ambiguous modal remains owner-operated and fails closed.
    Composer discovery and response verification remain separate gates.
    """
    def live_composer_is_unobstructed() -> bool:
        """Use live DOM hit-testing to reject a vision-only false modal positive.

        This read-only check returns true only when a visible editable composer
        is actually hit-test accessible. It never authorizes clicking a modal.
        """
        try:
            live = page.evaluate(GEMINI_PAGE_UNOBSTRUCTED_JS)
        except Exception:
            return False
        return (isinstance(live, dict)
                and live.get("unobstructed") is True
                and live.get("editor_visible") is True)

    def classify_current_dom() -> tuple[int, float]:
        """Classify only states Python can prove from the live DOM.

        This release uses no screenshot-based AI inference. Unknown overlays
        remain blocked for the owner instead of being guessed away.
        """
        if live_composer_is_unobstructed():
            print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=DOM_COMPOSER_UNOBSTRUCTED", flush=True)
            return -1, 0.99
        state = _gemini_ui_state(page)
        state_code = re.sub(r"[^A-Z0-9_]", "_", state.upper())[:50]
        print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=DOM_ONLY_" + state_code, flush=True)
        owner_errors = {
            "OWNER_LOGIN_REQUIRED": "GEMINI_OWNER_LOGIN_REQUIRED",
            "CONSENT_REQUIRED": "GEMINI_OWNER_CONSENT_REQUIRED",
            "UNEXPECTED_REDIRECT": "GEMINI_UNEXPECTED_REDIRECT",
        }
        if state in owner_errors:
            raise GateError(owner_errors[state])
        print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=UI_STATE_UNVERIFIED", flush=True)
        return -2, 0.99

    if wait_ms > 0:
        page.wait_for_timeout(min(wait_ms, 650))

    deadline = time.monotonic() + max(0.0, float(owner_wait_seconds))
    dom_kind, known_scope, _dom_outcomes = _inspect_gemini_notice_dom(page)
    known_notice_dom_observed = dom_kind == "known"
    blocker_observed = dom_kind != "none"

    notice_clicked = False
    if dom_kind == "known":
        _, scope_name, _, _, _, _ = known_scope
        print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=KNOWN_NOTICE_VISIBLE_" +
              scope_name.upper(), flush=True)
        print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=EXACT_NOTICE_DOM_VERIFIED", flush=True)
        notice_clicked = _click_guarded_known_video_notice(page, known_scope)
        if not notice_clicked:
            raise GateError("GEMINI_KNOWN_NOTICE_APPROVE_BUTTON_NOT_VERIFIED")
    else:
        # The coarse dialog detector can miss a shadow-root variant. Allow
        # the exact target guard to recognize it; unknown modal buttons still
        # fail because the guard requires the exact title + rights text ancestry.
        notice_clicked = _click_guarded_known_video_notice(page)
        if notice_clicked:
            print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=EXACT_NOTICE_RECOGNIZED_BY_TARGET_GUARD",
                  flush=True)

    if notice_clicked:
        page.wait_for_timeout(650)
        after_kind, _, _ = _inspect_gemini_notice_dom(page)
        if after_kind != "none":
            print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=DISMISSAL_NOT_VERIFIED", flush=True)
            raise GateError("GEMINI_NOTICE_DISMISSAL_NOT_VERIFIED")
        try:
            fresh_image = page.screenshot(type="jpeg", quality=65, timeout=12000)
            if not isinstance(fresh_image, (bytes, bytearray)) or not fresh_image:
                raise ValueError("EMPTY_SCREENSHOT")
        except Exception as exc:
            raise GateError("GEMINI_NOTICE_POST_CLICK_SCREENSHOT_FAILED") from exc
        print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=POST_CLICK_FRESH_SCREENSHOT_CAPTURED", flush=True)
        print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=NOTICE_ABSENCE_DOM_VERIFIED", flush=True)
        _save_verified_notice_lesson(
            remember, "AUTO_CLICK_EXACT_DOM_GUARD; POST_CLICK_NOTICE_ABSENT")
        return True

    if dom_kind == "title_unresolved":
        print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=NOTICE_TEXT_UNRESOLVED", flush=True)
    elif dom_kind == "unknown_modal":
        print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=UNKNOWN_MODAL_VISIBLE", flush=True)
    else:
        # No exact guarded button was found. A fresh visual classification may
        # prove there is no blocker or identify an unrelated modal; it cannot
        # authorize clicking a generic "agree" control.
        state_id, _ = classify_current_dom()
        if state_id == -2:
            raise GateError("GEMINI_CPU_ONLY_UI_STATE_UNVERIFIED")
        if state_id == -1:
            if os.environ.get("TOOL_SYSTEM_COMPUTE_MODE", "").upper() == "CPU_ONLY":
                print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=NO_EXACT_NOTICE_DOM_COMPOSER_UNOBSTRUCTED",
                      flush=True)
            else:
                print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=NOT_PRESENT_CONFIRMED_BY_SCREEN_AND_DOM",
                      flush=True)
            return False
        if state_id == 0 and live_composer_is_unobstructed():
            # The visual classifier can mistake Gemini's centered home/chat UI
            # for a modal. DOM inspection found no dialog, and the composer hit
            # test proves the alleged blocker does not cover the real editor.
            # Continue without clicking anything or waiting 120s for the owner.
            print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=VISUAL_MODAL_REJECTED_BY_LIVE_COMPOSER_HIT_TEST",
                  flush=True)
            print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=LIVE_COMPOSER_UNOBSTRUCTED_CONFIRMED",
                  flush=True)
            return False
        blocker_observed = True
        if state_id == 1:
            print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=VISUAL_NOTICE_REQUIRES_OWNER_ACTION",
                  flush=True)
        else:
            print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=VISUAL_MODAL_REQUIRES_OWNER_ACTION",
                  flush=True)

    print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=OWNER_ACTION_REQUIRED_NO_AGENT_CLICK",
          flush=True)
    clear_screenshots = 0

    while True:
        if time.monotonic() >= deadline:
            print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=OWNER_ACTION_TIMEOUT", flush=True)
            raise GateError("GEMINI_OWNER_ACTION_REQUIRED_TIMEOUT")

        dom_kind, known_scope, _dom_outcomes = _inspect_gemini_notice_dom(page)
        if dom_kind != "none":
            blocker_observed = True
            known_notice_dom_observed = known_notice_dom_observed or dom_kind == "known"
            clear_screenshots = 0
            if time.monotonic() >= deadline:
                print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=OWNER_ACTION_TIMEOUT", flush=True)
                raise GateError("GEMINI_OWNER_ACTION_REQUIRED_TIMEOUT")
            page.wait_for_timeout(750)
            continue

        state_id, _ = classify_current_dom()
        if state_id == -2:
            raise GateError("GEMINI_CPU_ONLY_UI_STATE_UNVERIFIED")
        if state_id == -1:
            if not blocker_observed:
                print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=NOT_PRESENT_CONFIRMED_BY_SCREEN_AND_DOM",
                      flush=True)
                return False
            clear_screenshots += 1
            if clear_screenshots >= 2:
                print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=BLOCKER_CLEARED_NO_MODAL_CONFIRMED",
                      flush=True)
                if known_notice_dom_observed:
                    _save_verified_notice_lesson(
                        remember, "NO_AGENT_CLICK; BLOCKER_CLEARED; TWO_SCREENSHOTS_NO_MODAL")
                return True
        else:
            if state_id == 0 and live_composer_is_unobstructed():
                print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=VISUAL_MODAL_REJECTED_BY_LIVE_COMPOSER_HIT_TEST",
                      flush=True)
                print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=LIVE_COMPOSER_UNOBSTRUCTED_CONFIRMED",
                      flush=True)
                return False
            blocker_observed = True
            clear_screenshots = 0
            if state_id == 1:
                print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=VISUAL_NOTICE_STILL_PRESENT_OWNER_ACTION_REQUIRED",
                      flush=True)
            else:
                print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=VISUAL_MODAL_STILL_PRESENT_OWNER_ACTION_REQUIRED",
                      flush=True)

        if time.monotonic() >= deadline:
            print("TOOL_SYSTEM_GEMINI_VIDEO_NOTICE=OWNER_ACTION_TIMEOUT", flush=True)
            raise GateError("GEMINI_OWNER_ACTION_REQUIRED_TIMEOUT")
        page.wait_for_timeout(750)


def _gemini_ui_state(page) -> str:
    """Classify missing composer from actual UI, never claim login from guess."""
    from urllib.parse import urlparse
    host=(urlparse(page.url).hostname or "").lower()
    if host=="accounts.google.com":
        return "OWNER_LOGIN_REQUIRED"
    if host not in ("gemini.google.com","www.gemini.google.com"):
        return "UNEXPECTED_REDIRECT"
    try:
        if (page.locator('input[type="password"]').count() or
            page.locator('a[href*="accounts.google.com/ServiceLogin"]').count()):
            return "OWNER_LOGIN_REQUIRED"
        if page.get_by_role("button",name=re.compile(
                r"^(sign in|đăng nhập|log in)$",re.I)).count():
            return "OWNER_LOGIN_REQUIRED"
        if page.locator('iframe[src*="consent.google"]').count():
            return "CONSENT_REQUIRED"
    except Exception:
        pass
    return "COMPOSER_NOT_RENDERED"


def _wait_for_composer(page, *, seconds: int = 42):
    """Wait for client-side Gemini hydration and distinguish owner gates.

    Does not click or bypass sign-in/consent, invent a new browser, or send
    a prompt before the input field really becomes editable.
    """
    deadline=time.monotonic()+seconds
    last_state="COMPOSER_NOT_RENDERED"
    while time.monotonic()<deadline:
        try:
            editor=_visible_composer(page)
            print("TOOL_SYSTEM_GEMINI_COMPOSER=VISIBLE_EDITABLE",flush=True)
            return editor
        except GateError:
            pass
        last_state=_gemini_ui_state(page)
        if last_state in ("OWNER_LOGIN_REQUIRED","UNEXPECTED_REDIRECT",
                          "CONSENT_REQUIRED"):
            break
        page.wait_for_timeout(1100)
    # Compact diagnostic counts only: no prompt text, cookies or page HTML.
    # Next failure should be attributable to missing DOM, iframe/shadow or
    # authentication, not another generic "composer not found".
    try:
        shape=page.evaluate(r"""() => {
            let shadow=0;
            for(const node of document.querySelectorAll('*')){
                if(node.shadowRoot)shadow++;
            }
            return {
                textareas:document.querySelectorAll('textarea').length,
                inputs:document.querySelectorAll('input').length,
                roles:document.querySelectorAll('[role="textbox"]').length,
                editable:document.querySelectorAll('[contenteditable]').length,
                frames:document.querySelectorAll('iframe').length,
                shadow_hosts:shadow,
            };
        }""")
        if isinstance(shape,dict):
            for name in ("textareas","inputs","roles","editable",
                         "frames","shadow_hosts"):
                value=shape.get(name,-1)
                if isinstance(value,int):
                    print("TOOL_SYSTEM_GEMINI_DOM_"+name.upper()+"="+
                          str(max(0,min(value,9999))),flush=True)
    except Exception:
        print("TOOL_SYSTEM_GEMINI_DOM_PROBE=UNAVAILABLE",flush=True)
    print("TOOL_SYSTEM_GEMINI_COMPOSER_STATE="+last_state,flush=True)
    if last_state=="OWNER_LOGIN_REQUIRED":
        raise GateError("GEMINI_OWNER_LOGIN_REQUIRED")
    if last_state=="CONSENT_REQUIRED":
        raise GateError("GEMINI_OWNER_CONSENT_REQUIRED")
    if last_state=="UNEXPECTED_REDIRECT":
        raise GateError("GEMINI_UNEXPECTED_REDIRECT")
    raise GateError("GEMINI_COMPOSER_NOT_RENDERED_AFTER_WAIT")


def _composer_text(composer) -> str:
    for method in ("input_value", "inner_text", "text_content"):
        try:
            value = getattr(composer, method)()
            if isinstance(value, str):
                return value.strip()
        except Exception:
            continue
    return ""


def _current_user_turns(page) -> dict:
    """Read visible user-turn elements so stale page text cannot prove sending."""
    scopes = [page]
    try:
        main_frame = page.main_frame
        scopes.extend(frame for frame in list(page.frames) if frame != main_frame)
    except Exception:
        pass
    count = 0
    last_text = ""
    for scope in scopes:
        try:
            snapshot = scope.evaluate(_GEMINI_USER_TURNS_JS)
        except Exception:
            continue
        if not isinstance(snapshot, dict):
            continue
        value = snapshot.get("last_text", "")
        try:
            observed_count = max(0, int(snapshot.get("count", 0)))
        except (TypeError, ValueError):
            observed_count = 0
        if isinstance(value, str) and value.strip() and observed_count >= count:
            last_text = " ".join(value.split())
            count = observed_count
    from hashlib import sha256
    return {
        "count": count,
        "last_text": last_text,
        "last_sha256": sha256(last_text.encode("utf-8")).hexdigest() if last_text else "",
    }


def _send(page, prompt: str, *, stage: str = "PROMPT",
          lessons: list[dict] | None = None, remember=None):
    # Do not send merely because a field exists. Repeated live hit-tests must
    # prove the composer stays unobstructed for a quiet interval. This catches
    # rights dialogs that arrive asynchronously after the MP4 attachment.
    _ensure_gemini_interactive_before_send(
        page, stage=stage, timeout_seconds=14, stable_seconds=4.0,
        lessons=lessons, remember=remember)
    turns_before = _current_user_turns(page)
    print("TOOL_SYSTEM_GEMINI_SEND=USER_TURN_COUNT_BEFORE_" +
          str(min(turns_before["count"], 999)), flush=True)
    composer = _visible_composer(page)
    composer.fill(prompt)
    expected_prefix = " ".join(prompt.split())[:70]
    entered = " ".join(_composer_text(composer).split())
    if expected_prefix and expected_prefix not in entered:
        raise GateError("GEMINI_PROMPT_FILL_NOT_VERIFIED")

    # The dialog may appear between the readiness check and filling the editor.
    # Re-test live hit interception immediately before pressing Send.
    live = page.evaluate(GEMINI_PAGE_UNOBSTRUCTED_JS)
    if not live.get("unobstructed"):
        print("TOOL_SYSTEM_GEMINI_SEND=BLOCKED_AFTER_FILL", flush=True)
        _ensure_gemini_interactive_before_send(
            page, stage=stage + "_AFTER_FILL", timeout_seconds=8,
            stable_seconds=1.5, lessons=lessons, remember=remember)
        composer = _visible_composer(page)
        current = " ".join(_composer_text(composer).split())
        if expected_prefix and expected_prefix not in current:
            raise GateError("GEMINI_PROMPT_LOST_DURING_MODAL_RECOVERY")

    used_send_button = False
    for label in (re.compile("send", re.I), re.compile("gửi", re.I)):
        button = page.get_by_role("button", name=label)
        if button.count():
            candidate = button.last
            try:
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click()
                    used_send_button = True
                    break
            except Exception:
                continue
    if used_send_button:
        print("TOOL_SYSTEM_GEMINI_SEND=BUTTON_CLICKED", flush=True)
    else:
        composer.press("Enter")
        print("TOOL_SYSTEM_GEMINI_SEND=ENTER_PRESSED", flush=True)

    # Verify an actual user turn was submitted: the editor cleared and the
    # unique prefix is visible in the conversation. Never label a click alone
    # as ANALYSIS_REQUEST_SENT.
    deadline = time.monotonic() + 12
    stable = 0
    while time.monotonic() < deadline:
        try:
            current_composer = _visible_composer(page)
            current = " ".join(_composer_text(current_composer).split())
            turns_after = _current_user_turns(page)
            turn_text = turns_after["last_text"]
            turn_is_new = (
                turns_after["count"] > turns_before["count"] or
                (turns_after["last_sha256"] and
                 turns_after["last_sha256"] != turns_before["last_sha256"])
            )
            turn_contains_prompt = bool(
                expected_prefix and expected_prefix in turn_text)
            if not current and turn_is_new and turn_contains_prompt:
                stable += 1
                if stable >= 2:
                    print("TOOL_SYSTEM_GEMINI_SEND=USER_TURN_COUNT_AFTER_" +
                          str(min(turns_after["count"], 999)), flush=True)
                    print("TOOL_SYSTEM_GEMINI_SEND=NEW_USER_TURN_CONTENT_VERIFIED",
                          flush=True)
                    print("TOOL_SYSTEM_GEMINI_SEND=USER_TURN_SUBMISSION_VERIFIED",
                          flush=True)
                    return
            else:
                stable = 0
        except Exception:
            stable = 0
        page.wait_for_timeout(500)
    print("TOOL_SYSTEM_GEMINI_SEND=USER_TURN_SUBMISSION_NOT_VERIFIED", flush=True)
    raise GateError("GEMINI_PROMPT_SUBMISSION_NOT_VERIFIED")


def _model_response_candidates(page) -> list[dict]:
    """Read current assistant responses across the main page, frames and open shadow roots."""
    scopes = [("main", page)]
    try:
        main_frame = page.main_frame
        for index, frame in enumerate(list(page.frames)):
            if frame == main_frame:
                continue
            scopes.append(("frame_" + str(index), frame))
    except Exception:
        pass

    collected: list[dict] = []
    order_offset = 0
    for scope_name, scope in scopes:
        try:
            snapshot = scope.evaluate(_MODEL_RESPONSE_SNAPSHOT_JS)
            rows = snapshot.get("candidates", []) if isinstance(snapshot, dict) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                value = row.get("text")
                if not isinstance(value, str):
                    continue
                value = " ".join(value.split()).strip()
                if len(value) < 25:
                    continue
                collected.append({
                    "text": value,
                    "selector": str(row.get("selector", "shadow_dom"))[:90],
                    "priority": int(row.get("priority", 0)),
                    "order": order_offset + int(row.get("order", 0)),
                    "scope": scope_name,
                })
            order_offset += 1000000
        except Exception:
            pass

        # Keep the known selectors as an independent compatibility fallback.
        for selector_index, selector in enumerate(SELECTORS):
            try:
                nodes = scope.locator(selector)
                count = min(nodes.count(), 24)
            except Exception:
                continue
            for index in range(count):
                try:
                    value = nodes.nth(index).inner_text(timeout=1200).strip()
                except Exception:
                    continue
                value = " ".join(value.split())
                if len(value) < 25:
                    continue
                collected.append({
                    "text": value,
                    "selector": selector,
                    "priority": 130 - selector_index,
                    "order": order_offset + index,
                    "scope": scope_name,
                })
            order_offset += 1000

    # Remove duplicate parent/child renderings, preferring the most specific
    # component and its latest position. Never log response contents.
    deduplicated: dict[str, dict] = {}
    for row in collected:
        key = row["text"]
        prior = deduplicated.get(key)
        if prior is None or (row["priority"], row["order"]) > (
                prior["priority"], prior["order"]):
            deduplicated[key] = row
    return list(deduplicated.values())


def _last_model_response(page, *, candidates: list[dict] | None = None) -> str:
    found = candidates if candidates is not None else _model_response_candidates(page)
    if not found:
        return ""
    latest = max(found, key=lambda row: (row.get("priority", 0), row.get("order", 0)))
    return str(latest.get("text", "")).strip()


def _await_distinct_reply(page, before: str, *, seconds: int = 240,
                          baseline: list[dict] | None = None) -> str:
    deadline = time.monotonic() + seconds
    baseline_texts = {str(row.get("text", "")) for row in (baseline or [])}
    if before:
        baseline_texts.add(before)
    stable_text = ""
    stable_signature = None
    stable = 0
    last_candidates: list[dict] = []
    while time.monotonic() < deadline:
        candidates = _model_response_candidates(page)
        last_candidates = candidates
        fresh = [
            row for row in candidates
            if str(row.get("text", "")) not in baseline_texts
            and len(str(row.get("text", ""))) >= 160
        ]
        if fresh:
            winner = max(
                fresh,
                key=lambda row: (row.get("priority", 0), row.get("order", 0)))
            current = str(winner["text"])
            signature = (winner.get("selector"), winner.get("scope"), current)
            if signature == stable_signature:
                stable += 1
            else:
                stable = 0
                stable_signature = signature
                stable_text = current
            if stable >= 2:
                from hashlib import sha256
                print("TOOL_SYSTEM_GEMINI_REPLY=NEW_RESPONSE_VERIFIED", flush=True)
                source = re.sub(r"[^A-Z0-9_]", "_",
                                str(winner.get("selector", "DOM")).upper())[:48]
                print("TOOL_SYSTEM_GEMINI_REPLY_SOURCE=" + source, flush=True)
                print("TOOL_SYSTEM_GEMINI_REPLY_LENGTH=" + str(len(stable_text)), flush=True)
                print("TOOL_SYSTEM_GEMINI_REPLY_SHA256=" +
                      sha256(stable_text.encode("utf-8")).hexdigest(), flush=True)
                return stable_text
        else:
            stable = 0
            stable_signature = None
            stable_text = ""
        page.wait_for_timeout(2000)

    print("TOOL_SYSTEM_GEMINI_REPLY=TIMEOUT_NO_NEW_RESPONSE", flush=True)
    print("TOOL_SYSTEM_GEMINI_REPLY_BEFORE_LENGTH=" + str(len(before)), flush=True)
    print("TOOL_SYSTEM_GEMINI_REPLY_CANDIDATE_COUNT=" +
          str(min(len(last_candidates), 999)), flush=True)
    print("TOOL_SYSTEM_GEMINI_REPLY_LAST_LENGTH=" + str(len(stable_text)), flush=True)
    # Log only selector names/counts and lengths, never response or prompt text.
    ranked = sorted(last_candidates, key=lambda row: (
        row.get("priority", 0), row.get("order", 0)), reverse=True)[:5]
    for index, row in enumerate(ranked, start=1):
        selector = re.sub(r"[^A-Z0-9_]", "_",
                          str(row.get("selector", "DOM")).upper())[:48]
        print("TOOL_SYSTEM_GEMINI_REPLY_CANDIDATE_" + str(index) + "=" +
              selector + "_LEN_" + str(len(str(row.get("text", "")))), flush=True)
    raise RuntimeError("GEMINI_RESPONSE_NOT_VERIFIED")


def _start_fresh_gemini_conversation(page, *, seconds: float = 12.0) -> None:
    """Start a new chat in the existing Gemini tab; reject old conversation data."""
    kind, _, _ = _inspect_gemini_notice_dom(page)
    if kind != "none":
        raise GateError("GEMINI_NEW_CHAT_BLOCKED_BY_DIALOG")
    new_chat_name = re.compile(
        r"^(?:cuộc trò chuyện mới|new chat|new conversation)$", re.I)
    clicked = False
    for role in ("button", "link"):
        try:
            locator = page.get_by_role(role, name=new_chat_name)
            count = min(locator.count(), 20)
        except Exception:
            continue
        for index in range(count - 1, -1, -1):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click(timeout=5000)
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            break
    if not clicked:
        try:
            locator = page.get_by_text(new_chat_name, exact=True)
            count = min(locator.count(), 20)
        except Exception:
            count = 0
            locator = None
        if locator is not None:
            for index in range(count - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible() and candidate.is_enabled():
                        candidate.click(timeout=5000)
                        clicked = True
                        break
                except Exception:
                    continue
    if not clicked:
        raise GateError("GEMINI_NEW_CHAT_CONTROL_NOT_FOUND")
    print("TOOL_SYSTEM_GEMINI_CHAT=NEW_CHAT_CONTROL_CLICKED", flush=True)

    deadline = time.monotonic() + max(0.1, float(seconds))
    while time.monotonic() < deadline:
        kind, _, _ = _inspect_gemini_notice_dom(page)
        if kind != "none":
            raise GateError("GEMINI_NEW_CHAT_BLOCKED_BY_DIALOG")
        turns = _current_user_turns(page)
        responses = _model_response_candidates(page)
        try:
            composer = _visible_composer(page)
            composer_empty = not _composer_text(composer)
        except Exception:
            composer_empty = False
        if turns.get("count") == 0 and not responses and composer_empty:
            print("TOOL_SYSTEM_GEMINI_CHAT=FRESH_CONVERSATION_VERIFIED", flush=True)
            return
        page.wait_for_timeout(500)
    raise GateError("GEMINI_FRESH_CONVERSATION_NOT_VERIFIED")



_UPLOAD_TRIGGER = re.compile(
    r"add[ _-]?files?|attach(?:ment)?|upload[ _-]?files?|"
    r"thêm tệp|thêm file|đính kèm|tải tệp|"
    r"add[_ -]?(?:2|circle|outline)|attach_file|paperclip",
    re.I,
)
_UPLOAD_MENU_ITEM = re.compile(
    r"upload[ _-]?files?|upload from computer|from computer|browse files|"
    r"from device|tải tệp lên|tải tệp",
    re.I,
)
_UPLOAD_DANGEROUS = re.compile(
    r"new chat|new conversation|send|submit|microphone|voice|record|"
    r"share|publish|delete|remove|sign.?in|log.?in|consent|payment|"
    r"đăng|gửi|ghi âm|đồng ý|xóa|xoá",
    re.I,
)


def _upload_element_metadata(element) -> dict:
    return element.evaluate("""el => {
      const iconNodes = el.querySelectorAll
        ? [...el.querySelectorAll('mat-icon,[data-icon],svg,[class*="icon"]')].slice(0,5)
        : [];
      const icons = iconNodes.map(n => [
        n.textContent || '', n.getAttribute('data-icon') || '',
        n.getAttribute('aria-label') || '', n.getAttribute('fonticon') || '',
        typeof n.className === 'string' ? n.className : ''
      ].join(' ')).join(' ');
      const attrs = ['aria-label','title','mattooltip','data-tooltip',
        'data-testid','data-test-id','role','name','id','class']
        .map(k => el.getAttribute?.(k) || '').join(' ');
      return {
        hint: (attrs + ' ' + icons + ' ' +
          (el.innerText || el.textContent || '').slice(0,100))
          .replace(/\\s+/g,' ').trim().slice(0,260),
        role: (el.getAttribute('role') || el.tagName || '').toLowerCase(),
        cursor: getComputedStyle(el).cursor,
        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
      };
    }""")


def _near_composer(element, composer_box: dict) -> tuple[bool, dict | None, dict | None]:
    try:
        if not element.is_visible() or not element.is_enabled():
            return False, None, None
        box = element.bounding_box()
        if not box:
            return False, None, None
        width, height = float(box["width"]), float(box["height"])
        if width < 10 or height < 10 or width > 110 or height > 110:
            return False, box, None
        cx = float(box["x"]) + width / 2
        cy = float(box["y"]) + height / 2
        left = float(composer_box["x"])
        right = left + float(composer_box["width"])
        center_y = float(composer_box["y"]) + float(composer_box["height"]) / 2
        near = left - 230 <= cx <= right + 100 and abs(cy - center_y) <= 185
        if not near:
            return False, box, None
        metadata = _upload_element_metadata(element)
        return True, box, metadata
    except Exception:
        return False, None, None


def _find_upload_trigger(page):
    """Prefer the historical accessible Add files button, then its live icon wrapper.

    CSS locators in Playwright traverse open Shadow DOM. This keeps the original
    two-step Gemini flow but also recognizes current Material icon names such as
    add_2/attach_file when the button has no accessible text label.
    """
    composer = _visible_composer(page)
    composer_box = composer.bounding_box()
    if not composer_box:
        raise GateError("GEMINI_COMPOSER_GEOMETRY_MISSING")

    ranked = []
    # Exact accessible-role path used by the original working implementation.
    try:
        locator = page.get_by_role("button", name=_UPLOAD_TRIGGER)
        count = min(locator.count(), 40)
    except Exception:
        count = 0
        locator = None
    if locator is not None:
        for index in range(count):
            element = locator.nth(index)
            ok, box, metadata = _near_composer(element, composer_box)
            if not ok or not metadata or _UPLOAD_DANGEROUS.search(metadata["hint"]):
                continue
            hint = metadata["hint"]
            score = 40 if re.search(
                r"add[ _-]?files?|attach|upload|thêm tệp|đính kèm|tải tệp", hint, re.I
            ) else 30
            ranked.append((score, element, box, hint))

    # Python-only live DOM fallback. Enumerate interactive elements through
    # Playwright rather than guessing screen coordinates or calling a model.
    try:
        controls = page.locator(
            'button,[role="button"],[aria-haspopup],[tabindex],'
            '[class*="add"],[class*="attach"],[class*="upload"]'
        )
        count = min(controls.count(), 700)
    except Exception:
        controls = None
        count = 0
    if controls is not None:
        for index in range(count):
            element = controls.nth(index)
            ok, box, metadata = _near_composer(element, composer_box)
            if not ok or not metadata:
                continue
            hint = metadata["hint"]
            if _UPLOAD_DANGEROUS.search(hint):
                continue
            # Never click an unlabeled random control. Its current DOM attributes
            # or child Material icon must explicitly identify it as attachment.
            if not _UPLOAD_TRIGGER.search(hint):
                continue
            if metadata["disabled"]:
                continue
            score = 40 if re.search(
                r"add[ _-]?files?|attach(?:ment)?|upload[ _-]?files?|"
                r"thêm tệp|đính kèm|tải tệp", hint, re.I
            ) else 32
            ranked.append((score, element, box, hint))

    # Deduplicate a button and its nested icon by their on-screen center.
    unique = []
    for item in sorted(ranked, key=lambda value: value[0], reverse=True):
        _, _, box, _ = item
        cx = float(box["x"]) + float(box["width"]) / 2
        cy = float(box["y"]) + float(box["height"]) / 2
        if any(abs(cx - x) <= 10 and abs(cy - y) <= 10 for x, y, _ in unique):
            continue
        unique.append((cx, cy, item))
    if not unique:
        raise GateError("GEMINI_ADD_FILES_CONTROL_NOT_FOUND")
    # Only a clearly named "Add files" action should outrank a generic icon.
    # Two different icon-only candidates remain ambiguous unless the winner has
    # a substantial semantic advantage; never choose based on tiny score noise.
    if len(unique) > 1 and unique[0][2][0] - unique[1][2][0] < 12:
        raise GateError("GEMINI_ADD_FILES_CONTROL_AMBIGUOUS")
    return unique[0][2][1]


def _find_upload_menu_item(page):
    for role in ("menuitem", "button", "option"):
        try:
            locator = page.get_by_role(role, name=_UPLOAD_MENU_ITEM)
            count = min(locator.count(), 30)
        except Exception:
            continue
        visible = []
        for index in range(count):
            element = locator.nth(index)
            try:
                if not element.is_visible() or not element.is_enabled():
                    continue
                metadata = _upload_element_metadata(element)
                if _UPLOAD_DANGEROUS.search(metadata["hint"]):
                    continue
                if _UPLOAD_MENU_ITEM.search(metadata["hint"]):
                    visible.append(element)
            except Exception:
                continue
        if len(visible) == 1:
            return visible[0]
        if len(visible) > 1:
            raise GateError("GEMINI_UPLOAD_MENU_ITEM_AMBIGUOUS")
    return None


def _set_existing_file_input(page, mp4: Path) -> bool:
    inputs = page.locator('input[type="file"]')
    count = min(inputs.count(), 12)
    if count == 0:
        return False
    candidates = []
    for index in range(count):
        item = inputs.nth(index)
        try:
            accept = (item.get_attribute("accept") or "").casefold()
            # Prefer inputs explicitly able to accept video, MP4 or arbitrary
            # files. A hidden input is valid: Playwright sets it without OS UI.
            score = 4 if ".mp4" in accept else 3 if "video" in accept else (
                2 if "*/*" in accept or not accept else 0)
            if score:
                candidates.append((score, item))
        except Exception:
            continue
    if not candidates:
        raise GateError("GEMINI_FILE_INPUT_NOT_COMPATIBLE")
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    best = [item for score, item in candidates if score == candidates[0][0]]
    if len(best) != 1:
        raise GateError("GEMINI_FILE_INPUT_AMBIGUOUS")
    best[0].set_input_files(str(mp4), timeout=30000)
    print("TOOL_SYSTEM_GEMINI=FILE_INPUT_POPULATED", flush=True)
    return True


def _wait_for_uploaded_attachment(page, mp4: Path, *, seconds: float = 90) -> None:
    deadline = time.monotonic() + max(0.1, float(seconds))
    stem = mp4.stem[:12]
    while time.monotonic() < deadline:
        try:
            body = page.locator("body").inner_text(timeout=8000)
        except Exception:
            body = ""
        if mp4.name in body or (len(stem) >= 8 and stem in body):
            print("TOOL_SYSTEM_GEMINI=VIDEO_ATTACHMENT_UI_VERIFIED", flush=True)
            return
        # Verify DOM metadata too; some Gemini versions display the filename in
        # the attachment chip's aria-label/title, not in body innerText.
        try:
            found = page.evaluate(r"""({name,stem}) => {
              const roots=[document], seen=new Set();
              const hits=[];
              for(let i=0;i<roots.length&&i<140;i++){
                const root=roots[i]; if(seen.has(root))continue; seen.add(root);
                for(const host of root.querySelectorAll('*'))
                  if(host.shadowRoot&&roots.length<140)roots.push(host.shadowRoot);
                for(const el of root.querySelectorAll(
                    '[aria-label],[title],[data-filename],[data-file-name],'+
                    '[data-testid*="file"],[data-test-id*="file"],'+
                    'img[alt],video[title]')){
                  const label=['aria-label','title','data-filename','data-file-name','alt']
                    .map(k=>el.getAttribute(k)||'').join(' ');
                  if(label.includes(name)||(stem.length>=8&&label.includes(stem)))hits.push(label);
                }
              }
              return hits.length>0;
            }""", {"name": mp4.name, "stem": stem})
            if found:
                print("TOOL_SYSTEM_GEMINI=VIDEO_ATTACHMENT_UI_VERIFIED", flush=True)
                return
        except Exception:
            pass
        page.wait_for_timeout(750)
    raise GateError("GEMINI_ATTACHMENT_NOT_VISIBLE")


_HISTORICAL_UPLOAD_MENU = "Nội dung tải lên và công cụ"
_HISTORICAL_UPLOAD_FILE_ITEM = re.compile(
    r"^(?:Tệp|Tải tệp lên|Upload files|Files)$", re.I,
)
_HISTORICAL_ATTACHMENT_CLOSE = re.compile(
    r"^(?:đóng|xóa|xoá|gỡ|remove|close)\s+"
    r"(?:tệp đính kèm|tệp|hình ảnh đính kèm|ảnh đính kèm|hình ảnh|ảnh|"
    r"attachment|attached file|uploaded file|file|image|media)"
    r"(?:[ :–-]+.+)?$",
    re.I,
)


def _upload_source(page, mp4: Path, *, lessons=None, remember=None) -> None:
    """Use the exact Playwright upload sequence from the older working project.

    Gemini's current accessible name is "Nội dung tải lên và công cụ"; the old
    implementation clicked that button, chose "Tệp"/"Upload files", then used
    Playwright's file chooser. Do not substitute the unrelated "Add files" name
    or a guessed coordinate click.
    """
    del lessons, remember
    if not mp4.is_file() or mp4.suffix.lower() != ".mp4":
        raise GateError("GEMINI_SOURCE_MP4_NOT_VERIFIED")

    print("TOOL_SYSTEM_GEMINI_UPLOAD_OPERATOR=PLAYWRIGHT_TWO_STEP", flush=True)
    print("TOOL_SYSTEM_GEMINI_UPLOAD_IMPLEMENTATION=HISTORICAL_TOOL_MENU", flush=True)

    # STEP 1 — use the concrete role label from the older implementation.
    attach = page.get_by_role("button", name=_HISTORICAL_UPLOAD_MENU)
    attach_count = min(max(int(attach.count()), 0), 99)
    print("TOOL_SYSTEM_GEMINI_ATTACH_MENU_ROLE_COUNT=" + str(attach_count), flush=True)
    if attach_count == 0:
        raise GateError("GEMINI_UPLOAD_TOOL_MENU_NOT_FOUND")

    visible_attach = []
    for index in range(attach_count):
        candidate = attach.nth(index)
        try:
            if candidate.is_visible() and candidate.is_enabled():
                visible_attach.append(candidate)
        except Exception:
            continue
    if len(visible_attach) != 1:
        raise GateError("GEMINI_UPLOAD_TOOL_MENU_AMBIGUOUS")
    visible_attach[0].click(timeout=15000)
    print("TOOL_SYSTEM_GEMINI_UPLOAD_TOOL_MENU=CLICKED", flush=True)
    page.wait_for_timeout(700)

    # Match the old successful flow first: button name "Tệp"/"Upload files".
    # If this Gemini build marks the same visible option as menuitem or a
    # generic text node, fall back in that order. Stop at the first usable
    # locator type; do not merge matches from different locator strategies.
    item_locators = [
        ("button", page.get_by_role("button", name=_HISTORICAL_UPLOAD_FILE_ITEM)),
        ("menuitem", page.get_by_role("menuitem", name=_HISTORICAL_UPLOAD_FILE_ITEM)),
        ("text", page.get_by_text(_HISTORICAL_UPLOAD_FILE_ITEM, exact=True)),
    ]
    selected_item = None
    role_counts = []
    visible_counts = []
    for role_name, locator in item_locators:
        try:
            count = min(max(int(locator.count()), 0), 99)
        except Exception:
            count = 0
        role_counts.append(role_name + ":" + str(count))
        visible = []
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible() and candidate.is_enabled():
                    visible.append(candidate)
            except Exception:
                continue
        visible_counts.append(role_name + ":" + str(len(visible)))
        if visible:
            # Exact matching can still find a hidden responsive duplicate.
            # Filter visibility, then choose the last visible exact label as
            # the older working implementation did with Locator.last.
            selected_item = visible[-1]
            print("TOOL_SYSTEM_GEMINI_UPLOAD_FILE_ITEM_SOURCE=" + role_name, flush=True)
            break

    print("TOOL_SYSTEM_GEMINI_UPLOAD_FILE_ITEM_ROLE_COUNTS=" + ",".join(role_counts), flush=True)
    print("TOOL_SYSTEM_GEMINI_UPLOAD_FILE_ITEM_VISIBLE_COUNTS=" + ",".join(visible_counts), flush=True)
    if selected_item is None:
        # Capture menu entries through open Shadow DOM as well as light DOM.
        # This diagnostic is specifically for the next run if the UI changed.
        try:
            menu_snapshot = page.evaluate(r"""() => {
              const roots=[document], seen=new Set(), items=[];
              for(let i=0;i<roots.length&&i<160;i++){
                const root=roots[i];
                if(seen.has(root))continue;
                seen.add(root);
                for(const host of root.querySelectorAll('*')){
                  if(host.shadowRoot&&roots.length<160)roots.push(host.shadowRoot);
                }
                for(const e of root.querySelectorAll(
                  '[role="menu"] *,[role="menuitem"],[role="option"],button,[aria-label],[title]'
                )){
                  const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                  if(!r.width||!r.height||s.visibility==="hidden"||s.display==="none")continue;
                  const text=(e.innerText||e.getAttribute('aria-label')||
                    e.getAttribute('title')||'').replace(/\s+/g,' ').trim().slice(0,100);
                  if(text)items.push({role:e.getAttribute('role')||e.tagName.toLowerCase(),text});
                }
              }
              return items.slice(-50);
            }""")
            print("TOOL_SYSTEM_GEMINI_UPLOAD_MENU_VISIBLE_ITEMS=" + json.dumps(menu_snapshot, ensure_ascii=False), flush=True)
        except Exception as exc:
            print("TOOL_SYSTEM_GEMINI_UPLOAD_MENU_VISIBLE_ITEMS=UNAVAILABLE_" +
                  type(exc).__name__, flush=True)
        raise GateError("GEMINI_UPLOAD_FILE_MENU_ITEM_NOT_FOUND")

    try:
        with page.expect_file_chooser(timeout=12000) as selected:
            selected_item.click(timeout=12000)
        selected.value.set_files(str(mp4))
    except Exception as exc:
        kind = type(exc).__name__
        print("TOOL_SYSTEM_GEMINI_FILE_CHOOSER=FAILED_" + kind, flush=True)
        raise GateError("GEMINI_FILE_CHOOSER_SELECTION_FAILED") from None
    print("TOOL_SYSTEM_GEMINI_FILE_CHOOSER=SELECTED", flush=True)

    # Require both the known attachment chip and the exact MP4 identity before
    # allowing _send() to type the analysis prompt.
    chips = page.get_by_role("button", name=_HISTORICAL_ATTACHMENT_CLOSE)
    deadline = time.monotonic() + 65.0
    while time.monotonic() < deadline:
        try:
            chip_count = min(max(int(chips.count()), 0), 99)
            if chip_count > 1:
                raise GateError("GEMINI_ATTACHMENT_CHIPS_AMBIGUOUS")
            if chip_count == 1 and chips.first.is_visible():
                print("TOOL_SYSTEM_GEMINI=VIDEO_ATTACHMENT_CHIP_VISIBLE", flush=True)
                break
        except GateError:
            raise
        except Exception:
            pass
        page.wait_for_timeout(850)
    else:
        raise GateError("GEMINI_UPLOADED_VIDEO_CHIP_MISSING")

    page.wait_for_timeout(2500)
    _wait_for_uploaded_attachment(page, mp4)
    print("TOOL_SYSTEM_GEMINI=HISTORICAL_UPLOAD_FLOW_VERIFIED", flush=True)


def _rewrite_prompt_for_job(job: Job) -> str:
    """Build the standard or owner-feedback revision prompt without browser side effects."""
    if job.stage != "REVISION_REQUESTED":
        return REWRITE
    feedback = " ".join(str(job.feedback or "").split())[:1800]
    if not feedback or feedback == "Owner requested revision":
        raise GateError("GEMINI_REVISION_FEEDBACK_REQUIRED")
    previous_script = " ".join(str(job.script or "").split())[:3500]
    return REWRITE + (
        "\\n\\nYÊU CẦU SỬA TỪ CHỦ PAGE (BẮT BUỘC ÁP DỤNG):\\n"
        + feedback
        + "\\n\\nKỊCH BẢN LẦN TRƯỚC — KHÔNG LẶP LẠI CÂU CHỮ/DIỄN BIẾN:\\n"
        + (previous_script or "[không có bản cũ để so sánh]")
        + "\\n\\nViết bản sửa mới hoàn chỉnh; giữ nhân vật khóa và các ràng buộc an toàn."
    )


def screenplay_from_mp4(job: Job, *, lessons=None, remember=None) -> ScriptResult:
    if job.stage not in {"SOURCE_READY", "REVISION_REQUESTED"} or not job.source_sha or not job.source_path:
        raise GateError("GEMINI_REQUIRES_SOURCE_READY_MP4")
    if job.page != "Mẹo nhỏ mỗi ngày":
        raise GateError("GEMINI_PAGE_NOT_CONFIGURED")
    file = Path(job.source_path)
    if not file.is_file() or file.suffix.lower() != ".mp4":
        raise GateError("GEMINI_SOURCE_FILE_MISSING")
    if digest_file(file) != job.source_sha:
        raise GateError("GEMINI_SOURCE_SHA_MISMATCH")
    rewrite_prompt = _rewrite_prompt_for_job(job)
    with Chrome(profile_dir=PROFILE, downloads_dir=DOWNLOADS,
                cdp_url="http://127.0.0.1:9222") as browser:
        context = browser.context
        if context is None:
            raise GateError("GEMINI_CHROME_CONTEXT_MISSING")
        tab, tab_created_by_step06 = _select_or_create_gemini_tab(context)
        owner_action_required = False
        try:
            current_host = (urlparse(tab.url).hostname or "").lower()
            if tab_created_by_step06 or current_host not in {
                    "gemini.google.com", "www.gemini.google.com"}:
                tab.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=45000)
            else:
                # Keep the exact signed-in user tab, with its live modal/UI state.
                try:
                    tab.wait_for_load_state("domcontentloaded", timeout=6000)
                except Exception:
                    pass
                print("TOOL_SYSTEM_GEMINI_TAB=EXISTING_TAB_STATE_PRESERVED", flush=True)
            if "accounts.google.com" in tab.url or tab.locator('input[type="password"]').count():
                raise GateError("GEMINI_OWNER_LOGIN_REQUIRED")
            # Owner-approved default: acknowledge ONLY Gemini's known video
            # rights reminder, if it appears; leave every other dialog untouched.
            _accept_known_video_creation_notice(tab, wait_ms=1800,
                                                lessons=lessons, remember=remember)
            # If the composer is absent, use the bounded Python DOM recovery
            # rules, then re-observe and verify the editor before continuing.
            try:
                _wait_for_composer(tab,seconds=12)
            except GateError as exc:
                if str(exc)!="GEMINI_COMPOSER_NOT_RENDERED_AFTER_WAIT":
                    raise
                from .tool_recovery import recover_editor
                print("TOOL_SYSTEM_GEMINI=COMPOSER_AGENT_SELF_RECOVERY_STARTED",
                      flush=True)
                print("TOOL_SYSTEM_GEMINI_COMPOSER_RECOVERY=PYTHON_RULES_ONLY", flush=True)
                recover_editor(
                    tab,probe=_visible_composer,classify=_gemini_ui_state,
                    lessons=lessons,
                    remember=remember)
            print("TOOL_SYSTEM_GEMINI=CHAT_COMPOSER_AVAILABLE", flush=True)
            _start_fresh_gemini_conversation(tab)
            _wait_for_composer(tab, seconds=20)
            print("TOOL_SYSTEM_GEMINI=FRESH_CHAT_COMPOSER_AVAILABLE", flush=True)
            _upload_source(tab, file, lessons=lessons, remember=remember)
            # Some Gemini UI surfaces defer the reminder until attachment.
            _accept_known_video_creation_notice(tab, wait_ms=1000,
                                                lessons=lessons, remember=remember)
            analysis_baseline = _model_response_candidates(tab)
            prev = _last_model_response(tab, candidates=analysis_baseline)
            _send(tab, ANALYZE, stage="ANALYSIS", lessons=lessons,
                  remember=remember)
            print("TOOL_SYSTEM_GEMINI=ANALYSIS_REQUEST_SENT", flush=True)
            analysis = _await_distinct_reply(
                tab, prev, baseline=analysis_baseline)
            if "KHÔNG ĐỌC ĐƯỢC VIDEO" in analysis.upper():
                raise RuntimeError("GEMINI_COULD_NOT_ACCESS_VIDEO")
            print("TOOL_SYSTEM_GEMINI=ORIGINAL_ANALYSIS_RECEIVED", flush=True)
            screenplay_baseline = _model_response_candidates(tab)
            prev = _last_model_response(tab, candidates=screenplay_baseline)
            if job.stage == "REVISION_REQUESTED":
                print("TOOL_SYSTEM_GEMINI_REVISION=OWNER_FEEDBACK_INCLUDED", flush=True)
            _send(tab, rewrite_prompt, stage="SCREENPLAY", lessons=lessons,
                  remember=remember)
            print("TOOL_SYSTEM_GEMINI=NEW_SCREENPLAY_REQUEST_SENT", flush=True)
            script = _await_distinct_reply(
                tab, prev, baseline=screenplay_baseline)
            if (len(script) < 250 or
                    "KHÔNG ĐỌC ĐƯỢC VIDEO" in script.upper()):
                raise RuntimeError("GEMINI_NEW_SCREENPLAY_UNVERIFIED")
            if digest_file(file) != job.source_sha:
                raise RuntimeError("GEMINI_SOURCE_CHANGED_DURING_REVIEW")
            print("TOOL_SYSTEM_GEMINI=NEW_SCREENPLAY_RECEIVED", flush=True)
            print("TOOL_SYSTEM_GEMINI_ANALYSIS_SHA256=" +
                  sha256(analysis.encode("utf-8")).hexdigest(), flush=True)
            print("TOOL_SYSTEM_GEMINI_SCREENPLAY_SHA256=" +
                  sha256(script.encode("utf-8")).hexdigest(), flush=True)
            return ScriptResult(analysis, script)
        except Exception as exc:
            # Login and unknown dialogs still require the OWNER. Only the one
            # pre-approved video-rights reminder may be clicked after exact DOM
            # title/rights + live target guards pass. Keep this same tab in the
            # existing Chrome; never open another browser or bypass sign-in.
            owner_action_required=(
                isinstance(exc,GateError) and str(exc) in {
                    "GEMINI_OWNER_LOGIN_REQUIRED",
                    "GEMINI_OWNER_CONSENT_REQUIRED"})
            # Keep a local visual observation for debugging the exact
            # signed-in Gemini UI, never publish screenshot to GitHub/logs.
            # This does NOT claim Gemini processed the source video.
            try:
                dest = Path("/kaggle/working/tool-system-v2-runtime/state/ui/gemini_step06_last_error.jpg")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.parent.chmod(0o700)
                shot = tab.screenshot(type="jpeg", quality=65, timeout=10000)
                dest.write_bytes(shot)
                dest.chmod(0o600)
                print("TOOL_SYSTEM_GEMINI_UI=SCREENSHOT_SAVED_LOCAL_PRIVATE",flush=True)
            except Exception:
                print("TOOL_SYSTEM_GEMINI_UI=SCREENSHOT_UNAVAILABLE",flush=True)
            raise
        finally:
            if owner_action_required or not tab_created_by_step06:
                print("TOOL_SYSTEM_GEMINI=OWNER_OR_REUSED_TAB_RETAINED_IN_EXISTING_CHROME",
                      flush=True)
            else:
                tab.close()
