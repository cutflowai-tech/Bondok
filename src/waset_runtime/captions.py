"""Caption drafting (WF1 "Write Caption" with the OpenRouter model): a draft is only ever a proposal to the owner;
the handler validates it and asks for approval (op_caption_draft)."""
from __future__ import annotations

import re

from .http import Http, HttpError

MODEL = 'openai/gpt-5.6-sol'
SYSTEM = (
    "You write Instagram captions for a premium video editing studio that edits real estate and branding videos for "
    "its clients. The studio posts the finished edits on its OWN page as portfolio work.\n\nWrite in English, in the "
    "studio's voice. Tone: hype, confident, punchy. Never corporate, never like a property listing.\n\nHard rules:\n"
    "- NEVER mention the client, agent, agency, brokerage, company, team members, people's names, street addresses, "
    "cities or neighborhoods. Do not use any of those names as hashtags either.\n- Do not invent facts. Only use the "
    "story, theme and style that appear in the brief.\n- Do not quote the script word for word.\n- No links, no "
    "@mentions.\n\nFormat (follow exactly):\nLine 1: ONE single line made of a short hook drawn from the video's own "
    "story, vibe or style (3 to 6 words), then a comma, then a short call to action inviting people to DM us for "
    "their edits (4 to 7 words), then exactly ONE energetic emoji at the end. No other sentences.\nExample of the "
    "shape: Sunlight sets the pace, DM us for edits that hit. 🔥\nVary the hook, the CTA wording and the emoji from "
    "video to video (for example 🔥⚡️🎬🚀💥✨).\nLine 2: empty.\nLine 3: 12 to 15 hashtags on one line: mix broad real "
    "estate video tags, video editing tags, reels/social tags and a few niche tags that fit this specific video's "
    "style or technique. No duplicates, no near-duplicates that only differ by case.\n\nOutput ONLY the caption, "
    "nothing else.")


def pick_brief(project: dict, fallback_title: str) -> dict:
    """Pick Client Brief: the earliest substantial update (≥60 characters without links) of the source project."""
    def strip(t):
        return re.sub(r'https?://\S+', '', t).strip()
    ups = [{'text': (u.get('text_body') or '').strip(), 'at': u.get('created_at') or ''}
           for u in (project or {}).get('updates') or []]
    ups = sorted([u for u in ups if len(strip(u['text'])) >= 60], key=lambda u: u['at'])
    brief = strip(ups[0]['text']).replace('{', '(').replace('}', ')')[:6000] if ups else ''
    return {'title': (project or {}).get('name') or fallback_title, 'brief': brief}


def clean(text: str) -> str:
    t = re.sub(r'^```[a-z]*\s*', '', str(text or ''), flags=re.I)
    t = re.sub(r'```\s*$', '', t)
    return re.sub(r'^caption:\s*', '', t, flags=re.I).strip()


class CaptionWriter:
    def __init__(self, key, http: Http | None = None, model=MODEL):
        self.key, self.model, self.http = key, model, http or Http(timeout=90)

    def __call__(self, title, brief) -> tuple[str | None, str | None]:
        """(text, error)."""
        try:
            r = self.http.request('POST', 'https://openrouter.ai/api/v1/chat/completions',
                                  headers={'Authorization': 'Bearer ' + self.key, 'X-Title': 'Waset Bondok captions'},
                                  json_body={'model': self.model, 'max_tokens': 600, 'temperature': 0.8,
                                             'messages': [{'role': 'system', 'content': SYSTEM},
                                                          {'role': 'user', 'content': f'Video title: {title}\n\n'
                                                           f'Client brief (script, notes and style):\n{brief}'}]})
        except HttpError as e:
            return None, str(e)[:300]
        try:
            t = clean(r['choices'][0]['message']['content'])
        except (KeyError, IndexError, TypeError):
            return None, 'empty model output'
        return (t, None) if t else (None, 'empty model output')
