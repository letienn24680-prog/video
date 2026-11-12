"""AI & local studio thumbnail generator service."""

from __future__ import annotations

import html
import shutil
import textwrap
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from pydantic import BaseModel

from backend.core.config import Settings, get_settings
from backend.core.logging import get_logger
from backend.models.enums import AspectRatio
from backend.models.project import ProjectResponse
from backend.models.scene import SceneResponse
from backend.providers.base.storage import BaseStorageProvider
from backend.providers.factory import ProviderFactory
from backend.services.media.ass_karaoke import RESOLUTION_MAP
from backend.services.media.ffmpeg_engine import FFmpegEngine

logger = get_logger(__name__)


class ThumbnailResult(BaseModel):
    """Result payload returned by ``ThumbnailService.generate_thumbnail``."""

    url: str
    local_path: str | None = None
    source: str


def _file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    raw_path = url2pathname(unquote(parsed.path))
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        raw_path = f"//{parsed.netloc}{raw_path}"
    return Path(raw_path)


def _resolve_local_video_path(raw_url: str | None, settings: Settings) -> Path | None:
    cleaned = (raw_url or "").strip()
    if not cleaned:
        return None

    if cleaned.startswith("file://"):
        candidate = _file_uri_to_path(cleaned)
        return candidate if candidate.is_file() else None

    if cleaned.startswith("/local-media/"):
        rel = cleaned[len("/local-media/") :].lstrip("/")
        if not rel or ".." in Path(rel).parts:
            return None

        temp_root = settings.temp_path.resolve()
        candidate = (temp_root / rel).resolve()
        try:
            candidate.relative_to(temp_root)
            if candidate.is_file():
                return candidate
        except ValueError:
            pass

        if rel.startswith("storage/"):
            storage_rel = rel[len("storage/") :]
            storage_root = (settings.temp_path.parent / "local_storage").resolve()
            storage_candidate = (storage_root / storage_rel).resolve()
            try:
                storage_candidate.relative_to(storage_root)
                if storage_candidate.is_file():
                    return storage_candidate
            except ValueError:
                pass
        return None

    if not cleaned.startswith(("http://", "https://")):
        direct = Path(cleaned)
        return direct if direct.is_file() else None

    return None


