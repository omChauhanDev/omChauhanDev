#!/usr/bin/env python3
"""Redraw the profile README from profile.toml and live GitHub and PyPI numbers.

    pip install -r scripts/requirements.txt
    GITHUB_TOKEN=$(gh auth token) python scripts/build.py
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import tomllib
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from svgtext import Font, num

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
FONTS = ASSETS / "fonts"
CACHE = ASSETS / "data.json"
CONFIG = tomllib.loads((ROOT / "profile.toml").read_text())
USER = CONFIG["user"]
RAW = f"https://raw.githubusercontent.com/{USER}/{USER}/main/assets"

SANS = Font(FONTS / "Geist-Regular.ttf", "r")
MEDIUM = Font(FONTS / "Geist-Medium.ttf", "m")
SEMIBOLD = Font(FONTS / "Geist-SemiBold.ttf", "s")
MONO = Font(FONTS / "GeistMono-Regular.ttf", "o")

# GitHub's own surface, border and text colours, so the cards sit in the page like native boxes.
THEMES = {
    "dark": {"card": "#151b23", "edge": "#3d444d", "rule": "#262c36",
             "fg": "#f0f6fc", "muted": "#9198a1", "live": "#3fb950"},
    "light": {"card": "#f6f8fa", "edge": "#d1d9e0", "rule": "#dfe3e8",
              "fg": "#1f2328", "muted": "#59636e", "live": "#1a7f37"},
}

# The README column is 846 px wide from a 1295 px window up: two 422 px cards per row.
# Narrower windows get one card per row, drawn at the width that column has, so the type
# stays the same size everywhere (GitHub's sidebar shrinks below 1012 px, then stacks at 768).
# Below 768 px GitHub stacks the sidebar above the README and the column is the window minus 82;
# from 768 to 1011 it is the window minus 370, then minus 434 up to its 846 px cap.
CARD_W, STATEMENT_W = 422, 844
DRAWINGS = {"phone-": 340, "w420-": 420, "w580-": 580, "w640-": 640, "w700-": 700}
CARD_TIERS = [  # first match wins, so narrowest first
    ("phone-", "(max-width: 501px)"),
    ("w420-", "(max-width: 661px)"),
    ("w580-", "(max-width: 721px)"),
    ("w640-", "(max-width: 767px)"),
    ("w420-", "(max-width: 899px)"),
    ("w580-", "(max-width: 1099px)"),
    ("w700-", "(max-width: 1294px)"),
]
STATEMENT_DRAWINGS = {"phone-": (340, 24, 31), "w420-": (420, 28, 36)}
STATEMENT_TIERS = [("phone-", "(max-width: 501px)"), ("w420-", "(max-width: 1099px)")]
GUTTER = 8                       # transparent edge around each card; two make the gaps
HEIGHTS = {"tall": 244, "compact": 162}   # before the header band grows (see SHIFT)
PAD = 24


# ---------------------------------------------------------------- data


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("set GITHUB_TOKEN (locally: export GITHUB_TOKEN=$(gh auth token))")
    return token


def graphql(query: str, **variables) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {_token()}", "User-Agent": USER},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise SystemExit(f"GraphQL error: {body['errors']}")
    return body["data"]


def rest(path: str):
    req = urllib.request.Request(f"https://api.github.com/{path}",
                                 headers={"Authorization": f"bearer {_token()}", "User-Agent": USER})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


SEARCH = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { title mergedAt repository { nameWithOwner owner { login } } } }
  }
}
"""


def merged_prs() -> list[dict]:
    query = f"is:pr is:public is:merged author:{USER} -user:{USER}"
    prs, after = [], None
    while True:
        page = graphql(SEARCH, q=query, after=after)["search"]
        prs += [n for n in page["nodes"]
                if n and n["repository"]["nameWithOwner"] not in CONFIG["exclude_repos"]]
        if not page["pageInfo"]["hasNextPage"]:
            return prs
        after = page["pageInfo"]["endCursor"]


def stars(repos: list[str]) -> dict[str, int]:
    fields = " ".join(
        f'r{i}: repository(owner: "{owner}", name: "{name}") {{ stargazerCount }}'
        for i, (owner, name) in enumerate(repo.split("/") for repo in repos)
    )
    data = graphql(f"query {{ {fields} }}")
    return {repo: data[f"r{i}"]["stargazerCount"] for i, repo in enumerate(repos)}


