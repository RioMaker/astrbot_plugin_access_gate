"""Render the awarded amount on bundled, pre-generated mascot sign templates."""

from __future__ import annotations

import io
import secrets
from pathlib import Path


class PokeCards:
    variants = ("happy", "wink", "pout")
    text_box = (0.17, 0.72, 0.83, 0.90)

    def __init__(self, directory=None):
        self.directory = (
            Path(directory) if directory else Path(__file__).parent / "assets" / "poke"
        )

    def render(self, points, *, variant=None):
        from PIL import Image, ImageDraw, ImageFont

        if type(points) is not int or not 1 <= points <= 10000:
            raise ValueError("奖励图片的积分必须为 1～10000 的整数")
        variant = secrets.choice(self.variants) if variant is None else variant
        if variant not in self.variants:
            raise ValueError("未知的奖励表情")
        with Image.open(self.directory / f"{variant}.png") as source:
            card = source.convert("RGB")
        card.thumbnail((768, 768), Image.Resampling.LANCZOS)
        width, height = card.size
        left, top, right, bottom = (
            self.text_box[0] * width,
            self.text_box[1] * height,
            self.text_box[2] * width,
            self.text_box[3] * height,
        )
        label = f"积分+{points}"
        draw = ImageDraw.Draw(card)
        font_path = self.directory / "fonts" / "RewardSans.otf"
        for size in range(round(width * 0.13), 15, -1):
            font = ImageFont.truetype(str(font_path), size)
            box = draw.textbbox((0, 0), label, font=font, stroke_width=1)
            if box[2] - box[0] <= right - left and box[3] - box[1] <= bottom - top:
                break
        else:
            raise ValueError("奖励文字无法放入举牌区域")
        x = (left + right - (box[2] - box[0])) / 2 - box[0]
        y = (top + bottom - (box[3] - box[1])) / 2 - box[1]
        draw.text(
            (x, y),
            label,
            font=font,
            fill="#6B412D",
            stroke_width=1,
            stroke_fill="#6B412D",
        )
        output = io.BytesIO()
        card.save(output, format="PNG", optimize=True)
        return output.getvalue()
