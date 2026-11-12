"""AI Viral Hook, SEO Title, Description, Hashtag & Thumbnail Prompt generator."""

from __future__ import annotations

import re
from typing import Any, Sequence

from pydantic import BaseModel, Field

from backend.core.config import Settings, get_settings
from backend.core.logging import get_logger
from backend.models.project import ProjectResponse
from backend.models.scene import SceneResponse
from backend.providers.factory import ProviderFactory

logger = get_logger(__name__)

_WORD_RE = re.compile(r"[^\W_]{3,}", re.UNICODE)
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "that", "this", "with", "from", "your", "you", "are",
        "was", "were", "have", "has", "had", "not", "but", "what", "when", "where",
        "which", "their", "about", "into", "just", "more", "some", "very", "will",
        "cua", "nhung", "cac", "mot", "cho", "khi", "trong", "nay", "nhu", "voi",
        "của", "những", "các", "một", "cho", "khi", "trong", "này", "như", "với",
        "được", "không", "đang", "sẽ", "là", "và", "để", "nếu", "bạn", "tôi",
    }
)


class MarketingKit(BaseModel):
    """Generated viral hooks, SEO titles, social description, hashtags & thumbnail prompt."""

    hooks: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    description: str
    hashtags: list[str] = Field(default_factory=list)
    thumbnail_prompt: str
    generated_by: str = "heuristic"


class _MarketingKitLLMSchema(BaseModel):
    """Structured JSON output schema requested from Gemini."""

    hooks: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    thumbnail_prompt: str = ""


def _extract_scene_lines(scenes: Sequence[SceneResponse]) -> list[str]:
    ordered = sorted(scenes, key=lambda s: (s.scene_index, s.start_ms))
    lines: list[str] = []
    for scene in ordered:
        text = (
            (scene.edited_text or "").strip()
            or (scene.narration_text or "").strip()
            or (scene.original_text or "").strip()
        )
        if text:
            lines.append(text)
    return lines


def _extract_keywords(project: ProjectResponse, scenes: Sequence[SceneResponse], limit: int = 8) -> list[str]:
    counts: dict[str, int] = {}
    sources: list[str] = [project.title]
    for scene in scenes:
        if scene.search_keywords:
            sources.append(scene.search_keywords)
        text = (scene.edited_text or scene.narration_text or scene.original_text or "").strip()
        if text:
            sources.append(text)

    topic = (project.source_inputs or {}).get("topic")
    if isinstance(topic, str) and topic.strip():
        sources.append(topic.strip())

    for chunk in sources:
        for token in _WORD_RE.findall(chunk.lower()):
            if token in _STOPWORDS or token.isdigit():
                continue
            counts[token] = counts.get(token, 0) + 1

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ranked[:limit]]


def _normalize_hashtag(raw: str) -> str:
    cleaned = re.sub(r"[^\w]", "", raw.strip().lstrip("#"), flags=re.UNICODE)
    return f"#{cleaned}" if cleaned else ""


def _build_heuristic_kit(
    project: ProjectResponse,
    scenes: Sequence[SceneResponse],
) -> MarketingKit:
    """Offline deterministic fallback so Marketing Kit works cleanly in local/sandbox mode."""
    p_cfg: dict[str, Any] = project.pipeline_config or {}
    target_lang = str(p_cfg.get("target_lang") or "vi").strip().lower()
    tone = str(p_cfg.get("tone") or "viral").strip().lower()

    lines = _extract_scene_lines(scenes)
    lead_line = lines[0] if lines else project.title
    lead_snippet = lead_line[:110].rstrip(" .,:;") + ("..." if len(lead_line) > 110 else "")
    title_clean = project.title.strip() or "Video Mới"
    keywords = _extract_keywords(project, scenes, limit=8)

    first_visual = next(
        ((s.image_prompt or s.search_keywords or "").strip() for s in scenes if (s.image_prompt or s.search_keywords)),
        "",
    )

    is_vi = target_lang.startswith("vi")
    if is_vi:
        hooks = [
            f"Khoan đã! 90% mọi người chưa biết sự thật về {title_clean}!",
            f"Đừng lướt qua nếu bạn đang quan tâm đến {title_clean} — xem hết 3 giây này!",
            f"Sự thật bất ngờ đằng sau: {lead_snippet}",
            f"Bạn sẽ ước mình biết điều này sớm hơn về {title_clean}.",
        ]
        titles = [
            f"{title_clean} — Sự Thật Ít Người Biết (Giải Mã Nhanh)",
            f"Bí Mật Đằng Sau {title_clean} | Tóm Tắt Trong 60 Giây",
            f"Tại Sao {title_clean} Đang Gây Sốt? Phân Tích Chi Tiết",
        ]
        summary_body = " ".join(lines[:3])[:260].strip() or title_clean
        description = (
            f"{summary_body}\n\n"
            f"Lưu video để xem lại khi cần và bình luận góc nhìn của bạn bên dưới nhé! "
            f"(Tông giọng: {tone})"
        )
        base_tags = [
            "#aivideostudio",
            "#xuhuong",
            "#shorts",
            "#reels",
            "#tiktokvietnam",
            "#kienthuc",
            "#viral",
            "#trending",
            "#review",
            "#storytelling",
        ]
    else:
        hooks = [
            f"Stop scrolling — almost nobody talks about this side of {title_clean}!",
            f"Wait until you hear the truth behind: {lead_snippet}",
            f"Here is why {title_clean} changes everything in the next 30 seconds.",
            f"You will wish you knew this about {title_clean} sooner.",
        ]
        titles = [
            f"{title_clean}: The Untold Story Explained Fast",
            f"Why Everyone Is Talking About {title_clean} Right Now",
            f"The Hidden Truth Behind {title_clean} (60s Breakdown)",
        ]
        summary_body = " ".join(lines[:3])[:260].strip() or title_clean
        description = (
            f"{summary_body}\n\n"
            f"Save this video for later and drop your thoughts in the comments! (Tone: {tone})"
        )
        base_tags = [
            "#aivideostudio",
            "#shorts",
            "#reels",
            "#tiktok",
            "#viral",
            "#trending",
            "#storytelling",
            "#insights",
            "#explained",
            "#creator",
        ]

    # Merge dynamic keyword hashtags up to 10-12 total
    merged_tags: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        tag = _normalize_hashtag(kw)
        if tag and tag.lower() not in seen and len(tag) > 2:
            seen.add(tag.lower())
            merged_tags.append(tag)
    for tag in base_tags:
        norm = _normalize_hashtag(tag)
        if norm and norm.lower() not in seen:
            seen.add(norm.lower())
            merged_tags.append(norm)
    hashtags = merged_tags[:10]

    visual_subject = first_visual or f"dramatic editorial scene representing {title_clean}"
    thumbnail_prompt = (
        f"High-contrast cinematic YouTube/TikTok thumbnail ({project.aspect_ratio.value}), "
        f"subject: {visual_subject}, bold rim lighting, vibrant violet and cyan accents, "
        f"shallow depth of field, ultra-sharp focal point, clean negative space for title text, no text"
    )

    return MarketingKit(
        hooks=hooks,
        titles=titles,
        description=description,
        hashtags=hashtags,
        thumbnail_prompt=thumbnail_prompt,
        generated_by="heuristic",
    )


