import argparse
import html
import json
import os
import re
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .config import DB_PATH, LOG_DIR, ROOT_DIR
from .db import connect, init_db


DOCS_DIR = ROOT_DIR / "docs"
TRIFECTA = "3連単"
PREDICTION_BET_TYPES = ["2車複", "2車単", "ワイド", "3連複", "3連単"]
DEFAULT_GITHUB_REPOSITORY = "smilebest2/Add_keirin_result_collector"
DEV_GITHUB_REPOSITORY = "smilebest2/Add_keirin_result_collector-dev"
PREDICTION_ANALYSIS_ROW_LIMIT = 1500
COMPONENT_ANALYSIS_ROW_LIMIT = 300


def workflow_url(workflow_name: str) -> str:
    default_repository = DEV_GITHUB_REPOSITORY if is_dev_environment() else DEFAULT_GITHUB_REPOSITORY
    repository = os.environ.get("GITHUB_REPOSITORY", default_repository)
    return f"https://github.com/{repository}/actions/workflows/{workflow_name}"


def h(value) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"winticket", "", text, flags=re.IGNORECASE)
    text = text.replace("ウィンチケット", "")
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = text.replace("・杯", "杯")
    return html.escape(text, quote=True)


def yen(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        if value.endswith("円"):
            return value
        value = re.sub(r"[^\d-]", "", value)
        if not value:
            return ""
    return f"{int(value):,}円"


def pct(value) -> str:
    if value is None:
        return ""
    return f"{float(value):.1f}%"


def number(value) -> str:
    if value is None:
        return ""
    return f"{int(value):,}"


def decimal(value, digits=2) -> str:
    if value is None:
        return ""
    return f"{float(value):.{digits}f}"


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rows(conn, sql: str, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(conn, sql: str, params=()):
    return conn.execute(sql, params).fetchone()[0]


def table(headers: list[str], data: list[dict], fields: list[str], empty="データがありません") -> str:
    if not data:
        return f'<div class="empty">{h(empty)}</div>'
    header_html = "".join(f"<th>{h(header)}</th>" for header in headers)
    body_html = ""
    for row in data:
        attrs = f' class="{h(row.get("_class"))}"' if row.get("_class") else ""
        for key, value in (row.get("_data") or {}).items():
            attrs += f' data-{h(key)}="{h(value)}"'
        body_html += f"<tr{attrs}>" + "".join(f"<td>{h(row.get(field))}</td>" for field in fields) + "</tr>"
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table>"


def rich_table(headers: list[str], data: list[dict], fields: list[str], empty="データがありません") -> str:
    if not data:
        return f'<div class="empty">{h(empty)}</div>'
    header_html = "".join(f"<th>{h(header)}</th>" for header in headers)
    body_html = ""
    for row in data:
        cells = []
        for field in fields:
            value = row.get(field, "")
            cells.append(str(value) if is_safe_inline_html(value) else h(value))
        attrs = f' class="{h(row.get("_class"))}"' if row.get("_class") else ""
        for key, value in (row.get("_data") or {}).items():
            attrs += f' data-{h(key)}="{h(value)}"'
        body_html += f"<tr{attrs}>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table>"


def is_safe_inline_html(value) -> bool:
    if not isinstance(value, str):
        return False
    lower = value.lower()
    return value.startswith("<a ") or value.startswith('<div class="prediction-pick') or value.startswith('<details class="compact-reason"') or value.startswith('<details class="compact-components"') or (
        value.startswith('<span class="') and value.endswith("</span>") and "<script" not in lower
    )


def pill(text: str, css_class: str = "") -> str:
    class_name = "pill" + (f" {css_class}" if css_class else "")
    return f'<span class="{class_name}">{h(text)}</span>'


def sample_class(count: int | None, threshold: int) -> str:
    return "sample-low" if (count or 0) < threshold else ""


def accordion_table(
    headers: list[str],
    data: list[dict],
    fields: list[str],
    visible_count: int = 10,
    rich: bool = False,
    empty="データがありません",
) -> str:
    renderer = rich_table if rich else table
    if not data:
        return renderer(headers, data, fields, empty)
    visible_rows = data[:visible_count]
    hidden_rows = data[visible_count:]
    html_body = renderer(headers, visible_rows, fields, empty)
    if hidden_rows:
        html_body += (
            f'<details class="ranking-more">'
            f'<summary>残り{len(hidden_rows)}件を表示</summary>'
            f'{renderer(headers, hidden_rows, fields, empty)}'
            f'</details>'
        )
    return html_body


def race_detail_href(race_id: str | None) -> str:
    if not race_id:
        return "races.html"
    compact_date = str(race_id).split("_", 1)[0]
    return f"race_detail.html?date={h(compact_date)}&race_id={h(race_id)}"


def race_detail_link(race_id: str | None, label: str = "詳細") -> str:
    return f'<a class="detail-link" href="{race_detail_href(race_id)}">{h(label)}</a>'


def section(title: str, html_body: str, intro: str = "") -> str:
    lead = f'<p class="section-lead">{h(intro)}</p>' if intro else ""
    return f"<section><h2>{h(title)}</h2>{lead}{html_body}</section>"


def is_dev_environment() -> bool:
    env = os.environ.get("SITE_ENV") or os.environ.get("APP_ENV") or ""
    repository = os.environ.get("GITHUB_REPOSITORY") or ""
    return env.lower() in {"dev", "development", "local"} or repository == DEV_GITHUB_REPOSITORY


def page(title: str, active: str, body: str) -> str:
    is_dev = is_dev_environment()
    title_prefix = "[DEV] " if is_dev else ""
    body_class = ' class="is-dev"' if is_dev else ""
    env_banner = '<div class="env-banner">DEV環境</div>' if is_dev else ""
    nav_items = [
        ("index.html", "TOP", "top"),
        ("venues.html", "会場分析", "venues"),
        ("car_numbers.html", "車番分析", "cars"),
        ("outcomes.html", "出目分析", "outcomes"),
        ("payouts.html", "配当分析", "payouts"),
        ("racers.html", "選手分析", "racers"),
        ("races.html", "レース一覧", "races"),
        ("quality.html", "データ品質", "quality"),
        ("custom.html", "独自ランキング", "custom"),
    ]
    nav_items.insert(6, ("predictions.html", "予想", "predictions"))
    nav_items.insert(7, ("prediction-results.html", "予想結果", "prediction-results"))
    nav_items.insert(8, ("lineup-features.html", "ライン解析", "lineup-features"))
    nav_items.insert(9, ("dice-bets.html", "サイコロ車券", "dice-bets"))
    nav = "".join(
        f'<a class="{"active" if key == active else ""}" href="{href}">{label}</a>'
        for href, label, key in nav_items
    )
    html = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title_prefix + title)} | 競輪統計</title>
  <style>
    :root {{
      --bg: #f5f7f9;
      --panel: #ffffff;
      --ink: #20242a;
      --muted: #697586;
      --line: #dbe1e8;
      --accent: #0f766e;
      --accent-2: #1d4ed8;
      --accent-3: #b45309;
      --soft: #e0f2ef;
      --soft-2: #e8eefc;
      --warn: #fef3c7;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
    }}
    header {{
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }}
    .env-banner {{
      position: sticky;
      top: 0;
      z-index: 20;
      background: #f59e0b;
      color: #111827;
      border-bottom: 1px solid #b45309;
      padding: 7px 12px;
      text-align: center;
      font-size: 14px;
      font-weight: 800;
      letter-spacing: 0;
    }}
    body.is-dev header {{
      border-top: 4px solid #f59e0b;
    }}
    .wrap {{
      max-width: 1240px;
      margin: 0 auto;
      padding: 18px 20px;
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 28px;
      letter-spacing: 0;
    }}
    nav {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    nav a {{
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--ink);
      padding: 7px 10px;
      text-decoration: none;
      font-size: 14px;
      background: #fbfcfd;
    }}
    nav a.active {{
      border-color: var(--accent);
      background: var(--soft);
      color: var(--accent);
      font-weight: 700;
    }}
    main .wrap {{
      display: grid;
      gap: 16px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(140px, 1fr));
      gap: 12px;
    }}
    .grid.two {{
      grid-template-columns: repeat(2, minmax(240px, 1fr));
    }}
    .card, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .card {{
      overflow: hidden;
      padding: 14px;
    }}
    section {{
      overflow-x: auto;
    }}
    .card span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
    }}
    .card strong {{
      display: block;
      margin-top: 3px;
      font-size: 22px;
    }}
    section h2 {{
      margin: 0;
      padding: 13px 15px;
      border-bottom: 1px solid var(--line);
      font-size: 17px;
      background: #fafbfc;
      letter-spacing: 0;
    }}
    .section-lead {{
      margin: 12px 15px 0;
      color: var(--muted);
      font-size: 13px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
      white-space: nowrap;
    }}
    th {{
      color: var(--muted);
      background: #fbfcfd;
      font-weight: 700;
    }}
    .empty {{
      padding: 22px;
      color: var(--muted);
      text-align: center;
    }}
    .chart {{
      display: grid;
      gap: 9px;
      padding: 14px 15px 16px;
    }}
    .bar-row {{
      display: grid;
      grid-template-columns: minmax(92px, 170px) minmax(160px, 1fr) 92px;
      gap: 10px;
      align-items: center;
      font-size: 13px;
    }}
    .bar-label {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .bar-track {{
      height: 12px;
      border-radius: 999px;
      background: #eef2f6;
      overflow: hidden;
    }}
    .bar-fill {{
      height: 100%;
      min-width: 2px;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
    }}
    .bar-value {{
      color: var(--muted);
      text-align: right;
      white-space: nowrap;
    }}
    .result-graph-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 16px;
    }}
    .line-chart {{
      padding: 14px 15px 16px;
    }}
    .line-chart svg {{
      display: block;
      width: 100%;
      height: auto;
      overflow: visible;
    }}
    .line-legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    .line-legend span {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    .line-legend i {{
      display: inline-block;
      width: 18px;
      height: 3px;
      border-radius: 999px;
    }}
    .dice-panel {{
      display: grid;
      gap: 18px;
    }}
    .dice-controls {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
      align-items: end;
    }}
    .dice-controls label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    .dice-controls select,
    .dice-controls input {{
      width: 100%;
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      background: #fff;
      color: var(--ink);
      font: inherit;
    }}
    .dice-button {{
      min-height: 42px;
      border: 0;
      border-radius: 8px;
      padding: 9px 16px;
      background: var(--accent);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
    }}
    .dice-button:hover {{
      background: #0b615b;
    }}
    .dice-summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 10px;
    }}
    .dice-summary div {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfd;
    }}
    .dice-summary span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
    }}
    .dice-summary strong {{
      display: block;
      margin-top: 4px;
      font-size: 22px;
    }}
    .dice-results {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
      gap: 8px;
    }}
    .dice-ticket {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 8px;
      background: #fff;
      text-align: center;
      font-weight: 800;
      font-variant-numeric: tabular-nums;
    }}
    .dice-note {{
      color: var(--muted);
      font-size: 13px;
    }}
    .heatmap {{
      padding: 0 0 2px;
      overflow-x: auto;
    }}
    .heatmap td {{
      min-width: 58px;
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .heat-0 {{ background: #f8fafc; }}
    .heat-1 {{ background: #e8f4f1; }}
    .heat-2 {{ background: #cbe9e2; }}
    .heat-3 {{ background: #94d6c8; }}
    .heat-4 {{ background: #4eb7a2; color: #062f29; font-weight: 700; }}
    .note {{
      padding: 12px 15px;
      color: var(--muted);
      background: #fbfcfd;
      border-top: 1px solid var(--line);
      font-size: 13px;
    }}
    .inline-note {{
      padding: 12px 15px;
      color: var(--muted);
      font-size: 13px;
      background: #fbfcfd;
      border-bottom: 1px solid var(--line);
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      padding: 14px 15px 16px;
    }}
    .operation-toggle {{
      width: 100%;
      border: 0;
      background: transparent;
      color: inherit;
      cursor: pointer;
      font: inherit;
      font-weight: 700;
      text-align: left;
      padding: 0;
    }}
    .operation-error {{
      margin: 12px 15px 0;
      color: #b91c1c;
      font-size: 13px;
      font-weight: 700;
    }}
    .operation-error:empty {{
      display: none;
    }}
    .action-button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      border-radius: 8px;
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #ffffff;
      padding: 8px 12px;
      text-decoration: none;
      font-size: 14px;
      font-weight: 700;
    }}
    .action-button.secondary {{
      border-color: #b91c1c;
      background: #b91c1c;
    }}
    .detail-link {{
      color: var(--accent-2);
      font-weight: 700;
      text-decoration: none;
    }}
    .detail-link:hover {{
      text-decoration: underline;
    }}
    .rank-note {{
      padding: 12px 15px;
      color: var(--muted);
      font-size: 13px;
      background: #fbfcfd;
      border-bottom: 1px solid var(--line);
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 3px 8px;
      background: var(--soft-2);
      color: var(--accent-2);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .pill.warn {{
      background: var(--warn);
      color: var(--accent-3);
    }}
    .pill.low {{
      background: #fee2e2;
      color: #991b1b;
    }}
    .pill.ok {{
      background: var(--soft);
      color: var(--accent);
    }}
    .prediction-pick {{
      display: grid;
      gap: 2px;
      min-width: 118px;
    }}
    .prediction-pick strong {{
      font-size: 14px;
      white-space: nowrap;
    }}
    .prediction-pick span {{
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .prediction-pick.empty {{
      color: var(--muted);
      min-width: 88px;
    }}
    .prediction-type-grid {{
      display: grid;
      grid-template-columns: repeat(5, minmax(160px, 1fr));
      gap: 10px;
      padding: 14px 15px 16px;
    }}
    .prediction-type-note {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfd;
    }}
    .prediction-type-note strong {{
      display: block;
      margin-bottom: 4px;
    }}
    .prediction-type-note span {{
      color: var(--muted);
      font-size: 13px;
    }}
    .decision-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 10px;
      padding: 14px 15px 0;
    }}
    .decision-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfd;
    }}
    .decision-card span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .decision-card strong {{
      display: block;
      margin-top: 4px;
      font-size: 22px;
      line-height: 1.15;
    }}
    .decision-card small {{
      display: block;
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
    }}
    #daily-recommendations td:nth-child(8) {{
      min-width: 260px;
      max-width: 520px;
      white-space: normal;
    }}
    .compact-reason {{
      max-width: 520px;
      color: var(--ink);
    }}
    .compact-reason summary {{
      cursor: pointer;
      color: var(--accent-2);
      font-weight: 700;
      list-style-position: inside;
    }}
    .compact-reason div {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      white-space: normal;
    }}
    .compact-components {{
      min-width: 220px;
      max-width: 360px;
      white-space: normal;
    }}
    .compact-components summary {{
      cursor: pointer;
      color: var(--accent-2);
      font-weight: 700;
      list-style-position: inside;
    }}
    .compact-components div {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      white-space: normal;
    }}
    .analysis-compact-table td:nth-child(12),
    .analysis-compact-table td:nth-child(13) {{
      min-width: 220px;
      max-width: 360px;
      white-space: normal;
    }}
    .recommendation-toolbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 15px 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .recommendation-toolbar strong {{
      color: var(--ink);
    }}
    .pill.buy {{
      background: #dcfce7;
      color: #166534;
    }}
    .pill.caution {{
      background: #fef3c7;
      color: #92400e;
    }}
    .result-focus {{
      display: grid;
      grid-template-columns: repeat(3, minmax(180px, 1fr));
      gap: 10px;
      padding: 14px 15px 0;
    }}
    .result-focus .decision-card {{
      background: #ffffff;
    }}
    .result-focus .hit strong {{
      color: #166534;
    }}
    .result-focus .return strong {{
      color: var(--accent);
    }}
    .result-focus .miss strong {{
      color: #991b1b;
    }}
    .analysis-fold {{
      margin-top: 16px;
    }}
    .analysis-fold > summary {{
      cursor: pointer;
      padding: 13px 15px;
      color: var(--accent);
      font-weight: 800;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      background: #fbfcfd;
    }}
    tr.sample-low td {{
      opacity: 0.48;
      background: #f8fafc;
    }}
    tr.sample-low td:first-child::after {{
      content: " 参考";
      color: var(--accent-3);
      font-size: 11px;
      font-weight: 700;
    }}
    tr.analysis-selected td {{
      background: #e8f1f8;
    }}
    .analysis-dashboard {{
      display: grid;
      gap: 14px;
      padding: 14px 15px 16px;
    }}
    .analysis-metrics {{
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 10px;
    }}
    .analysis-metric {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfd;
    }}
    .analysis-metric span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .analysis-metric strong {{
      font-size: 20px;
    }}
    .analysis-panels {{
      display: grid;
      grid-template-columns: repeat(2, minmax(260px, 1fr));
      gap: 14px;
    }}
    .analysis-panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #ffffff;
      min-width: 0;
    }}
    .analysis-panel h3 {{
      margin: 0 0 10px;
      font-size: 15px;
    }}
    .analysis-bars {{
      display: grid;
      gap: 8px;
    }}
    .analysis-bar-row {{
      display: grid;
      grid-template-columns: minmax(92px, 150px) 1fr minmax(54px, auto);
      gap: 8px;
      align-items: center;
      font-size: 12px;
    }}
    .analysis-bar-track {{
      height: 9px;
      border-radius: 999px;
      background: #edf1f4;
      overflow: hidden;
    }}
    .analysis-bar-fill {{
      height: 100%;
      border-radius: 999px;
      background: var(--accent);
    }}
    .analysis-ranking {{
      display: grid;
      gap: 7px;
    }}
    .analysis-rank-item {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: center;
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 9px;
      background: #fbfcfd;
      color: var(--ink);
      font: inherit;
      text-align: left;
      cursor: pointer;
    }}
    .analysis-rank-item span {{
      display: block;
      color: var(--muted);
      font-size: 11px;
      margin-top: 2px;
    }}
    .analysis-scatter {{
      position: relative;
      height: 260px;
      border-left: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      background:
        linear-gradient(#eef2f5 1px, transparent 1px),
        linear-gradient(90deg, #eef2f5 1px, transparent 1px);
      background-size: 100% 25%, 25% 100%;
      margin: 6px 4px 20px 22px;
    }}
    .analysis-point {{
      position: absolute;
      transform: translate(-50%, 50%);
      border: 0;
      border-radius: 999px;
      background: var(--accent);
      color: #fff;
      font-size: 0;
      cursor: pointer;
      box-shadow: 0 2px 8px rgba(29, 78, 116, 0.24);
    }}
    .analysis-point.verdict-strong {{ background: var(--accent-2); }}
    .analysis-point.verdict-weak {{ background: var(--accent-3); }}
    .analysis-point.verdict-watch {{ background: #8a6d3b; }}
    .analysis-axis-note {{
      color: var(--muted);
      font-size: 12px;
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin-top: -12px;
    }}
    .analysis-detail-toggle {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }}
    .analysis-detail-toggle summary {{
      cursor: pointer;
      padding: 12px 14px;
      font-weight: 700;
      color: var(--accent);
      background: #fbfcfd;
    }}
    .analysis-detail-toggle .section-lead {{
      padding: 0 14px 10px;
    }}
    .filters {{
      display: grid;
      grid-template-columns: repeat(4, minmax(130px, 1fr));
      gap: 10px;
      padding: 14px 15px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfd;
    }}
    .filters label {{
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .filters input, .filters select {{
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 7px 9px;
      background: #ffffff;
      color: var(--ink);
      font: inherit;
    }}
    .filters .check {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding-top: 20px;
    }}
    .filters .check input {{
      min-height: 0;
    }}
    .toolbar {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .toolbar label {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .toolbar input {{
      min-height: 38px;
      min-width: min(360px, 100%);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      background: #ffffff;
      color: var(--ink);
      font: inherit;
    }}
    .racer-filter-panel {{
      display: grid;
      gap: 12px;
    }}
    .racer-filter-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 10px;
    }}
    .racer-filter-grid label {{
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .racer-filter-grid select {{
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 7px 9px;
      background: #ffffff;
      color: var(--ink);
      font: inherit;
    }}
    .kana-filter {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .kana-filter button {{
      min-height: 32px;
      min-width: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      color: var(--muted);
      font-weight: 700;
      cursor: pointer;
    }}
    .kana-filter button.active {{
      border-color: var(--accent);
      background: var(--soft);
      color: var(--accent);
    }}
    .selected-racer-control {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 13px;
    }}
    .selected-racer-control strong {{
      color: var(--ink);
    }}
    .selected-racer-control button {{
      min-height: 32px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      color: var(--accent);
      font-weight: 700;
      cursor: pointer;
      padding: 6px 10px;
    }}
    .racer-search-table tbody tr {{
      cursor: pointer;
    }}
    .hit {{
      color: var(--accent);
      font-weight: 700;
    }}
    .miss {{
      color: #b91c1c;
      font-weight: 700;
    }}
    .ranking-more {{
      border-top: 1px solid var(--line);
    }}
    .ranking-more summary {{
      cursor: pointer;
      padding: 11px 15px;
      color: var(--accent-2);
      font-size: 13px;
      font-weight: 700;
      background: #fbfcfd;
      list-style-position: inside;
    }}
    @media (max-width: 780px) {{
      .grid, .grid.two {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
      section {{ overflow-x: auto; }}
      table {{ min-width: 760px; }}
      .bar-row {{ grid-template-columns: 96px minmax(130px, 1fr) 74px; }}
      .filters {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
      .decision-grid {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
      .result-focus {{ grid-template-columns: 1fr; }}
      .prediction-type-grid {{ grid-template-columns: 1fr; }}
      .analysis-metrics {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
      .analysis-panels {{ grid-template-columns: 1fr; }}
      .analysis-bar-row {{ grid-template-columns: 96px minmax(120px, 1fr) 58px; }}
      .analysis-scatter {{ min-width: 520px; }}
      h1 {{ font-size: 24px; }}
    }}
  </style>
</head>
<body{body_class}>
  {env_banner}
  <header>
    <div class="wrap">
      <h1>{h(title)}</h1>
      <nav>{nav}</nav>
    </div>
  </header>
  <main>
    <div class="wrap">
      {body}
    </div>
  </main>
  <script>
    (() => {{
      const toggle = document.querySelector("[data-operation-toggle]");
      const actions = document.querySelector("[data-operation-actions]");
      const error = document.querySelector("[data-operation-error]");
      if (!toggle || !actions) return;

      toggle.addEventListener("click", () => {{
        const password = window.prompt("パスワードを入力してください");
        if (password === "0415") {{
          actions.hidden = false;
          toggle.setAttribute("aria-expanded", "true");
          if (error) error.textContent = "";
          return;
        }}
        if (error) error.textContent = "パスワードが違います。";
      }});
    }})();
  </script>
</body>
</html>
"""
    return "\n".join(line.rstrip() for line in html.splitlines()) + "\n"


def bar_chart(data: list[dict], label_field: str, value_field: str, value_format=str, limit=12) -> str:
    values = [to_float(row.get(value_field)) for row in data[:limit]]
    values = [value for value in values if value is not None]
    if not data or not values:
        return '<div class="empty">グラフ化できるデータがありません</div>'
    max_value = max(values) or 1
    html_rows = []
    for row in data[:limit]:
        value = to_float(row.get(value_field))
        if value is None:
            continue
        width = max(1, min(100, value / max_value * 100))
        html_rows.append(
            '<div class="bar-row">'
            f'<div class="bar-label" title="{h(row.get(label_field))}">{h(row.get(label_field))}</div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{width:.1f}%"></div></div>'
            f'<div class="bar-value">{h(value_format(value))}</div>'
            '</div>'
        )
    return '<div class="chart">' + "".join(html_rows) + "</div>"


def line_chart(data: list[dict], label_field: str, series: list[tuple[str, str, str]], limit=30) -> str:
    chart_data = data[-limit:]
    values: list[float] = []
    for row in chart_data:
        for key, _, _ in series:
            value = to_float(row.get(key))
            if value is not None:
                values.append(value)
    if not chart_data or not values:
        return '<div class="empty">グラフ化できるデータがありません</div>'

    width = 640
    height = 220
    pad_left = 44
    pad_right = 18
    pad_top = 18
    pad_bottom = 34
    plot_width = width - pad_left - pad_right
    plot_height = height - pad_top - pad_bottom
    min_value = 0
    max_value = max(100, max(values))

    def point(index: int, value: float) -> tuple[float, float]:
        x = pad_left + (plot_width * index / max(1, len(chart_data) - 1))
        y = pad_top + plot_height - ((value - min_value) / (max_value - min_value) * plot_height)
        return x, y

    grid_lines = []
    for tick in [0, 25, 50, 75, 100]:
        y = pad_top + plot_height - (tick / max_value * plot_height)
        grid_lines.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" stroke="#e5eaf0" />'
            f'<text x="8" y="{y + 4:.1f}" font-size="11" fill="#697586">{tick}%</text>'
        )

    polylines = []
    for key, label, color in series:
        points = []
        for index, row in enumerate(chart_data):
            value = to_float(row.get(key))
            if value is None:
                continue
            x, y = point(index, value)
            points.append(f"{x:.1f},{y:.1f}")
        if points:
            polylines.append(
                f'<polyline points="{" ".join(points)}" fill="none" stroke="{h(color)}" '
                f'stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />'
            )

    first_label = chart_data[0].get(label_field, "")
    last_label = chart_data[-1].get(label_field, "")
    legend = "".join(
        f'<span><i style="background:{h(color)}"></i>{h(label)}</span>'
        for _, label, color in series
    )
    return (
        '<div class="line-chart">'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="日別推移グラフ">'
        f'{"".join(grid_lines)}'
        f'<line x1="{pad_left}" y1="{pad_top + plot_height}" x2="{width - pad_right}" y2="{pad_top + plot_height}" stroke="#cbd5df" />'
        f'{"".join(polylines)}'
        f'<text x="{pad_left}" y="{height - 8}" font-size="11" fill="#697586">{h(first_label)}</text>'
        f'<text x="{width - pad_right}" y="{height - 8}" text-anchor="end" font-size="11" fill="#697586">{h(last_label)}</text>'
        '</svg>'
        f'<div class="line-legend">{legend}</div>'
        '</div>'
    )


def race_context_label(row: dict) -> str:
    race_class = str(row.get("race_class") or row.get("race_title") or "")
    if "初特選" in race_class or "初日特選" in race_class:
        return "初日特選"
    if "特選" in race_class:
        return "特選"
    if "準決" in race_class:
        return "準決勝"
    if "決勝" in race_class:
        return "決勝"
    if "予選" in race_class:
        return "予選"
    if "一般" in race_class:
        return "一般/敗者戦"
    if "選抜" in race_class:
        return "選抜"
    return "その他"


def axis_line_role(row: dict) -> str:
    if row.get("axis_is_tanki"):
        return "単騎"
    position = row.get("axis_line_position")
    try:
        position = int(position)
    except (TypeError, ValueError):
        return "並び不明"
    if position == 1:
        return "先頭"
    if position == 2:
        return "番手"
    if position >= 3:
        return "三番手以降"
    return "並び不明"


def axis_line_size_label(row: dict) -> str:
    if row.get("axis_is_tanki"):
        return "単騎"
    size = row.get("axis_line_size")
    try:
        size = int(size)
    except (TypeError, ValueError):
        return "不明"
    if size <= 1:
        return "単騎"
    if size == 2:
        return "2車ライン"
    if size == 3:
        return "3車ライン"
    return "4車以上ライン"


def axis_condition_rows(items: list[dict], group_field: str, label: str, limit=12) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in items:
        value = row.get(group_field) or "不明"
        groups[str(value)].append(row)
    result = []
    for group, rows_in_group in groups.items():
        total = len(rows_in_group)
        axis_miss = sum(1 for item in rows_in_group if not item.get("hit_1st"))
        exact_hits = sum(1 for item in rows_in_group if item.get("hit_exact"))
        returned = sum(int(item.get("return_amount") or 0) for item in rows_in_group)
        stake = sum(int(item.get("result_stake_amount") or item.get("stake_amount") or 0) for item in rows_in_group)
        result.append({
            "condition": group,
            "axis_miss_count": axis_miss,
            "axis_miss_rate": axis_miss * 100 / total if total else 0,
            "exact_rate": exact_hits * 100 / total if total else 0,
            "predictions": total,
            "roi": returned * 100 / stake if stake else 0,
            "_class": sample_class(total, 30),
        })
    return sorted(
        result,
        key=lambda row: (row["axis_miss_rate"], row["axis_miss_count"], row["predictions"]),
        reverse=True,
    )[:limit]


def format_axis_condition_rows(items: list[dict]) -> list[dict]:
    return [
        {
            "condition": row["condition"],
            "axis_miss_count": number(row["axis_miss_count"]),
            "axis_miss_rate": pct(row["axis_miss_rate"]),
            "exact_rate": pct(row["exact_rate"]),
            "predictions": number(row["predictions"]),
            "roi": pct(row["roi"]),
            "_class": row.get("_class", ""),
        }
        for row in items
    ]


def prediction_miss_reason(row: dict) -> str:
    if row.get("hit_exact"):
        return "的中"
    if not row.get("hit_1st"):
        return "軸選手が飛んだ"
    top3_count = int(row.get("hit_top3_count") or 0)
    if top3_count >= 3:
        return "順番違い"
    if row.get("hit_top2"):
        return "3着候補抜け"
    if top3_count >= 2:
        return "相手抜け"
    return "相手候補不足"


def heat_class(value, max_value) -> str:
    if value is None or max_value <= 0:
        return "heat-0"
    ratio = value / max_value
    if ratio >= 0.8:
        return "heat-4"
    if ratio >= 0.55:
        return "heat-3"
    if ratio >= 0.3:
        return "heat-2"
    if ratio > 0:
        return "heat-1"
    return "heat-0"


def venue_car_heatmap(conn) -> str:
    data = rows(conn, """
        SELECT m.venue, r.car_no,
               COUNT(*) AS starts,
               ROUND(SUM(CASE WHEN r.rank = 1 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS win_rate
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        WHERE r.car_no IS NOT NULL
        GROUP BY m.venue, r.car_no
    """)
    if not data:
        return '<div class="empty">データがありません</div>'
    venues = sorted({row["venue"] for row in data})
    rates = {(row["venue"], row["car_no"]): row["win_rate"] for row in data}
    starts = {(row["venue"], row["car_no"]): row["starts"] for row in data}
    max_rate = max((row["win_rate"] or 0 for row in data), default=0)
    header = "<tr><th>会場</th>" + "".join(f"<th>{car}番</th>" for car in range(1, 10)) + "</tr>"
    body = ""
    for venue in venues:
        body += f"<tr><th>{h(venue)}</th>"
        for car in range(1, 10):
            rate = rates.get((venue, car))
            sample = starts.get((venue, car), 0)
            text = "" if rate is None else f"{rate:.1f}%"
            body += f'<td class="{heat_class(rate, max_rate)}" title="出走 {sample}">{h(text)}</td>'
        body += "</tr>"
    return f'<div class="heatmap"><table><thead>{header}</thead><tbody>{body}</tbody></table></div>'


def median_by_group(conn, group_expr: str, value_expr: str, from_sql: str, params=()) -> dict:
    data = defaultdict(list)
    for row in conn.execute(f"SELECT {group_expr} AS group_key, {value_expr} AS value {from_sql}", params):
        value = to_float(row["value"])
        if row["group_key"] is not None and value is not None:
            data[row["group_key"]].append(value)
    return {key: statistics.median(values) for key, values in data.items() if values}


def summary(conn):
    return {
        "races": scalar(conn, "SELECT COUNT(*) FROM race_master"),
        "racers": scalar(conn, "SELECT COUNT(DISTINCT racer_name) FROM race_result"),
        "venues": scalar(conn, "SELECT COUNT(DISTINCT venue) FROM race_master"),
        "payout_total": scalar(conn, "SELECT COALESCE(SUM(payout), 0) FROM payout"),
        "latest_created": scalar(conn, "SELECT MAX(created_at) FROM race_master"),
        "latest_race_date": scalar(conn, "SELECT MAX(race_date) FROM race_master"),
        "first_race_date": scalar(conn, "SELECT MIN(race_date) FROM race_master"),
        "trifecta_avg": scalar(conn, "SELECT ROUND(AVG(payout), 0) FROM payout WHERE bet_type = ?", (TRIFECTA,)),
        "trifecta_max": scalar(conn, "SELECT MAX(payout) FROM payout WHERE bet_type = ?", (TRIFECTA,)),
        "trifecta_high_rate": scalar(
            conn,
            "SELECT ROUND(AVG(CASE WHEN payout >= 10000 THEN 100.0 ELSE 0 END), 1) FROM payout WHERE bet_type = ?",
            (TRIFECTA,),
        ),
    }


def render_top(conn) -> str:
    s = summary(conn)
    daily = rows(conn, """
        SELECT race_date, COUNT(*) AS races
        FROM race_master
        GROUP BY race_date
        ORDER BY race_date DESC
        LIMIT 30
    """)
    daily_chart = list(reversed(daily))
    monthly = rows(conn, """
        SELECT strftime('%Y-%m', m.race_date) AS month,
               ROUND(AVG(p.payout), 0) AS avg_payout
        FROM payout p
        JOIN race_master m ON m.race_id = p.race_id
        WHERE p.bet_type = ?
        GROUP BY month
        ORDER BY month DESC
        LIMIT 12
    """, (TRIFECTA,))
    body = f"""
    <div class="grid">
      <div class="card"><span>総レース数</span><strong>{h(number(s["races"]))}</strong></div>
      <div class="card"><span>総選手数</span><strong>{h(number(s["racers"]))}</strong></div>
      <div class="card"><span>総会場数</span><strong>{h(number(s["venues"]))}</strong></div>
      <div class="card"><span>最新レース日</span><strong>{h(s["latest_race_date"] or "-")}</strong></div>
      <div class="card"><span>3連単平均配当</span><strong>{h(yen(s["trifecta_avg"]))}</strong></div>
      <div class="card"><span>3連単万車券率</span><strong>{h(pct(s["trifecta_high_rate"]))}</strong></div>
      <div class="card"><span>3連単最高配当</span><strong>{h(yen(s["trifecta_max"]))}</strong></div>
      <div class="card"><span>最終保存日時</span><strong>{h(s["latest_created"] or "-")}</strong></div>
    </div>
    """
    body += f"""
    <section>
      <h2><button class="operation-toggle" type="button" data-operation-toggle aria-expanded="false">運用操作</button></h2>
      <p class="section-lead">ボタン先のGitHub Actions画面で Run workflow を押すと実行できます。通常の自動取得は毎日8:00 JSTに前日分を取得します。</p>
      <p class="operation-error" data-operation-error></p>
      <div class="actions" data-operation-actions hidden>
        <a class="action-button" href="{h(workflow_url("analyze.yml"))}">予想・ページ更新</a>
        <a class="action-button" href="{h(workflow_url("collect.yml"))}">手動で取得する</a>
        <a class="action-button secondary" href="{h(workflow_url("reset-data.yml"))}">取得データを削除する</a>
      </div>
    </section>
    """
    body += '<div class="grid two">'
    body += section("日別取得レース数", bar_chart(daily_chart, "race_date", "races", lambda v: f"{int(v)}R", 30))
    body += section("月別3連単平均配当", bar_chart(list(reversed(monthly)), "month", "avg_payout", yen, 12))
    body += "</div>"
    return page("競輪統計 TOP", "top", body)


def render_venues(conn) -> str:
    venue_stats = rows(conn, """
        WITH race_counts AS (
            SELECT venue, COUNT(*) AS races
            FROM race_master
            GROUP BY venue
        ),
        time_stats AS (
            SELECT m.venue, ROUND(AVG(CAST(NULLIF(r.time, '') AS REAL)), 2) AS avg_time
            FROM race_result r
            JOIN race_master m ON m.race_id = r.race_id
            WHERE r.time IS NOT NULL AND r.time != ''
            GROUP BY m.venue
        ),
        payout_stats AS (
            SELECT m.venue,
                   ROUND(AVG(p.payout), 0) AS trifecta_avg,
                   MAX(p.payout) AS trifecta_max,
                   ROUND(AVG(CASE WHEN p.payout >= 10000 THEN 100.0 ELSE 0 END), 1) AS high_rate
            FROM payout p
            JOIN race_master m ON m.race_id = p.race_id
            WHERE p.bet_type = ?
            GROUP BY m.venue
        )
        SELECT c.venue, c.races, t.avg_time, p.trifecta_avg, p.trifecta_max, p.high_rate
        FROM race_counts c
        LEFT JOIN time_stats t ON t.venue = c.venue
        LEFT JOIN payout_stats p ON p.venue = c.venue
        ORDER BY c.races DESC, c.venue
    """, (TRIFECTA,))
    medians = median_by_group(conn, "m.venue", "p.payout", """
        FROM payout p
        JOIN race_master m ON m.race_id = p.race_id
        WHERE p.bet_type = ?
    """, (TRIFECTA,))
    ranking = []
    for row in venue_stats:
        median = medians.get(row["venue"])
        row["trifecta_median_raw"] = median
        score = (row["trifecta_avg"] or 0) + (median or 0) + ((row["high_rate"] or 0) * 100)
        ranking.append({**row, "score": score})

    display_stats = []
    for row in ranking:
        low_sample = (row["races"] or 0) < 10
        display_stats.append({
            "_class": sample_class(row["races"], 10),
            "venue": row["venue"],
            "races": row["races"],
            "avg_time": decimal(row["avg_time"]),
            "trifecta_avg": yen(row["trifecta_avg"]),
            "trifecta_median": yen(row["trifecta_median_raw"]),
            "trifecta_max": yen(row["trifecta_max"]),
            "high_rate": pct(row["high_rate"]),
            "score": f'{row["score"]:.0f}',
            "sample_note": pill("10件未満", "warn") if low_sample else pill("通常", "ok"),
        })
    turbulence = sorted(display_stats, key=lambda row: float(row["score"]), reverse=True)
    solid = sorted(display_stats, key=lambda row: float(row["score"]))

    body = '<div class="grid two">'
    body += section("会場別平均タイム", bar_chart(
        sorted([row for row in ranking if row["avg_time"] is not None], key=lambda row: row["avg_time"]),
        "venue",
        "avg_time",
        lambda v: f"{v:.2f}秒",
    ))
    body += section("会場別3連単平均配当", bar_chart(
        sorted([row for row in ranking if row["trifecta_avg"] is not None], key=lambda row: row["trifecta_avg"], reverse=True),
        "venue",
        "trifecta_avg",
        yen,
    ))
    body += "</div>"
    body += '<div class="grid two">'
    body += section("荒れやすい会場 TOP3", rich_table(
        ["会場", "レース数", "3連単平均", "万車券率", "荒れ度", "母数"],
        turbulence[:3],
        ["venue", "races", "trifecta_avg", "high_rate", "score", "sample_note"],
    ))
    body += section("堅い会場 TOP3", rich_table(
        ["会場", "レース数", "3連単平均", "万車券率", "荒れ度", "母数"],
        solid[:3],
        ["venue", "races", "trifecta_avg", "high_rate", "score", "sample_note"],
    ))
    body += "</div>"
    body += section("会場別統計", rich_table(
        ["会場", "レース数", "平均タイム", "3連単平均", "3連単中央値", "3連単最高", "万車券率", "荒れ度", "母数"],
        display_stats,
        ["venue", "races", "avg_time", "trifecta_avg", "trifecta_median", "trifecta_max", "high_rate", "score", "sample_note"],
    ), "レース数10未満の会場は薄く表示します。平均タイムは展開差も受けるため、荒れ度とは別軸の参考値です。")
    body += section("荒れ度の計算説明", """
      <div class="inline-note">
        荒れ度 = 3連単平均配当 + 3連単中央値 + 万車券率 × 100。平均配当だけだと1本の高配当に引っ張られるため、中央値と万車券率を合わせて見ます。
      </div>
    """)
    body += section("会場×車番 勝率ヒートマップ", venue_car_heatmap(conn), "色が濃いほど、その会場で1着になった割合が高い車番です。")
    body += section("荒れ度ランキング", rich_table(
        ["会場", "レース数", "3連単平均", "3連単中央値", "万車券率", "荒れ度", "母数"],
        turbulence,
        ["venue", "races", "trifecta_avg", "trifecta_median", "high_rate", "score", "sample_note"],
    ))
    return page("会場分析", "venues", body)


def render_car_numbers(conn) -> str:
    stats = rows(conn, """
        SELECT car_no,
               COUNT(*) AS starts,
               SUM(CASE WHEN rank = 1 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN rank <= 2 THEN 1 ELSE 0 END) AS quinella,
               SUM(CASE WHEN rank <= 3 THEN 1 ELSE 0 END) AS top3,
               ROUND(AVG(rank), 2) AS avg_rank,
               ROUND(SUM(CASE WHEN rank = 1 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS win_rate,
               ROUND(SUM(CASE WHEN rank <= 2 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS quinella_rate,
               ROUND(SUM(CASE WHEN rank <= 3 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS top3_rate
        FROM race_result
        WHERE car_no IS NOT NULL
        GROUP BY car_no
        ORDER BY car_no
    """)
    for row in stats:
        row["_class"] = sample_class(row["starts"], 50)
        row["car_label"] = f'{row["car_no"]}番'
        row["win_rate_display"] = pct(row["win_rate"])
        row["quinella_rate_display"] = pct(row["quinella_rate"])
        row["top3_rate_display"] = pct(row["top3_rate"])
        row["avg_rank_display"] = decimal(row["avg_rank"])
        row["sample_note"] = pill("母数不足", "warn") if row["starts"] < 50 else pill("通常", "ok")
    recovery = car_recovery(conn)

    body = """
    <div class="inline-note">
      現在のデータは7車立て中心です。8番・9番は出走数が少ないため、勝率や回収率が極端に見えやすくなります。勝率は1着の強さ、3着内率は舟券圏内の安定感、回収率は配当の跳ね方として分けて見てください。
    </div>
    """
    body += '<div class="grid two">'
    body += section("車番別勝率", bar_chart(stats, "car_label", "win_rate", pct, 9))
    body += section("車番別3着内率", bar_chart(stats, "car_label", "top3_rate", pct, 9))
    body += "</div>"
    body += section("車番別成績", rich_table(
        ["車番", "出走", "1着", "連対", "3着内", "勝率", "連対率", "3着内率", "平均着順", "母数"],
        stats,
        ["car_no", "starts", "wins", "quinella", "top3", "win_rate_display", "quinella_rate_display", "top3_rate_display", "avg_rank_display", "sample_note"],
    ), "8番・9番は9車立てでしか出ないため、他車番と同じ感覚で比較しないでください。")
    body += section("会場×車番 勝率ヒートマップ", venue_car_heatmap(conn))
    body += section("車番別回収率 2車単・100円購入想定", rich_table(
        ["車番", "対象レース", "払戻合計", "投資額", "回収率", "母数"],
        recovery,
        ["car_no", "races", "return_total", "investment", "recovery_rate", "sample_note"],
    ), "回収率は的中頻度ではなく払戻の大きさに強く影響されます。母数不足の車番は薄く表示しています。")
    return page("車番分析", "cars", body)


def render_outcomes(conn) -> str:
    bet_placeholders = ",".join("?" for _ in PREDICTION_BET_TYPES)
    eligible_races = scalar(
        conn,
        "SELECT COUNT(*) FROM race_master WHERE COALESCE(dead_heat, 0) = 0",
    )
    dead_heat_races = scalar(
        conn,
        "SELECT COUNT(*) FROM race_master WHERE COALESCE(dead_heat, 0) = 1",
    )
    date_range = conn.execute(
        """
        SELECT MIN(race_date) AS first_date, MAX(race_date) AS last_date
        FROM race_master
        WHERE COALESCE(dead_heat, 0) = 0
        """
    ).fetchone()

    bet_summary = rows(
        conn,
        f"""
        SELECT p.bet_type,
               COUNT(*) AS winning_rows,
               COUNT(DISTINCT p.race_id) AS races,
               COUNT(DISTINCT p.combination) AS combinations
        FROM payout p
        JOIN race_master m ON m.race_id = p.race_id
        WHERE COALESCE(m.dead_heat, 0) = 0
          AND p.bet_type IN ({bet_placeholders})
        GROUP BY p.bet_type
        """,
        PREDICTION_BET_TYPES,
    )
    summary_by_type = {row["bet_type"]: row for row in bet_summary}
    bet_summary = [
        {
            "bet_type": bet_type,
            "races": number(summary_by_type.get(bet_type, {}).get("races") or 0),
            "winning_rows": number(summary_by_type.get(bet_type, {}).get("winning_rows") or 0),
            "combinations": number(summary_by_type.get(bet_type, {}).get("combinations") or 0),
        }
        for bet_type in PREDICTION_BET_TYPES
    ]

    outcome_rows = rows(
        conn,
        f"""
        WITH totals AS (
            SELECT p.bet_type,
                   COUNT(DISTINCT p.race_id) AS race_total
            FROM payout p
            JOIN race_master m ON m.race_id = p.race_id
            WHERE COALESCE(m.dead_heat, 0) = 0
              AND p.bet_type IN ({bet_placeholders})
            GROUP BY p.bet_type
        )
        SELECT p.bet_type, p.combination,
               COUNT(*) AS appearances,
               ROUND(COUNT(*) * 100.0 / NULLIF(t.race_total, 0), 2) AS appearance_rate,
               ROUND(AVG(p.payout), 0) AS avg_payout,
               MIN(p.payout) AS min_payout,
               MAX(p.payout) AS max_payout,
               ROUND(AVG(p.popularity), 1) AS avg_popularity,
               MIN(m.race_date) AS first_date,
               MAX(m.race_date) AS last_date
        FROM payout p
        JOIN race_master m ON m.race_id = p.race_id
        JOIN totals t ON t.bet_type = p.bet_type
        WHERE COALESCE(m.dead_heat, 0) = 0
          AND p.bet_type IN ({bet_placeholders})
        GROUP BY p.bet_type, p.combination
        ORDER BY
          CASE p.bet_type
            WHEN '2車複' THEN 1 WHEN '2車単' THEN 2 WHEN 'ワイド' THEN 3
            WHEN '3連複' THEN 4 WHEN '3連単' THEN 5 ELSE 9
          END,
          appearances DESC, avg_payout DESC, p.combination
        """,
        (*PREDICTION_BET_TYPES, *PREDICTION_BET_TYPES),
    )
    rank_by_type = defaultdict(int)
    for row in outcome_rows:
        rank_by_type[row["bet_type"]] += 1
        row["rank"] = rank_by_type[row["bet_type"]]
        row["appearance_rate_display"] = pct(row["appearance_rate"])
        row["avg_payout_display"] = yen(row["avg_payout"])
        row["min_payout_display"] = yen(row["min_payout"])
        row["max_payout_display"] = yen(row["max_payout"])
        row["avg_popularity_display"] = decimal(row["avg_popularity"], 1)
        row["_data"] = {
            "bet-type": row["bet_type"],
            "combination": row["combination"],
        }

    trifecta_top = [
        row for row in outcome_rows
        if row["bet_type"] == TRIFECTA
    ][:20]

    venue_leaders = rows(
        conn,
        f"""
        WITH grouped AS (
            SELECT m.venue, p.bet_type, p.combination,
                   COUNT(*) AS appearances,
                   ROUND(AVG(p.payout), 0) AS avg_payout
            FROM payout p
            JOIN race_master m ON m.race_id = p.race_id
            WHERE COALESCE(m.dead_heat, 0) = 0
              AND p.bet_type IN ({bet_placeholders})
            GROUP BY m.venue, p.bet_type, p.combination
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY venue, bet_type
                       ORDER BY appearances DESC, avg_payout DESC, combination
                   ) AS outcome_rank
            FROM grouped
        )
        SELECT venue, bet_type, combination, appearances, avg_payout
        FROM ranked
        WHERE outcome_rank = 1
        ORDER BY venue,
          CASE bet_type
            WHEN '2車複' THEN 1 WHEN '2車単' THEN 2 WHEN 'ワイド' THEN 3
            WHEN '3連複' THEN 4 WHEN '3連単' THEN 5 ELSE 9
          END
        """,
        PREDICTION_BET_TYPES,
    )
    for row in venue_leaders:
        row["avg_payout_display"] = yen(row["avg_payout"])
        row["_data"] = {"bet-type": row["bet_type"]}

    race_no_leaders = rows(
        conn,
        f"""
        WITH grouped AS (
            SELECT m.race_no, p.bet_type, p.combination,
                   COUNT(*) AS appearances,
                   ROUND(AVG(p.payout), 0) AS avg_payout
            FROM payout p
            JOIN race_master m ON m.race_id = p.race_id
            WHERE COALESCE(m.dead_heat, 0) = 0
              AND p.bet_type IN ({bet_placeholders})
            GROUP BY m.race_no, p.bet_type, p.combination
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY race_no, bet_type
                       ORDER BY appearances DESC, avg_payout DESC, combination
                   ) AS outcome_rank
            FROM grouped
        )
        SELECT race_no, bet_type, combination, appearances, avg_payout
        FROM ranked
        WHERE outcome_rank = 1
        ORDER BY race_no,
          CASE bet_type
            WHEN '2車複' THEN 1 WHEN '2車単' THEN 2 WHEN 'ワイド' THEN 3
            WHEN '3連複' THEN 4 WHEN '3連単' THEN 5 ELSE 9
          END
        """,
        PREDICTION_BET_TYPES,
    )
    for row in race_no_leaders:
        row["race_no_display"] = f'{row["race_no"]}R'
        row["avg_payout_display"] = yen(row["avg_payout"])
        row["_data"] = {"bet-type": row["bet_type"]}

    bet_options = "".join(
        f'<option value="{h(bet_type)}"{" selected" if bet_type == TRIFECTA else ""}>{h(bet_type)}</option>'
        for bet_type in PREDICTION_BET_TYPES
    )
    body = f"""
    <div class="grid">
      <div class="card"><span>通常レース</span><strong>{h(number(eligible_races))}</strong></div>
      <div class="card"><span>集計開始</span><strong>{h(date_range["first_date"] or "-")}</strong></div>
      <div class="card"><span>集計終了</span><strong>{h(date_range["last_date"] or "-")}</strong></div>
      <div class="card"><span>同着除外</span><strong>{h(number(dead_heat_races))}</strong></div>
    </div>
    """
    body += section(
        "賭式別データ量",
        table(
            ["賭式", "対象レース", "的中組番行", "確認できる出目数"],
            bet_summary,
            ["bet_type", "races", "winning_rows", "combinations"],
        ),
        "ワイドは1レースにつき複数の的中組番があるため、的中組番行は対象レース数より多くなります。",
    )
    body += section(
        "3連単 出目ランキング TOP20",
        bar_chart(trifecta_top, "combination", "appearances", lambda value: f"{int(value)}回", 20),
        "同着を除いた通常レースで、出現回数の多い順です。",
    )
    body += section(
        "出目ランキング",
        f"""
        <div class="filters">
          <label>賭式
            <select id="outcome-bet-filter">{bet_options}</select>
          </label>
          <label>組番検索
            <input id="outcome-combination-filter" type="search" placeholder="例: 1-2-3">
          </label>
        </div>
        {table(
            ["順位", "賭式", "組番", "出現", "レース出現率", "平均配当", "最低", "最高", "平均人気", "初回", "最終"],
            outcome_rows,
            ["rank", "bet_type", "combination", "appearances", "appearance_rate_display",
             "avg_payout_display", "min_payout_display", "max_payout_display",
             "avg_popularity_display", "first_date", "last_date"],
        ).replace("<table>", '<table id="outcome-ranking-table">', 1)}
        """,
        "レース出現率は、その賭式が発売された通常レースのうち該当組番が出た割合です。",
    )
    body += '<div class="grid two">'
    body += section(
        "会場別 最多出目",
        table(
            ["会場", "賭式", "最多組番", "出現", "平均配当"],
            venue_leaders,
            ["venue", "bet_type", "combination", "appearances", "avg_payout_display"],
        ).replace("<table>", '<table id="outcome-venue-table">', 1),
    )
    body += section(
        "レース番号別 最多出目",
        table(
            ["R", "賭式", "最多組番", "出現", "平均配当"],
            race_no_leaders,
            ["race_no_display", "bet_type", "combination", "appearances", "avg_payout_display"],
        ).replace("<table>", '<table id="outcome-race-no-table">', 1),
    )
    body += "</div>"
    body += """
    <script>
    (() => {
      const bet = document.getElementById("outcome-bet-filter");
      const combination = document.getElementById("outcome-combination-filter");
      const tables = [
        document.getElementById("outcome-ranking-table"),
        document.getElementById("outcome-venue-table"),
        document.getElementById("outcome-race-no-table")
      ].filter(Boolean);
      const apply = () => {
        const selectedBet = bet.value;
        const query = combination.value.trim();
        tables.forEach((table) => {
          table.querySelectorAll("tbody tr").forEach((row) => {
            const betMatch = !selectedBet || row.dataset.betType === selectedBet;
            const combinationMatch = table.id !== "outcome-ranking-table"
              || !query
              || (row.dataset.combination || "").includes(query);
            row.hidden = !(betMatch && combinationMatch);
          });
        });
      };
      bet.addEventListener("change", apply);
      combination.addEventListener("input", apply);
      apply();
    })();
    </script>
    """
    return page("出目分析", "outcomes", body)


def render_payouts(conn) -> str:
    bet_summary = rows(conn, """
        SELECT bet_type,
               COUNT(*) AS count,
               ROUND(AVG(payout), 0) AS avg_payout,
               MAX(payout) AS max_payout,
               ROUND(AVG(CASE WHEN payout >= 10000 THEN 100.0 ELSE 0 END), 1) AS high_rate
        FROM payout
        GROUP BY bet_type
        ORDER BY avg_payout DESC
    """)
    medians = median_by_group(conn, "bet_type", "payout", "FROM payout")
    for row in bet_summary:
        row["median_payout"] = yen(medians.get(row["bet_type"]))
        row["avg_payout_display"] = yen(row["avg_payout"])
        row["max_payout_display"] = yen(row["max_payout"])
        row["high_rate_display"] = pct(row["high_rate"])

    high = rows(conn, """
        SELECT m.race_date, m.venue, m.race_no, p.bet_type, p.combination,
               p.payout, p.popularity
        FROM payout p
        JOIN race_master m ON m.race_id = p.race_id
        ORDER BY p.payout DESC
        LIMIT 100
    """)
    tickets = [row.copy() for row in high if row["payout"] and row["payout"] >= 10000]
    monthly = rows(conn, """
        SELECT strftime('%Y-%m', m.race_date) AS month,
               ROUND(AVG(p.payout), 0) AS avg_payout
        FROM payout p
        JOIN race_master m ON m.race_id = p.race_id
        WHERE p.bet_type = ?
        GROUP BY month
        ORDER BY month
    """, (TRIFECTA,))
    weekday = rows(conn, """
        SELECT CASE strftime('%w', m.race_date)
                 WHEN '0' THEN '日'
                 WHEN '1' THEN '月'
                 WHEN '2' THEN '火'
                 WHEN '3' THEN '水'
                 WHEN '4' THEN '木'
                 WHEN '5' THEN '金'
                 ELSE '土'
               END AS weekday,
               ROUND(AVG(p.payout), 0) AS avg_payout
        FROM payout p
        JOIN race_master m ON m.race_id = p.race_id
        WHERE p.bet_type = ?
        GROUP BY strftime('%w', m.race_date)
        ORDER BY strftime('%w', m.race_date)
    """, (TRIFECTA,))
    histogram = payout_histogram(conn)
    for collection in (high, tickets, monthly, weekday, histogram):
        for row in collection:
            if "payout" in row:
                row["payout"] = yen(row["payout"])
            if "avg_payout" in row:
                row["avg_payout_display"] = yen(row["avg_payout"])

    trifecta_summary = next((row for row in bet_summary if row["bet_type"] == TRIFECTA), {})
    body = f"""
    <div class="grid">
      <div class="card"><span>3連単万車券率</span><strong>{h(pct(trifecta_summary.get("high_rate")))}</strong></div>
      <div class="card"><span>3連単最高配当</span><strong>{h(yen(trifecta_summary.get("max_payout")))}</strong></div>
      <div class="card"><span>3連単平均配当</span><strong>{h(yen(trifecta_summary.get("avg_payout")))}</strong></div>
      <div class="card"><span>3連単件数</span><strong>{h(number(trifecta_summary.get("count") or 0))}</strong></div>
    </div>
    """
    body += '<div class="grid two">'
    body += section("3連単配当分布", bar_chart(histogram, "range", "count", lambda v: f"{int(v)}件"))
    body += section("配当ゾーン", rich_table(
        ["ゾーン", "配当帯", "件数"],
        histogram,
        ["zone", "range", "count"],
    ), "低配当ゾーンは9,999円以下、万車券ゾーンは10,000円以上、大荒れゾーンは100,000円以上です。")
    body += "</div>"
    body += section("賭式別平均配当", bar_chart(bet_summary, "bet_type", "avg_payout", yen))
    body += section("賭式別サマリー", table(
        ["賭式", "件数", "平均", "中央値", "最高", "万車券率"],
        bet_summary,
        ["bet_type", "count", "avg_payout_display", "median_payout", "max_payout_display", "high_rate_display"],
    ))
    body += section("高配当ランキング TOP20", accordion_table(
        ["日付", "会場", "R", "賭式", "組番", "払戻", "人気"],
        high,
        ["race_date", "venue", "race_no", "bet_type", "combination", "payout", "popularity"],
        visible_count=20,
    ), "まず上位20件だけ表示します。残りは折りたたみ内で確認できます。")
    body += section("万車券ランキング", accordion_table(
        ["日付", "会場", "R", "賭式", "組番", "払戻", "人気"],
        tickets,
        ["race_date", "venue", "race_no", "bet_type", "combination", "payout", "popularity"],
        visible_count=20,
    ))
    body += '<div class="grid two">'
    body += section("月別3連単平均配当", bar_chart(monthly, "month", "avg_payout", yen, 12))
    body += section("曜日別3連単平均配当", bar_chart(weekday, "weekday", "avg_payout", yen, 7))
    body += "</div>"
    return page("配当分析", "payouts", body)


def render_racers(conn) -> str:
    threshold = max(3, racer_threshold(conn))

    def with_rates(data):
        for row in data:
            row["win_rate_display"] = pct(row.get("win_rate"))
            row["quinella_rate_display"] = pct(row.get("quinella_rate"))
            row["top3_rate_display"] = pct(row.get("top3_rate"))
            row["top2_rate_display"] = pct(row.get("top2_rate"))
            row["avg_rank_display"] = decimal(row.get("avg_rank"))
            row["avg_time_display"] = decimal(row.get("avg_time"))
            row["sample_warning"] = "サンプル不足" if int(row.get("starts") or 0) < 30 else ""
        return data

    def searchable_table(headers, data, fields):
        return table(headers, data, fields).replace("<table>", '<table class="racer-search-table">', 1)

    def grade_group(value):
        text = str(value or "").upper()
        if text.startswith("S"):
            return "S"
        if text.startswith("A"):
            return "A"
        if text.startswith("L"):
            return "L"
        return ""

    def kana_group(name):
        text = str(name or "").strip()
        if not text:
            return ""
        ch = text[0]
        kanji_initial_groups = {
            "阿": "あ", "安": "あ", "井": "あ", "伊": "あ", "岩": "あ", "石": "あ", "一": "あ", "網": "あ",
            "上": "あ", "内": "あ", "宇": "あ", "浦": "あ", "梅": "あ", "右": "あ",
            "大": "あ", "岡": "あ", "奥": "あ", "及": "あ", "小": "か", "加": "か", "勝": "か", "亀": "か",
            "川": "か", "北": "か", "國": "か", "国": "か", "倉": "か", "黒": "か", "後": "か",
            "佐": "さ", "坂": "さ", "塩": "さ", "白": "さ", "下": "さ", "隅": "さ",
            "高": "た", "田": "た", "多": "た", "千": "た", "塚": "た", "土": "た", "寺": "た",
            "出": "た", "戸": "た", "富": "た", "十": "た", "滝": "た", "瀧": "た", "當": "た",
            "中": "な", "仲": "な", "夏": "な", "長": "な", "西": "な", "布": "な",
            "原": "は", "早": "は", "服": "は", "橋": "は", "林": "は", "廣": "は", "広": "は",
            "深": "は", "福": "は", "堀": "は",
            "前": "ま", "増": "ま", "松": "ま", "真": "ま", "水": "ま", "三": "ま", "南": "ま",
            "宮": "ま", "村": "ま", "森": "ま", "元": "ま", "守": "ま",
            "山": "や", "横": "や", "吉": "や", "弓": "や",
            "龍": "ら",
            "渡": "わ",
        }
        if ch in kanji_initial_groups:
            return kanji_initial_groups[ch]
        groups = [
            ("あ", "あいうえおアイウエオ"),
            ("か", "かきくけこがぎぐげごカキクケコガギグゲゴ"),
            ("さ", "さしすせそざじずぜぞサシスセソザジズゼゾ"),
            ("た", "たちつてとだぢづでどタチツテトダヂヅデド"),
            ("な", "なにぬねのナニヌネノ"),
            ("は", "はひふへほばびぶべぼぱぴぷぺぽハヒフヘホバビブベボパピプペポ"),
            ("ま", "まみむめもマミムメモ"),
            ("や", "やゆよヤユヨ"),
            ("ら", "らりるれろラリルレロ"),
            ("わ", "わをんワヲン"),
        ]
        for key, chars in groups:
            if ch in chars:
                return key
        return "その他"

    latest_profile_rows = rows(conn, """
        WITH profile_source AS (
            SELECT e.racer_name, e.class AS latest_class, e.term AS latest_term,
                   m.race_date, e.id, 1 AS source_priority
            FROM race_entry e
            LEFT JOIN race_master m ON m.race_id = e.race_id
            WHERE e.racer_name IS NOT NULL AND e.racer_name != ''
            UNION ALL
            SELECT r.racer_name, r.class AS latest_class, r.term AS latest_term,
                   m.race_date, r.id, 2 AS source_priority
            FROM race_result r
            LEFT JOIN race_master m ON m.race_id = r.race_id
            WHERE r.racer_name IS NOT NULL AND r.racer_name != ''
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                     PARTITION BY racer_name
                     ORDER BY race_date DESC, source_priority ASC, id DESC
                   ) AS rn
            FROM profile_source
        )
        SELECT racer_name,
               COALESCE(latest_class, '') AS latest_class,
               COALESCE(latest_term, '') AS latest_term
        FROM ranked
        WHERE rn = 1
    """)
    latest_profiles = {row["racer_name"]: row for row in latest_profile_rows}

    def enrich_rows(data):
        for row in data:
            name = row.get("racer_name")
            profile = latest_profiles.get(name, {})
            latest_class = profile.get("latest_class") or ""
            latest_term = profile.get("latest_term") or ""
            row["latest_class"] = latest_class
            row["latest_term"] = latest_term
            row["_data"] = {
                "racer-name": name or "",
                "grade-group": grade_group(latest_class),
                "racer-class": latest_class,
                "racer-term": latest_term,
                "kana-group": kana_group(name),
            }
        return data

    class_values = sorted({row["latest_class"] for row in latest_profile_rows if row.get("latest_class")})
    term_values = sorted(
        {str(row["latest_term"]) for row in latest_profile_rows if row.get("latest_term")},
        key=lambda value: int(value) if str(value).isdigit() else 9999,
        reverse=True,
    )

    base = """
        SELECT racer_name,
               COUNT(*) AS starts,
               SUM(CASE WHEN rank = 1 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN rank <= 2 THEN 1 ELSE 0 END) AS quinella,
               SUM(CASE WHEN rank <= 3 THEN 1 ELSE 0 END) AS top3,
               ROUND(AVG(rank), 2) AS avg_rank,
               ROUND(AVG(CAST(NULLIF(time, '') AS REAL)), 2) AS avg_time,
               ROUND(SUM(CASE WHEN rank = 1 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS win_rate,
               ROUND(SUM(CASE WHEN rank <= 2 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS quinella_rate,
               ROUND(SUM(CASE WHEN rank <= 3 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS top3_rate
        FROM race_result
        WHERE racer_name IS NOT NULL AND racer_name != ''
        GROUP BY racer_name
    """
    starts = enrich_rows(with_rates(rows(conn, base + " ORDER BY starts DESC, racer_name LIMIT 100")))
    wins = enrich_rows(with_rates(rows(conn, "SELECT * FROM (" + base + ") WHERE starts >= ? ORDER BY win_rate DESC, starts DESC LIMIT 100", (threshold,))))
    quinella = enrich_rows(with_rates(rows(conn, "SELECT * FROM (" + base + ") WHERE starts >= ? ORDER BY quinella_rate DESC, starts DESC LIMIT 100", (threshold,))))
    avg_rank = enrich_rows(with_rates(rows(conn, "SELECT * FROM (" + base + ") WHERE starts >= ? ORDER BY avg_rank ASC, starts DESC LIMIT 100", (threshold,))))

    summary_rows = enrich_rows(with_rates(rows(conn, """
        WITH base_result AS (
            SELECT r.racer_name,
                   COUNT(*) AS starts,
                   SUM(CASE WHEN r.rank = 1 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN r.rank <= 2 THEN 1 ELSE 0 END) AS quinella,
                   SUM(CASE WHEN r.rank <= 3 THEN 1 ELSE 0 END) AS top3,
                   ROUND(AVG(r.rank), 2) AS avg_rank,
                   ROUND(SUM(CASE WHEN r.rank = 1 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS win_rate,
                   ROUND(SUM(CASE WHEN r.rank <= 2 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS quinella_rate,
                   ROUND(SUM(CASE WHEN r.rank <= 3 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS top3_rate,
                   MAX(m.race_date) AS latest_race_date
            FROM race_result r
            LEFT JOIN race_master m ON m.race_id = r.race_id
            WHERE r.racer_name IS NOT NULL AND r.racer_name != ''
            GROUP BY r.racer_name
        ),
        main_car AS (
            SELECT racer_name, car_no AS main_car_no
            FROM (
                SELECT racer_name, car_no, COUNT(*) AS cnt,
                       ROW_NUMBER() OVER (PARTITION BY racer_name ORDER BY COUNT(*) DESC, car_no) AS rn
                FROM race_result
                WHERE racer_name IS NOT NULL AND racer_name != '' AND car_no IS NOT NULL
                GROUP BY racer_name, car_no
            )
            WHERE rn = 1
        ),
        main_role AS (
            SELECT racer_name, line_role AS main_line_role
            FROM (
                SELECT racer_name,
                       CASE
                         WHEN is_tanki = 1 THEN '単騎'
                         WHEN line_position = 1 THEN '先頭'
                         WHEN line_position = 2 THEN '番手'
                         ELSE '三番手以降'
                       END AS line_role,
                       COUNT(*) AS cnt,
                       ROW_NUMBER() OVER (
                         PARTITION BY racer_name
                         ORDER BY COUNT(*) DESC,
                                  CASE
                                    WHEN is_tanki = 1 THEN 4
                                    WHEN line_position = 1 THEN 1
                                    WHEN line_position = 2 THEN 2
                                    ELSE 3
                                  END
                       ) AS rn
                FROM race_line_features
                WHERE racer_name IS NOT NULL AND racer_name != ''
                GROUP BY racer_name, line_role
            )
            WHERE rn = 1
        )
        SELECT b.*, COALESCE(c.main_car_no, '') AS main_car_no, COALESCE(l.main_line_role, '') AS main_line_role
        FROM base_result b
        LEFT JOIN main_car c ON c.racer_name = b.racer_name
        LEFT JOIN main_role l ON l.racer_name = b.racer_name
        ORDER BY b.starts DESC, b.racer_name
        LIMIT 300
    """)))

    line_role_rows = enrich_rows(with_rates(rows(conn, """
        SELECT racer_name,
               CASE
                 WHEN is_tanki = 1 THEN '単騎'
                 WHEN line_position = 1 THEN '先頭'
                 WHEN line_position = 2 THEN '番手'
                 ELSE '三番手以降'
               END AS line_role,
               COUNT(*) AS starts,
               SUM(CASE WHEN rank = 1 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN rank <= 2 THEN 1 ELSE 0 END) AS quinella,
               SUM(CASE WHEN rank <= 3 THEN 1 ELSE 0 END) AS top3,
               ROUND(SUM(CASE WHEN rank = 1 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS win_rate,
               ROUND(SUM(CASE WHEN rank <= 2 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS quinella_rate,
               ROUND(SUM(CASE WHEN rank <= 3 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS top3_rate
        FROM race_line_features
        WHERE racer_name IS NOT NULL AND racer_name != '' AND rank IS NOT NULL
        GROUP BY racer_name, line_role
        ORDER BY starts DESC, racer_name, line_position
        LIMIT 300
    """)))

    leader_followers_rows = enrich_rows(with_rates(rows(conn, """
        SELECT racer_name,
               followers,
               COUNT(*) AS starts,
               SUM(CASE WHEN rank = 1 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN rank <= 2 THEN 1 ELSE 0 END) AS quinella,
               SUM(CASE WHEN rank <= 3 THEN 1 ELSE 0 END) AS top3,
               ROUND(SUM(CASE WHEN rank = 1 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS win_rate,
               ROUND(SUM(CASE WHEN rank <= 2 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS quinella_rate,
               ROUND(SUM(CASE WHEN rank <= 3 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS top3_rate
        FROM race_line_features
        WHERE racer_name IS NOT NULL AND racer_name != ''
          AND rank IS NOT NULL
          AND is_tanki = 0
          AND line_position = 1
        GROUP BY racer_name, followers
        ORDER BY starts DESC, racer_name, followers
        LIMIT 300
    """)))

    bunsen_rows = enrich_rows(with_rates(rows(conn, """
        SELECT racer_name,
               bunsen_count,
               COUNT(*) AS starts,
               SUM(CASE WHEN rank = 1 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN rank <= 2 THEN 1 ELSE 0 END) AS quinella,
               SUM(CASE WHEN rank <= 3 THEN 1 ELSE 0 END) AS top3,
               ROUND(SUM(CASE WHEN rank = 1 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS win_rate,
               ROUND(SUM(CASE WHEN rank <= 2 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS quinella_rate,
               ROUND(SUM(CASE WHEN rank <= 3 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS top3_rate
        FROM race_line_features
        WHERE racer_name IS NOT NULL AND racer_name != ''
          AND rank IS NOT NULL
          AND bunsen_count >= 2
        GROUP BY racer_name, bunsen_count
        ORDER BY starts DESC, racer_name, bunsen_count
        LIMIT 300
    """)))

    kimarite = enrich_rows(rows(conn, """
        SELECT racer_name, kimarite, COUNT(*) AS count
        FROM race_result
        WHERE rank = 1 AND kimarite IS NOT NULL AND kimarite != ''
        GROUP BY racer_name, kimarite
        ORDER BY count DESC
        LIMIT 100
    """))

    search = """
        <div class="toolbar">
          <label for="racer-search">選手検索</label>
          <input id="racer-search" type="search" placeholder="選手名・車番・ライン位置で検索" autocomplete="off">
        </div>
        <script>
        (() => {
          const input = document.getElementById("racer-search");
          if (!input) return;
          const filter = () => {
            const keyword = input.value.trim().toLowerCase();
            document.querySelectorAll(".racer-search-table tbody tr").forEach((tr) => {
              tr.style.display = tr.textContent.toLowerCase().includes(keyword) ? "" : "none";
            });
          };
          input.addEventListener("input", filter);
        })();
        </script>
    """

    class_options = "".join(f'<option value="{h(value)}">{h(value)}</option>' for value in class_values)
    term_options = "".join(f'<option value="{h(value)}">{h(value)}期</option>' for value in term_values)
    kana_buttons = "".join(
        f'<button type="button" data-kana="{h(value)}">{h(label)}</button>'
        for value, label in [
            ("", "すべて"),
            ("あ", "あ"),
            ("か", "か"),
            ("さ", "さ"),
            ("た", "た"),
            ("な", "な"),
            ("は", "は"),
            ("ま", "ま"),
            ("や", "や"),
            ("ら", "ら"),
            ("わ", "わ"),
            ("その他", "その他"),
        ]
    )
    search = f"""
        <div class="racer-filter-panel">
          <div class="toolbar">
            <label for="racer-search">選手検索</label>
            <input id="racer-search" type="search" placeholder="選手名・車番・ライン位置で検索" autocomplete="off">
          </div>
          <div class="racer-filter-grid">
            <label>級班
              <select id="racer-grade-group">
                <option value="">すべて</option>
                <option value="S">S級</option>
                <option value="A">A級</option>
                <option value="L">L級</option>
              </select>
            </label>
            <label>詳細級班
              <select id="racer-class">
                <option value="">すべて</option>
                {class_options}
              </select>
            </label>
            <label>期別From
              <select id="racer-term-from">
                <option value="">指定なし</option>
                {term_options}
              </select>
            </label>
            <label>期別To
              <select id="racer-term-to">
                <option value="">すべて</option>
                {term_options}
              </select>
            </label>
          </div>
          <div class="kana-filter" id="racer-kana-filter">
            {kana_buttons}
          </div>
          <div class="selected-racer-control">
            <span id="selected-racer-label">選手単独表示: なし</span>
            <button type="button" id="clear-racer-selection">絞り込み解除</button>
          </div>
        </div>
        <script>
        (() => {{
          const input = document.getElementById("racer-search");
          const gradeGroup = document.getElementById("racer-grade-group");
          const racerClass = document.getElementById("racer-class");
          const termFrom = document.getElementById("racer-term-from");
          const termTo = document.getElementById("racer-term-to");
          const selectedLabel = document.getElementById("selected-racer-label");
          const clearSelection = document.getElementById("clear-racer-selection");
          const kanaButtons = Array.from(document.querySelectorAll("#racer-kana-filter button"));
          if (!input || !gradeGroup || !racerClass || !termFrom || !termTo || !selectedLabel || !clearSelection) return;
          let kana = "";
          let selectedRacer = "";
          const filter = () => {{
            const keyword = input.value.trim().toLowerCase();
            const gradeGroupValue = gradeGroup.value;
            const classValue = racerClass.value;
            const termFromValue = termFrom.value ? Number(termFrom.value) : null;
            const termToValue = termTo.value ? Number(termTo.value) : null;
            const termMin = termFromValue !== null && termToValue !== null ? Math.min(termFromValue, termToValue) : termFromValue;
            const termMax = termFromValue !== null && termToValue !== null ? Math.max(termFromValue, termToValue) : termToValue;
            document.querySelectorAll(".racer-search-table tbody tr").forEach((tr) => {{
              const rowTerm = tr.dataset.racerTerm ? Number(tr.dataset.racerTerm) : null;
              const matchesKeyword = tr.textContent.toLowerCase().includes(keyword);
              const matchesGradeGroup = !gradeGroupValue || tr.dataset.gradeGroup === gradeGroupValue;
              const matchesClass = !classValue || tr.dataset.racerClass === classValue;
              const matchesTermFrom = termMin === null || (rowTerm !== null && rowTerm >= termMin);
              const matchesTermTo = termMax === null || (rowTerm !== null && rowTerm <= termMax);
              const matchesKana = !kana || tr.dataset.kanaGroup === kana;
              const matchesSelectedRacer = !selectedRacer || tr.dataset.racerName === selectedRacer;
              tr.style.display = matchesKeyword && matchesGradeGroup && matchesClass && matchesTermFrom && matchesTermTo && matchesKana && matchesSelectedRacer ? "" : "none";
            }});
            selectedLabel.textContent = selectedRacer
              ? "選手単独表示: " + selectedRacer
              : "選手単独表示: なし";
          }};
          input.addEventListener("input", filter);
          gradeGroup.addEventListener("change", filter);
          racerClass.addEventListener("change", filter);
          termFrom.addEventListener("change", filter);
          termTo.addEventListener("change", filter);
          kanaButtons.forEach((button) => {{
            button.addEventListener("click", () => {{
              kana = button.dataset.kana || "";
              kanaButtons.forEach((item) => item.classList.toggle("active", item === button));
              filter();
            }});
          }});
          document.addEventListener("click", (event) => {{
            const tr = event.target.closest(".racer-search-table tbody tr");
            if (!tr) return;
            selectedRacer = tr.dataset.racerName || "";
            if (selectedRacer) input.value = selectedRacer;
            filter();
          }});
          clearSelection.addEventListener("click", () => {{
            selectedRacer = "";
            input.value = "";
            gradeGroup.value = "";
            racerClass.value = "";
            termFrom.value = "";
            termTo.value = "";
            kana = "";
            kanaButtons.forEach((item, index) => item.classList.toggle("active", index === 0));
            filter();
          }});
          if (kanaButtons[0]) kanaButtons[0].classList.add("active");
        }})();
        </script>
    """

    body = f'<div class="inline-note">初期表示は出走数を重視します。勝率・連対率ランキングは出走{threshold}回以上に限定し、ライン条件別の表では出走30未満をサンプル不足として表示します。</div>'
    body += section("選手検索", search)
    body += section("選手別サマリー", searchable_table(
        ["選手", "最新級班", "期別", "主な車番", "主なライン位置", "出走", "1着", "2連対", "3連対", "勝率", "2連対率", "3連対率", "平均着順", "最終日付", "注意"],
        summary_rows,
        ["racer_name", "latest_class", "latest_term", "main_car_no", "main_line_role", "starts", "wins", "quinella", "top3", "win_rate_display", "quinella_rate_display", "top3_rate_display", "avg_rank_display", "latest_race_date", "sample_warning"],
    ), "選手名で絞り込むと、下のライン条件別テーブルも同時に絞り込まれます。")
    body += section("ライン位置別成績", searchable_table(
        ["選手", "ライン位置", "出走", "1着", "2連対", "3連対", "勝率", "2連対率", "3連対率", "注意"],
        line_role_rows,
        ["racer_name", "line_role", "starts", "wins", "quinella", "top3", "win_rate_display", "quinella_rate_display", "top3_rate_display", "sample_warning"],
    ), "先頭・番手・三番手以降・単騎で、その選手がどの役割で成績を出しているかを確認します。")
    body += section("先頭時 後続人数別成績", searchable_table(
        ["選手", "後続人数", "出走", "1着", "2連対", "3連対", "勝率", "2連対率", "3連対率", "注意"],
        leader_followers_rows,
        ["racer_name", "followers", "starts", "wins", "quinella", "top3", "win_rate_display", "quinella_rate_display", "top3_rate_display", "sample_warning"],
    ), "先頭選手に限定し、後ろに何人いる時に強いかを確認します。")
    body += section("分線数別成績", searchable_table(
        ["選手", "分線数", "出走", "1着", "2連対", "3連対", "勝率", "2連対率", "3連対率", "注意"],
        bunsen_rows,
        ["racer_name", "bunsen_count", "starts", "wins", "quinella", "top3", "win_rate_display", "quinella_rate_display", "top3_rate_display", "sample_warning"],
    ), "単騎を除いたライン数が2以上のレースだけを対象にしています。")
    body += section("選手別出走数ランキング", searchable_table(
        ["選手", "出走", "1着", "2連対", "3着内", "平均着順", "平均タイム"],
        starts,
        ["racer_name", "starts", "wins", "quinella", "top3", "avg_rank_display", "avg_time_display"],
    ), "まずデータ量を確認するためのランキングです。出走数が増えるほど勝率や平均着順の信頼度が上がります。")
    body += '<div class="grid two">'
    body += section("選手別勝率", bar_chart(wins, "racer_name", "win_rate", pct, 20))
    body += section("選手別平均着順", bar_chart(list(reversed(avg_rank[:20])), "racer_name", "avg_rank", lambda v: f"{v:.2f}"))
    body += "</div>"
    body += section("選手別勝率ランキング", searchable_table(
        ["選手", "出走", "勝率", "1着", "2連対率", "3着内率", "平均着順"],
        wins,
        ["racer_name", "starts", "win_rate_display", "wins", "quinella_rate_display", "top3_rate_display", "avg_rank_display"],
    ), f"出走{threshold}回以上のみ。出走1回の勝率100%は初期表示から外しています。")
    body += section("選手別連対率ランキング", searchable_table(
        ["選手", "出走", "2連対率", "2連対", "勝率", "平均着順"],
        quinella,
        ["racer_name", "starts", "quinella_rate_display", "quinella", "win_rate_display", "avg_rank_display"],
    ))
    body += section("選手別決まり手ランキング", searchable_table(
        ["選手", "決まり手", "回数"],
        kimarite,
        ["racer_name", "kimarite", "count"],
    ))
    return page("選手分析", "racers", body)


def render_racers_legacy(conn) -> str:
    threshold = max(3, racer_threshold(conn))
    base = """
        SELECT racer_name,
               COUNT(*) AS starts,
               SUM(CASE WHEN rank = 1 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN rank <= 2 THEN 1 ELSE 0 END) AS quinella,
               SUM(CASE WHEN rank <= 3 THEN 1 ELSE 0 END) AS top3,
               ROUND(AVG(rank), 2) AS avg_rank,
               ROUND(AVG(CAST(NULLIF(time, '') AS REAL)), 2) AS avg_time,
               ROUND(SUM(CASE WHEN rank = 1 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS win_rate,
               ROUND(SUM(CASE WHEN rank <= 2 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS quinella_rate,
               ROUND(SUM(CASE WHEN rank <= 3 THEN 100.0 ELSE 0 END) / COUNT(*), 1) AS top3_rate
        FROM race_result
        WHERE racer_name IS NOT NULL AND racer_name != ''
        GROUP BY racer_name
    """
    starts = rows(conn, base + " ORDER BY starts DESC, racer_name LIMIT 100")
    wins = rows(conn, "SELECT * FROM (" + base + ") WHERE starts >= ? ORDER BY win_rate DESC, starts DESC LIMIT 100", (threshold,))
    quinella = rows(conn, "SELECT * FROM (" + base + ") WHERE starts >= ? ORDER BY quinella_rate DESC, starts DESC LIMIT 100", (threshold,))
    avg_rank = rows(conn, "SELECT * FROM (" + base + ") WHERE starts >= ? ORDER BY avg_rank ASC, starts DESC LIMIT 100", (threshold,))
    kimarite = rows(conn, """
        SELECT racer_name, kimarite, COUNT(*) AS count
        FROM race_result
        WHERE rank = 1 AND kimarite IS NOT NULL AND kimarite != ''
        GROUP BY racer_name, kimarite
        ORDER BY count DESC
        LIMIT 100
    """)
    for collection in (starts, wins, quinella, avg_rank):
        for row in collection:
            row["win_rate_display"] = pct(row["win_rate"])
            row["quinella_rate_display"] = pct(row["quinella_rate"])
            row["top3_rate_display"] = pct(row["top3_rate"])
            row["avg_rank_display"] = decimal(row["avg_rank"])
            row["avg_time_display"] = decimal(row["avg_time"])

    body = f'<div class="inline-note">現在は多くの選手が出走1〜2回中心のため、初期表示は実力比較ではなく出走数ランキングを優先します。勝率・連対率ランキングは出走{threshold}回以上に限定しています。</div>'
    body += section("選手別出走数ランキング", table(
        ["選手", "出走", "1着", "連対", "3着内", "平均着順", "平均タイム"],
        starts,
        ["racer_name", "starts", "wins", "quinella", "top3", "avg_rank_display", "avg_time_display"],
    ), "まずデータ量を確認するためのランキングです。出走数が増えるほど勝率や平均着順の信頼度が上がります。")
    body += '<div class="grid two">'
    body += section("選手別勝率", bar_chart(wins, "racer_name", "win_rate", pct, 20))
    body += section("選手別平均着順", bar_chart(list(reversed(avg_rank[:20])), "racer_name", "avg_rank", lambda v: f"{v:.2f}"))
    body += "</div>"
    body += section("選手別勝率ランキング", table(
        ["選手", "出走", "勝率", "1着", "連対率", "3着内率", "平均着順"],
        wins,
        ["racer_name", "starts", "win_rate_display", "wins", "quinella_rate_display", "top3_rate_display", "avg_rank_display"],
    ), f"出走{threshold}回以上のみ。出走1回の勝率100%は初期表示から外しています。")
    body += section("選手別連対率ランキング", table(
        ["選手", "出走", "連対率", "連対", "勝率", "平均着順"],
        quinella,
        ["racer_name", "starts", "quinella_rate_display", "quinella", "win_rate_display", "avg_rank_display"],
    ))
    body += section("選手別決まり手ランキング", table(
        ["選手", "決まり手", "回数"],
        kimarite,
        ["racer_name", "kimarite", "count"],
    ))
    return page("選手分析", "racers", body)


def render_races(conn) -> str:
    race_rows = rows(conn, """
        SELECT m.race_id, m.race_date, m.venue, m.race_no, m.event_name, m.race_title,
               m.race_class, m.start_time, m.distance, m.weather,
               CASE WHEN m.wind_speed IS NULL THEN '' ELSE m.wind_speed || 'm/s' END AS wind_speed,
               (
                 SELECT r.car_no
                 FROM race_result r
                 WHERE r.race_id = m.race_id AND r.rank = 1
                 ORDER BY r.id
                 LIMIT 1
               ) AS winner_car,
               (
                 SELECT r.racer_name
                 FROM race_result r
                 WHERE r.race_id = m.race_id AND r.rank = 1
                 ORDER BY r.id
                 LIMIT 1
               ) AS winner,
               p.payout AS trifecta_payout,
               m.lineup_text,
               (
                 SELECT GROUP_CONCAT(r.car_no, ' ')
                 FROM race_result r
                 WHERE r.race_id = m.race_id AND r.car_no IS NOT NULL
               ) AS result_car_nos
        FROM race_master m
        LEFT JOIN payout p ON p.race_id = m.race_id AND p.bet_type = ?
        ORDER BY m.race_date DESC, m.venue, m.race_no
        LIMIT 500
    """, (TRIFECTA,))
    venues = sorted({row["venue"] for row in race_rows if row.get("venue")})
    for row in race_rows:
        row["trifecta_payout_raw"] = row["trifecta_payout"] or 0
        row["trifecta_payout"] = yen(row["trifecta_payout"])
        row["distance_display"] = "" if row["distance"] is None else f'{row["distance"]}m'
        row["lineup_text"] = format_lineup_text(row.get("lineup_text"), row.get("result_car_nos"))
        row["detail"] = race_detail_link(row.get("race_id"))

    venue_options = "".join(f'<option value="{h(venue)}">{h(venue)}</option>' for venue in venues)
    header = "".join(f"<th>{h(label)}</th>" for label in ["詳細", "日付", "会場", "R", "開催", "レース名", "発走", "3連単配当", "1着車番", "1着選手", "並び"])
    body_rows = ""
    for row in race_rows:
        body_rows += (
            f'<tr data-date="{h(row.get("race_date"))}" data-venue="{h(row.get("venue"))}" data-payout="{h(row.get("trifecta_payout_raw"))}">'
            f'<td>{row["detail"]}</td>'
            f'<td>{h(row.get("race_date"))}</td>'
            f'<td>{h(row.get("venue"))}</td>'
            f'<td>{h(row.get("race_no"))}</td>'
            f'<td>{h(row.get("event_name"))}</td>'
            f'<td>{h(row.get("race_title"))}</td>'
            f'<td>{h(row.get("start_time"))}</td>'
            f'<td>{h(row.get("trifecta_payout"))}</td>'
            f'<td>{h(row.get("winner_car"))}</td>'
            f'<td>{h(row.get("winner"))}</td>'
            f'<td>{h(row.get("lineup_text"))}</td>'
            "</tr>"
        )
    race_table = f"""
      <div class="filters">
        <label>日付<input id="race-filter-date" type="date"></label>
        <label>会場<select id="race-filter-venue"><option value="">すべて</option>{venue_options}</select></label>
        <label>3連単配当<input id="race-filter-payout" type="number" min="0" step="1000" placeholder="下限なし"></label>
        <label class="check"><input id="race-filter-high" type="checkbox">万車券のみ</label>
      </div>
      <table id="race-list-table"><thead><tr>{header}</tr></thead><tbody>{body_rows}</tbody></table>
      <script>
      (() => {{
        const table = document.getElementById("race-list-table");
        const date = document.getElementById("race-filter-date");
        const venue = document.getElementById("race-filter-venue");
        const payout = document.getElementById("race-filter-payout");
        const high = document.getElementById("race-filter-high");
        const apply = () => {{
          const minPayout = Number(payout.value || 0);
          for (const row of table.tBodies[0].rows) {{
            const rowPayout = Number(row.dataset.payout || 0);
            const okDate = !date.value || row.dataset.date === date.value;
            const okVenue = !venue.value || row.dataset.venue === venue.value;
            const okPayout = !minPayout || rowPayout >= minPayout;
            const okHigh = !high.checked || rowPayout >= 10000;
            row.style.display = okDate && okVenue && okPayout && okHigh ? "" : "none";
          }}
        }};
        [date, venue, payout, high].forEach((item) => item.addEventListener("input", apply));
        [venue, high].forEach((item) => item.addEventListener("change", apply));
      }})();
      </script>
    """
    body = section("取得済みレース一覧", race_table, "最新500レースを表示します。日付・会場・3連単配当・万車券のみで絞り込めます。")
    return page("レース一覧", "races", body)


def render_quality(conn) -> str:
    rendered = LOG_DIR / "results_rendered.html"
    log_file = LOG_DIR / "collector.log"
    html_saved_at = "-"
    if rendered.exists():
        html_saved_at = datetime.fromtimestamp(rendered.stat().st_mtime).isoformat(timespec="seconds")
    log_tail = []
    if log_file.exists():
        log_tail = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
    metrics = [
        {"name": "総レース数", "value": number(scalar(conn, "SELECT COUNT(*) FROM race_master"))},
        {"name": "着順0件のレース", "value": number(scalar(conn, """
            SELECT COUNT(*) FROM race_master m
            WHERE NOT EXISTS (SELECT 1 FROM race_result r WHERE r.race_id = m.race_id)
        """))},
        {"name": "配当0件のレース", "value": number(scalar(conn, """
            SELECT COUNT(*) FROM race_master m
            WHERE NOT EXISTS (SELECT 1 FROM payout p WHERE p.race_id = m.race_id)
        """))},
        {"name": "天候未取得", "value": number(scalar(conn, "SELECT COUNT(*) FROM race_master WHERE weather IS NULL OR weather = ''"))},
        {"name": "風速未取得", "value": number(scalar(conn, "SELECT COUNT(*) FROM race_master WHERE wind_speed IS NULL"))},
        {"name": "並び未取得", "value": number(scalar(conn, "SELECT COUNT(*) FROM race_master WHERE lineup_text IS NULL OR lineup_text = ''"))},
        {"name": "Playwright HTML保存日時", "value": html_saved_at},
    ]
    venue_counts = rows(conn, """
        SELECT venue, COUNT(*) AS races, MIN(race_date) AS first_date, MAX(race_date) AS latest_date
        FROM race_master
        GROUP BY venue
        ORDER BY races DESC, venue
    """)
    log_rows = [{"line": line} for line in log_tail]
    body = section("データ品質サマリー", table(["項目", "値"], metrics, ["name", "value"]))
    body += section("会場別蓄積状況", table(
        ["会場", "レース数", "最初の日付", "最新日付"],
        venue_counts,
        ["venue", "races", "first_date", "latest_date"],
    ))
    body += section("直近ログ", table(["ログ"], log_rows, ["line"]))
    return page("データ品質", "quality", body)


def render_custom(conn) -> str:
    upset = rows(conn, """
        SELECT m.race_date, m.venue, m.race_no, r.racer_name, r.rank,
               p.popularity, (p.popularity - r.rank) AS score
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        JOIN payout p ON p.race_id = r.race_id AND p.bet_type = ?
        WHERE p.popularity IS NOT NULL AND r.rank <= 3
        ORDER BY score DESC
        LIMIT 100
    """, (TRIFECTA,))
    fade = rows(conn, """
        SELECT m.race_date, m.venue, m.race_no, r.racer_name, r.rank,
               p.popularity, (r.rank - p.popularity) AS score
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        JOIN payout p ON p.race_id = r.race_id AND p.bet_type = ?
        WHERE p.popularity IS NOT NULL
        ORDER BY score DESC
        LIMIT 100
    """, (TRIFECTA,))
    growth = growth_index(conn)
    yearly = rows(conn, """
        SELECT racer_name, strftime('%Y', m.race_date) AS year, COUNT(*) AS starts
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        WHERE racer_name IS NOT NULL AND racer_name != ''
        GROUP BY racer_name, year
        ORDER BY starts DESC
        LIMIT 100
    """)
    body = '<div class="note">人気順位は3連単配当の人気を使った代理指標です。個別選手人気ではないため、参考ランキングとして扱います。</div>'
    body += section("ヘテオジマーベリック指数", table(
        ["日付", "会場", "R", "選手", "着順", "人気", "指数"],
        upset,
        ["race_date", "venue", "race_no", "racer_name", "rank", "popularity", "score"],
    ))
    body += section("感情ブヒー指数", table(
        ["日付", "会場", "R", "選手", "着順", "人気", "指数"],
        fade,
        ["race_date", "venue", "race_no", "racer_name", "rank", "popularity", "score"],
    ))
    body += section("達成パオーン指数", table(
        ["選手", "直近20走", "過去20走", "指数"],
        growth,
        ["racer_name", "recent_avg", "past_avg", "score"],
    ))
    body += section("行動ヒヒーン指数", table(
        ["選手", "年", "出走数"],
        yearly,
        ["racer_name", "year", "starts"],
    ))
    return page("独自ランキング", "custom", body)


def payout_histogram(conn) -> list[dict]:
    bins = [
        (0, 999, "999円以下"),
        (1000, 2999, "1,000-2,999円"),
        (3000, 4999, "3,000-4,999円"),
        (5000, 9999, "5,000-9,999円"),
        (10000, 29999, "10,000-29,999円"),
        (30000, 99999, "30,000-99,999円"),
        (100000, None, "100,000円以上"),
    ]
    values = [
        row[0]
        for row in conn.execute("SELECT payout FROM payout WHERE bet_type = ? AND payout IS NOT NULL", (TRIFECTA,))
    ]
    output = []
    for low, high, label in bins:
        if high is None:
            count = sum(1 for value in values if value >= low)
        else:
            count = sum(1 for value in values if low <= value <= high)
        if low < 10000:
            zone = pill("低配当ゾーン", "ok")
        elif low < 100000:
            zone = pill("万車券ゾーン", "warn")
        else:
            zone = pill("大荒れゾーン", "low")
        output.append({"range": label, "count": count, "zone": zone})
    return output


def car_recovery(conn) -> list[dict]:
    starts = {
        row["car_no"]: row["starts"]
        for row in conn.execute("""
            SELECT car_no, COUNT(*) AS starts
            FROM race_result
            WHERE car_no IS NOT NULL
            GROUP BY car_no
        """)
    }
    returns = defaultdict(int)
    for row in conn.execute("SELECT combination, payout FROM payout WHERE bet_type = '2車単'"):
        first_car = str(row["combination"]).split("-")[0]
        if first_car.isdigit():
            returns[int(first_car)] += int(row["payout"] or 0)
    output = []
    for car_no in range(1, 10):
        race_count = starts.get(car_no, 0)
        investment = race_count * 100
        recovery_rate = (returns[car_no] / investment * 100) if investment else 0
        output.append({
            "_class": sample_class(race_count, 50),
            "car_no": car_no,
            "races": race_count,
            "return_total": yen(returns[car_no]),
            "investment": yen(investment),
            "recovery_rate": pct(recovery_rate),
            "sample_note": pill("母数不足", "warn") if race_count < 50 else pill("通常", "ok"),
        })
    return output


def racer_threshold(conn) -> int:
    enough_30 = scalar(conn, """
        SELECT COUNT(*) FROM (
            SELECT racer_name
            FROM race_result
            GROUP BY racer_name
            HAVING COUNT(*) >= 30
        )
    """)
    if enough_30 >= 10:
        return 30
    enough_3 = scalar(conn, """
        SELECT COUNT(*) FROM (
            SELECT racer_name
            FROM race_result
            GROUP BY racer_name
            HAVING COUNT(*) >= 3
        )
    """)
    return 3 if enough_3 >= 10 else 1


def growth_index(conn) -> list[dict]:
    by_racer = defaultdict(list)
    for row in conn.execute("""
        SELECT r.racer_name, r.rank
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        WHERE r.racer_name IS NOT NULL AND r.racer_name != ''
        ORDER BY r.racer_name, m.race_date, m.race_no
    """):
        by_racer[row["racer_name"]].append(row["rank"])
    rankings = []
    for racer_name, ranks in by_racer.items():
        if len(ranks) < 40:
            continue
        recent = ranks[-20:]
        past = ranks[-40:-20]
        recent_avg = statistics.mean(recent)
        past_avg = statistics.mean(past)
        score = past_avg - recent_avg
        rankings.append({
            "racer_name": racer_name,
            "recent_avg": f"{recent_avg:.2f}",
            "past_avg": f"{past_avg:.2f}",
            "score": f"{score:.2f}",
        })
    rankings.sort(key=lambda row: float(row["score"]), reverse=True)
    return rankings[:100]


def custom_race_rows(conn) -> list[dict]:
    race_rows = rows(conn, """
        WITH winners AS (
            SELECT race_id, car_no, racer_name, time
            FROM (
                SELECT race_id, car_no, racer_name, time,
                       ROW_NUMBER() OVER (PARTITION BY race_id ORDER BY id) AS row_no
                FROM race_result
                WHERE rank = 1
            )
            WHERE row_no = 1
        ),
        trifecta AS (
            SELECT race_id, payout, popularity
            FROM payout
            WHERE bet_type = ?
        ),
        winner_line AS (
            SELECT l.race_id, l.car_no, l.line_no, l.line_position
            FROM race_lineup l
            JOIN winners w ON w.race_id = l.race_id AND w.car_no = l.car_no
        )
        SELECT m.race_id, m.race_date, m.venue, m.race_no, m.race_title,
               m.start_time, m.weather,
               CASE WHEN m.wind_speed IS NULL THEN '' ELSE m.wind_speed || 'm/s' END AS wind_speed,
               w.car_no AS winner_car, w.racer_name AS winner, w.time AS winner_time,
               t.payout AS trifecta_payout, t.popularity,
               wl.line_no, wl.line_position,
               ROUND(
                   COALESCE(t.payout, 0) / 1000.0
                   + COALESCE(t.popularity, 0) * 2.0
                   + CASE WHEN w.car_no >= 6 THEN 12 ELSE 0 END
                   + CASE WHEN wl.line_position >= 3 THEN 10 ELSE 0 END,
                   1
               ) AS surprise_score
        FROM race_master m
        LEFT JOIN winners w ON w.race_id = m.race_id
        LEFT JOIN trifecta t ON t.race_id = m.race_id
        LEFT JOIN winner_line wl ON wl.race_id = m.race_id
        ORDER BY surprise_score DESC, t.payout DESC
        LIMIT 100
    """, (TRIFECTA,))
    for row in race_rows:
        row["detail"] = race_detail_link(row["race_id"])
        row["trifecta_payout"] = yen(row["trifecta_payout"])
        row["line_position"] = "" if row["line_position"] is None else row["line_position"]
        row["popularity"] = "" if row["popularity"] is None else row["popularity"]
        row["surprise_score"] = decimal(row["surprise_score"], 1)
    return race_rows


def render_custom_v2(conn) -> str:
    s = summary(conn)
    min_starts = racer_threshold(conn)
    target_note = "データが少ない間は参考値です。選手系ランキングは蓄積量に応じて最低出走数を自動調整します。"
    high_payout_races = scalar(conn, "SELECT COUNT(*) FROM payout WHERE bet_type = ? AND payout >= 10000", (TRIFECTA,))
    avg_surprise = scalar(conn, """
        WITH race_scores AS (
            SELECT m.race_id,
                   COALESCE(p.payout, 0) / 1000.0
                   + COALESCE(p.popularity, 0) * 2.0
                   + COALESCE((SELECT CASE WHEN r.car_no >= 6 THEN 12 ELSE 0 END FROM race_result r WHERE r.race_id = m.race_id AND r.rank = 1 LIMIT 1), 0) AS score
            FROM race_master m
            LEFT JOIN payout p ON p.race_id = m.race_id AND p.bet_type = ?
        )
        SELECT ROUND(AVG(score), 1) FROM race_scores
    """, (TRIFECTA,))
    daily_high = rows(conn, """
        SELECT m.race_date, COUNT(*) AS count
        FROM payout p
        JOIN race_master m ON m.race_id = p.race_id
        WHERE p.bet_type = ? AND p.payout >= 10000
        GROUP BY m.race_date
        ORDER BY m.race_date DESC
        LIMIT 30
    """, (TRIFECTA,))
    venue_surprise = rows(conn, """
        WITH winners AS (
            SELECT race_id, car_no
            FROM (
                SELECT race_id, car_no,
                       ROW_NUMBER() OVER (PARTITION BY race_id ORDER BY id) AS row_no
                FROM race_result
                WHERE rank = 1
            )
            WHERE row_no = 1
        )
        SELECT m.venue,
               COUNT(*) AS races,
               ROUND(AVG(COALESCE(p.payout, 0) / 1000.0 + COALESCE(p.popularity, 0) * 2.0 + CASE WHEN w.car_no >= 6 THEN 12 ELSE 0 END), 1) AS score
        FROM race_master m
        LEFT JOIN payout p ON p.race_id = m.race_id AND p.bet_type = ?
        LEFT JOIN winners w ON w.race_id = m.race_id
        GROUP BY m.venue
        ORDER BY score DESC
        LIMIT 20
    """, (TRIFECTA,))
    car_surprise = rows(conn, """
        SELECT r.car_no,
               COUNT(*) AS wins,
               ROUND(AVG(COALESCE(p.payout, 0)), 0) AS avg_payout
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        LEFT JOIN payout p ON p.race_id = r.race_id AND p.bet_type = ?
        WHERE r.rank = 1 AND r.car_no IS NOT NULL
        GROUP BY r.car_no
        ORDER BY avg_payout DESC
    """, (TRIFECTA,))
    for row in car_surprise:
        row["car_label"] = f'{row["car_no"]}番'
    race_rankings = custom_race_rows(conn)
    top_performers = rows(conn, """
        SELECT r.racer_name,
               COUNT(*) AS starts,
               SUM(CASE WHEN r.rank = 1 THEN 1 ELSE 0 END) AS wins,
               ROUND(AVG(r.rank), 2) AS avg_rank,
               ROUND(SUM(CASE WHEN r.rank <= 3 THEN COALESCE(p.popularity, 0) - r.rank ELSE 0 END), 1) AS score
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        LEFT JOIN payout p ON p.race_id = r.race_id AND p.bet_type = ?
        WHERE r.racer_name IS NOT NULL AND r.racer_name != ''
        GROUP BY r.racer_name
        HAVING COUNT(*) >= ?
        ORDER BY score DESC, starts DESC
        LIMIT 100
    """, (TRIFECTA, min_starts))
    fade_rows = rows(conn, """
        SELECT r.racer_name,
               COUNT(*) AS starts,
               ROUND(AVG(r.rank), 2) AS avg_rank,
               ROUND(SUM(CASE WHEN r.rank > 3 THEN r.rank - COALESCE(p.popularity, 0) ELSE 0 END), 1) AS score
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        LEFT JOIN payout p ON p.race_id = r.race_id AND p.bet_type = ?
        WHERE r.racer_name IS NOT NULL AND r.racer_name != ''
        GROUP BY r.racer_name
        HAVING COUNT(*) >= ?
        ORDER BY score DESC, starts DESC
        LIMIT 100
    """, (TRIFECTA, min_starts))
    growth = growth_index(conn)
    yearly = rows(conn, """
        SELECT racer_name, strftime('%Y', m.race_date) AS year, COUNT(*) AS starts
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        WHERE racer_name IS NOT NULL AND racer_name != ''
        GROUP BY racer_name, year
        ORDER BY starts DESC
        LIMIT 100
    """)
    body = f"""
    <div class="grid">
      <div class="card"><span>対象レース数</span><strong>{h(number(s["races"]))}</strong></div>
      <div class="card"><span>対象選手数</span><strong>{h(number(s["racers"]))}</strong></div>
      <div class="card"><span>万車券レース</span><strong>{h(number(high_payout_races))}</strong></div>
      <div class="card"><span>平均サプライズ</span><strong>{h(decimal(avg_surprise, 1))}</strong></div>
      <div class="card"><span>対象期間</span><strong>{h((s["first_race_date"] or "-") + " - " + (s["latest_race_date"] or "-"))}</strong></div>
      <div class="card"><span>選手ランキング条件</span><strong>{h(str(min_starts) + "走以上")}</strong></div>
      <div class="card"><span>3連単最高配当</span><strong>{h(yen(s["trifecta_max"]))}</strong></div>
      <div class="card"><span>最新更新</span><strong>{h(s["latest_created"] or "-")}</strong></div>
    </div>
    <div class="rank-note">{h(target_note)}</div>
    """
    body += '<div class="grid two">'
    body += section("日別万車券件数", bar_chart(list(reversed(daily_high)), "race_date", "count", lambda v: f"{int(v)}件", 30))
    body += section("会場別サプライズ指数", bar_chart(venue_surprise, "venue", "score", lambda v: f"{v:.1f}", 20))
    body += "</div>"
    body += section("1着車番別 平均3連単配当", bar_chart(car_surprise, "car_label", "avg_payout", yen, 9))
    body += section("注目レース TOP100", accordion_table(
        ["詳細", "日付", "会場", "R", "レース名", "発走", "1着車番", "1着選手", "3連単", "人気", "並び位置", "指数"],
        race_rankings,
        ["detail", "race_date", "venue", "race_no", "race_title", "start_time", "winner_car", "winner", "trifecta_payout", "popularity", "line_position", "surprise_score"],
        rich=True,
    ), "配当、3連単人気、1着車番、並び位置を組み合わせ、荒れたレースや見返したいレースを上位に出します。")
    body += section("人気を覆した選手", accordion_table(
        ["選手", "出走", "1着", "平均着順", "指数"],
        top_performers,
        ["racer_name", "starts", "wins", "avg_rank", "score"],
    ), "3着内に入ったレースで、3連単人気に対して着順が良かった選手を集計します。")
    body += section("人気倒れ傾向", accordion_table(
        ["選手", "出走", "平均着順", "指数"],
        fade_rows,
        ["racer_name", "starts", "avg_rank", "score"],
    ), "3連単人気を代理指標として、着順が伸びなかったケースを集計します。")
    body += '<div class="grid two">'
    body += section("急成長ランキング", accordion_table(
        ["選手", "直近20走", "過去20走", "指数"],
        growth,
        ["racer_name", "recent_avg", "past_avg", "score"],
    ), "40走以上たまると、直近20走と過去20走の平均着順差で表示します。")
    body += section("継続力ランキング", accordion_table(
        ["選手", "年", "出走数"],
        yearly,
        ["racer_name", "year", "starts"],
    ))
    body += "</div>"
    return page("独自分析", "custom", body)


def render_race_detail(conn, race_id: str) -> str:
    master = rows(conn, "SELECT * FROM race_master WHERE race_id = ?", (race_id,))
    if not master:
        return page("レース詳細", "races", section("レース詳細", '<div class="empty">データがありません</div>'))
    race = master[0]
    result_rows = rows(conn, """
        SELECT rank, car_no, racer_name, class, prefecture, age, term, margin,
               time, kimarite, start_mark, back_mark
        FROM race_result
        WHERE race_id = ?
        ORDER BY rank IS NULL, rank, car_no
    """, (race_id,))
    rank_counts = defaultdict(int)
    for row in result_rows:
        rank_counts[row.get("rank")] += 1
    for row in result_rows:
        rank = row.get("rank")
        row["rank_display"] = f"{rank}（同着）" if rank is not None and rank_counts[rank] > 1 else rank
    payout_rows = rows(conn, """
        SELECT bet_type, combination, payout, popularity
        FROM payout
        WHERE race_id = ?
        ORDER BY id
    """, (race_id,))
    lineup_rows = rows(conn, """
        SELECT line_no, line_position, car_no
        FROM race_lineup
        WHERE race_id = ?
        ORDER BY line_no, line_position, car_no
    """, (race_id,))
    for row in payout_rows:
        row["payout"] = yen(row["payout"])
    body = f"""
    <div class="grid">
      <div class="card"><span>日付</span><strong>{h(race["race_date"])}</strong></div>
      <div class="card"><span>会場</span><strong>{h(race["venue"])}</strong></div>
      <div class="card"><span>レース</span><strong>{h(str(race["race_no"]) + "R")}</strong></div>
      <div class="card"><span>発走</span><strong>{h(race["start_time"] or "-")}</strong></div>
      <div class="card"><span>距離</span><strong>{h((str(race["distance"]) + "m") if race["distance"] else "-")}</strong></div>
      <div class="card"><span>天候</span><strong>{h(race["weather"] or "-")}</strong></div>
      <div class="card"><span>風速</span><strong>{h((str(race["wind_speed"]) + "m/s") if race["wind_speed"] is not None else "-")}</strong></div>
      <div class="card"><span>級班</span><strong>{h(race["race_class"] or "-")}</strong></div>
    </div>
    """
    body += section("レース情報", table(
        ["項目", "値"],
        [
            {"name": "開催", "value": race["event_name"]},
            {"name": "レース名", "value": race["race_title"]},
            {"name": "締切", "value": race["deadline_time"]},
            {"name": "状態", "value": race["status"]},
            {"name": "周回", "value": race["laps"]},
            {"name": "気温", "value": "" if race["temperature"] is None else f'{race["temperature"]}℃'},
            {"name": "風向", "value": race["wind_direction"]},
            {"name": "並び", "value": race["lineup_text"]},
            {"name": "コメント", "value": race["race_comment"]},
        ],
        ["name", "value"],
    ))
    body += '<div class="grid two">'
    body += section("着順", table(
        ["着順", "車番", "選手", "級班", "府県", "年齢", "期", "着差", "上り", "決まり手", "S", "B"],
        result_rows,
        ["rank_display", "car_no", "racer_name", "class", "prefecture", "age", "term", "margin", "time", "kimarite", "start_mark", "back_mark"],
    ))
    body += section("払戻", table(
        ["賭式", "組番", "払戻", "人気"],
        payout_rows,
        ["bet_type", "combination", "payout", "popularity"],
    ))
    body += "</div>"
    body += section("並び詳細", table(
        ["ライン", "位置", "車番"],
        lineup_rows,
        ["line_no", "line_position", "car_no"],
    ))
    return page(f'{race["race_date"]} {race["venue"]} {race["race_no"]}R', "races", body)


def normalize_compact_date(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("-", "")


def default_detail_dates(conn) -> set[str]:
    latest_date = scalar(conn, "SELECT MAX(race_date) FROM race_master")
    compact_date = normalize_compact_date(latest_date)
    return {compact_date} if compact_date else set()


def race_detail_payloads(conn, target_dates: set[str] | None = None) -> dict[str, list[dict]]:
    payloads: dict[str, list[dict]] = defaultdict(list)
    masters = rows(conn, "SELECT * FROM race_master ORDER BY race_date DESC, venue, race_no")
    for race in masters:
        race_id = race["race_id"]
        compact_date = str(race_id).split("_", 1)[0]
        if target_dates is not None and compact_date not in target_dates:
            continue
        detail_results = rows(conn, """
            SELECT rank, car_no, racer_name, class, prefecture, age, term, margin,
                   time, kimarite, start_mark, back_mark
            FROM race_result
            WHERE race_id = ?
            ORDER BY rank IS NULL, rank, car_no
        """, (race_id,))
        rank_counts = defaultdict(int)
        for row in detail_results:
            rank_counts[row.get("rank")] += 1
        for row in detail_results:
            rank = row.get("rank")
            row["rank_display"] = f"{rank}（同着）" if rank is not None and rank_counts[rank] > 1 else rank
        payloads[compact_date].append({
            "race": race,
            "results": detail_results,
            "payouts": rows(conn, """
                SELECT bet_type, combination, payout, popularity
                FROM payout
                WHERE race_id = ?
                ORDER BY id
            """, (race_id,)),
            "lineup": rows(conn, """
                SELECT line_no, line_position, car_no
                FROM race_lineup
                WHERE race_id = ?
                ORDER BY line_no, line_position, car_no
            """, (race_id,)),
        })
    return payloads


PREDICTION_TYPE_ORDER = [
    "本命予想",
    "穴予想",
    "ヘテオジマーベリック予想",
    "感情ブヒー予想",
    "行動ヒヒーン予想",
]

PREDICTION_TYPE_ORDER.extend([
    "feature_3rentan",
    "feature_box_3rentan",
    "feature_line_mix",
])

PREDICTION_TYPE_SUMMARY = {
    "本命予想": "総合上位",
    "穴予想": "中位上昇",
    "ヘテオジマーベリック予想": "反人気",
    "感情ブヒー予想": "人気倒れ回避",
    "行動ヒヒーン予想": "継続安定",
}


PREDICTION_TYPE_SUMMARY.update({
    "feature_3rentan": "feature_score top3",
    "feature_box_3rentan": "feature_score top3 box",
    "feature_line_mix": "feature score + line mix",
})


def prediction_type_order(prediction_type: str) -> int:
    try:
        return PREDICTION_TYPE_ORDER.index(prediction_type)
    except ValueError:
        return len(PREDICTION_TYPE_ORDER)


def prediction_combo(row: dict, prefix: str = "predicted") -> str:
    values = [row.get(f"{prefix}_1st"), row.get(f"{prefix}_2nd"), row.get(f"{prefix}_3rd")]
    if any(value is None for value in values):
        return ""
    return "-".join(str(int(value)) for value in values)


def prediction_bet_combinations(row: dict) -> dict[str, list[str]]:
    values = [row.get("predicted_1st"), row.get("predicted_2nd"), row.get("predicted_3rd")]
    if any(value is None for value in values):
        return {}
    first, second, third = [int(value) for value in values]
    return {
        "2車複": ["=".join(str(item) for item in sorted([first, second]))],
        "2車単": [f"{first}-{second}"],
        "ワイド": [
            "=".join(str(item) for item in sorted(pair))
            for pair in [(first, second), (first, third), (second, third)]
        ],
        "3連複": ["=".join(str(item) for item in sorted([first, second, third]))],
        "3連単": [f"{first}-{second}-{third}"],
    }


def prediction_bet_text(row: dict) -> str:
    combinations = prediction_bet_combinations(row)
    if not combinations:
        return ""
    return " / ".join(
        f'{bet_type} {",".join(combinations[bet_type])}'
        for bet_type in PREDICTION_BET_TYPES
    )


def prediction_pick_cell(row: dict | None) -> str:
    if not row:
        return '<div class="prediction-pick empty">-</div>'
    confidence = h(row.get("confidence") or "C")
    score = decimal(row.get("score"), 1)
    return (
        '<div class="prediction-pick">'
        f'<strong>{h(prediction_combo(row))}</strong>'
        f'<span>{confidence} / {h(score)}</span>'
        f'<span>{h(prediction_bet_text(row))}</span>'
        '</div>'
    )


def actual_combo(row: dict) -> str:
    candidate_fields = [
        row.get("actual_1st_candidates"),
        row.get("actual_2nd_candidates"),
        row.get("actual_3rd_candidates"),
    ]
    if any(candidate_fields):
        values = [value or "-" for value in candidate_fields]
        combo = "-".join(value.replace(",", "/") for value in values)
        return f"{combo}（同着）" if row.get("dead_heat") else combo
    values = [row.get("actual_1st"), row.get("actual_2nd"), row.get("actual_3rd")]
    if any(value is None for value in values):
        return "-"
    return "-".join(str(int(value)) for value in values)


def prediction_result_label(row: dict) -> str:
    if row.get("hit_exact"):
        return "完全的中"
    if row.get("hit_1st"):
        return "1着的中"
    if int(row.get("hit_top3_count") or 0) > 0:
        return "3着内一致"
    return "不的中"


def prediction_result_cell(row: dict | None) -> str:
    if not row:
        return '<div class="prediction-pick empty">-</div>'
    label = prediction_result_label(row)
    combo = prediction_combo(row)
    top3 = int(row.get("hit_top3_count") or 0)
    return_amount = int(row.get("return_amount") or 0)
    return (
        '<div class="prediction-pick">'
        f'<strong>{h(combo)}</strong>'
        f'<span>{h(label)} / {top3}一致 / {h(yen(return_amount))}</span>'
        '</div>'
    )


def parse_score_detail_json(row: dict) -> list[dict]:
    raw = row.get("score_detail_json")
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return items if isinstance(items, list) else []


def component_text(components: dict | None) -> str:
    if not components:
        return ""
    return " / ".join(f"{key}{float(value):+.1f}" for key, value in components.items())


def prediction_score_analysis_rows(prediction_rows: list[dict]) -> list[dict]:
    analysis_rows = []
    for row in prediction_rows:
        details = parse_score_detail_json(row)
        for index, detail in enumerate(details, start=1):
            analysis_rows.append({
                "race_date": row.get("race_date"),
                "prediction_type": row.get("prediction_type"),
                "race": f'{row.get("venue") or ""} {row.get("race_no") or ""}R',
                "start_time": row.get("start_time"),
                "confidence": row.get("confidence"),
                "pick_order": index,
                "car_no": detail.get("car_no"),
                "racer_name": detail.get("racer_name"),
                "base_score": decimal(detail.get("base_score"), 1),
                "type_adjustment": decimal(detail.get("type_adjustment"), 1),
                "final_score": decimal(detail.get("final_score"), 1),
                "base_components": component_text(detail.get("base_components")),
                "type_components": component_text(detail.get("type_components")),
                "prediction_score": decimal(row.get("score"), 1),
                "model_version": row.get("model_version"),
            })
    return analysis_rows


def actual_rank_for_car(row: dict, car_no) -> str:
    try:
        car_no = int(car_no)
    except (TypeError, ValueError):
        return ""
    for rank in [1, 2, 3]:
        if row.get(f"actual_{rank}st") == car_no:
            return str(rank)
    if row.get("actual_2nd") == car_no:
        return "2"
    if row.get("actual_3rd") == car_no:
        return "3"
    return "-"


def prediction_score_result_analysis_rows(result_rows: list[dict]) -> list[dict]:
    analysis_rows = []
    for row in result_rows:
        details = parse_score_detail_json(row)
        actual_top3 = {row.get("actual_1st"), row.get("actual_2nd"), row.get("actual_3rd")}
        for index, detail in enumerate(details, start=1):
            car_no = detail.get("car_no")
            analysis_rows.append({
                "race_date": row.get("race_date"),
                "prediction_type": row.get("prediction_type"),
                "race": f'{row.get("venue") or ""} {row.get("race_no") or ""}R',
                "confidence": row.get("confidence"),
                "pick_order": index,
                "car_no": car_no,
                "racer_name": detail.get("racer_name"),
                "base_score": decimal(detail.get("base_score"), 1),
                "type_adjustment": decimal(detail.get("type_adjustment"), 1),
                "final_score": decimal(detail.get("final_score"), 1),
                "type_components": component_text(detail.get("type_components")),
                "actual_rank": actual_rank_for_car(row, car_no),
                "is_first": "○" if row.get("actual_1st") == car_no else "×",
                "is_top3": "○" if car_no in actual_top3 else "×",
                "judgment": prediction_result_label(row),
                "return_amount": yen(row.get("return_amount")),
                "model_version": row.get("model_version"),
            })
    return analysis_rows


def component_verdict(count: int, top3_rate: float, roi: float) -> str:
    if count < 10:
        return "要観察"
    if roi >= 100 or top3_rate >= 65:
        return "強化候補"
    if top3_rate < 35 and roi < 60:
        return "弱化候補"
    return "継続"


def component_result_analysis_rows(result_rows: list[dict]) -> list[dict]:
    analysis_rows = []
    for row in result_rows:
        actual_top3 = {row.get("actual_1st"), row.get("actual_2nd"), row.get("actual_3rd")}
        stake = int(row.get("result_stake_amount") or row.get("stake_amount") or 0)
        returned = int(row.get("return_amount") or 0)
        for detail in parse_score_detail_json(row):
            car_no = detail.get("car_no")
            for component_type, components in [
                ("基礎", detail.get("base_components") or {}),
                ("タイプ補正", detail.get("type_components") or {}),
            ]:
                for component_name, value in components.items():
                    analysis_rows.append({
                        "race_date": row.get("race_date"),
                        "prediction_type": row.get("prediction_type"),
                        "race": f'{row.get("venue") or ""} {row.get("race_no") or ""}R',
                        "confidence": row.get("confidence") or "C",
                        "component_type": component_type,
                        "component_name": component_name,
                        "component_value": decimal(value, 1),
                        "pick_order": len(analysis_rows) + 1,
                        "car_no": car_no,
                        "racer_name": detail.get("racer_name"),
                        "base_score": decimal(detail.get("base_score"), 1),
                        "final_score": decimal(detail.get("final_score"), 1),
                        "actual_rank": actual_rank_for_car(row, car_no),
                        "is_first": "○" if row.get("actual_1st") == car_no else "×",
                        "is_top3": "○" if car_no in actual_top3 else "×",
                        "judgment": prediction_result_label(row),
                        "prediction": prediction_combo(row),
                        "return_amount": yen(returned),
                        "_data": {
                            "race-date": row.get("race_date") or "",
                            "prediction-type": row.get("prediction_type") or "",
                            "confidence": row.get("confidence") or "C",
                            "component-type": component_type,
                            "component-name": component_name,
                            "is-first": "1" if row.get("actual_1st") == car_no else "0",
                            "is-top3": "1" if car_no in actual_top3 else "0",
                            "hit-exact": "1" if row.get("hit_exact") else "0",
                            "stake": stake,
                            "return": returned,
                            "final-score": float(detail.get("final_score") or 0),
                            "component-value": float(value or 0),
                            "judgment": prediction_result_label(row),
                        },
                        "_raw_value": float(value or 0),
                        "_raw_final_score": float(detail.get("final_score") or 0),
                        "_raw_stake": stake,
                        "_raw_return": returned,
                        "_raw_is_first": row.get("actual_1st") == car_no,
                        "_raw_is_top3": car_no in actual_top3,
                        "_raw_hit_exact": bool(row.get("hit_exact")),
                    })
    return analysis_rows


def component_result_summary_rows(component_rows: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in component_rows:
        buckets[(row.get("prediction_type"), row.get("component_type"), row.get("component_name"))].append(row)
    summary_rows = []
    for (prediction_type, component_type, component_name), items in sorted(
        buckets.items(),
        key=lambda item: (prediction_type_order(item[0][0]), item[0][1], item[0][2]),
    ):
        count = len(items)
        if not count:
            continue
        first_rate = sum(1 for item in items if item["_raw_is_first"]) * 100 / count
        top3_rate = sum(1 for item in items if item["_raw_is_top3"]) * 100 / count
        exact_rate = sum(1 for item in items if item["_raw_hit_exact"]) * 100 / count
        stake = sum(item["_raw_stake"] for item in items)
        returned = sum(item["_raw_return"] for item in items)
        roi = returned * 100 / stake if stake else 0
        verdict = component_verdict(count, top3_rate, roi)
        summary_rows.append({
            "prediction_type": prediction_type,
            "component_type": component_type,
            "component_name": component_name,
            "count": count,
            "avg_value": decimal(sum(item["_raw_value"] for item in items) / count, 1),
            "avg_final_score": decimal(sum(item["_raw_final_score"] for item in items) / count, 1),
            "first_rate": pct(first_rate),
            "top3_rate": pct(top3_rate),
            "exact_rate": pct(exact_rate),
            "roi": pct(roi),
            "verdict": pill(verdict),
            "_class": sample_class(count, 10),
            "_data": {
                "prediction-type": prediction_type,
                "component-type": component_type,
                "component-name": component_name,
                "count": count,
                "verdict": verdict,
            },
        })
    return summary_rows


def select_options(values: list[str]) -> str:
    return "".join(f'<option value="{h(value)}">{h(value)}</option>' for value in values if value)


def render_component_result_analysis(summary_rows: list[dict], detail_rows: list[dict]) -> str:
    dates = sorted({row.get("race_date") for row in detail_rows if row.get("race_date")}, reverse=True)
    component_types = sorted({row.get("component_type") for row in detail_rows if row.get("component_type")})
    component_names = sorted({row.get("component_name") for row in detail_rows if row.get("component_name")})
    detail_display_rows = sorted(
        detail_rows,
        key=lambda row: (
            row.get("race_date") or "",
            prediction_type_order(row.get("prediction_type") or ""),
            row.get("component_type") or "",
            row.get("component_name") or "",
            row.get("race") or "",
        ),
        reverse=True,
    )[:PREDICTION_ANALYSIS_ROW_LIMIT]
    return f"""
      <div class="filters" id="component-analysis-filters">
        <label>対象日<select id="component-filter-date"><option value="">すべて</option>{select_options(dates)}</select></label>
        <label>予想タイプ<select id="component-filter-type"><option value="">すべて</option>{select_options(PREDICTION_TYPE_ORDER)}</select></label>
        <label>補正種別<select id="component-filter-kind"><option value="">すべて</option>{select_options(component_types)}</select></label>
        <label>補正項目<select id="component-filter-name"><option value="">すべて</option>{select_options(component_names)}</select></label>
        <label>信頼度<select id="component-filter-confidence"><option value="">すべて</option><option value="A">A</option><option value="B">B</option><option value="C">C</option></select></label>
        <label>採用数下限<input id="component-filter-count" type="number" min="0" step="1" placeholder="指定なし"></label>
        <label>判定<select id="component-filter-verdict"><option value="">すべて</option><option value="強化候補">強化候補</option><option value="継続">継続</option><option value="弱化候補">弱化候補</option><option value="要観察">要観察</option></select></label>
      </div>
      <div class="analysis-dashboard">
        <div class="analysis-metrics">
          <div class="analysis-metric"><span>採用数</span><strong id="component-metric-count">-</strong></div>
          <div class="analysis-metric"><span>1着率</span><strong id="component-metric-first">-</strong></div>
          <div class="analysis-metric"><span>3着内率</span><strong id="component-metric-top3">-</strong></div>
          <div class="analysis-metric"><span>完全的中率</span><strong id="component-metric-exact">-</strong></div>
          <div class="analysis-metric"><span>回収率</span><strong id="component-metric-roi">-</strong></div>
        </div>
        <div class="analysis-panels">
          <div class="analysis-panel">
            <h3>予想タイプ別 3着内率</h3>
            <div class="analysis-bars" id="component-type-bars"></div>
          </div>
          <div class="analysis-panel">
            <h3>判定分布</h3>
            <div class="analysis-bars" id="component-verdict-bars"></div>
          </div>
          <div class="analysis-panel">
            <h3>回収率 TOP10</h3>
            <div class="analysis-ranking" id="component-roi-ranking"></div>
          </div>
          <div class="analysis-panel">
            <h3>弱化候補</h3>
            <div class="analysis-ranking" id="component-risk-ranking"></div>
          </div>
        </div>
        <div class="analysis-panel">
          <h3>補正項目マップ</h3>
          <div class="analysis-scatter" id="component-scatter"></div>
          <div class="analysis-axis-note"><span>横: 採用数</span><span>縦: 3着内率</span><span>大きさ: 回収率</span></div>
        </div>
        <div class="analysis-panel">
          <h3>補正項目別 成績サマリー</h3>
          <table id="component-summary-table">
            <thead><tr><th>予想タイプ</th><th>補正種別</th><th>補正項目</th><th>採用数</th><th>1着率</th><th>3着内率</th><th>完全的中率</th><th>平均補正値</th><th>平均最終点</th><th>回収率</th><th>判定</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <details class="analysis-detail-toggle">
          <summary>採用明細を開く</summary>
          <p class="section-lead">現在のフィルタと選択中の補正項目に合わせて明細を絞り込みます。表示は最大200件です。</p>
          {table(
            ["対象日", "レース", "予想タイプ", "信頼度", "補正種別", "補正項目", "補正値", "車番", "選手", "基礎点", "最終点", "実着順", "1着", "3着内", "判定", "買い目", "回収額"],
            detail_display_rows,
            ["race_date", "race", "prediction_type", "confidence", "component_type", "component_name", "component_value", "car_no", "racer_name", "base_score", "final_score", "actual_rank", "is_first", "is_top3", "judgment", "prediction", "return_amount"],
          ).replace("<table>", '<table id="component-detail-table">', 1)}
        </details>
      </div>
      <script>
      (() => {{
        const summaryTable = document.getElementById("component-summary-table");
        const detailTable = document.getElementById("component-detail-table");
        if (!summaryTable || !detailTable) return;
        const date = document.getElementById("component-filter-date");
        const type = document.getElementById("component-filter-type");
        const kind = document.getElementById("component-filter-kind");
        const name = document.getElementById("component-filter-name");
        const confidence = document.getElementById("component-filter-confidence");
        const count = document.getElementById("component-filter-count");
        const verdict = document.getElementById("component-filter-verdict");
        const summaryBody = summaryTable.querySelector("tbody");
        const detailRows = Array.from(detailTable.querySelectorAll("tbody tr"));
        const number = (value) => Number(value || 0);
        const pct = (value) => `${{value.toFixed(1)}}%`;
        const yen = (value) => `${{Math.round(value).toLocaleString()}}円`;
        const rowMatches = (row) => {{
          if (date.value && row.dataset.raceDate && row.dataset.raceDate !== date.value) return false;
          if (type.value && row.dataset.predictionType !== type.value) return false;
          if (kind.value && row.dataset.componentType !== kind.value) return false;
          if (name.value && row.dataset.componentName !== name.value) return false;
          if (confidence.value && row.dataset.confidence !== confidence.value) return false;
          return true;
        }};
        const verdictFor = (item) => {{
          if (item.count < 10) return "要観察";
          if (item.roi >= 100 || item.top3Rate >= 65) return "強化候補";
          if (item.top3Rate < 35 && item.roi < 60) return "弱化候補";
          return "継続";
        }};
        const verdictClass = (value) => {{
          if (value === "強化候補") return "verdict-strong";
          if (value === "弱化候補") return "verdict-weak";
          if (value === "要観察") return "verdict-watch";
          return "";
        }};
        const aggregate = (rows) => {{
          const groups = new Map();
          rows.forEach((row) => {{
            const key = [row.dataset.predictionType, row.dataset.componentType, row.dataset.componentName].join("\\t");
            if (!groups.has(key)) {{
              groups.set(key, {{
                predictionType: row.dataset.predictionType || "",
                componentType: row.dataset.componentType || "",
                componentName: row.dataset.componentName || "",
                count: 0,
                first: 0,
                top3: 0,
                exact: 0,
                stake: 0,
                returned: 0,
                valueTotal: 0,
                finalTotal: 0,
              }});
            }}
            const item = groups.get(key);
            item.count += 1;
            item.first += row.dataset.isFirst === "1" ? 1 : 0;
            item.top3 += row.dataset.isTop3 === "1" ? 1 : 0;
            item.exact += row.dataset.hitExact === "1" ? 1 : 0;
            item.stake += number(row.dataset.stake);
            item.returned += number(row.dataset.return);
            item.valueTotal += number(row.dataset.componentValue);
            item.finalTotal += number(row.dataset.finalScore);
          }});
          return Array.from(groups.values()).map((item) => {{
            item.firstRate = item.count ? item.first * 100 / item.count : 0;
            item.top3Rate = item.count ? item.top3 * 100 / item.count : 0;
            item.exactRate = item.count ? item.exact * 100 / item.count : 0;
            item.roi = item.stake ? item.returned * 100 / item.stake : 0;
            item.avgValue = item.count ? item.valueTotal / item.count : 0;
            item.avgFinal = item.count ? item.finalTotal / item.count : 0;
            item.verdict = verdictFor(item);
            return item;
          }}).filter((item) => {{
            if (count.value && item.count < Number(count.value)) return false;
            if (verdict.value && item.verdict !== verdict.value) return false;
            return true;
          }});
        }};
        const renderMetrics = (rows) => {{
          const total = rows.length;
          const first = rows.filter((row) => row.dataset.isFirst === "1").length;
          const top3 = rows.filter((row) => row.dataset.isTop3 === "1").length;
          const exact = rows.filter((row) => row.dataset.hitExact === "1").length;
          const stake = rows.reduce((sum, row) => sum + number(row.dataset.stake), 0);
          const returned = rows.reduce((sum, row) => sum + number(row.dataset.return), 0);
          document.getElementById("component-metric-count").textContent = total.toLocaleString();
          document.getElementById("component-metric-first").textContent = total ? pct(first * 100 / total) : "-";
          document.getElementById("component-metric-top3").textContent = total ? pct(top3 * 100 / total) : "-";
          document.getElementById("component-metric-exact").textContent = total ? pct(exact * 100 / total) : "-";
          document.getElementById("component-metric-roi").textContent = stake ? pct(returned * 100 / stake) : "-";
        }};
        const barHtml = (label, value, maxValue, sub = "") => `
          <div class="analysis-bar-row">
            <div title="${{label}}">${{label}}</div>
            <div class="analysis-bar-track"><div class="analysis-bar-fill" style="width:${{Math.min(100, maxValue ? value * 100 / maxValue : 0).toFixed(1)}}%"></div></div>
            <div>${{sub || pct(value)}}</div>
          </div>`;
        const renderTypeBars = (rows) => {{
          const buckets = new Map();
          rows.forEach((row) => {{
            const key = row.dataset.predictionType || "";
            const item = buckets.get(key) || {{ count: 0, top3: 0 }};
            item.count += 1;
            item.top3 += row.dataset.isTop3 === "1" ? 1 : 0;
            buckets.set(key, item);
          }});
          const items = Array.from(buckets.entries()).map(([label, item]) => ({{
            label,
            value: item.count ? item.top3 * 100 / item.count : 0,
          }})).sort((a, b) => b.value - a.value);
          document.getElementById("component-type-bars").innerHTML = items.map((item) => barHtml(item.label, item.value, 100)).join("") || '<div class="empty">データがありません</div>';
        }};
        const renderVerdictBars = (groups) => {{
          const buckets = new Map([["強化候補", 0], ["継続", 0], ["弱化候補", 0], ["要観察", 0]]);
          groups.forEach((item) => buckets.set(item.verdict, (buckets.get(item.verdict) || 0) + 1));
          const maxValue = Math.max(...Array.from(buckets.values()), 1);
          document.getElementById("component-verdict-bars").innerHTML = Array.from(buckets.entries()).map(([label, value]) => barHtml(label, value, maxValue, `${{value}}件`)).join("");
        }};
        const rankButton = (item, value) => `
          <button class="analysis-rank-item" type="button" data-prediction-type="${{item.predictionType}}" data-component-type="${{item.componentType}}" data-component-name="${{item.componentName}}">
            <div><strong>${{item.componentName}}</strong><span>${{item.predictionType}} / ${{item.componentType}} / 採用${{item.count}}件</span></div>
            <div>${{value}}</div>
          </button>`;
        const bindRankButtons = (root) => {{
          root.querySelectorAll("button").forEach((button) => {{
            button.addEventListener("click", () => {{
              type.value = button.dataset.predictionType || "";
              kind.value = button.dataset.componentType || "";
              name.value = button.dataset.componentName || "";
              apply();
            }});
          }});
        }};
        const renderRankings = (groups) => {{
          const roiRoot = document.getElementById("component-roi-ranking");
          const riskRoot = document.getElementById("component-risk-ranking");
          const roiItems = groups.filter((item) => item.count >= 10).sort((a, b) => b.roi - a.roi).slice(0, 10);
          const riskItems = groups.filter((item) => item.count >= 10 && item.top3Rate < 50).sort((a, b) => a.top3Rate - b.top3Rate || b.count - a.count).slice(0, 10);
          roiRoot.innerHTML = roiItems.map((item) => rankButton(item, pct(item.roi))).join("") || '<div class="empty">データがありません</div>';
          riskRoot.innerHTML = riskItems.map((item) => rankButton(item, `${{pct(item.top3Rate)}}`)).join("") || '<div class="empty">データがありません</div>';
          bindRankButtons(roiRoot);
          bindRankButtons(riskRoot);
        }};
        const renderScatter = (groups) => {{
          const root = document.getElementById("component-scatter");
          const maxCount = Math.max(...groups.map((item) => item.count), 1);
          const maxRoi = Math.max(...groups.map((item) => item.roi), 100);
          root.innerHTML = groups.map((item) => {{
            const left = Math.max(4, Math.min(96, item.count * 92 / maxCount + 4));
            const bottom = Math.max(4, Math.min(96, item.top3Rate));
            const size = Math.max(9, Math.min(28, 9 + item.roi * 19 / maxRoi));
            return `<button class="analysis-point ${{verdictClass(item.verdict)}}" type="button"
              title="${{item.predictionType}} / ${{item.componentName}} / 採用${{item.count}}件 / 3着内${{pct(item.top3Rate)}} / 回収${{pct(item.roi)}}"
              data-prediction-type="${{item.predictionType}}" data-component-type="${{item.componentType}}" data-component-name="${{item.componentName}}"
              style="left:${{left.toFixed(1)}}%; bottom:${{bottom.toFixed(1)}}%; width:${{size.toFixed(1)}}px; height:${{size.toFixed(1)}}px;">${{item.componentName}}</button>`;
          }}).join("");
          bindRankButtons(root);
        }};
        const renderSummary = (groups) => {{
          const rows = groups.sort((a, b) => b.count - a.count || b.top3Rate - a.top3Rate).slice(0, 80);
          summaryBody.innerHTML = rows.map((item) => `
            <tr data-prediction-type="${{item.predictionType}}" data-component-type="${{item.componentType}}" data-component-name="${{item.componentName}}">
              <td>${{item.predictionType}}</td><td>${{item.componentType}}</td><td>${{item.componentName}}</td><td>${{item.count}}</td>
              <td>${{pct(item.firstRate)}}</td><td>${{pct(item.top3Rate)}}</td><td>${{pct(item.exactRate)}}</td>
              <td>${{item.avgValue.toFixed(1)}}</td><td>${{item.avgFinal.toFixed(1)}}</td><td>${{pct(item.roi)}}</td><td><span class="pill">${{item.verdict}}</span></td>
            </tr>`).join("") || '<tr><td colspan="11">データがありません</td></tr>';
          summaryBody.querySelectorAll("tr").forEach((row) => {{
            row.addEventListener("click", () => {{
              type.value = row.dataset.predictionType || "";
              kind.value = row.dataset.componentType || "";
              name.value = row.dataset.componentName || "";
              apply();
            }});
          }});
        }};
        const apply = () => {{
          const filteredDetails = detailRows.filter(rowMatches);
          const groups = aggregate(filteredDetails);
          renderMetrics(filteredDetails);
          renderTypeBars(filteredDetails);
          renderVerdictBars(groups);
          renderRankings(groups);
          renderScatter(groups);
          renderSummary(groups);
          let shown = 0;
          detailRows.forEach((row) => {{
            const visible = rowMatches(row) && shown < 200;
            row.hidden = !visible;
            if (visible) shown += 1;
          }});
        }};
        [date, type, kind, name, confidence, count, verdict].forEach((item) => item.addEventListener("input", apply));
        apply();
      }})();
      </script>
    """


def prediction_rows_for_date(conn, target_date: str | None) -> list[dict]:
    if not target_date:
        return []
    prediction_rows = rows(conn, """
        SELECT p.*, s.venue, s.race_no, s.race_title, s.start_time, s.lineup_text,
               c.confidence_score, c.confidence_stars, c.expected_value_score,
               v.volatility_probability,
               (
                 SELECT GROUP_CONCAT(e.car_no, ' ')
                 FROM race_entry e
                 WHERE e.race_id = p.race_id
               ) AS entry_car_nos
        FROM race_prediction p
        LEFT JOIN race_schedule s ON s.race_id = p.race_id
        LEFT JOIN race_confidence c ON c.race_id = p.race_id
        LEFT JOIN race_volatility_features v ON v.race_id = p.race_id
        WHERE p.race_date = ?
        ORDER BY s.venue, s.race_no, p.prediction_type
    """, (target_date,))
    return sorted(
        prediction_rows,
        key=lambda row: (
            row.get("venue") or "",
            int(row.get("race_no") or 0),
            prediction_type_order(row.get("prediction_type") or ""),
        ),
    )


def featured_prediction_rows(prediction_rows: list[dict], per_type: int = 3) -> list[dict]:
    by_type: dict[str, list[dict]] = {prediction_type: [] for prediction_type in PREDICTION_TYPE_ORDER}
    for row in prediction_rows:
        by_type.setdefault(row["prediction_type"], []).append(row)

    featured = []
    used_race_ids = set()
    for prediction_type in [*PREDICTION_TYPE_ORDER, *sorted(set(by_type) - set(PREDICTION_TYPE_ORDER))]:
        candidates = sorted(by_type.get(prediction_type, []), key=lambda row: row.get("score") or 0, reverse=True)
        picked = []
        for row in candidates:
            if row.get("race_id") in used_race_ids:
                continue
            picked.append(row)
            used_race_ids.add(row.get("race_id"))
            if len(picked) == per_type:
                break
        if len(picked) < per_type:
            for row in candidates:
                if row in picked:
                    continue
                picked.append(row)
                if len(picked) == per_type:
                    break
        featured.extend(picked)
    return featured


def format_lineup_text(lineup_text: str | None, entry_car_nos: str | None = None) -> str:
    if not lineup_text:
        return ""
    car_nos = {
        int(value)
        for value in re.findall(r"\d+", entry_car_nos or "")
        if 1 <= int(value) <= 9
    }
    if not car_nos:
        car_nos = {int(value) for value in re.findall(r"\d+", lineup_text) if 1 <= int(value) <= 9}
    tokens = re.findall(r"/|\d+", lineup_text)
    candidates: list[tuple[bool, int, list[list[int]]]] = []

    for start in range(len(tokens)):
        groups = [[]]
        seen = set()
        matched_all = False
        for token in tokens[start:]:
            if token == "/":
                if groups[-1]:
                    groups.append([])
                continue
            value = int(token)
            if value not in car_nos or value in seen:
                break
            groups[-1].append(value)
            seen.add(value)
            if seen == car_nos:
                matched_all = True
                break
        clean_groups = [group[:] for group in groups if group]
        if len(clean_groups) >= 2 and len(seen) >= 3:
            candidates.append((matched_all, len(seen), clean_groups))

    if candidates:
        groups = max(enumerate(candidates), key=lambda item: (item[1][0], item[1][1], item[0]))[1][2]
        return " / ".join(" ".join(str(car_no) for car_no in group) for group in groups)
    return lineup_text


def compact_lineup_text(lineup_text: str | None, entry_car_nos: str | None = None) -> str:
    text = format_lineup_text(lineup_text, entry_car_nos)
    if len(text) <= 80:
        return text
    car_nos = sorted({
        int(value)
        for value in re.findall(r"\d+", entry_car_nos or "")
        if 1 <= int(value) <= 9
    })
    return " ".join(str(car_no) for car_no in car_nos)


def parse_feature_json(value) -> dict:
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def chaos_display(features: dict) -> str:
    if not features:
        return "-"
    score = features.get("chaos_score")
    level = features.get("chaos_level") or "-"
    reasons = features.get("chaos_reasons") or []
    score_text = decimal(score, 0) if score is not None else "-"
    reason_text = "、".join(str(item) for item in reasons[:2])
    return f"{score_text} / {level}" + (f" / {reason_text}" if reason_text else "")


def compact_reason_text(value: str | None, max_len: int = 74) -> str:
    text = re.sub(r"\s+", " ", str(value or "-")).strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip("、。,. ") + "..."


def compact_reason_html(value: str | None) -> str:
    text = re.sub(r"\s+", " ", str(value or "-")).strip()
    short = compact_reason_text(text)
    if text == short or text == "-":
        return h(short)
    return f'<details class="compact-reason"><summary>{h(short)}</summary><div>{h(text)}</div></details>'


def compact_components_html(value: str | None, visible_count: int = 3) -> str:
    text = re.sub(r"\s+", " ", str(value or "-")).strip()
    if text == "-":
        return "-"
    parts = [part.strip() for part in text.split(" / ") if part.strip()]
    if len(parts) <= visible_count:
        return h(text)
    short = " / ".join(parts[:visible_count]) + f" / 他{len(parts) - visible_count}件"
    return f'<details class="compact-components"><summary>{h(short)}</summary><div>{h(text)}</div></details>'


def recommendation_decision(row: dict, features: dict) -> tuple[str, str]:
    bet_type = row.get("recommended_bet_type") or "見送り"
    confidence = row.get("confidence") or "C"
    chaos = features.get("chaos_level") or ""
    if bet_type != "見送り" and confidence in {"A", "B"} and chaos != "high":
        return "buy", "買い候補"
    if bet_type != "見送り":
        return "caution", "慎重"
    return "skip", "見送り"


def bet_recommendation_rows_for_date(conn, target_date: str | None) -> list[dict]:
    if not target_date:
        return []
    recommendations = rows(
        conn,
        """
        SELECT r.*, s.venue, s.race_no, s.race_title, s.start_time
        FROM race_bet_recommendation r
        LEFT JOIN race_schedule s ON s.race_id = r.race_id
        WHERE r.race_date = ?
        ORDER BY s.venue, s.race_no
        """,
        (target_date,),
    )
    result = []
    for row in recommendations:
        try:
            combinations = json.loads(row.get("combinations_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            combinations = []
        features = parse_feature_json(row.get("feature_json"))
        decision_key, decision_label = recommendation_decision(row, features)
        chaos = features.get("chaos_level") or ""
        result.append({
            **row,
            "race": f'{row.get("venue") or ""} {row.get("race_no") or ""}R',
            "decision": decision_label,
            "decision_display": pill(
                decision_label,
                "buy" if decision_key == "buy" else "caution" if decision_key == "caution" else "low",
            ),
            "chaos_display": chaos_display(features),
            "recommended_bet_type": (
                f'<span class="pill">{h(row.get("recommended_bet_type") or "見送り")}</span>'
            ),
            "buy": " / ".join(str(item) for item in combinations) or "-",
            "confidence_display": f'<span class="pill">{h(row.get("confidence") or "C")}</span>',
            "similar_result": (
                f'{number(row.get("similar_sample_count") or 0)}件 / '
                f'的中{decimal(row.get("similar_hit_rate"), 1)}% / '
                f'回収{decimal(row.get("similar_roi"), 1)}%'
                if row.get("similar_sample_count")
                else "参考データ不足"
            ),
            "classification_reason": row.get("reason_text") or row.get("skip_reason") or "-",
            "classification_reason_compact": compact_reason_html(row.get("reason_text") or row.get("skip_reason") or "-"),
            "_data": {
                "decision": decision_key,
                "venue": row.get("venue") or "",
                "bet": row.get("recommended_bet_type") or "見送り",
                "confidence": row.get("confidence") or "C",
                "chaos": chaos,
            },
        })
    return result


def render_predictions(conn) -> str:
    target_date = scalar(conn, "SELECT MAX(race_date) FROM race_prediction")
    if not target_date:
        target_date = scalar(conn, "SELECT MAX(race_date) FROM race_schedule")
    prediction_rows = prediction_rows_for_date(conn, target_date)
    summary_rows = rows(conn, """
        SELECT prediction_type, COUNT(*) AS predictions, ROUND(AVG(score), 1) AS avg_score
        FROM race_prediction
        WHERE race_date = COALESCE(?, race_date)
        GROUP BY prediction_type
        ORDER BY prediction_type
    """, (target_date,))
    schedule_count = scalar(conn, "SELECT COUNT(*) FROM race_schedule WHERE race_date = COALESCE(?, race_date)", (target_date,))
    latest_created = scalar(conn, "SELECT MAX(created_at) FROM race_prediction")

    body = f"""
    <div class="grid">
      <div class="card"><span>対象日</span><strong>{h(target_date or "-")}</strong></div>
      <div class="card"><span>出走表レース数</span><strong>{h(number(schedule_count or 0))}</strong></div>
      <div class="card"><span>予想件数</span><strong>{h(number(sum(row["predictions"] for row in summary_rows)))}</strong></div>
      <div class="card"><span>生成日時</span><strong>{h(latest_created or "-")}</strong></div>
    </div>
    """
    recommendation_rows = bet_recommendation_rows_for_date(conn, target_date)
    if recommendation_rows:
        buy_count = sum(1 for row in recommendation_rows if row.get("_data", {}).get("decision") == "buy")
        caution_count = sum(1 for row in recommendation_rows if row.get("_data", {}).get("decision") == "caution")
        skip_count = sum(1 for row in recommendation_rows if row.get("_data", {}).get("decision") == "skip")
        high_chaos_count = sum(1 for row in recommendation_rows if row.get("_data", {}).get("chaos") == "high")
        recommendation_venues = sorted({row.get("_data", {}).get("venue") for row in recommendation_rows if row.get("_data", {}).get("venue")})
        recommendation_bets = sorted({row.get("_data", {}).get("bet") for row in recommendation_rows if row.get("_data", {}).get("bet")})
        body += section(
            "本日の買い候補",
            f"""
              <div class="decision-grid">
                <div class="decision-card"><span>買い候補</span><strong>{h(number(buy_count))}</strong><small>A/B評価かつ高荒れ以外</small></div>
                <div class="decision-card"><span>慎重</span><strong>{h(number(caution_count))}</strong><small>買い目ありだが条件注意</small></div>
                <div class="decision-card"><span>見送り</span><strong>{h(number(skip_count))}</strong><small>基準未満・材料不足</small></div>
                <div class="decision-card"><span>荒れ度 high</span><strong>{h(number(high_chaos_count))}</strong><small>波乱警戒のレース</small></div>
              </div>
              <div class="recommendation-toolbar"><span>初期表示は <strong>買い候補＋慎重</strong> のみです。見送りは判定フィルタで表示できます。</span><span id="recommendation-visible-count"></span></div>
              <div class="filters" id="recommendation-filters">
                <label>判定<select id="recommendation-filter-decision"><option value="active" selected>買い候補＋慎重</option><option value="buy">買い候補</option><option value="caution">慎重</option><option value="skip">見送り</option><option value="">すべて</option></select></label>
                <label>会場<select id="recommendation-filter-venue"><option value="">すべて</option>{''.join(f'<option value="{h(venue)}">{h(venue)}</option>' for venue in recommendation_venues)}</select></label>
                <label>券種<select id="recommendation-filter-bet"><option value="">すべて</option>{''.join(f'<option value="{h(bet)}">{h(bet)}</option>' for bet in recommendation_bets)}</select></label>
                <label>荒れ度<select id="recommendation-filter-chaos"><option value="">すべて</option><option value="low">low</option><option value="medium">medium</option><option value="high">high</option></select></label>
              </div>
              {rich_table(
                  [
                      "判定",
                      "レース",
                      "発走",
                      "推奨券種",
                      "買い目",
                      "確信度",
                      "荒れ度",
                      "理由",
                  ],
                  recommendation_rows,
                  [
                      "decision_display",
                      "race",
                      "start_time",
                      "recommended_bet_type",
                      "buy",
                      "confidence_display",
                      "chaos_display",
                      "classification_reason_compact",
                  ],
              ).replace("<table>", '<table id="daily-recommendations">', 1)}
              <script>
              (() => {{
                const table = document.getElementById("daily-recommendations");
                if (!table) return;
                const decision = document.getElementById("recommendation-filter-decision");
                const venue = document.getElementById("recommendation-filter-venue");
                const bet = document.getElementById("recommendation-filter-bet");
                const chaos = document.getElementById("recommendation-filter-chaos");
                const rows = Array.from(table.querySelectorAll("tbody tr"));
                const visibleCount = document.getElementById("recommendation-visible-count");
                const decisionMatches = (row) => {{
                  if (decision.value === "active") return row.dataset.decision === "buy" || row.dataset.decision === "caution";
                  return !decision.value || row.dataset.decision === decision.value;
                }};
                const apply = () => {{
                  let shown = 0;
                  rows.forEach((row) => {{
                    const show =
                      decisionMatches(row) &&
                      (!venue.value || row.dataset.venue === venue.value) &&
                      (!bet.value || row.dataset.bet === bet.value) &&
                      (!chaos.value || row.dataset.chaos === chaos.value);
                    row.hidden = !show;
                    if (show) shown += 1;
                  }});
                  if (visibleCount) visibleCount.textContent = `表示 ${{shown}} / ${{rows.length}} 件`;
                }};
                [decision, venue, bet, chaos].forEach((item) => item.addEventListener("change", apply));
                apply();
              }})();
              </script>
            """,
            "最初に見る判断表です。買い候補だけに絞ってから、荒れ度と理由を確認してください。",
        )
    body += section("予想タイプ別サマリー", table(
        ["予想タイプ", "件数", "平均スコア"],
        sorted(
            [{"prediction_type": row["prediction_type"], "predictions": row["predictions"], "avg_score": decimal(row["avg_score"], 1)} for row in summary_rows],
            key=lambda row: prediction_type_order(row["prediction_type"]),
        ),
        ["prediction_type", "predictions", "avg_score"],
    ), "対象日の予想件数と平均スコアです。おすすめだけでなく、当日全レース予想も下に表示します。")

    featured_rows = featured_prediction_rows(prediction_rows)
    if featured_rows:
        featured_display = []
        for row in featured_rows:
            featured_display.append({
                "prediction_type": row.get("prediction_type"),
                "race": f'{row.get("venue") or ""} {row.get("race_no") or ""}R',
                "start_time": row.get("start_time"),
                "prediction": prediction_combo(row),
                "confidence": f'<span class="pill">{h(row.get("confidence") or "C")}</span>',
                "race_confidence": f'{h(row.get("confidence_stars") or "-")} {h(decimal(row.get("confidence_score"), 2))}',
                "score": decimal(row.get("score"), 1),
                "lineup_text": compact_lineup_text(row.get("lineup_text"), row.get("entry_car_nos")),
                "reason_text": row.get("reason_text"),
                "score_detail_text": row.get("score_detail_text"),
            })
        featured_headers = ["予想タイプ", "レース", "発走", "予想", "信頼度", "スコア", "並び", "根拠"]
        featured_fields = ["prediction_type", "race", "start_time", "prediction", "confidence", "score", "lineup_text", "reason_text"]
        featured_headers.insert(5, "race confidence")
        featured_fields.insert(5, "race_confidence")
        if is_dev_environment():
            featured_headers.append("補正内訳")
            featured_fields.append("score_detail_text")
        body += section("今日の注目予想", rich_table(
            featured_headers,
            featured_display,
            featured_fields,
        ), "各タイプ3件まで表示します。タイプ間で同じレースが続かないよう、可能な範囲で重複を抑えます。")

    if prediction_rows:
        venues = sorted({row.get("venue") for row in prediction_rows if row.get("venue")})
        venue_options = "".join(f'<option value="{h(venue)}">{h(venue)}</option>' for venue in venues)
        grouped: dict[str, dict] = {}
        for row in prediction_rows:
            race_id = row.get("race_id") or ""
            group = grouped.setdefault(race_id, {
                "race_id": race_id,
                "venue": row.get("venue") or "",
                "race_no": row.get("race_no") or "",
                "race_title": row.get("race_title") or "",
                "start_time": row.get("start_time") or "",
                "lineup_text": compact_lineup_text(row.get("lineup_text"), row.get("entry_car_nos")),
                "predictions": {},
            })
            group["predictions"][row.get("prediction_type")] = row

        all_rows = []
        for group in sorted(grouped.values(), key=lambda item: (item["venue"], int(item["race_no"] or 0))):
            combos = [prediction_combo(item) for item in group["predictions"].values()]
            duplicate = len(combos) != len(set(combos))
            confidences = " ".join(sorted({str(item.get("confidence") or "C") for item in group["predictions"].values()}))
            types = " ".join(group["predictions"].keys())
            cells = {
                "race": f'{group["venue"]} {group["race_no"]}R',
                "start_time": group["start_time"],
                "lineup_text": group["lineup_text"],
                "duplicate": "あり" if duplicate else "なし",
                "_data": {
                    "venue": group["venue"],
                    "confidence": confidences,
                    "type": types,
                    "duplicate": "yes" if duplicate else "no",
                },
            }
            for prediction_type in PREDICTION_TYPE_ORDER:
                cells[prediction_type] = prediction_pick_cell(group["predictions"].get(prediction_type))
            all_rows.append(cells)

        body += section("全レース予想（詳細）", f"""
          <details class="analysis-fold">
            <summary>詳細: 全レースの5タイプ予想を開く</summary>
            <div class="filters" id="prediction-filters">
              <label>会場<select id="prediction-filter-venue"><option value="">すべて</option>{venue_options}</select></label>
              <label>信頼度<select id="prediction-filter-confidence"><option value="">すべて</option><option value="A">A</option><option value="B">B</option><option value="C">C</option></select></label>
              <label>予想タイプ<select id="prediction-filter-type"><option value="">すべて</option>{''.join(f'<option value="{h(item)}">{h(item)}</option>' for item in PREDICTION_TYPE_ORDER)}</select></label>
              <label>重複買い目<select id="prediction-filter-duplicate"><option value="">すべて</option><option value="yes">あり</option><option value="no">なし</option></select></label>
            </div>
            {rich_table(
                ["レース", "発走", "並び", *PREDICTION_TYPE_ORDER, "重複"],
                all_rows,
                ["race", "start_time", "lineup_text", *PREDICTION_TYPE_ORDER, "duplicate"],
            ).replace("<table>", '<table id="all-race-predictions">', 1)}
          </details>
          <script>
          (() => {{
            const table = document.getElementById("all-race-predictions");
            if (!table) return;
            const venue = document.getElementById("prediction-filter-venue");
            const confidence = document.getElementById("prediction-filter-confidence");
            const type = document.getElementById("prediction-filter-type");
            const duplicate = document.getElementById("prediction-filter-duplicate");
            const rows = Array.from(table.querySelectorAll("tbody tr"));
            const apply = () => {{
              rows.forEach((row) => {{
                const show =
                  (!venue.value || row.dataset.venue === venue.value) &&
                  (!confidence.value || (row.dataset.confidence || "").includes(confidence.value)) &&
                  (!type.value || (row.dataset.type || "").includes(type.value)) &&
                  (!duplicate.value || row.dataset.duplicate === duplicate.value);
                row.hidden = !show;
              }});
            }};
            [venue, confidence, type, duplicate].forEach((item) => item.addEventListener("change", apply));
          }})();
          </script>
        """, "会場・R順で、各レースの5タイプの買い目を横並びで比較できます。通常は上の本日の買い候補を優先してください。")

    type_notes = "".join(
        f'<div class="prediction-type-note"><strong>{h(prediction_type.replace("予想", ""))}</strong><span>{h(summary)}</span></div>'
        for prediction_type, summary in PREDICTION_TYPE_SUMMARY.items()
    )
    body += section("詳細: 予想タイプの説明", f'<div class="prediction-type-grid">{type_notes}</div>')
    body += section("詳細: 賭式別の買い目", table(
        ["賭式", "買い目の作り方", "購入点数", "1予想あたり投資額"],
        [
            {"bet_type": "2車複", "rule": "予想1・2着の2車（順不同）", "tickets": "1点", "stake": "100円"},
            {"bet_type": "2車単", "rule": "予想1着→2着", "tickets": "1点", "stake": "100円"},
            {"bet_type": "ワイド", "rule": "予想上位3車から2車の全組み合わせ", "tickets": "3点", "stake": "300円"},
            {"bet_type": "3連複", "rule": "予想上位3車（順不同）", "tickets": "1点", "stake": "100円"},
            {"bet_type": "3連単", "rule": "予想1着→2着→3着", "tickets": "1点", "stake": "100円"},
        ],
        ["bet_type", "rule", "tickets", "stake"],
    ), "各予想タイプの順位予想から、5賭式の買い目を自動生成します。")

    if is_dev_environment() and prediction_rows:
        raw_analysis_rows = prediction_score_analysis_rows(prediction_rows)
        analysis_rows = []
        for row in raw_analysis_rows[:300]:
            analysis_rows.append({
                **row,
                "base_components_compact": compact_components_html(row.get("base_components")),
                "type_components_compact": compact_components_html(row.get("type_components")),
            })
        analysis_table = rich_table(
            ["対象日", "予想タイプ", "レース", "発走", "信頼度", "買い目順", "車番", "選手", "基礎点", "タイプ補正", "最終点", "基礎内訳", "タイプ補正内訳", "予想スコア", "モデル"],
            analysis_rows,
            ["race_date", "prediction_type", "race", "start_time", "confidence", "pick_order", "car_no", "racer_name", "base_score", "type_adjustment", "final_score", "base_components_compact", "type_components_compact", "prediction_score", "model_version"],
        ).replace("<table>", '<table class="analysis-compact-table">', 1)
        body += section(
            "詳細: 予想補正値 分析",
            f"""
            <details class="analysis-fold">
              <summary>補正値テーブルを開く（表示 {len(analysis_rows)} / 全 {len(raw_analysis_rows)} 行）</summary>
              <div class="inline-note">買い目に入った選手の基礎点・タイプ補正・最終点です。内訳は先頭だけ表示し、クリックで全文を開けます。</div>
              {analysis_table}
            </details>
            """,
            "dev環境のみ表示します。通常の確認では上の買い候補を優先し、補正値は気になるレースの深掘りに使います。",
        )

    if not prediction_rows:
        body += section("予想", '<div class="empty">予想データがありません。手動実行または毎朝の自動取得後に表示されます。</div>')
    return page("予想", "predictions", body)


def render_prediction_results(conn) -> str:
    latest_result_date = scalar(conn, """
        SELECT MAX(p.race_date)
        FROM race_prediction p
        JOIN race_prediction_result r ON r.prediction_id = p.id
    """)
    all_result_rows = rows(conn, """
        SELECT p.*, r.actual_1st, r.actual_2nd, r.actual_3rd,
               r.actual_1st_candidates, r.actual_2nd_candidates,
               r.actual_3rd_candidates, r.dead_heat,
               r.hit_exact, r.hit_1st, r.hit_top2, r.hit_top3_count,
               r.payout, r.stake_amount AS result_stake_amount,
               r.return_amount, r.roi, r.checked_at,
               COALESCE(s.venue, m.venue) AS venue,
               COALESCE(s.race_no, m.race_no) AS race_no,
               COALESCE(s.race_class, m.race_class) AS race_class,
               s.race_title,
               s.start_time,
               s.lineup_text,
               lf.line_position AS axis_line_position,
               lf.line_size AS axis_line_size,
               lf.followers AS axis_followers,
               lf.is_tanki AS axis_is_tanki,
               lf.is_max_line AS axis_is_max_line,
               lf.bunsen_count AS axis_bunsen_count,
               (
                 SELECT GROUP_CONCAT(e.car_no, ' ')
                 FROM race_entry e
                 WHERE e.race_id = p.race_id
               ) AS entry_car_nos
        FROM race_prediction p
        JOIN race_prediction_result r ON r.prediction_id = p.id
        LEFT JOIN race_schedule s ON s.race_id = p.race_id
        LEFT JOIN race_master m ON m.race_id = p.race_id
        LEFT JOIN race_line_features lf
          ON lf.race_id = p.race_id
         AND lf.car_no = p.predicted_1st
        ORDER BY p.race_date DESC, COALESCE(s.venue, m.venue),
                 COALESCE(s.race_no, m.race_no), p.prediction_type
    """)
    result_rows = [
        row for row in all_result_rows
        if row.get("race_date") == latest_result_date
    ]
    daily_rows = rows(conn, """
        SELECT p.prediction_type,
               COUNT(*) AS predictions,
               SUM(r.hit_exact) AS exact_hits,
               ROUND(AVG(r.hit_exact) * 100, 1) AS exact_rate,
               ROUND(AVG(r.hit_1st) * 100, 1) AS first_rate,
               ROUND(AVG(r.hit_top3_count), 2) AS avg_top3_count,
               SUM(r.stake_amount) AS stake_total,
               SUM(r.return_amount) AS return_total,
               ROUND(SUM(r.return_amount) * 100.0 / NULLIF(SUM(r.stake_amount), 0), 1) AS roi
        FROM race_prediction p
        JOIN race_prediction_result r ON r.prediction_id = p.id
        WHERE p.race_date = COALESCE(?, p.race_date)
        GROUP BY p.prediction_type
    """, (latest_result_date,))
    total = rows(conn, """
        SELECT p.prediction_type,
               COUNT(*) AS predictions,
               SUM(r.hit_exact) AS exact_hits,
               ROUND(AVG(r.hit_exact) * 100, 1) AS exact_rate,
               ROUND(AVG(r.hit_1st) * 100, 1) AS first_rate,
               ROUND(AVG(r.hit_top3_count), 2) AS avg_top3_count,
               SUM(r.stake_amount) AS stake_total,
               SUM(r.return_amount) AS return_total,
               ROUND(SUM(r.return_amount) * 100.0 / NULLIF(SUM(r.stake_amount), 0), 1) AS roi
        FROM race_prediction p
        JOIN race_prediction_result r ON r.prediction_id = p.id
        WHERE COALESCE(p.sample_kind, 'live') = 'live'
        GROUP BY p.prediction_type
    """)
    daily_trend = rows(conn, """
        SELECT p.race_date,
               COALESCE(p.sample_kind, 'live') AS sample_kind,
               COUNT(DISTINCT p.race_id) AS races,
               COUNT(*) AS predictions,
               SUM(r.hit_exact) AS exact_hits,
               ROUND(AVG(r.hit_exact) * 100, 1) AS exact_rate,
               ROUND(AVG(r.hit_1st) * 100, 1) AS first_rate,
               ROUND(AVG(r.hit_top3_count), 2) AS avg_top3_count,
               SUM(r.stake_amount) AS stake_total,
               SUM(r.return_amount) AS return_total,
               ROUND(SUM(r.return_amount) * 100.0 / NULLIF(SUM(r.stake_amount), 0), 1) AS roi
        FROM race_prediction p
        JOIN race_prediction_result r ON r.prediction_id = p.id
        GROUP BY p.race_date, COALESCE(p.sample_kind, 'live')
        ORDER BY p.race_date
    """)
    bet_type_daily = rows(conn, """
        SELECT b.race_date, b.bet_type,
               COUNT(*) AS tickets,
               SUM(r.hit) AS hits,
               ROUND(AVG(r.hit) * 100, 1) AS hit_rate,
               SUM(r.stake_amount) AS stake_total,
               SUM(r.return_amount) AS return_total,
               ROUND(SUM(r.return_amount) * 100.0 / NULLIF(SUM(r.stake_amount), 0), 1) AS roi
        FROM race_prediction_bet b
        JOIN race_prediction_bet_result r ON r.prediction_bet_id = b.id
        GROUP BY b.race_date, b.bet_type
        ORDER BY b.race_date, b.bet_type
    """)
    bet_type_total = rows(conn, """
        SELECT b.bet_type,
               COUNT(*) AS tickets,
               SUM(r.hit) AS hits,
               ROUND(AVG(r.hit) * 100, 1) AS hit_rate,
               SUM(r.stake_amount) AS stake_total,
               SUM(r.return_amount) AS return_total,
               ROUND(SUM(r.return_amount) * 100.0 / NULLIF(SUM(r.stake_amount), 0), 1) AS roi
        FROM race_prediction_bet b
        JOIN race_prediction p ON p.id = b.prediction_id
        JOIN race_prediction_bet_result r ON r.prediction_bet_id = b.id
        WHERE COALESCE(p.sample_kind, 'live') = 'live'
        GROUP BY b.bet_type
        ORDER BY CASE b.bet_type
            WHEN '2車複' THEN 1 WHEN '2車単' THEN 2 WHEN 'ワイド' THEN 3
            WHEN '3連複' THEN 4 WHEN '3連単' THEN 5 ELSE 9 END
    """)
    type_bet_total = rows(conn, """
        SELECT b.prediction_type, b.bet_type,
               COUNT(*) AS tickets,
               SUM(r.hit) AS hits,
               ROUND(AVG(r.hit) * 100, 1) AS hit_rate,
               SUM(r.stake_amount) AS stake_total,
               SUM(r.return_amount) AS return_total,
               ROUND(SUM(r.return_amount) * 100.0 / NULLIF(SUM(r.stake_amount), 0), 1) AS roi
        FROM race_prediction_bet b
        JOIN race_prediction p ON p.id = b.prediction_id
        JOIN race_prediction_bet_result r ON r.prediction_bet_id = b.id
        WHERE COALESCE(p.sample_kind, 'live') = 'live'
        GROUP BY b.prediction_type, b.bet_type
        ORDER BY b.prediction_type, CASE b.bet_type
            WHEN '2車複' THEN 1 WHEN '2車単' THEN 2 WHEN 'ワイド' THEN 3
            WHEN '3連複' THEN 4 WHEN '3連単' THEN 5 ELSE 9 END
    """)

    total_predictions = sum(row["predictions"] for row in daily_rows)
    total_hits = sum(row["exact_hits"] or 0 for row in daily_rows)
    stake_total = sum(row["stake_total"] or 0 for row in daily_rows)
    return_total = sum(row["return_total"] or 0 for row in daily_rows)
    roi_total = (return_total * 100 / stake_total) if stake_total else 0
    race_count = len({row.get("race_id") for row in result_rows})
    first_rate = (
        sum(1 for row in result_rows if row.get("hit_1st")) * 100 / len(result_rows)
        if result_rows else 0
    )
    avg_top3_count = (
        sum(int(row.get("hit_top3_count") or 0) for row in result_rows) / len(result_rows)
        if result_rows else 0
    )
    latest_checked = max((row.get("checked_at") or "" for row in result_rows), default="")
    live_daily_trend = [
        row for row in daily_trend
        if (row.get("sample_kind") or "live") == "live"
    ]
    live_result_rows = [
        row for row in all_result_rows
        if (row.get("sample_kind") or "live") == "live"
    ]
    miss_reason_counts: dict[str, int] = defaultdict(int)
    for row in live_result_rows:
        reason = prediction_miss_reason(row)
        if reason != "的中":
            miss_reason_counts[reason] += 1
    miss_reason_rows = [
        {"reason": reason, "count": count}
        for reason, count in sorted(miss_reason_counts.items(), key=lambda item: item[1], reverse=True)
    ]
    axis_analysis_rows = []
    for row in live_result_rows:
        item = dict(row)
        item["race_context"] = race_context_label(row)
        item["axis_line_role"] = axis_line_role(row)
        item["axis_line_size_label"] = axis_line_size_label(row)
        item["bunsen_label"] = f'{int(row.get("axis_bunsen_count") or 0)}分戦' if row.get("axis_bunsen_count") is not None else "不明"
        item["confidence_label"] = row.get("confidence") or "C"
        axis_analysis_rows.append(item)
    body = f"""
    <div class="grid">
      <div class="card"><span>対象日</span><strong>{h(latest_result_date or "-")}</strong></div>
      <div class="card"><span>判定済みレース数</span><strong>{h(number(race_count))}</strong></div>
      <div class="card"><span>予想件数</span><strong>{h(number(total_predictions))}</strong></div>
      <div class="card"><span>完全的中</span><strong>{h(number(total_hits))}</strong></div>
      <div class="card"><span>1着的中率</span><strong>{h(pct(first_rate))}</strong></div>
      <div class="card"><span>3着内一致平均</span><strong>{h(decimal(avg_top3_count, 2))}</strong></div>
      <div class="card"><span>投資額</span><strong>{h(yen(stake_total))}</strong></div>
      <div class="card"><span>払戻額</span><strong>{h(yen(return_total))}</strong></div>
      <div class="card"><span>回収率</span><strong>{h(pct(roi_total))}</strong></div>
      <div class="card"><span>集計日時</span><strong>{h(latest_checked or "-")}</strong></div>
    </div>
    """
    body += """
    <section>
      <h2>データ更新状況</h2>
      <p class="section-lead">公開JSONから、日次取得・予想生成・予想結果評価の進み具合を確認します。</p>
      <div class="grid" id="prediction-result-freshness">
        <div class="card"><span>生成日時</span><strong>-</strong></div>
        <div class="card"><span>最新結果日</span><strong>-</strong></div>
        <div class="card"><span>最新予想日</span><strong>-</strong></div>
        <div class="card"><span>最新評価日</span><strong>-</strong></div>
        <div class="card"><span>未評価予想</span><strong>-</strong></div>
        <div class="card"><span>予想なし結果</span><strong>-</strong></div>
      </div>
      <div class="inline-note" id="prediction-result-freshness-note">更新状況を読み込んでいます。</div>
      <div id="prediction-result-daily-counts"></div>
    </section>
    <script>
    (() => {
      const freshness = document.getElementById("prediction-result-freshness");
      const note = document.getElementById("prediction-result-freshness-note");
      const daily = document.getElementById("prediction-result-daily-counts");
      if (!freshness || !note || !daily) return;

      const safe = (value) => value === undefined || value === null || value === "" ? "-" : String(value);
      const number = (value) => Number(value || 0).toLocaleString("ja-JP");
      const setCards = (summary) => {
        const items = [
          ["生成日時", summary.generated_at],
          ["最新結果日", summary.latest_result_date],
          ["最新予想日", summary.latest_prediction_date],
          ["最新評価日", summary.latest_prediction_result_date],
          ["未評価予想", number(summary.unevaluated_prediction_count)],
          ["予想なし結果", number(summary.result_races_without_predictions)]
        ];
        freshness.innerHTML = items.map(([label, value]) => (
          `<div class="card"><span>${label}</span><strong>${safe(value)}</strong></div>`
        )).join("");
      };
      const renderDaily = (rows) => {
        const latest = rows.slice(0, 10);
        daily.innerHTML = `
          <table>
            <thead>
              <tr>
                <th>日付</th>
                <th>番組</th>
                <th>結果</th>
                <th>予想</th>
                <th>評価済み</th>
              </tr>
            </thead>
            <tbody>
              ${latest.map((row) => `
                <tr>
                  <td>${safe(row.race_date)}</td>
                  <td>${number(row.scheduled_races)}</td>
                  <td>${number(row.result_races)}</td>
                  <td>${number(row.predictions)}</td>
                  <td>${number(row.prediction_results)}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        `;
      };
      const statusMessage = (summary) => {
        const resultDate = summary.latest_result_date || "";
        const evaluatedDate = summary.latest_prediction_result_date || "";
        const predictionDate = summary.latest_prediction_date || "";
        const unevaluated = Number(summary.unevaluated_prediction_count || 0);
        const missingPredictions = Number(summary.result_races_without_predictions || 0);
        const messages = [];
        if (resultDate && evaluatedDate && resultDate > evaluatedDate) {
          messages.push(`結果は ${resultDate} までありますが、予想結果評価は ${evaluatedDate} までです。`);
        }
        if (predictionDate && resultDate && predictionDate > resultDate) {
          messages.push(`予想は ${predictionDate} まで作成済みです。結果反映後に評価されます。`);
        }
        if (unevaluated > 0) {
          messages.push(`未評価予想が ${number(unevaluated)} 件あります。`);
        }
        if (missingPredictions > 0) {
          messages.push(`結果はあるが予想がないレースが ${number(missingPredictions)} 件あります。`);
        }
        return messages.length ? messages.join(" ") : "日次取得、予想生成、予想結果評価は同期しています。";
      };
      Promise.all([
        fetch("data/public_summary.json", { cache: "no-store" }).then((response) => response.json()),
        fetch("data/daily_counts.json", { cache: "no-store" }).then((response) => response.json())
      ]).then(([summary, counts]) => {
        setCards(summary);
        renderDaily(Array.isArray(counts) ? counts : []);
        note.textContent = statusMessage(summary);
      }).catch(() => {
        note.textContent = "更新状況JSONを読み込めませんでした。";
      });
    })();
    </script>
    """

    def format_stats(items: list[dict]) -> list[dict]:
        formatted = []
        for row in sorted(items, key=lambda item: prediction_type_order(item["prediction_type"])):
            formatted.append({
                "prediction_type": row["prediction_type"],
                "predictions": row["predictions"],
                "exact_hits": row["exact_hits"] or 0,
                "exact_rate": pct(row["exact_rate"]),
                "first_rate": pct(row["first_rate"]),
                "avg_top3_count": decimal(row["avg_top3_count"], 2),
                "stake_total": yen(row["stake_total"]),
                "return_total": yen(row["return_total"]),
                "roi": pct(row["roi"]),
            })
        return formatted

    if not result_rows:
        body += section("予想結果", '<div class="empty">判定済みの予想結果がありません。</div>')
        return page("予想結果", "prediction-results", body)

    sample_kind_labels = {
        "live": "実運用",
        "backtest": "バックテスト",
        "reference": "参考値",
    }
    trend_display = []
    for row in daily_trend:
        trend_display.append({
            "race_date": row["race_date"],
            "sample_kind": sample_kind_labels.get(row["sample_kind"], row["sample_kind"]),
            "races": row["races"],
            "predictions": row["predictions"],
            "exact_hits": row["exact_hits"] or 0,
            "exact_rate": pct(row["exact_rate"]),
            "first_rate": pct(row["first_rate"]),
            "avg_top3_count": decimal(row["avg_top3_count"], 2),
            "stake_total": yen(row["stake_total"]),
            "return_total": yen(row["return_total"]),
            "roi": pct(row["roi"]),
            "_class": "sample-low" if row["sample_kind"] == "reference" else "",
        })
    trend_section = section("日別 的中率・回収率推移", f"""
      <div class="grid two">
        {section("完全的中率", bar_chart(daily_trend, "race_date", "exact_rate", pct, 30))}
        {section("1着的中率", bar_chart(daily_trend, "race_date", "first_rate", pct, 30))}
      </div>
      {table(
          ["日付", "区分", "レース", "予想数", "完全的中", "完全的中率", "1着的中率", "3着内一致平均", "投資額", "払戻額", "回収率"],
          trend_display,
          ["race_date", "sample_kind", "races", "predictions", "exact_hits", "exact_rate", "first_rate", "avg_top3_count", "stake_total", "return_total", "roi"],
      )}
    """, "参考値は薄く表示します。6月13日のように結果取得後に生成された予想は、実運用成績と区別して確認できます。")

    bet_total_display = [
        {
            "bet_type": row["bet_type"],
            "tickets": row["tickets"],
            "hits": row["hits"] or 0,
            "hit_rate": pct(row["hit_rate"]),
            "stake_total": yen(row["stake_total"]),
            "return_total": yen(row["return_total"]),
            "roi": pct(row["roi"]),
        }
        for row in bet_type_total
    ]
    bet_total_section = section("累積 live 賭式別成績", table(
        ["賭式", "買い目数", "的中", "的中率", "投資額", "払戻額", "回収率"],
        bet_total_display,
        ["bet_type", "tickets", "hits", "hit_rate", "stake_total", "return_total", "roi"],
    ), "2車複・2車単・3連複・3連単は各1点、ワイドは上位3車の組み合わせ3点を各100円で集計します。")

    bet_daily_display = [
        {
            "race_date": row["race_date"],
            "bet_type": row["bet_type"],
            "tickets": row["tickets"],
            "hits": row["hits"] or 0,
            "hit_rate": pct(row["hit_rate"]),
            "stake_total": yen(row["stake_total"]),
            "return_total": yen(row["return_total"]),
            "roi": pct(row["roi"]),
        }
        for row in bet_type_daily
    ]
    bet_daily_section = section("日別 賭式別成績", table(
        ["日付", "賭式", "買い目数", "的中", "的中率", "投資額", "払戻額", "回収率"],
        bet_daily_display,
        ["race_date", "bet_type", "tickets", "hits", "hit_rate", "stake_total", "return_total", "roi"],
    ))

    type_chart_rows = [
        {
            "prediction_type": row["prediction_type"],
            "exact_rate": row["exact_rate"] or 0,
            "first_rate": row["first_rate"] or 0,
            "avg_top3_count": row["avg_top3_count"] or 0,
        }
        for row in sorted(total, key=lambda item: prediction_type_order(item["prediction_type"]))
    ]
    bet_chart_rows = [
        {
            "bet_type": row["bet_type"],
            "hit_rate": row["hit_rate"] or 0,
            "roi": row["roi"] or 0,
        }
        for row in sorted(
            bet_type_total,
            key=lambda item: PREDICTION_BET_TYPES.index(item["bet_type"]) if item["bet_type"] in PREDICTION_BET_TYPES else 99,
        )
    ]
    body += section("予想結果グラフ", f"""
      <div class="result-graph-grid">
        {section("日別 的中率推移", line_chart(
            live_daily_trend,
            "race_date",
            [
                ("exact_rate", "完全的中率", "#0f766e"),
                ("first_rate", "1着的中率", "#1d4ed8"),
            ],
            30,
        ))}
        {section("予想タイプ別 的中率", bar_chart(type_chart_rows, "prediction_type", "exact_rate", pct, 12))}
        {section("賭式別 的中率", bar_chart(bet_chart_rows, "bet_type", "hit_rate", pct, 8))}
        {section("外れ理由ランキング", bar_chart(miss_reason_rows, "reason", "count", lambda value: f"{int(value)}件", 8))}
      </div>
    """, "日々の推移、予想タイプ、賭式、外れ方をまとめて確認できます。外れ理由はlive予想の累計から分類しています。")

    axis_by_context = axis_condition_rows(axis_analysis_rows, "race_context", "レース種別")
    axis_by_line_role = axis_condition_rows(axis_analysis_rows, "axis_line_role", "ライン位置")
    axis_by_line_size = axis_condition_rows(axis_analysis_rows, "axis_line_size_label", "ライン規模")
    axis_by_confidence = axis_condition_rows(axis_analysis_rows, "confidence_label", "信頼度")
    body += section("軸飛び条件分析", f"""
      <div class="result-graph-grid">
        {section("レース種別別 軸飛び率", bar_chart(axis_by_context, "condition", "axis_miss_rate", pct, 12))}
        {section("ライン位置別 軸飛び率", bar_chart(axis_by_line_role, "condition", "axis_miss_rate", pct, 8))}
        {section("ライン規模別 軸飛び率", bar_chart(axis_by_line_size, "condition", "axis_miss_rate", pct, 8))}
        {section("信頼度別 軸飛び率", bar_chart(axis_by_confidence, "condition", "axis_miss_rate", pct, 8))}
      </div>
      <details class="analysis-fold">
        <summary>軸飛び条件テーブルを開く</summary>
        <div class="grid two">
          {section("レース種別", table(
              ["条件", "軸飛び", "軸飛び率", "完全的中率", "予想数", "回収率"],
              format_axis_condition_rows(axis_by_context),
              ["condition", "axis_miss_count", "axis_miss_rate", "exact_rate", "predictions", "roi"],
          ))}
          {section("ライン位置", table(
              ["条件", "軸飛び", "軸飛び率", "完全的中率", "予想数", "回収率"],
              format_axis_condition_rows(axis_by_line_role),
              ["condition", "axis_miss_count", "axis_miss_rate", "exact_rate", "predictions", "roi"],
          ))}
          {section("ライン規模", table(
              ["条件", "軸飛び", "軸飛び率", "完全的中率", "予想数", "回収率"],
              format_axis_condition_rows(axis_by_line_size),
              ["condition", "axis_miss_count", "axis_miss_rate", "exact_rate", "predictions", "roi"],
          ))}
          {section("信頼度", table(
              ["条件", "軸飛び", "軸飛び率", "完全的中率", "予想数", "回収率"],
              format_axis_condition_rows(axis_by_confidence),
              ["condition", "axis_miss_count", "axis_miss_rate", "exact_rate", "predictions", "roi"],
          ))}
        </div>
      </details>
    """, "軸候補（予想1着）が外れた条件をlive予想の累計で分解します。サンプルが少ない条件は薄く表示します。")

    exact_hit_count = sum(1 for row in result_rows if row.get("hit_exact"))
    return_hit_count = sum(1 for row in result_rows if int(row.get("return_amount") or 0) > 0)
    first_miss_count = sum(1 for row in result_rows if not row.get("hit_1st"))
    body += f"""
    <div class="result-focus">
      <div class="decision-card hit"><span>完全的中</span><strong>{h(number(exact_hit_count))}</strong><small>順位まで一致した予想</small></div>
      <div class="decision-card return"><span>回収あり</span><strong>{h(number(return_hit_count))}</strong><small>払戻が発生した予想</small></div>
      <div class="decision-card miss"><span>1着不一致</span><strong>{h(number(first_miss_count))}</strong><small>軸候補の見直し対象</small></div>
    </div>
    """

    focus_rows = []
    for row in sorted(result_rows, key=lambda item: int(item.get("return_amount") or 0), reverse=True):
        if int(row.get("return_amount") or 0) <= 0:
            continue
        race_label = f'{row.get("venue") or ""} {row.get("race_no") or ""}R'
        focus_rows.append({
            "focus": pill("回収あり", "buy"),
            "race": race_detail_link(row.get("race_id"), race_label),
            "prediction_type": row.get("prediction_type"),
            "predicted": prediction_combo(row),
            "actual": actual_combo(row),
            "judgment": f'<span class="{"hit" if row.get("hit_exact") else "miss"}">{h(prediction_result_label(row))}</span>',
            "return_amount": yen(row.get("return_amount")),
            "roi": pct(row.get("roi")),
        })
        if len(focus_rows) >= 5:
            break
    for row in result_rows:
        if row.get("hit_exact") or not row.get("hit_1st"):
            continue
        race_label = f'{row.get("venue") or ""} {row.get("race_no") or ""}R'
        focus_rows.append({
            "focus": pill("惜しい", "caution"),
            "race": race_detail_link(row.get("race_id"), race_label),
            "prediction_type": row.get("prediction_type"),
            "predicted": prediction_combo(row),
            "actual": actual_combo(row),
            "judgment": f'<span class="miss">{h(prediction_result_label(row))}</span>',
            "return_amount": yen(row.get("return_amount")),
            "roi": pct(row.get("roi")),
        })
        if len(focus_rows) >= 10:
            break
    body += section("当日の要点", rich_table(
        ["分類", "レース", "予想タイプ", "予想", "結果", "判定", "回収額", "回収率"],
        focus_rows,
        ["focus", "race", "prediction_type", "predicted", "actual", "judgment", "return_amount", "roi"],
        "当日の回収あり・惜しい予想はありません。",
    ), "回収が出た予想と、1着は取れて順位が崩れた予想を先に確認できます。")

    featured_display = []
    for row in featured_prediction_rows(result_rows):
        race_label = f'{row.get("venue") or ""} {row.get("race_no") or ""}R'
        featured_display.append({
            "prediction_type": row.get("prediction_type"),
            "race": race_detail_link(row.get("race_id"), race_label),
            "start_time": row.get("start_time"),
            "predicted": prediction_combo(row),
            "actual": actual_combo(row),
            "judgment": f'<span class="{"hit" if row.get("hit_exact") else "miss"}">{h(prediction_result_label(row))}</span>',
            "hit_1st": "○" if row.get("hit_1st") else "×",
            "hit_top3_count": row.get("hit_top3_count"),
            "return_amount": yen(row.get("return_amount")),
            "roi": pct(row.get("roi")),
        })
    body += section("今日の注目予想 結果", rich_table(
        ["予想タイプ", "レース", "発走", "予想", "結果", "判定", "1着", "3着内一致", "回収額", "回収率"],
        featured_display,
        ["prediction_type", "race", "start_time", "predicted", "actual", "judgment", "hit_1st", "hit_top3_count", "return_amount", "roi"],
    ), "予想ページの注目予想と同じ条件で、各タイプ3件まで答え合わせします。")

    def risk_stats(group_expr: str, where_extra: str = "") -> list[dict]:
        query = f"""
            SELECT {group_expr} AS bucket,
                   COUNT(*) AS predictions,
                   SUM(r.hit_exact) AS exact_hits,
                   ROUND(AVG(r.hit_exact) * 100, 1) AS exact_rate,
                   ROUND(AVG(r.hit_1st) * 100, 1) AS first_rate,
                   ROUND(AVG(r.hit_top3_count), 2) AS avg_top3_count,
                   SUM(r.stake_amount) AS stake_total,
                   SUM(r.return_amount) AS return_total,
                   ROUND(SUM(r.return_amount) * 100.0 / NULLIF(SUM(r.stake_amount), 0), 1) AS roi
            FROM race_prediction p
            JOIN race_prediction_result r ON r.prediction_id = p.id
            LEFT JOIN race_confidence c ON c.race_id = p.race_id
            LEFT JOIN race_volatility_features v ON v.race_id = p.race_id
            WHERE p.prediction_type = 'feature_line_mix'
              AND COALESCE(p.sample_kind, 'live') = 'live'
              {where_extra}
            GROUP BY bucket
            ORDER BY bucket
        """
        return [
            {
                "bucket": row["bucket"],
                "predictions": row["predictions"],
                "exact_hits": row["exact_hits"] or 0,
                "exact_rate": pct(row["exact_rate"]),
                "first_rate": pct(row["first_rate"]),
                "avg_top3_count": decimal(row["avg_top3_count"], 2),
                "stake_total": yen(row["stake_total"]),
                "return_total": yen(row["return_total"]),
                "roi": pct(row["roi"]),
            }
            for row in rows(conn, query)
        ]

    confidence_distribution_display = [
        {
            "bucket": row["bucket"],
            "races": row["races"],
            "avg_confidence": decimal(row["avg_confidence"], 3),
            "avg_expected_value": decimal(row["avg_expected_value"], 3),
        }
        for row in rows(conn, """
            SELECT CASE
                     WHEN confidence_score >= 0.9 THEN '0.9-1.0'
                     WHEN confidence_score >= 0.8 THEN '0.8-0.9'
                     WHEN confidence_score >= 0.7 THEN '0.7-0.8'
                     WHEN confidence_score >= 0.6 THEN '0.6-0.7'
                     WHEN confidence_score >= 0.5 THEN '0.5-0.6'
                     WHEN confidence_score >= 0.4 THEN '0.4-0.5'
                     WHEN confidence_score >= 0.3 THEN '0.3-0.4'
                     WHEN confidence_score >= 0.2 THEN '0.2-0.3'
                     WHEN confidence_score >= 0.1 THEN '0.1-0.2'
                     ELSE '0.0-0.1'
                   END AS bucket,
                   COUNT(*) AS races,
                   ROUND(AVG(confidence_score), 3) AS avg_confidence,
                   ROUND(AVG(expected_value_score), 3) AS avg_expected_value
            FROM race_confidence
            GROUP BY bucket
            ORDER BY bucket
        """)
    ]
    confidence_perf = risk_stats("""
        CASE
          WHEN c.confidence_score >= 0.9 THEN '0.9-1.0'
          WHEN c.confidence_score >= 0.8 THEN '0.8-0.9'
          WHEN c.confidence_score >= 0.7 THEN '0.7-0.8'
          WHEN c.confidence_score >= 0.6 THEN '0.6-0.7'
          WHEN c.confidence_score >= 0.5 THEN '0.5-0.6'
          WHEN c.confidence_score >= 0.4 THEN '0.4-0.5'
          WHEN c.confidence_score >= 0.3 THEN '0.3-0.4'
          WHEN c.confidence_score >= 0.2 THEN '0.2-0.3'
          WHEN c.confidence_score >= 0.1 THEN '0.1-0.2'
          ELSE '0.0-0.1'
        END
    """, "AND c.race_id IS NOT NULL")
    volatility_perf = risk_stats("""
        CASE
          WHEN v.volatility_probability >= 0.7 THEN 'high'
          WHEN v.volatility_probability >= 0.4 THEN 'middle'
          ELSE 'low'
        END
    """, "AND v.race_id IS NOT NULL")
    max_line_perf = risk_stats("CAST(c.max_line_members AS TEXT)", "AND c.race_id IS NOT NULL")
    line_count_perf = risk_stats("CAST(v.line_count AS TEXT)", "AND v.race_id IS NOT NULL")
    tanki_count_perf = risk_stats("CAST(v.tanki_count AS TEXT)", "AND v.race_id IS NOT NULL")
    expected_value_perf = risk_stats("""
        CASE
          WHEN c.expected_value_score >= 0.8 THEN '0.8-1.0'
          WHEN c.expected_value_score >= 0.6 THEN '0.6-0.8'
          WHEN c.expected_value_score >= 0.4 THEN '0.4-0.6'
          WHEN c.expected_value_score >= 0.2 THEN '0.2-0.4'
          ELSE '0.0-0.2'
        END
    """, "AND c.race_id IS NOT NULL")
    risk_headers = ["区分", "予想数", "完全的中", "完全的中率", "1着率", "3着内平均", "投資", "払戻", "回収率"]
    risk_fields = ["bucket", "predictions", "exact_hits", "exact_rate", "first_rate", "avg_top3_count", "stake_total", "return_total", "roi"]
    body += section("feature_line_mix 回収率改善分析", f"""
      <div class="grid two">
        {section("confidence_score分布", table(
            ["区分", "レース数", "平均confidence", "平均期待値"],
            confidence_distribution_display,
            ["bucket", "races", "avg_confidence", "avg_expected_value"],
        ))}
        {section("confidence別成績", table(risk_headers, confidence_perf, risk_fields))}
        {section("荒れる確率別成績", table(risk_headers, volatility_perf, risk_fields))}
        {section("ライン人数別成績", table(risk_headers, max_line_perf, risk_fields))}
        {section("分線数別成績", table(risk_headers, line_count_perf, risk_fields))}
        {section("単騎数別成績", table(risk_headers, tanki_count_perf, risk_fields))}
        {section("期待値スコア別成績", table(risk_headers, expected_value_perf, risk_fields))}
      </div>
    """, "feature_line_mixを買う/見送る条件を確認するための分析です。予想ロジック自体は変更せず、confidence・荒れ度・ライン構成ごとの回収率を比較します。")

    result_dates = sorted(
        {row.get("race_date") for row in all_result_rows if row.get("race_date")},
        reverse=True,
    )
    date_options = "".join(
        f'<option value="{h(item)}"{" selected" if item == latest_result_date else ""}>{h(item)}</option>'
        for item in result_dates
    )
    venues = sorted({row.get("venue") for row in all_result_rows if row.get("venue")})
    venue_options = "".join(f'<option value="{h(venue)}">{h(venue)}</option>' for venue in venues)
    grouped: dict[str, dict] = {}
    for row in all_result_rows:
        race_id = row.get("race_id") or ""
        race_date = row.get("race_date") or ""
        group_key = f"{race_date}:{race_id}"
        group = grouped.setdefault(group_key, {
            "race_id": race_id,
            "race_date": race_date,
            "venue": row.get("venue") or "",
            "race_no": row.get("race_no") or "",
            "start_time": row.get("start_time") or "",
            "actual": actual_combo(row),
            "lineup_text": compact_lineup_text(row.get("lineup_text"), row.get("entry_car_nos")),
            "predictions": {},
        })
        group["predictions"][row.get("prediction_type")] = row

    all_rows = []
    duplicate_groups = {"あり": [], "なし": []}
    for group in sorted(
        grouped.values(),
        key=lambda item: (item["race_date"], item["venue"], int(item["race_no"] or 0)),
        reverse=True,
    ):
        predictions = group["predictions"]
        combos = [prediction_combo(item) for item in predictions.values()]
        duplicate = len(combos) != len(set(combos))
        confidences = " ".join(sorted({str(item.get("confidence") or "C") for item in predictions.values()}))
        types = " ".join(predictions.keys())
        judgments = " ".join(sorted({prediction_result_label(item) for item in predictions.values()}))
        has_return = any(int(item.get("return_amount") or 0) > 0 for item in predictions.values())
        if group["race_date"] == latest_result_date:
            duplicate_groups["あり" if duplicate else "なし"].extend(predictions.values())
        race_label = f'{group["venue"]} {group["race_no"]}R'
        cells = {
            "race_date": group["race_date"],
            "race": race_detail_link(group["race_id"], race_label),
            "start_time": group["start_time"],
            "actual": group["actual"],
            "lineup_text": group["lineup_text"],
            "duplicate": "あり" if duplicate else "なし",
            "_data": {
                "date": group["race_date"],
                "venue": group["venue"],
                "confidence": confidences,
                "type": types,
                "judgment": judgments,
                "duplicate": "yes" if duplicate else "no",
                "return": "yes" if has_return else "no",
            },
        }
        for prediction_type in PREDICTION_TYPE_ORDER:
            cells[prediction_type] = prediction_result_cell(predictions.get(prediction_type))
        all_rows.append(cells)

    body += section("当日全レース予想 結果", f"""
      <div class="filters" id="prediction-result-filters">
        <label>日付<select id="result-filter-date">{date_options}</select></label>
        <label>会場<select id="result-filter-venue"><option value="">すべて</option>{venue_options}</select></label>
        <label>信頼度<select id="result-filter-confidence"><option value="">すべて</option><option value="A">A</option><option value="B">B</option><option value="C">C</option></select></label>
        <label>予想タイプ<select id="result-filter-type"><option value="">すべて</option>{''.join(f'<option value="{h(item)}">{h(item)}</option>' for item in PREDICTION_TYPE_ORDER)}</select></label>
        <label>判定<select id="result-filter-judgment"><option value="">すべて</option><option value="完全的中">完全的中</option><option value="1着的中">1着的中</option><option value="3着内一致">3着内一致</option><option value="不的中">不的中</option></select></label>
        <label>重複買い目<select id="result-filter-duplicate"><option value="">すべて</option><option value="yes">あり</option><option value="no">なし</option></select></label>
        <label>回収<select id="result-filter-return"><option value="">すべて</option><option value="yes">あり</option><option value="no">なし</option></select></label>
      </div>
      {rich_table(
          ["日付", "レース", "発走", "結果", "並び", *PREDICTION_TYPE_ORDER, "重複"],
          all_rows,
          ["race_date", "race", "start_time", "actual", "lineup_text", *PREDICTION_TYPE_ORDER, "duplicate"],
      ).replace("<table>", '<table id="all-race-prediction-results">', 1)}
      <script>
      (() => {{
        const table = document.getElementById("all-race-prediction-results");
        if (!table) return;
        const date = document.getElementById("result-filter-date");
        const venue = document.getElementById("result-filter-venue");
        const confidence = document.getElementById("result-filter-confidence");
        const type = document.getElementById("result-filter-type");
        const judgment = document.getElementById("result-filter-judgment");
        const duplicate = document.getElementById("result-filter-duplicate");
        const returned = document.getElementById("result-filter-return");
        const rows = Array.from(table.querySelectorAll("tbody tr"));
        const apply = () => {{
          rows.forEach((row) => {{
            const show =
              (!date.value || row.dataset.date === date.value) &&
              (!venue.value || row.dataset.venue === venue.value) &&
              (!confidence.value || (row.dataset.confidence || "").includes(confidence.value)) &&
              (!type.value || (row.dataset.type || "").includes(type.value)) &&
              (!judgment.value || (row.dataset.judgment || "").includes(judgment.value)) &&
              (!duplicate.value || row.dataset.duplicate === duplicate.value) &&
              (!returned.value || row.dataset.return === returned.value);
            row.hidden = !show;
          }});
        }};
        [date, venue, confidence, type, judgment, duplicate, returned].forEach((item) => item.addEventListener("change", apply));
        apply();
      }})();
      </script>
    """, "日付を切り替え、会場・R順で各レースの結果と5タイプの買い目を横並びで比較できます。")

    daily_type_section = section("日別 予想タイプ別成績", table(
        ["予想タイプ", "予想数", "完全的中", "完全的中率", "1着的中率", "3着内一致平均", "投資額", "払戻額", "回収率"],
        format_stats(daily_rows),
        ["prediction_type", "predictions", "exact_hits", "exact_rate", "first_rate", "avg_top3_count", "stake_total", "return_total", "roi"],
    ))
    total_type_section = section("累積 live 予想タイプ別成績", table(
        ["予想タイプ", "予想数", "完全的中", "完全的中率", "1着的中率", "3着内一致平均", "投資額", "払戻額", "回収率"],
        format_stats(total),
        ["prediction_type", "predictions", "exact_hits", "exact_rate", "first_rate", "avg_top3_count", "stake_total", "return_total", "roi"],
    ), "1点100円購入想定です。完全的中時のみ3連単払戻を回収額に入れます。")
    type_bet_display = [
        {
            "prediction_type": row["prediction_type"],
            "bet_type": row["bet_type"],
            "tickets": row["tickets"],
            "hits": row["hits"] or 0,
            "hit_rate": pct(row["hit_rate"]),
            "stake_total": yen(row["stake_total"]),
            "return_total": yen(row["return_total"]),
            "roi": pct(row["roi"]),
        }
        for row in sorted(
            type_bet_total,
            key=lambda item: (
                prediction_type_order(item["prediction_type"]),
                PREDICTION_BET_TYPES.index(item["bet_type"]) if item["bet_type"] in PREDICTION_BET_TYPES else 99,
            ),
        )
    ]
    type_bet_section = section("累積 live 予想タイプ×賭式別成績", table(
        ["予想タイプ", "賭式", "買い目数", "的中", "的中率", "投資額", "払戻額", "回収率"],
        type_bet_display,
        ["prediction_type", "bet_type", "tickets", "hits", "hit_rate", "stake_total", "return_total", "roi"],
    ))

    def stats_from_group(label: str, items: list[dict]) -> dict:
        stake = sum(int(item.get("result_stake_amount") or item.get("stake_amount") or 0) for item in items)
        returned = sum(int(item.get("return_amount") or 0) for item in items)
        return {
            "group": label,
            "predictions": len(items),
            "exact_rate": pct(sum(1 for item in items if item.get("hit_exact")) * 100 / len(items) if items else 0),
            "first_rate": pct(sum(1 for item in items if item.get("hit_1st")) * 100 / len(items) if items else 0),
            "avg_top3_count": decimal(sum(int(item.get("hit_top3_count") or 0) for item in items) / len(items), 2) if items else "0.00",
            "stake_total": yen(stake),
            "return_total": yen(returned),
            "roi": pct(returned * 100 / stake if stake else 0),
        }

    confidence_groups = []
    for confidence in ["A", "B", "C"]:
        items = [row for row in result_rows if (row.get("confidence") or "C") == confidence]
        if items:
            confidence_groups.append(stats_from_group(confidence, items))
    duplicate_rows = [
        stats_from_group(label, items)
        for label, items in duplicate_groups.items()
        if items
    ]
    confidence_grid_section = '<div class="grid two">'
    confidence_grid_section += section("信頼度別成績", table(
        ["信頼度", "予想数", "完全的中率", "1着的中率", "3着内一致平均", "投資額", "払戻額", "回収率"],
        confidence_groups,
        ["group", "predictions", "exact_rate", "first_rate", "avg_top3_count", "stake_total", "return_total", "roi"],
    ))
    confidence_grid_section += section("重複買い目別成績", table(
        ["重複買い目", "予想数", "完全的中率", "1着的中率", "3着内一致平均", "投資額", "払戻額", "回収率"],
        duplicate_rows,
        ["group", "predictions", "exact_rate", "first_rate", "avg_top3_count", "stake_total", "return_total", "roi"],
    ))
    confidence_grid_section += "</div>"

    body += f"""
    <details class="analysis-fold">
      <summary>成績分析を開く</summary>
      {daily_type_section}
      {confidence_grid_section}
      {trend_section}
      {bet_total_section}
      {bet_daily_section}
      {total_type_section}
      {type_bet_section}
    </details>
    """

    if is_dev_environment():
        component_detail_rows = component_result_analysis_rows(result_rows)
        component_summary_rows = component_result_summary_rows(component_detail_rows)[:COMPONENT_ANALYSIS_ROW_LIMIT]
        body += section(
            "補正項目別 成績分析",
            render_component_result_analysis(component_summary_rows, component_detail_rows),
            "dev環境のみ表示します。フィルタに合わせて指標、グラフ、ランキング、補正項目マップが変化します。",
        )

    historical_rows = rows(conn, """
        SELECT p.*, r.actual_1st, r.actual_2nd, r.actual_3rd,
               r.actual_1st_candidates, r.actual_2nd_candidates,
               r.actual_3rd_candidates, r.dead_heat,
               r.hit_exact, r.hit_1st, r.hit_top2, r.hit_top3_count,
               r.payout, r.return_amount, r.roi, r.checked_at,
               COALESCE(s.venue, m.venue) AS venue,
               COALESCE(s.race_no, m.race_no) AS race_no
        FROM race_prediction p
        JOIN race_prediction_result r ON r.prediction_id = p.id
        LEFT JOIN race_schedule s ON s.race_id = p.race_id
        LEFT JOIN race_master m ON m.race_id = p.race_id
        ORDER BY p.race_date DESC, COALESCE(s.venue, m.venue),
                 COALESCE(s.race_no, m.race_no), p.prediction_type
        LIMIT 3000
    """)
    historical_bets = rows(conn, """
        SELECT b.prediction_id, b.bet_type, b.combination,
               r.hit, r.return_amount
        FROM race_prediction_bet b
        JOIN race_prediction_bet_result r ON r.prediction_bet_id = b.id
        ORDER BY b.prediction_id,
                 CASE b.bet_type
                   WHEN '2車複' THEN 1 WHEN '2車単' THEN 2 WHEN 'ワイド' THEN 3
                   WHEN '3連複' THEN 4 WHEN '3連単' THEN 5 ELSE 9 END,
                 b.combination
    """)
    bets_by_prediction: dict[int, list[str]] = defaultdict(list)
    for bet in historical_bets:
        mark = "○" if bet.get("hit") else "×"
        returned = f' {yen(bet.get("return_amount"))}' if bet.get("return_amount") else ""
        bets_by_prediction[int(bet["prediction_id"])].append(
            f'{bet["bet_type"]} {bet["combination"]}{mark}{returned}'
        )

    details = []
    for row in historical_rows:
        sample_kind = row.get("sample_kind") or "live"
        race_label = f'{row.get("race_date") or ""} {row.get("venue") or ""} {row.get("race_no") or ""}R'
        details.append({
            "prediction_type": row["prediction_type"],
            "race": race_detail_link(row.get("race_id"), race_label),
            "sample_kind": sample_kind_labels.get(sample_kind, sample_kind),
            "predicted": prediction_combo(row),
            "actual": actual_combo(row),
            "judgment": f'<span class="{"hit" if row.get("hit_exact") else "miss"}">{h(prediction_result_label(row))}</span>',
            "hit_1st": "○" if row["hit_1st"] else "×",
            "hit_top3_count": row["hit_top3_count"],
            "payout": yen(row["payout"]),
            "return_amount": yen(row["return_amount"]),
            "roi": pct(row["roi"]),
            "bet_results": " / ".join(bets_by_prediction.get(int(row["id"]), [])),
            "_class": "sample-low" if sample_kind == "reference" else "",
            "_data": {"date": row.get("race_date") or ""},
        })
    result_dates = sorted({row.get("race_date") for row in historical_rows if row.get("race_date")}, reverse=True)
    date_options = "".join(f'<option value="{h(item)}">{h(item)}</option>' for item in result_dates)
    history_section = section("予想結果 明細", f"""
      <div class="filters">
        <label>対象日
          <select id="history-result-date">
            <option value="">すべて</option>
            {date_options}
          </select>
        </label>
      </div>
      {rich_table(
          ["予想タイプ", "レース", "区分", "順位予想", "結果", "判定", "1着", "3着内一致", "3連単払戻", "回収額", "回収率", "賭式別結果"],
          details,
          ["prediction_type", "race", "sample_kind", "predicted", "actual", "judgment", "hit_1st", "hit_top3_count", "payout", "return_amount", "roi", "bet_results"],
      ).replace("<table>", '<table id="prediction-result-history">', 1)}
      <script>
      (() => {{
        const select = document.getElementById("history-result-date");
        const table = document.getElementById("prediction-result-history");
        if (!select || !table) return;
        const rows = Array.from(table.querySelectorAll("tbody tr"));
        const apply = () => rows.forEach((row) => {{
          row.hidden = Boolean(select.value && row.dataset.date !== select.value);
        }});
        select.addEventListener("change", apply);
      }})();
      </script>
    """, "日付を選ぶと、その日の全予想と5賭式の判定を確認できます。")
    body += f"""
    <details class="analysis-fold">
      <summary>過去明細を開く</summary>
      {history_section}
    </details>
    """
    return page("予想結果", "prediction-results", body)


def render_lineup_features(conn) -> str:
    sample_rows = rows(conn, """
        SELECT race_id, car_no, racer_name, line_no, line_size, line_position,
               followers, is_tanki, is_max_line, bunsen_count
        FROM race_line_features
        ORDER BY race_date DESC, venue, race_no, line_no, line_position
        LIMIT 200
    """)
    stat_rows = rows(conn, """
        SELECT racer_name, condition_type, condition_key, races, wins, top2, top3,
               win_rate, top2_rate, top3_rate
        FROM racer_line_condition_stats
        ORDER BY races DESC, condition_type, racer_name
        LIMIT 200
    """)
    leader_rows = rows(conn, """
        SELECT followers,
               COUNT(DISTINCT race_id) AS race_count,
               COUNT(*) AS races,
               SUM(CASE WHEN rank = 1 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN rank <= 2 THEN 1 ELSE 0 END) AS top2,
               SUM(CASE WHEN rank <= 3 THEN 1 ELSE 0 END) AS top3,
               AVG(CASE WHEN rank = 1 THEN 1.0 ELSE 0 END) * 100 AS win_rate,
               AVG(CASE WHEN rank <= 2 THEN 1.0 ELSE 0 END) * 100 AS top2_rate,
               AVG(CASE WHEN rank <= 3 THEN 1.0 ELSE 0 END) * 100 AS top3_rate
        FROM race_line_features
        WHERE is_leader = 1 AND rank IS NOT NULL
        GROUP BY followers
        ORDER BY followers
    """)
    bunsen_rows = rows(conn, """
        SELECT bunsen_count,
               COUNT(DISTINCT race_id) AS race_count,
               COUNT(*) AS races,
               SUM(CASE WHEN rank = 1 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN rank <= 2 THEN 1 ELSE 0 END) AS top2,
               SUM(CASE WHEN rank <= 3 THEN 1 ELSE 0 END) AS top3,
               AVG(CASE WHEN rank = 1 THEN 1.0 ELSE 0 END) * 100 AS win_rate,
               AVG(CASE WHEN rank <= 2 THEN 1.0 ELSE 0 END) * 100 AS top2_rate,
               AVG(CASE WHEN rank <= 3 THEN 1.0 ELSE 0 END) * 100 AS top3_rate
        FROM race_line_features
        WHERE rank IS NOT NULL
        GROUP BY bunsen_count
        ORDER BY bunsen_count
    """)
    position_bunsen_rows = rows(conn, """
        SELECT CASE
                   WHEN is_tanki = 1 THEN '単騎'
                   WHEN line_position = 1 THEN '先頭'
                   WHEN line_position = 2 THEN '番手'
                   WHEN line_position >= 3 THEN '三番手以降'
                   ELSE '不明'
               END AS line_role,
               bunsen_count,
               SUM(races) AS races,
               SUM(wins) AS wins,
               SUM(top2) AS top2,
               SUM(top3) AS top3,
               SUM(wins) * 100.0 / NULLIF(SUM(races), 0) AS win_rate,
               SUM(top2) * 100.0 / NULLIF(SUM(races), 0) AS top2_rate,
               SUM(top3) * 100.0 / NULLIF(SUM(races), 0) AS top3_rate
        FROM racer_line_condition_stats
        WHERE condition_type = 'exact_condition'
          AND bunsen_count IS NOT NULL
        GROUP BY line_role, bunsen_count
        ORDER BY CASE line_role
                     WHEN '先頭' THEN 1
                     WHEN '番手' THEN 2
                     WHEN '三番手以降' THEN 3
                     WHEN '単騎' THEN 4
                     ELSE 5
                 END,
                 bunsen_count
    """)
    position_effect_rows = rows(conn, """
        SELECT CASE
                   WHEN is_tanki = 1 OR position_label = 'tanki' THEN '単騎'
                   WHEN line_position = 1 OR position_label = 'leader' THEN '先頭'
                   WHEN line_position = 2 OR position_label = 'second' THEN '番手'
                   WHEN line_position >= 3 OR position_label IN ('third', 'fourth_plus') THEN '三番手以降'
                   ELSE '不明'
               END AS line_role,
               SUM(races) AS races,
               SUM(wins) AS wins,
               SUM(top2) AS top2,
               SUM(top3) AS top3,
               SUM(wins) * 100.0 / NULLIF(SUM(races), 0) AS win_rate,
               SUM(top2) * 100.0 / NULLIF(SUM(races), 0) AS top2_rate,
               SUM(top3) * 100.0 / NULLIF(SUM(races), 0) AS top3_rate
        FROM racer_line_condition_stats
        WHERE condition_type = 'position'
        GROUP BY line_role
        ORDER BY CASE line_role
                     WHEN '先頭' THEN 1
                     WHEN '番手' THEN 2
                     WHEN '三番手以降' THEN 3
                     WHEN '単騎' THEN 4
                     ELSE 5
                 END
    """)
    followers_effect_rows = rows(conn, """
        SELECT followers,
               SUM(races) AS races,
               SUM(wins) AS wins,
               SUM(top2) AS top2,
               SUM(top3) AS top3,
               SUM(wins) * 100.0 / NULLIF(SUM(races), 0) AS win_rate,
               SUM(top2) * 100.0 / NULLIF(SUM(races), 0) AS top2_rate,
               SUM(top3) * 100.0 / NULLIF(SUM(races), 0) AS top3_rate
        FROM racer_line_condition_stats
        WHERE condition_type = 'exact_condition'
          AND followers IS NOT NULL
        GROUP BY followers
        ORDER BY followers
    """)
    leader_followers_effect_rows = rows(conn, """
        SELECT followers,
               SUM(races) AS races,
               SUM(wins) AS wins,
               SUM(top2) AS top2,
               SUM(top3) AS top3,
               SUM(wins) * 100.0 / NULLIF(SUM(races), 0) AS win_rate,
               SUM(top2) * 100.0 / NULLIF(SUM(races), 0) AS top2_rate,
               SUM(top3) * 100.0 / NULLIF(SUM(races), 0) AS top3_rate
        FROM racer_line_condition_stats
        WHERE condition_type = 'exact_condition'
          AND line_position = 1
          AND COALESCE(is_tanki, 0) = 0
          AND followers IS NOT NULL
        GROUP BY followers
        ORDER BY followers
    """)
    position_followers_rows = rows(conn, """
        SELECT CASE
                   WHEN is_tanki = 1 THEN '単騎'
                   WHEN line_position = 1 THEN '先頭'
                   WHEN line_position = 2 THEN '番手'
                   WHEN line_position >= 3 THEN '三番手以降'
                   ELSE '不明'
               END AS line_role,
               followers,
               SUM(races) AS races,
               SUM(wins) AS wins,
               SUM(top2) AS top2,
               SUM(top3) AS top3,
               SUM(wins) * 100.0 / NULLIF(SUM(races), 0) AS win_rate,
               SUM(top2) * 100.0 / NULLIF(SUM(races), 0) AS top2_rate,
               SUM(top3) * 100.0 / NULLIF(SUM(races), 0) AS top3_rate
        FROM racer_line_condition_stats
        WHERE condition_type = 'exact_condition'
          AND followers IS NOT NULL
        GROUP BY line_role, followers
        ORDER BY CASE line_role
                     WHEN '先頭' THEN 1
                     WHEN '番手' THEN 2
                     WHEN '三番手以降' THEN 3
                     WHEN '単騎' THEN 4
                     ELSE 5
                 END,
                 followers
    """)
    bunsen_reason_rows = rows(conn, """
        SELECT bunsen_count, starter_count, line_count, tanki_count, max_line_size,
               COUNT(DISTINCT race_id) AS race_count
        FROM race_line_features
        WHERE bunsen_count IN (0, 1)
        GROUP BY bunsen_count, starter_count, line_count, tanki_count, max_line_size
        ORDER BY bunsen_count, race_count DESC, starter_count
    """)
    low_bunsen_samples = rows(conn, """
        SELECT DISTINCT race_id, venue, race_no, source_lineup_text,
               starter_count, line_count, bunsen_count, tanki_count, max_line_size
        FROM race_line_features
        WHERE bunsen_count IN (0, 1)
        ORDER BY bunsen_count, race_id DESC
        LIMIT 20
    """)

    for row in stat_rows:
        row["win_rate"] = pct(row.get("win_rate"))
        row["top2_rate"] = pct(row.get("top2_rate"))
        row["top3_rate"] = pct(row.get("top3_rate"))
    for group in (leader_rows, bunsen_rows):
        for row in group:
            row["win_rate"] = pct(row.get("win_rate"))
            row["top2_rate"] = pct(row.get("top2_rate"))
            row["top3_rate"] = pct(row.get("top3_rate"))
    for row in position_bunsen_rows:
        row["win_rate"] = pct(row.get("win_rate"))
        row["top2_rate"] = pct(row.get("top2_rate"))
        row["top3_rate"] = pct(row.get("top3_rate"))
    for group in (position_effect_rows, followers_effect_rows, leader_followers_effect_rows, position_followers_rows):
        for row in group:
            row["sample_warning"] = "サンプル不足" if (row.get("races") or 0) < 30 else ""
            row["win_rate"] = pct(row.get("win_rate"))
            row["top2_rate"] = pct(row.get("top2_rate"))
            row["top3_rate"] = pct(row.get("top3_rate"))

    body = section(
        "race_line_features サンプル",
        table(
            ["race_id", "car_no", "racer_name", "line_no", "line_size", "line_position", "followers", "is_tanki", "is_max_line", "bunsen_count"],
            sample_rows,
            ["race_id", "car_no", "racer_name", "line_no", "line_size", "line_position", "followers", "is_tanki", "is_max_line", "bunsen_count"],
        ),
        "lineup_text から生成した直近明細です。1レース内の各選手ごとにライン位置、後続人数、分線数を確認します。",
    )
    body += section(
        "racer_line_condition_stats 集計",
        accordion_table(
            ["選手名", "条件種別", "条件値", "出走数", "1着数", "2連対数", "3連対数", "勝率", "2連対率", "3連対率"],
            stat_rows,
            ["racer_name", "condition_type", "condition_key", "races", "wins", "top2", "top3", "win_rate", "top2_rate", "top3_rate"],
            visible_count=50,
        ),
        "選手別・条件別の集計です。予想スコアへ反映する前に、条件値と母数の妥当性を確認します。",
    )
    body += section(
        "leader_followers 集計",
        table(
            ["後続人数", "レース数", "サンプル数", "1着数", "2連対数", "3連対数", "勝率", "2連対率", "3連対率"],
            leader_rows,
            ["followers", "race_count", "races", "wins", "top2", "top3", "win_rate", "top2_rate", "top3_rate"],
        ),
        "先頭選手だけを対象に、後ろに付く人数別の成績を表示します。",
    )
    body += section(
        "bunsen 算出ロジック",
        """
        <div class="rank-note">
          bunsen_count は、単騎を除いたライン数です。line_size が2人以上のラインだけを数えます。
          例: 3-2-2 は3分線、3-3-2-1 は単騎を除いて3分線、全員単騎は0分線です。
          女子戦は全員単騎が自然に発生します。男子でも全員単騎や1本線+単騎のレースはあり得るため、
          予想ロジックでは2分線以上を主対象、0分線・1分線は別枠または参考扱いにするのが安全です。
        </div>
        """,
    )
    body += section(
        "ライン位置別成績ランキング",
        table(
            ["ライン位置", "出走数", "1着数", "勝率", "2連対率", "3連対率", "警告"],
            position_effect_rows,
            ["line_role", "races", "wins", "win_rate", "top2_rate", "top3_rate", "sample_warning"],
        ),
        "先頭・番手・三番手以降・単騎で有意差があるか確認します。出走数30未満はサンプル不足です。",
    )
    body += section(
        "後続人数別成績",
        table(
            ["後続人数", "出走数", "1着数", "勝率", "2連対率", "3連対率", "警告"],
            followers_effect_rows,
            ["followers", "races", "wins", "win_rate", "top2_rate", "top3_rate", "sample_warning"],
        ),
        "全ライン位置を対象に、後続人数が増えるほど成績が向上するか確認します。",
    )
    body += section(
        "先頭限定 後続人数別成績",
        table(
            ["後続人数", "出走数", "1着数", "勝率", "2連対率", "3連対率", "警告"],
            leader_followers_effect_rows,
            ["followers", "races", "wins", "win_rate", "top2_rate", "top3_rate", "sample_warning"],
        ),
        "ライン位置が先頭の選手だけを対象に、後ろ何人で強いかを直接確認します。",
    )
    body += section(
        "ライン位置 × 後続人数",
        table(
            ["ライン位置", "後続人数", "出走数", "1着数", "勝率", "2連対率", "3連対率", "警告"],
            position_followers_rows,
            ["line_role", "followers", "races", "wins", "win_rate", "top2_rate", "top3_rate", "sample_warning"],
        ),
        "番手や三番手以降でも後続人数の影響があるか確認します。",
    )
    body += section(
        "ライン位置別 × 分線数別 成績",
        table(
            ["ライン位置", "分線数", "出走数", "1着数", "2連対数", "3連対数", "勝率", "2連対率", "3連対率"],
            position_bunsen_rows,
            ["line_role", "bunsen_count", "races", "wins", "top2", "top3", "win_rate", "top2_rate", "top3_rate"],
        ),
        "racer_line_condition_stats の exact_condition を利用し、ライン内の役割ごとに分線数別成績を再集計しています。",
    )
    body += section(
        "bunsen 集計",
        table(
            ["分線数", "レース数", "サンプル数", "1着数", "2連対数", "3連対数", "勝率", "2連対率", "3連対率"],
            bunsen_rows,
            ["bunsen_count", "race_count", "races", "wins", "top2", "top3", "win_rate", "top2_rate", "top3_rate"],
        ),
        "単騎を除いたライン数ごとの成績です。混戦度に応じた傾向確認に使います。",
    )
    body += section(
        "0分線・1分線の発生理由",
        table(
            ["分線数", "出走人数", "ライン数", "単騎数", "最大ライン人数", "レース数"],
            bunsen_reason_rows,
            ["bunsen_count", "starter_count", "line_count", "tanki_count", "max_line_size", "race_count"],
        ),
        "0分線は全員単騎、1分線は2人以上のラインが1本だけで残りが単騎の構造です。",
    )
    body += section(
        "0分線・1分線 サンプル",
        table(
            ["race_id", "会場", "R", "並び", "出走人数", "ライン数", "分線数", "単騎数", "最大ライン人数"],
            low_bunsen_samples,
            ["race_id", "venue", "race_no", "source_lineup_text", "starter_count", "line_count", "bunsen_count", "tanki_count", "max_line_size"],
        ),
        "集計対象として妥当か確認するため、低分線レースの実例を表示します。",
    )
    return page("ライン解析", "lineup-features", body)


def render_dice_bets() -> str:
    body = """
    <section>
      <h2>サイコロ車券</h2>
      <div class="dice-panel" id="dice-bets">
        <div class="dice-controls">
          <label>車立て
            <select id="dice-car-count">
              <option value="5">5車</option>
              <option value="6">6車</option>
              <option value="7" selected>7車</option>
              <option value="8">8車</option>
              <option value="9">9車</option>
            </select>
          </label>
          <label>買い目数
            <input id="dice-ticket-count" type="number" min="5" max="100" value="10">
          </label>
          <label>賭式
            <select id="dice-bet-type">
              <option value="wide">ワイド</option>
              <option value="two_pair">2車複</option>
              <option value="two_exact">2車単</option>
              <option value="trio">3連複</option>
              <option value="trifecta" selected>3連単</option>
            </select>
          </label>
          <button class="dice-button" id="dice-roll" type="button">サイコロを振る</button>
        </div>
        <div class="dice-summary" id="dice-summary" aria-live="polite"></div>
        <div class="dice-note" id="dice-note"></div>
        <div class="dice-results" id="dice-results"></div>
      </div>
    </section>
    <script>
    (() => {
      const carCount = document.getElementById("dice-car-count");
      const ticketCount = document.getElementById("dice-ticket-count");
      const betType = document.getElementById("dice-bet-type");
      const roll = document.getElementById("dice-roll");
      const summary = document.getElementById("dice-summary");
      const note = document.getElementById("dice-note");
      const results = document.getElementById("dice-results");

      const labels = {
        wide: "ワイド",
        two_pair: "2車複",
        two_exact: "2車単",
        trio: "3連複",
        trifecta: "3連単"
      };

      const cars = (count) => Array.from({ length: count }, (_, index) => index + 1);
      const comboText = (combo, type) => {
        if (type === "two_exact" || type === "trifecta") return combo.join("-");
        return combo.join("=");
      };
      const sortCombos = (items) => items.sort((a, b) => {
        for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
          const diff = (a[i] || 0) - (b[i] || 0);
          if (diff) return diff;
        }
        return 0;
      });
      const shuffle = (items) => {
        const copy = [...items];
        for (let i = copy.length - 1; i > 0; i -= 1) {
          const j = Math.floor(Math.random() * (i + 1));
          [copy[i], copy[j]] = [copy[j], copy[i]];
        }
        return copy;
      };

      const allCombos = (count, type) => {
        const ns = cars(count);
        const combos = [];
        if (type === "wide" || type === "two_pair") {
          for (let i = 0; i < ns.length; i += 1) {
            for (let j = i + 1; j < ns.length; j += 1) combos.push([ns[i], ns[j]]);
          }
        } else if (type === "two_exact") {
          ns.forEach((a) => ns.forEach((b) => { if (a !== b) combos.push([a, b]); }));
        } else if (type === "trio") {
          for (let i = 0; i < ns.length; i += 1) {
            for (let j = i + 1; j < ns.length; j += 1) {
              for (let k = j + 1; k < ns.length; k += 1) combos.push([ns[i], ns[j], ns[k]]);
            }
          }
        } else if (type === "trifecta") {
          ns.forEach((a) => ns.forEach((b) => ns.forEach((c) => {
            if (a !== b && a !== c && b !== c) combos.push([a, b, c]);
          })));
        }
        return combos;
      };

      const render = () => {
        const count = Number(carCount.value);
        const requested = Math.max(5, Math.min(100, Number(ticketCount.value || 5)));
        ticketCount.value = requested;
        const type = betType.value;
        const pool = allCombos(count, type);
        const generated = Math.min(requested, pool.length);
        const picked = sortCombos(shuffle(pool).slice(0, generated));

        summary.innerHTML = [
          ["賭式", labels[type]],
          ["指定", `${requested}点`],
          ["生成", `${generated}点`],
          ["合計", `${(generated * 100).toLocaleString("ja-JP")}円`]
        ].map(([label, value]) => `<div><span>${label}</span><strong>${value}</strong></div>`).join("");
        note.textContent = requested > pool.length
          ? `${count}車 / ${labels[type]} の最大点数は ${pool.length}点です。`
          : "";
        results.innerHTML = picked.map((combo) => `<div class="dice-ticket">${comboText(combo, type)}</div>`).join("");
      };

      roll.addEventListener("click", render);
      [carCount, ticketCount, betType].forEach((item) => item.addEventListener("change", render));
      render();
    })();
    </script>
    """
    return page("サイコロ車券", "dice-bets", body)


def render_race_detail_shell() -> str:
    body = """
    <section>
      <h2 id="detail-title">レース詳細</h2>
      <div id="race-detail-root"><div class="empty">読み込み中です</div></div>
    </section>
    <script>
    (() => {
      const root = document.getElementById("race-detail-root");
      const title = document.getElementById("detail-title");
      const params = new URLSearchParams(window.location.search);
      const raceId = params.get("race_id") || "";
      const date = params.get("date") || raceId.split("_")[0] || "";

      const esc = (value) => {
        if (value === null || value === undefined) return "";
        return String(value).replace(/[&<>"']/g, (char) => ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;"
        }[char]));
      };
      const yen = (value) => {
        if (value === null || value === undefined || value === "") return "";
        return `${Number(value).toLocaleString("ja-JP")}円`;
      };
      const card = (label, value) => `<div class="card"><span>${esc(label)}</span><strong>${esc(value || "-")}</strong></div>`;
      const table = (headers, rows, fields) => {
        if (!rows || rows.length === 0) return '<div class="empty">データがありません</div>';
        const head = headers.map((header) => `<th>${esc(header)}</th>`).join("");
        const body = rows.map((row) => `<tr>${fields.map((field) => `<td>${esc(row[field])}</td>`).join("")}</tr>`).join("");
        return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
      };
      const section = (heading, content) => `<section><h2>${esc(heading)}</h2>${content}</section>`;

      if (!raceId || !date) {
        root.innerHTML = '<div class="empty">race_id が指定されていません</div>';
        return;
      }

      fetch(`data/race_details/${date}.json`, { cache: "no-store" })
        .then((response) => {
          if (!response.ok) throw new Error("detail json not found");
          return response.json();
        })
        .then((items) => {
          const item = items.find((entry) => entry.race && entry.race.race_id === raceId);
          if (!item) {
            root.innerHTML = '<div class="empty">該当レースが見つかりません</div>';
            return;
          }
          const race = item.race;
          const payouts = (item.payouts || []).map((row) => ({ ...row, payout_display: yen(row.payout) }));
          title.textContent = `${race.race_date || ""} ${race.venue || ""} ${race.race_no || ""}R`;
          const cards = [
            card("日付", race.race_date),
            card("会場", race.venue),
            card("レース", race.race_no ? `${race.race_no}R` : ""),
            card("発走", race.start_time),
            card("距離", race.distance ? `${race.distance}m` : ""),
            card("天候", race.weather),
            card("風速", race.wind_speed !== null && race.wind_speed !== undefined ? `${race.wind_speed}m/s` : ""),
            card("級班", race.race_class)
          ].join("");
          const infoRows = [
            { name: "開催", value: race.event_name },
            { name: "レース名", value: race.race_title },
            { name: "締切", value: race.deadline_time },
            { name: "状態", value: race.status },
            { name: "周回", value: race.laps },
            { name: "気温", value: race.temperature !== null && race.temperature !== undefined ? `${race.temperature}℃` : "" },
            { name: "風向", value: race.wind_direction },
            { name: "並び", value: race.lineup_text },
            { name: "コメント", value: race.race_comment }
          ];
          root.innerHTML = `
            <div class="grid">${cards}</div>
            ${section("レース情報", table(["項目", "値"], infoRows, ["name", "value"]))}
            <div class="grid two">
              ${section("着順", table(["着順", "車番", "選手", "級班", "府県", "年齢", "期", "着差", "上り", "決まり手", "S", "B"], item.results || [], ["rank_display", "car_no", "racer_name", "class", "prefecture", "age", "term", "margin", "time", "kimarite", "start_mark", "back_mark"]))}
              ${section("払戻", table(["賭式", "組番", "払戻", "人気"], payouts, ["bet_type", "combination", "payout_display", "popularity"]))}
            </div>
            ${section("並び詳細", table(["ライン", "位置", "車番"], item.lineup || [], ["line_no", "line_position", "car_no"]))}
          `;
        })
        .catch(() => {
          root.innerHTML = '<div class="empty">詳細データを読み込めませんでした</div>';
        });
    })();
    </script>
    """
    return page("レース詳細", "races", body)


def export_all(output_dir: Path = DOCS_DIR, detail_dates: set[str] | None = None) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    data_detail_dir = output_dir / "data" / "race_details"
    data_detail_dir.mkdir(parents=True, exist_ok=True)
    with connect(DB_PATH) as conn:
        init_db(conn)
        target_detail_dates = detail_dates if detail_dates is not None else default_detail_dates(conn)
        pages = {
            "index.html": render_top(conn),
            "venues.html": render_venues(conn),
            "car_numbers.html": render_car_numbers(conn),
            "outcomes.html": render_outcomes(conn),
            "payouts.html": render_payouts(conn),
            "racers.html": render_racers(conn),
            "races.html": render_races(conn),
            "predictions.html": render_predictions(conn),
            "prediction-results.html": render_prediction_results(conn),
            "lineup-features.html": render_lineup_features(conn),
            "dice-bets.html": render_dice_bets(),
            "quality.html": render_quality(conn),
            "custom.html": render_custom_v2(conn),
            "race_detail.html": render_race_detail_shell(),
        }
        detail_payloads = race_detail_payloads(conn, target_detail_dates)
    written = []
    for filename, content in pages.items():
        path = output_dir / filename
        path.write_text(content, encoding="utf-8")
        written.append(path)
    for compact_date, payload in detail_payloads.items():
        path = data_detail_dir / f"{compact_date}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        written.append(path)
    for compact_date in target_detail_dates - set(detail_payloads):
        path = data_detail_dir / f"{compact_date}.json"
        if path.exists():
            path.unlink()
            written.append(path)
    (output_dir / ".nojekyll").touch()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate keirin analytics HTML reports")
    parser.add_argument(
        "--detail-date",
        action="append",
        help="Detail JSON date to update in YYYY-MM-DD or YYYYMMDD. Default: latest race date in DB.",
    )
    args = parser.parse_args()
    detail_dates = None
    if args.detail_date:
        detail_dates = {normalize_compact_date(value) for value in args.detail_date}
        detail_dates = {value for value in detail_dates if value}
    for path in export_all(detail_dates=detail_dates):
        print(f"Exported {path}")


if __name__ == "__main__":
    main()
