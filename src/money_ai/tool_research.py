"""Autonomous operator's evidence-based web research, NOT an action script.

Before interacting with a changing website, Tool System V2 retrieves the provider's
current OFFICIAL guidance online, extracts the relevant excerpt, and uses that
as context for deciding actions from the LIVE browser observation. It must
not execute instructions found inside website text. All navigation and
irreversible actions remain under Python permission gates.

No paid search API, no extra browser and no credentials sent to search.
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urlparse
import re

import requests

# Seeds are OFFICIAL documentation, not browser selectors. Search attempts
# allow Tool System V2 to discover further official help URLs as products evolve.
PROVIDERS = {
    "gemini.google.com": {
        "official_hosts": frozenset({"support.google.com"}),
        "seed": "https://support.google.com/gemini/answer/14903178?hl=en",
        "topic": "Gemini Apps upload a video file on the web Add files",
    },
    "meta.ai": {
        "official_hosts": frozenset({"meta.ai", "www.meta.ai", "facebook.com", "www.facebook.com"}),
        "seed": None,
        "topic": "Meta AI website upload reference image to create video",
    },
    "labs.google": {
        "official_hosts": frozenset({"support.google.com", "labs.google", "labs.google.com"}),
        "seed": "https://support.google.com/flow/answer/16353334?hl=en",
        "topic": "Google Flow create video with scene references and extend",
    },
    "labs.google.com": {
        "official_hosts": frozenset({"support.google.com", "labs.google", "labs.google.com"}),
        "seed": "https://support.google.com/flow/answer/16353334?hl=en",
        "topic": "Google Flow create video with scene references and extend",
    },
    "flow.google.com": {
        "official_hosts": frozenset({"support.google.com", "flow.google.com"}),
        "seed": "https://support.google.com/flow/answer/16353334?hl=en",
        "topic": "Google Flow create video with scene references and extend",
    },
}
MAX_BYTES = 2_000_000
MAX_TEXT = 2800

# Small source-attributed offline knowledge, verified against official docs
# on 2026-10-19. Never pretend a cached snippet was fetched LIVE.
VERIFIED_CACHE = {
    "gemini.google.com": (
        "https://support.google.com/gemini/answer/14903178?hl=en",
        "In the Gemini web app, enter a prompt. Click Add files, then "
        "Upload files to select a video from your device. Wait for the "
        "video attachment to appear before submitting the prompt."
    ),
    "labs.google": (
        "https://support.google.com/flow/answer/16353334?hl=en",
        "Google Flow can create videos using prompts, ingredients, frames "
        "and reference media. First choose a project and video mode, then "
        "add media references and configure generation settings."
    ),
}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1
    def handle_data(self, data):
        if not self.skip:
            s = " ".join(data.split())
            if s and len(s) > 2:
                self.parts.append(s)


@dataclass(frozen=True)
class Guide:
    url: str
    excerpt: str
    source: str


def _host_allowed(url: str, hosts: frozenset[str]) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in hosts


def _extract_relevant(html: str, terms: tuple[str, ...]) -> str:
    parser = _Text()
    parser.feed(html)
    lines = parser.parts
    interesting = [i for i,s in enumerate(lines) if any(t in s.casefold() for t in terms)]
    selected = []
    for index in interesting[:12]:
        selected.extend(lines[max(0,index-1):min(len(lines),index+3)])
    selected = list(dict.fromkeys(selected))
    return "\n".join(selected)[:MAX_TEXT]


class GuideResearch:
    def __init__(self, session=None):
        self.session = session or requests.Session()

    def _fetch(self, url: str, hosts: frozenset[str]) -> str:
        if not _host_allowed(url, hosts):
            raise ValueError("UNAPPROVED_RESEARCH_DOMAIN")
        # requests will verify TLS; explicitly check the *final* redirect
        # target so official support links cannot redirect to arbitrary hosts.
        response = self.session.get(url, timeout=18,
                                    headers={"User-Agent":"MoneyAI/1.0 (official-help-research)"})
        response.raise_for_status()
        if not _host_allowed(getattr(response, "url", url), hosts):
            raise ValueError("RESEARCH_REDIRECTED_OUTSIDE_OFFICIAL_DOMAIN")
        if len(response.content) > MAX_BYTES:
            raise ValueError("RESEARCH_DOCUMENT_TOO_LARGE")
        return response.text

    def learn(self, provider: str, task: str, *, prefer_search: bool = False) -> Guide:
        provider = provider.lower().removeprefix("www.")
        spec = PROVIDERS.get(provider)
        if not spec:
            raise ValueError("RESEARCH_UNAPPROVED_TOOL_PROVIDER")
        # Research follows the current TASK, not an upload-only keyword
        # template. A missing chat editor needs official chat instructions,
        # not yet another answer about choosing a local MP4.
        task_lower=task.casefold()
        looking_for_chat=any(fragment in task_lower for fragment in (
            "chat", "composer", "text box", "input", "prompt", "conversation"))
        if looking_for_chat:
            terms=("start a chat","chat","conversation","enter a prompt",
                   "ask gemini","type your","text box","message",
                   "prompt","hỏi","trò chuyện","nhập câu lệnh")
        else:
            terms=("add files", "upload files", "upload", "video",
                   "tải tệp", "đính kèm", "file", "attach")
        # Use the official, discoverable help URL where one is known. Search
        # is a secondary option, not a hard-coded CSS or pixel route.
        if spec["seed"] and not prefer_search:
            try:
                html = self._fetch(spec["seed"], spec["official_hosts"])
                evidence = _extract_relevant(html, terms)
                if len(evidence) >= 50:
                    print("TOOL_SYSTEM_RESEARCH=OFFICIAL_GUIDE_READ", flush=True)
                    return Guide(spec["seed"], evidence, "OFFICIAL_LIVE_PAGE")
            except (requests.RequestException, ValueError, TypeError):
                pass

        # Search via public HTML, no API token. Extract ONLY official URLs.
        # If the search engine blocks automated requests, never make up
        # instructions or retry in a busy loop.
        query = (
            provider + " official help how to start chat enter prompt "
            if looking_for_chat else spec["topic"]+" "
        ) + task[:100]
        url = "https://www.google.com/search?" + urlencode({"q":query})
        try:
            response = self.session.get(url, timeout=12,
                                        headers={"User-Agent":"Mozilla/5.0"})
            response.raise_for_status()
            candidates = re.findall(r'https?://[^"\s<>\\]+', response.text)
            clean = []
            for candidate in candidates:
                candidate = candidate.replace("&amp;", "&").split("&")[0]
                if _host_allowed(candidate, spec["official_hosts"]):
                    clean.append(candidate)
            for result_url in list(dict.fromkeys(clean))[:4]:
                try:
                    html = self._fetch(result_url, spec["official_hosts"])
                    snippet = _extract_relevant(html, terms)
                    if len(snippet) >= 50:
                        print("TOOL_SYSTEM_RESEARCH=OFFICIAL_SEARCH_RESULT_READ",flush=True)
                        return Guide(result_url, snippet, "OFFICIAL_SEARCH_RESULT")
                except (requests.RequestException, ValueError, TypeError):
                    continue
        except (requests.RequestException, ValueError):
            pass
        if not prefer_search:
            cached = VERIFIED_CACHE.get(provider)
            if cached:
                print("TOOL_SYSTEM_RESEARCH=SOURCE_ATTRIBUTED_OFFLINE_CACHE",flush=True)
                return Guide(cached[0],cached[1],"OFFICIAL_CACHED_2026_10_19")
        print("TOOL_SYSTEM_RESEARCH=UNAVAILABLE_NO_INVENTED_GUIDE",flush=True)
        raise RuntimeError("OFFICIAL_TOOL_GUIDANCE_UNAVAILABLE")
