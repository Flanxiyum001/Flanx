"""Multi-Modal Input Handling for Max.

Supports:
- Voice commands: "max, analyze this screen recording..."
- Image analysis: Screenshot of UI bug → identifies issue and suggests code fix
- File drag-and-drop: "Fix this broken component" with attached file
- Screen recordings: Video analysis for bug reproduction
"""

from __future__ import annotations

import base64
import io
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord
from PIL import Image

from model_router import ModelRouter, ModelError
from free_sandbox import run_code


MULTIMODAL_DIR = os.getenv("MAX_MULTIMODAL_DIR") or os.path.join(
    os.path.dirname(__file__), ".max_state", "multimodal"
)
Path(MULTIMODAL_DIR).mkdir(parents=True, exist_ok=True)

# Supported file types
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mov', '.mkv', '.avi'}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.m4a', '.flac'}
CODE_EXTENSIONS = {'.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.rs', '.rb', '.java', '.cpp', '.c', '.cs', '.php', '.sh', '.json', '.yaml', '.yml', '.toml', '.md'}

MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB
MAX_FILE_SIZE = 10 * 1024 * 1024   # 10MB


@dataclass
class MultimodalInput:
    input_type: str  # 'image', 'video', 'audio', 'file', 'text'
    content: str | bytes
    filename: str
    mime_type: str
    size: int
    user_id: int
    timestamp: datetime
    metadata: dict = None


@dataclass
class AnalysisResult:
    input_type: str
    summary: str
    details: str
    suggested_actions: list[str]
    code_fixes: list[dict]  # [{file_path, issue, fix, confidence}]
    confidence: float