class MarketingKitService:
    """Generates viral hooks, SEO titles, social copy, hashtags, and thumbnail prompts."""

    async def generate(
        self,
        project: ProjectResponse,
        scenes: list[SceneResponse],
        settings: Settings | None = None,
    ) -> MarketingKit:
        """Generate a ``MarketingKit`` via Gemini JSON output with offline heuristic fallback."""
        cfg = settings or get_settings()
        fallback = _build_heuristic_kit(project, scenes)

        p_cfg: dict[str, Any] = project.pipeline_config or {}
        target_lang = str(p_cfg.get("target_lang") or "vi").strip()
        tone = str(p_cfg.get("tone") or "viral").strip()
        script_lines = _extract_scene_lines(scenes)
        script_excerpt = "\n".join(f"- {line}" for line in script_lines[:20]) or project.title

        try:
            llm = ProviderFactory.get_llm(override=p_cfg.get("llm_provider"), settings=cfg)
            generate_structured = getattr(llm, "_generate_structured", None)
            if not callable(generate_structured):
                logger.info(
                    "LLM provider '%s' has no structured JSON helper; using heuristic MarketingKit",
                    getattr(llm, "provider_name", "unknown"),
                )
                return fallback

            system_instruction = (
                "You are an elite short-form video growth strategist & YouTube SEO specialist. "
                "Generate scroll-stopping 3-second hooks, clickable SEO titles, a concise social "
                "description, 8-12 targeted hashtags, and a vivid English AI thumbnail image prompt."
            )
            prompt = (
                f"Project title: {project.title}\n"
                f"Mode: {project.mode.value}\n"
                f"Aspect ratio: {project.aspect_ratio.value}\n"
                f"Target language for hooks, titles, description: {target_lang}\n"
                f"Tone: {tone}\n\n"
                "Requirements:\n"
                "- hooks: 3 to 5 punchy 3-second opening hooks in the target language.\n"
                "- titles: exactly 3 high-CTR SEO titles in the target language.\n"
                "- description: engaging 2-3 sentence social media caption with a clear CTA.\n"
                "- hashtags: 8 to 12 relevant hashtags starting with '#'.\n"
                "- thumbnail_prompt: detailed English visual prompt (no text overlays) for generating the cover thumbnail.\n\n"
                f"Video script / scenes:\n{script_excerpt}"
            )

            raw_result = await generate_structured(
                prompt=prompt,
                schema_cls=_MarketingKitLLMSchema,
                system_instruction=system_instruction,
            )
            parsed = _MarketingKitLLMSchema.model_validate(raw_result)

            hooks = [h.strip() for h in parsed.hooks if h and h.strip()][:5]
            for h in fallback.hooks:
                if len(hooks) >= 3:
                    break
                if h not in hooks:
                    hooks.append(h)

            titles = [t.strip() for t in parsed.titles if t and t.strip()][:3]
            for t in fallback.titles:
                if len(titles) >= 3:
                    break
                if t not in titles:
                    titles.append(t)

            hashtags: list[str] = []
            seen_tags: set[str] = set()
            for raw_tag in parsed.hashtags:
                norm = _normalize_hashtag(raw_tag)
                if norm and norm.lower() not in seen_tags:
                    seen_tags.add(norm.lower())
                    hashtags.append(norm)
            for fb_tag in fallback.hashtags:
                if len(hashtags) >= 8:
                    break
                if fb_tag.lower() not in seen_tags:
                    seen_tags.add(fb_tag.lower())
                    hashtags.append(fb_tag)
            hashtags = hashtags[:12]

            description = (parsed.description or "").strip() or fallback.description
            thumbnail_prompt = (parsed.thumbnail_prompt or "").strip() or fallback.thumbnail_prompt

            return MarketingKit(
                hooks=hooks,
                titles=titles,
                description=description,
                hashtags=hashtags,
                thumbnail_prompt=thumbnail_prompt,
                generated_by=getattr(llm, "provider_name", "gemini"),
            )
        except Exception as exc:
            logger.warning(
                "MarketingKit LLM generation unavailable for project=%s (%s); using heuristic fallback",
                project.id,
                exc,
            )
            return fallback
