"""Render a committed digest to a local HTML file without touching SMTP."""

import argparse
from pathlib import Path

from app.daily_ai.email_digest import METADATA_RE, render_bodies, split_digest_body
from app.daily_ai.global_digest import model_display_name, prompt_display_version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--analysis-model", required=True)
    parser.add_argument("--prompt-version", required=True)
    args = parser.parse_args()

    body = split_digest_body(args.input.read_text(encoding="utf-8"))
    if not METADATA_RE.search(body):
        title, separator, remainder = body.partition("\n")
        metadata = (
            f"分析元数据：{model_display_name(args.analysis_model)} · "
            f"{prompt_display_version(args.prompt_version)}"
        )
        body = f"{title}\n\n{metadata}\n{remainder.lstrip()}" if separator else f"{title}\n\n{metadata}"
    _, html_body = render_bodies(args.input.stem, body)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html_body, encoding="utf-8")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