def contributor_rank(repo: str) -> tuple[int, int]:
    """Place among the repo's human contributors, as GitHub's contributors list ranks them."""
    rank = total = 0
    page = 1
    while batch := rest(f"repos/{repo}/contributors?per_page=100&page={page}"):
        for person in batch:
            if person.get("type") == "User":
                total += 1
                if person["login"].lower() == USER.lower():
                    rank = total
        page += 1
    return rank, total


def monthly_downloads(package: str) -> int:
    # pypistats.org: PyPI downloads over the last 30 days, mirrors excluded. No key needed.
    req = urllib.request.Request(f"https://pypistats.org/api/packages/{package}/recent",
                                 headers={"User-Agent": USER})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["data"]["last_month"]


def search_link(*orgs: str) -> str:
    query = f"is:pr is:merged author:{USER} " + " ".join(f"org:{o}" for o in orgs)
    return "https://github.com/search?" + urllib.parse.urlencode({"q": query, "type": "pullrequests"})


# ------------------------------------------------------------- helpers


def short(n: int) -> str:
    """Counts the way GitHub prints them: 430, 1.5k, 15k, 16.9k."""
    return str(n) if n < 1000 else f"{n / 1000:.1f}".removesuffix(".0") + "k"


def ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def ago(day: date, today: date) -> str:
    days = (today - day).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return day.strftime("%b %Y")


def write_if_changed(path: Path, text: str) -> None:
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
        print(f"updated {path.relative_to(ROOT)}")


class Svg:
    def __init__(self, width: int, height: int, label: str):
        self.width, self.height, self.label = width, height, label
        self.defs: dict[str, str] = {}
        self.body: list[str] = []
        self.dy = 0.0   # added to every y below the header band (see head())

    def add(self, element: str) -> None:
        self.body.append(element)

    def text(self, font: Font, text: str, size: float, x: float, y: float, fill: str,
             tracking: float = 0.0, anchor: str = "start") -> float:
        placed, width = font.place(text, size, x, y + self.dy, tracking, anchor)
        scale = f"{size / font.upem:.6g}"
        uses = []
        for gid, gx, gy in placed:
            outline = font.outline(gid)
            if outline:
                ref = f"{font.key}{gid}"
                self.defs.setdefault(ref, outline)
                uses.append(f'<use href="#{ref}" transform="translate({num(gx)} {num(gy)}) scale({scale})"/>')
        if uses:
            self.add(f'<g fill="{fill}">{"".join(uses)}</g>')
        return width

    def __str__(self) -> str:
        label = html.escape(self.label)
        defs = "".join(f'<path id="{ref}" d="{d}"/>' for ref, d in self.defs.items())
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
                f'viewBox="0 0 {self.width} {self.height}" role="img" aria-label="{label}">'
                f"<title>{label}</title><defs>{defs}</defs>{''.join(self.body)}</svg>\n")


# --------------------------------------------------------------- cards


def frame(svg: Svg, t: dict, x0: float, w: float) -> tuple[float, float]:
    svg.add(f'<rect x="{num(x0 + .5)}" y="{GUTTER + .5}" width="{num(w - 1)}" '
            f'height="{svg.height - 2 * GUTTER - 1}" rx="6" fill="{t["card"]}" stroke="{t["edge"]}"/>')
    left, right = x0 + PAD, x0 + w - PAD
    top = BAND_MID - 4.5
    svg.add(f'<path d="M{num(right - 9)} {num(top)}h9v9M{num(right)} {num(top)}l-10 10" fill="none" stroke="{t["muted"]}" '
            'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>')
    return left, right


LOGO = int(os.environ.get("LOGO_SIZE", "24"))   # organisation mark, px
BAND_TOP = GUTTER + PAD                          # the mark sits one padding from the card's top edge
BAND_MID = BAND_TOP + LOGO / 2                   # label and arrow centre on the mark
LABEL_Y = BAND_MID + 3.4                         # 12 px mono: baseline that centres its x-height
SHIFT = BAND_TOP + LOGO + 28 - 76                # title baseline sits 12 px under the mark, plus cap height