class MultimodalProcessor:
    """Processes multi-modal inputs and generates analysis."""
    
    def __init__(self):
        self.router = ModelRouter()
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
    
    def detect_input_type(self, attachment: discord.Attachment) -> str:
        """Detect input type from Discord attachment."""
        ext = Path(attachment.filename).suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            return 'image'
        elif ext in VIDEO_EXTENSIONS:
            return 'video'
        elif ext in AUDIO_EXTENSIONS:
            return 'audio'
        elif ext in CODE_EXTENSIONS or attachment.filename.endswith(('.txt', '.log', '.xml', '.html', '.css')):
            return 'file'
        return 'unknown'
    
    async def process_attachment(self, attachment: discord.Attachment, user_id: int) -> MultimodalInput:
        """Download and process a Discord attachment."""
        input_type = self.detect_input_type(attachment)
        limit = MAX_IMAGE_SIZE if input_type == "image" else MAX_FILE_SIZE
        if attachment.size > limit:
            raise ValueError(f"Attachment exceeds the allowed size limit ({limit} bytes)")
        
        content = await attachment.read()
        # Trust the bytes received, not only Discord's advertised size.
        if len(content) > limit:
            raise ValueError(f"Attachment exceeds the allowed size limit ({limit} bytes)")
        
        return MultimodalInput(
            input_type=input_type,
            content=content,
            filename=attachment.filename,
            mime_type=attachment.content_type or 'application/octet-stream',
            size=len(content),
            user_id=user_id,
            timestamp=datetime.now(timezone.utc),
            metadata={'url': attachment.url, 'width': getattr(attachment, 'width', None), 'height': getattr(attachment, 'height', None)}
        )
    
    async def analyze_image(self, image_data: bytes, filename: str, context: str = "") -> AnalysisResult:
        """Analyze an image (screenshot, UI mockup, error screen, etc.)."""
        # Save image temporarily for analysis
        with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as f:
            f.write(image_data)
            temp_path = f.name
        
        try:
            # Open with PIL to get info and potentially resize
            img = Image.open(temp_path)
            width, height = img.size
            mode = img.mode
            
            # Convert to base64 for model input
            buffered = io.BytesIO()
            # Resize if too large (max 1024px on longest side for analysis)
            if max(width, height) > 1024:
                img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            # Normalize to PNG so the data URL matches the actual encoded bytes.
            img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
            img.save(buffered, format="PNG", optimize=True)
            img_b64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
            
            # Analyze with vision-capable model
            prompt = f"""Analyze this image in the context of software development.

Context from user: {context}

What do you see? Look for:
1. UI bugs, layout issues, visual regressions
2. Error messages, stack traces, console output
3. Code snippets visible in screenshots
4. Architecture diagrams, flowcharts
5. Database schemas, API docs
6. Anything that suggests a code issue or improvement

Provide:
- Summary of what the image shows
- Specific issues identified (if any)
- Suggested code fixes with file paths where possible
- Confidence level (0.0-1.0)"""
            
            try:
                reply = await self.router.generate(
                    messages=[
                        {"role": "system", "content": "You are Max, a senior engineer analyzing visual content for code issues. Be specific and actionable."},
                        {"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
                        ]}
                    ],
                    task="review",
                    # User screenshots can contain private code, tokens, or personal data.
                    # Route through the source-data policy instead of implicitly treating them as public.
                    data_class="source",
                    max_tokens=1500,
                )
                
                analysis_text = reply.text
                
                # Parse the analysis for structured data
                suggested_actions = []
                code_fixes = []
                
                # Extract suggested actions (simple heuristic)
                for line in analysis_text.split('\n'):
                    line = line.strip()
                    if line.startswith(('- ', '* ', '• ', '1.', '2.', '3.')):
                        action = line.lstrip('- *•0123456789. ')
                        if action:
                            suggested_actions.append(action)
                
                return AnalysisResult(
                    input_type='image',
                    summary=f"Image analysis: {width}x{height} {mode} image",
                    details=analysis_text,
                    suggested_actions=suggested_actions[:5],
                    code_fixes=code_fixes,
                    confidence=0.75,
                )
            
            except ModelError as e:
                return AnalysisResult(
                    input_type='image',
                    summary=f"Image analysis failed: {e}",
                    details="",
                    suggested_actions=[],
                    code_fixes=[],
                    confidence=0.0,
                )
        
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    
    async def analyze_code_file(self, file_data: bytes, filename: str, context: str = "") -> AnalysisResult:
        """Analyze a code file for issues, improvements, and patterns."""
        try:
            code = file_data.decode('utf-8', errors='replace')
        except UnicodeDecodeError:
            return AnalysisResult(
                input_type='file',
                summary=f"Cannot decode {filename} as text",
                details="",
                suggested_actions=[],
                code_fixes=[],
                confidence=0.0,
            )
        
        language = self._detect_language(filename)
        
        prompt = f"""Analyze this {language} code file for issues and improvements.

File: {filename}
Context: {context}

Code:
```{language}
{code[:8000]}
```

Provide:
1. Summary of what this code does
2. Issues found: bugs, security vulnerabilities, performance problems, style violations
3. Specific suggested fixes with line numbers where possible
4. Missing tests or edge cases
5. Refactoring opportunities
6. Confidence level (0.0-1.0)"""
        
        try:
            reply = await self.router.generate(
                messages=[
                    {"role": "system", "content": "You are Max, a senior engineer doing code review. Be thorough but concise. Focus on actionable issues."},
                    {"role": "user", "content": prompt},
                ],
                task="review",
                data_class="source",
                max_tokens=2000,
            )
            
            analysis_text = reply.text
            suggested_actions = []
            code_fixes = []
            
            # Extract fixes from analysis
            for line in analysis_text.split('\n'):
                line = line.strip()
                if any(kw in line.lower() for kw in ['fix', 'change', 'add', 'remove', 'update', 'refactor']):
                    if line.startswith(('- ', '* ', '• ', '1.', '2.', '3.')):
                        action = line.lstrip('- *•0123456789. ')
                        if action:
                            suggested_actions.append(action)
            
            return AnalysisResult(
                input_type='file',
                summary=f"Code analysis: {filename} ({len(code)} chars, {language})",
                details=analysis_text,
                suggested_actions=suggested_actions[:7],
                code_fixes=code_fixes,
                confidence=0.8,
            )
        
        except ModelError as e:
            return AnalysisResult(
                input_type='file',
                summary=f"Code analysis failed: {e}",
                details="",
                suggested_actions=[],
                code_fixes=[],
                confidence=0.0,
            )
    
    async def analyze_video(self, video_data: bytes, filename: str, context: str = "") -> AnalysisResult:
        """Analyze a video (screen recording of bug reproduction)."""
        # For video, we'd ideally extract key frames and analyze them
        # This is a simplified version - in production you'd use video processing
        return AnalysisResult(
            input_type='video',
            summary=f"Video received: {filename} ({len(video_data)} bytes)",
            details="Video analysis requires frame extraction. Key frames would be analyzed for UI changes, error messages, and user actions.",
            suggested_actions=[
                "Extract key frames from the recording",
                "Analyze each frame for UI changes and errors",
                "Reconstruct user flow from the recording",
                "Identify the exact moment the bug occurs",
            ],
            code_fixes=[],
            confidence=0.5,
        )
    
    async def analyze_audio(self, audio_data: bytes, filename: str, context: str = "") -> AnalysisResult:
        """Analyze audio (voice command)."""
        # In production, you'd use speech-to-text (Whisper, etc.)
        return AnalysisResult(
            input_type='audio',
            summary=f"Audio received: {filename} ({len(audio_data)} bytes)",
            details="Voice command processing requires speech-to-text transcription. The transcribed text would then be processed as a regular command.",
            suggested_actions=[
                "Transcribe audio to text",
                "Process transcribed text as a Max command",
                "Execute the requested action",
            ],
            code_fixes=[],
            confidence=0.4,
        )
    
    def _detect_language(self, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        return {
            '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
            '.jsx': 'javascript', '.tsx': 'typescript', '.go': 'go',
            '.rs': 'rust', '.rb': 'ruby', '.java': 'java', '.cpp': 'cpp',
            '.c': 'c', '.cs': 'csharp', '.php': 'php', '.sh': 'bash',
            '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml',
            '.toml': 'toml', '.md': 'markdown',
        }.get(ext, 'text')
    
    async def process_multimodal_input(self, input_data: MultimodalInput, context: str = "") -> AnalysisResult:
        """Route input to appropriate analyzer based on type."""
        if input_data.input_type == 'image':
            return await self.analyze_image(input_data.content, input_data.filename, context)
        elif input_data.input_type == 'file':
            return await self.analyze_code_file(input_data.content, input_data.filename, context)
        elif input_data.input_type == 'video':
            return await self.analyze_video(input_data.content, input_data.filename, context)
        elif input_data.input_type == 'audio':
            return await self.analyze_audio(input_data.content, input_data.filename, context)
        else:
            return AnalysisResult(
                input_type=input_data.input_type,
                summary=f"Unsupported input type: {input_data.input_type}",
                details="",
                suggested_actions=[],
                code_fixes=[],
                confidence=0.0,
            )


# Global instance
multimodal_processor = MultimodalProcessor()


async def handle_discord_attachments(message: discord.Message, context: str = "") -> list[AnalysisResult]:
    """Process all attachments in a Discord message."""
    results = []
    for attachment in message.attachments:
        try:
            input_data = await multimodal_processor.process_attachment(attachment, message.author.id)
            result = await multimodal_processor.process_multimodal_input(input_data, context)
            results.append(result)
        except Exception:
            # Avoid echoing untrusted exception text or remote service details into Discord.
            results.append(AnalysisResult(
                input_type='error',
                summary=f"Failed to process {attachment.filename}. Check the file type, size, and model configuration.",
                details="",
                suggested_actions=[],
                code_fixes=[],
                confidence=0.0,
            ))
    return results


def format_analysis_result(result: AnalysisResult) -> str:
    """Format analysis result for Discord display."""
    lines = [
        f"🔍 **{result.input_type.title()} Analysis**",
        f"**Summary:** {result.summary}",
        f"**Confidence:** {result.confidence:.0%}",
    ]
    
    if result.details:
        lines.append(f"\n**Details:**\n{result.details[:1500]}")
    
    if result.suggested_actions:
        lines.append("\n**Suggested Actions:**")
        for i, action in enumerate(result.suggested_actions, 1):
            lines.append(f"  {i}. {action}")
    
    if result.code_fixes:
        lines.append("\n**Code Fixes:**")
        for fix in result.code_fixes:
            lines.append(f"  • {fix.get('file_path', 'unknown')}: {fix.get('issue', '')} → {fix.get('fix', '')}")
    
    return "\n".join(lines)