def _render_studio_svg(
    project: ProjectResponse,
    headline: str,
    subline: str,
) -> str:
    """Build a crisp high-contrast editorial studio SVG thumbnail."""
    width, height = RESOLUTION_MAP.get(project.aspect_ratio, (1080, 1920))
    is_landscape = width > height
    pad_x = int(width * 0.08)
    title_size = 74 if is_landscape else (66 if project.aspect_ratio == AspectRatio.SQUARE_1_1 else 78)
    wrap_chars = 26 if is_landscape else 18

    wrapped_lines = textwrap.wrap(headline.strip() or "AI Video Studio", width=wrap_chars)[:3]
    if not wrapped_lines:
        wrapped_lines = ["AI Video Studio"]

    sub_wrapped = textwrap.wrap(subline.strip(), width=42 if is_landscape else 28)[:2]

    start_y = int(height * (0.40 if is_landscape else 0.42))
    line_step = int(title_size * 1.24)

    title_tspans = "\n".join(
        f'    <text x="{pad_x}" y="{start_y + idx * line_step}" '
        f'fill="#F8FAFC" font-family="Inter, Segoe UI, Arial, sans-serif" '
        f'font-size="{title_size}" font-weight="800" letter-spacing="-0.02em">'
        f"{html.escape(line)}</text>"
        for idx, line in enumerate(wrapped_lines)
    )

    sub_start_y = start_y + len(wrapped_lines) * line_step + 34
    sub_size = 32 if is_landscape else 30
    sub_tspans = "\n".join(
        f'    <text x="{pad_x}" y="{sub_start_y + idx * int(sub_size * 1.35)}" '
        f'fill="#CBD5E1" font-family="Inter, Segoe UI, Arial, sans-serif" '
        f'font-size="{sub_size}" font-weight="500">'
        f"{html.escape(line)}</text>"
        for idx, line in enumerate(sub_wrapped)
    )

    mode_label = html.escape(project.mode.value.upper())
    ratio_label = html.escape(project.aspect_ratio.value)
    badge_y = int(height * 0.14)
    footer_y = int(height * 0.88)

    bar_count = 14
    bar_width = max(12, int((width - pad_x * 2) / (bar_count * 1.7)))
    bar_gap = int(bar_width * 0.65)
    bars_svg = []
    for i in range(bar_count):
        bar_h = 26 + ((i * 37 + 19) % 64)
        bx = pad_x + i * (bar_width + bar_gap)
        by = footer_y - bar_h - 30
        color = "#8B5CF6" if i % 2 == 0 else "#06B6D4"
        bars_svg.append(
            f'    <rect x="{bx}" y="{by}" width="{bar_width}" height="{bar_h}" '
            f'rx="{bar_width // 2}" fill="{color}" opacity="0.65"/>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        "  <defs>\n"
        '    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">\n'
        '      <stop offset="0%" stop-color="#0B0D12"/>\n'
        '      <stop offset="55%" stop-color="#13192B"/>\n'
        '      <stop offset="100%" stop-color="#191F2E"/>\n'
        "    </linearGradient>\n"
        '    <radialGradient id="violetGlow" cx="20%" cy="20%" r="55%">\n'
        '      <stop offset="0%" stop-color="#8B5CF6" stop-opacity="0.38"/>\n'
        '      <stop offset="100%" stop-color="#8B5CF6" stop-opacity="0"/>\n'
        "    </radialGradient>\n"
        '    <radialGradient id="cyanGlow" cx="85%" cy="80%" r="50%">\n'
        '      <stop offset="0%" stop-color="#06B6D4" stop-opacity="0.30"/>\n'
        '      <stop offset="100%" stop-color="#06B6D4" stop-opacity="0"/>\n'
        "    </radialGradient>\n"
        '    <linearGradient id="accentBar" x1="0%" y1="0%" x2="100%" y2="0%">\n'
        '      <stop offset="0%" stop-color="#8B5CF6"/>\n'
        '      <stop offset="100%" stop-color="#06B6D4"/>\n'
        "    </linearGradient>\n"
        "  </defs>\n"
        f'  <rect width="{width}" height="{height}" fill="url(#bgGrad)"/>\n'
        f'  <rect width="{width}" height="{height}" fill="url(#violetGlow)"/>\n'
        f'  <rect width="{width}" height="{height}" fill="url(#cyanGlow)"/>\n'
        f'  <rect x="28" y="28" width="{width - 56}" height="{height - 56}" rx="32" '
        'fill="none" stroke="#262E42" stroke-width="3"/>\n'
        f'  <rect x="{pad_x}" y="{badge_y}" width="320" height="54" rx="27" '
        'fill="#191F2E" stroke="#8B5CF6" stroke-width="2"/>\n'
        f'  <text x="{pad_x + 160}" y="{badge_y + 35}" text-anchor="middle" '
        'fill="#E2E8F0" font-family="Inter, Segoe UI, Arial, sans-serif" '
        f'font-size="22" font-weight="700" letter-spacing="0.08em">{mode_label} • {ratio_label}</text>\n'
        f'  <rect x="{pad_x}" y="{start_y - title_size - 26}" width="140" height="10" rx="5" '
        'fill="url(#accentBar)"/>\n'
        f"{title_tspans}\n"
        f"{sub_tspans}\n"
        + "\n".join(bars_svg)
        + "\n"
        f'  <text x="{pad_x}" y="{footer_y + 24}" fill="#94A3B8" '
        'font-family="Inter, Segoe UI, Arial, sans-serif" font-size="24" font-weight="600">'
        "AI VIDEO STUDIO • THUMBNAIL</text>\n"
        "</svg>\n"
    )