def logo_mark(svg: Svg, t: dict, x: float, y: float, path: str) -> None:
    """An organisation's own mark, unaltered: raster marks as GitHub-style rounded tiles,
    vector marks drawn as they are."""
    file = ASSETS / path
    if file.suffix == ".svg":
        source = file.read_text()
        box = [float(v) for v in re.search(r'viewBox="([^"]+)"', source).group(1).split()]
        inner = re.search(r"<svg[^>]*>(.*)</svg>", source, re.S).group(1)
        svg.add(f'<g transform="translate({num(x)} {num(y)}) scale({LOGO / box[3]:.6g})">{inner}</g>')
        return
    data = base64.b64encode(file.read_bytes()).decode()
    svg.add(f'<clipPath id="logo"><rect x="{num(x)}" y="{num(y)}" width="{LOGO}" height="{LOGO}" rx="5"/></clipPath>'
            f'<image href="data:image/png;base64,{data}" x="{num(x)}" y="{num(y)}" width="{LOGO}" height="{LOGO}" '
            'clip-path="url(#logo)"/>'
            f'<rect x="{num(x + .5)}" y="{num(y + .5)}" width="{LOGO - 1}" height="{LOGO - 1}" rx="4.5" '
            f'fill="none" stroke="{t["edge"]}"/>')


def band(svg: Svg, t: dict, left: float, label: str, logo: str | None = None) -> None:
    """The header band: an optional organisation mark and the label, then shift what follows."""
    x = left
    if logo:
        logo_mark(svg, t, left, BAND_TOP, logo)
        x += LOGO + 10
    svg.text(MONO, label, 12, x, LABEL_Y, t["muted"])
    svg.dy = SHIFT


def head(svg: Svg, t: dict, left: float, label: str, title: str, logo: str | None = None) -> None:
    band(svg, t, left, label, logo)
    svg.text(SEMIBOLD, title, 22, left, 76, t["fg"], tracking=-0.3)


def figure(svg: Svg, t: dict, left: float, text: str, y: float = 190) -> None:
    svg.text(SEMIBOLD, text, 48, left - SEMIBOLD.left_bearing(text, 48), y, t["fg"], tracking=-1.4)


def clamp(text: str, width: float, most: int) -> list[str]:
    """Wrap to at most `most` lines, ending in an ellipsis if words had to go."""
    words = text.split()
    while len(SANS.wrap(" ".join(words), 14.5, width)) > most:
        words = words[:-1]
        words[-1] = words[-1].rstrip(",.:;") + "…"
    return SANS.wrap(" ".join(words), 14.5, width)


def meta_right(svg: Svg, t: dict, right: float, text: str, y: float) -> None:
    """Right-aligned footnote whose ink, not its advance, ends flush with the arrow above it."""
    svg.text(MONO, text, 12, right + MONO.right_bearing(text, 12), y, t["muted"], anchor="end")


def fit(svg: Svg, lines: list, shrink: bool) -> float:
    """On one-card-per-row layouts, close the gap a missing second line of text would leave."""
    lift = 19 * (2 - len(lines)) if shrink else 0
    svg.height -= lift
    return lift


def lines_of(text: str, width: float, most: int, name: str) -> list[str]:
    lines = SANS.wrap(text, 14.5, width)
    if len(lines) > most:
        raise SystemExit(f"{name}: '{text}' needs {len(lines)} lines at this width; {most} fit")
    return lines


def project_card(svg, t, x0, w, card, big, unit, meta, data=None, shrink=False):
    """Tall card: label, title, up to two lines of text, a large figure with its unit, and a footer.

    `data` is an optional (number, words) line set like the compact cards' footers, on the
    second text line, level with the card beside it.
    """
    lines = lines_of(card["text"], w - 2 * PAD, 1 if data else 2, card["title"])
    lift = fit(svg, lines + ([""] if data else []), shrink)
    left, right = frame(svg, t, x0, w)
    head(svg, t, left, card["label"], card["title"], card.get("logo"))
    for i, line in enumerate(lines):
        svg.text(SANS, line, 14.5, left, 102 + 19 * i, t["muted"])
    if data:
        x = left + svg.text(MEDIUM, data[0], 14.5, left, 121, t["fg"])
        svg.text(SANS, data[1], 14.5, x, 121, t["muted"])
    figure(svg, t, left, big, 190 - lift)
    svg.text(SANS, unit, 13, left, 214 - lift, t["muted"])
    meta_right(svg, t, right, meta, 214 - lift)


def compact_card(svg, t, x0, w, card, count, meta):
    """Short card for smaller contributions: no display number."""
    left, right = frame(svg, t, x0, w)
    head(svg, t, left, card["label"], card["title"], card.get("logo"))
    svg.text(SANS, lines_of(card["text"], right - left, 1, card["title"])[0], 14.5, left, 102, t["muted"])
    x = left + svg.text(MEDIUM, str(count), 13, left, 132, t["fg"])
    svg.text(SANS, " merged pull request" + ("s" if count != 1 else ""), 13, x, 132, t["muted"])
    meta_right(svg, t, right, meta, 132)


def areas_card(svg, t, x0, w, card, monthly):
    footer = [(SANS, "I wrote ", 13), (MONO, CONFIG["package"], 12.5), (SANS, " · ", 13),
              (MEDIUM, short(monthly), 13), (SANS, " downloads a month", 13)]
    if sum(font.width(text, size) for font, text, size in footer) > w - 2 * PAD:
        svg.height += 19   # the footer takes two lines; one card per row, so only this card grows
    left, right = frame(svg, t, x0, w)
    band(svg, t, left, card["label"])
    rows = card["areas"]
    if len(rows) > 5:
        raise SystemExit("in pipecat: five areas at most")
    for i, row in enumerate(rows):
        y = 76 + 26 * i
        svg.text(SANS, row, 15, left, y, t["fg"])
        if i < len(rows) - 1:
            svg.add(f'<path d="M{num(left)} {num(y + 9.5 + svg.dy)}H{num(right)}" stroke="{t["rule"]}"/>')
    wrote = [(SANS, "I wrote ", 13, "muted"), (MONO, CONFIG["package"], 12.5, "muted")]
    usage = [(MEDIUM, short(monthly), 13, "fg"), (SANS, " downloads a month", 13, "muted")]
    one_line = wrote + [(SANS, " · ", 13, "muted")] + usage
    lines = [one_line] if sum(f.width(x, z) for f, x, z, _ in one_line) <= right - left else [wrote, usage]
    for row, runs in enumerate(lines):
        x = left
        for font, text, size, tone in runs:
            x += svg.text(font, text, size, x, 214 + 19 * row, t[tone])


def total_card(svg, t, x0, w, card, count, since, newest, merged_ago, alone=False):
    """The latest merged PR and when it landed sit under the title; the total takes the figure.

    Two-card rows must match heights, so there the title is clipped to one line; a card alone
    in its row may take a second line and grow by it.
    """
    lines = clamp(f"Latest: {newest}", w - 2 * PAD, 2 if alone else 1)
    grow = 19 * (len(lines) - 1)
    svg.height += grow
    left, right = frame(svg, t, x0, w)
    head(svg, t, left, card["label"], card["title"], card.get("logo"))
    for i, line in enumerate(lines):
        svg.text(SANS, line, 14.5, left, 102 + 19 * i, t["muted"])
    svg.add(f'<circle cx="{num(left + 3.5)}" cy="{num(116 + grow + svg.dy)}" r="3.5" fill="{t["live"]}"/>')
    svg.text(SANS, f"merged {merged_ago}", 14.5, left + 14, 121 + grow, t["muted"])
    figure(svg, t, left, str(count), 190 + grow)
    svg.text(SANS, since, 13, left, 214 + grow, t["muted"])


def statement(t: dict, width: int, size: float, leading: float) -> Svg:
    lead, rest = CONFIG["statement"]
    lines = ([(line, t["fg"]) for line in MEDIUM.wrap(lead, size, width)]
             + [(line, t["muted"]) for line in MEDIUM.wrap(rest, size, width)])
    svg = Svg(width, round(size + leading * (len(lines) - 1) + size * 0.3), f"{lead} {rest}")
    for i, (line, fill) in enumerate(lines):
        svg.text(MEDIUM, line, size, -MEDIUM.left_bearing(line, size), size + leading * i, fill,
                 tracking=-0.02 * size)
    return svg


# -------------------------------------------------------------- readme


def picture(name: str, alt: str, tiers: list[tuple[str, str]]) -> str:
    """<picture> serving each width tier's drawing, dark first; the light desktop drawing is the fallback."""
    sources = []
    for tag, media in tiers:
        sources.append(f'<source media="{media} and (prefers-color-scheme: dark)" srcset="{RAW}/{name}-{tag}dark.svg">')
        sources.append(f'<source media="{media}" srcset="{RAW}/{name}-{tag}light.svg">')
    sources.append(f'<source media="(prefers-color-scheme: dark)" srcset="{RAW}/{name}-dark.svg">')
    return (f"<picture>{''.join(sources)}"
            f'<img src="{RAW}/{name}-light.svg" align="top" alt="{html.escape(alt)}">'
            "</picture>")