class ThumbnailService:
    """Generates video thumbnails via Flux/Pexels, FFmpeg frame extraction, or studio SVG fallback."""

    async def _maybe_upload(
        self,
        local_file: Path,
        project: ProjectResponse,
        settings: Settings,
        storage: BaseStorageProvider | None,
        content_type: str,
        default_url: str,
    ) -> str:
        is_sandbox = bool(getattr(settings, "LOCAL_SANDBOX_MODE", False))
        if storage is None or is_sandbox or settings.STORAGE_PROVIDER == "local":
            return default_url
        try:
            remote_key = f"{project.user_id}/{project.id}/{local_file.name}"
            stored = await storage.upload_file(local_file, remote_key, content_type=content_type)
            return stored.signed_url or stored.public_url or default_url
        except Exception as exc:
            logger.warning("Thumbnail storage upload failed (%s); falling back to %s", exc, default_url)
            return default_url

    async def generate_thumbnail(
        self,
        project: ProjectResponse,
        scenes: list[SceneResponse],
        prompt: str | None = None,
        settings: Settings | None = None,
        storage: BaseStorageProvider | None = None,
    ) -> ThumbnailResult:
        """Generate or extract a cover thumbnail for ``project`` and return its URL and local path."""
        cfg = settings or get_settings()
        thumbs_dir = cfg.temp_path / "thumbnails"
        thumbs_dir.mkdir(parents=True, exist_ok=True)

        ordered = sorted(scenes, key=lambda s: (s.scene_index, s.start_ms))
        first_scene = ordered[0] if ordered else None

        mk = (project.pipeline_config or {}).get("marketing_kit")
        mk_prompt = mk.get("thumbnail_prompt") if isinstance(mk, dict) else None

        effective_prompt = (
            (prompt or "").strip()
            or (str(mk_prompt).strip() if mk_prompt else "")
            or ((first_scene.image_prompt or "").strip() if first_scene else "")
            or project.title.strip()
        )
        stock_query = (
            ((first_scene.search_keywords or "").strip() if first_scene else "")
            or project.title.strip()
            or "cinematic abstract"
        )

        # 1. Try Flux or Pexels if API key is configured
        visual_override: str | None = None
        if (cfg.FLUX_API_KEY or "").strip():
            visual_override = "flux"
        elif (cfg.PEXELS_API_KEY or "").strip():
            visual_override = "pexels"

        if visual_override is not None:
            try:
                visual_provider = ProviderFactory.get_visual(override=visual_override, settings=cfg)
                asset = await visual_provider.fetch_visual(
                    query=stock_query,
                    prompt=effective_prompt,
                    aspect_ratio=project.aspect_ratio,
                    media_type="image",
                    output_dir=thumbs_dir,
                )
                if asset.local_path is not None and Path(asset.local_path).is_file():
                    src_img = Path(asset.local_path)
                    ext = src_img.suffix.lower() or ".png"
                    dest_img = thumbs_dir / f"{project.id}{ext}"
                    if src_img.resolve() != dest_img.resolve():
                        shutil.copy2(src_img, dest_img)
                    ctype = "image/png" if ext == ".png" else "image/jpeg"
                    url = await self._maybe_upload(
                        dest_img,
                        project,
                        cfg,
                        storage,
                        ctype,
                        f"/local-media/thumbnails/{dest_img.name}",
                    )
                    return ThumbnailResult(
                        url=url,
                        local_path=str(dest_img),
                        source=visual_override,
                    )
                if asset.url:
                    return ThumbnailResult(url=asset.url, local_path=None, source=visual_override)
            except Exception as exc:
                logger.warning(
                    "Visual thumbnail provider '%s' failed for project=%s (%s); trying fallback",
                    visual_override,
                    project.id,
                    exc,
                )

        # 2. Try extracting frame from local output_video_url via FFmpegEngine
        local_video = _resolve_local_video_path(project.output_video_url, cfg)
        if local_video is not None:
            jpg_dest = thumbs_dir / f"{project.id}.jpg"
            ffmpeg = FFmpegEngine(settings=cfg)
            for seek_ts in ("00:00:01", "00:00:00"):
                try:
                    await ffmpeg._exec(
                        [
                            ffmpeg.ffmpeg_bin,
                            "-y",
                            "-ss",
                            seek_ts,
                            "-i",
                            str(local_video),
                            "-frames:v",
                            "1",
                            "-q:v",
                            "2",
                            str(jpg_dest),
                        ],
                        f"extract thumbnail {project.id}",
                    )
                    if jpg_dest.is_file() and jpg_dest.stat().st_size > 0:
                        url = await self._maybe_upload(
                            jpg_dest,
                            project,
                            cfg,
                            storage,
                            "image/jpeg",
                            f"/local-media/thumbnails/{jpg_dest.name}",
                        )
                        return ThumbnailResult(
                            url=url,
                            local_path=str(jpg_dest),
                            source="video_frame",
                        )
                except Exception as exc:
                    logger.warning("FFmpeg frame extract (%s) failed for %s: %s", seek_ts, project.id, exc)

        # 3. Clean high-contrast studio SVG fallback
        subline = ""
        if first_scene is not None:
            subline = (
                (first_scene.edited_text or "").strip()
                or (first_scene.narration_text or "").strip()
                or (first_scene.original_text or "").strip()
            )
        if not subline and prompt:
            subline = prompt.strip()

        svg_text = _render_studio_svg(
            project=project,
            headline=project.title,
            subline=subline,
        )
        svg_path = thumbs_dir / f"{project.id}.svg"
        svg_path.write_text(svg_text, encoding="utf-8")

        final_url = await self._maybe_upload(
            svg_path,
            project,
            cfg,
            storage,
            "image/svg+xml",
            f"/local-media/thumbnails/{svg_path.name}",
        )
        return ThumbnailResult(
            url=final_url,
            local_path=str(svg_path),
            source="studio_svg",
        )