def main() -> None:
    today = datetime.now(timezone.utc).date()
    prs = merged_prs()
    by_org = Counter(pr["repository"]["owner"]["login"] for pr in prs)
    newest = max(prs, key=lambda pr: pr["mergedAt"])
    first = min(date.fromisoformat(pr["mergedAt"][:10]) for pr in prs)
    merged_ago = ago(date.fromisoformat(newest["mergedAt"][:10]), today)
    repo_stars = stars([c["repo"] for c in CONFIG["cards"] if "repo" in c])

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    try:
        cache["monthly_downloads"] = monthly_downloads(CONFIG["package"])
    except OSError as err:
        print(f"pypistats.org unreachable ({err}); keeping {cache.get('monthly_downloads')}")
    cache.pop("downloads", None)
    monthly = cache["monthly_downloads"]
    write_if_changed(CACHE, json.dumps(cache, indent=2) + "\n")

    pictures = []
    for i, card in enumerate(CONFIG["cards"]):
        kind, size = card["kind"], card.get("size", "tall")
        count = by_org.get(card.get("org"), 0)
        if kind == "project":
            name = card["title"].lower().replace(" ", "-")
            meta = card.get("meta") or f"{short(repo_stars[card['repo']])} stars"
            prs_line = f"{count} merged pull request{'s' if count != 1 else ''}"
            big, unit, extra = str(count), prs_line.split(" ", 1)[1], None
            if "rank_repo" in card:
                rank, people = contributor_rank(card["rank_repo"])
                if rank:  # the standing among contributors outranks the raw count
                    big, unit, extra = ordinal(rank), f"of {people} contributors", (str(count), prs_line[len(str(count)):])
            alt = f"{card['title']}: {card['text']} {f'{big} {unit}; ' if extra else ''}{prs_line}. {meta}."
            link = card.get("link") or search_link(card["org"])

            def draw(svg, t, x0, w, shrink, card=card, count=count, big=big, unit=unit, meta=meta,
                     extra=extra, size=size):
                if size == "compact":
                    compact_card(svg, t, x0, w, card, count, meta)
                else:
                    project_card(svg, t, x0, w, card, big, unit, meta, extra, shrink)
        elif kind == "areas":
            name = f"{card['org']}-areas"
            alt = (f"Pipecat PRs by area: {', '.join(card['areas'])}. "
                   f"I wrote {CONFIG['package']}, {short(monthly)} downloads a month.")
            link = search_link(card["org"])

            def draw(svg, t, x0, w, shrink, card=card):
                areas_card(svg, t, x0, w, card, monthly)
        else:
            name = "merged-upstream"
            total = sum(by_org.values())
            since = f"since {first:%B %Y}"
            alt = (f"{total} pull requests merged upstream since {first:%B %Y}. "
                   f"Latest: {newest['title']}, merged {merged_ago}.")
            link = search_link(*by_org)

            def draw(svg, t, x0, w, shrink, card=card, total=total, since=since):
                total_card(svg, t, x0, w, card, total, since, newest["title"], merged_ago, alone=shrink)

        height = HEIGHTS[size] + round(SHIFT)
        for theme, t in THEMES.items():
            for variant, width, x0, w in ([("", CARD_W, 0 if i % 2 == 0 else GUTTER, CARD_W - GUTTER)]
                                          + [(tag, px, 0, px) for tag, px in DRAWINGS.items()]):
                svg = Svg(width, height, alt)
                draw(svg, t, x0, w, shrink=bool(variant))
                write_if_changed(ASSETS / f"{name}-{variant}{theme}.svg", str(svg))
        pictures.append(f'<a href="{html.escape(link)}">{picture(name, alt, CARD_TIERS)}</a>')

    for theme, t in THEMES.items():
        write_if_changed(ASSETS / f"statement-{theme}.svg", str(statement(t, STATEMENT_W, 34, 42)))
        for tag, (width, size, leading) in STATEMENT_DRAWINGS.items():
            write_if_changed(ASSETS / f"statement-{tag}{theme}.svg", str(statement(t, width, size, leading)))

    lead, rest = CONFIG["statement"]
    rows = ["".join(pictures[j:j + 2]) for j in range(0, len(pictures), 2)]
    readme = (
        "<!-- Built by scripts/build.py from profile.toml. Edit those; this file is overwritten. -->\n\n"
        f'<p><a href="{html.escape(search_link(*by_org))}">'
        f"{picture('statement', f'{lead} {rest}', STATEMENT_TIERS)}</a></p>\n\n"
        "<p>\n" + "<br>\n".join(rows) + "\n</p>\n"
    )
    write_if_changed(ROOT / "README.md", readme)
    print(f"{sum(by_org.values())} merged PRs across {len(by_org)} organizations; latest {newest['mergedAt'][:10]}")


if __name__ == "__main__":
    main()
