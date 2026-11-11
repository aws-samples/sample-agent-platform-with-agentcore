#!/usr/bin/env python3
"""The record of judge:pr: for each kind of finding, how often a reviewer accepted it.

judge:pr is advisory until a kind of finding holds up. Every finding carries a
label id (j-<8 hex>, model_judge.py); a reviewer labels it with a line in an MR
note:

    judge j-1a2b3c4d accept   the split-off PASS on a crash is real
    judge j-5e6f7a8b reject   config regression check, not decorative

Most findings are answered by a fix, not a note: a commit or a merge request
description that names the id ("judge:pr j-9e29dec0") counts as an accept,
and a label line there ("judge <id> reject ...") as that label. An id in
inline code or a fenced code block (``` or ~~~) is talked about, not cited:
write it bare to cite. Citations are a counting aid, not a gate: Markdown
this does not parse (indented code, HTML) can miscount, and a note label
always overrides a citation, so a wrong one is corrected with a reject note. A citation
counts for one finding: the one in the citing MR when that MR has the id,
otherwise the latest earlier MR that produced it (ids repeat across MRs, the
same kind at the same place). A note label beats a citation.

This reads every judge:pr job of the project (its verdict from the job's
artifacts, read in memory), the notes of the merge requests they ran on, the
descriptions and commits of the merge requests since, and prints per kind:
findings, accepted (of them by citation), rejected, resolved (gone from the
MR's last run with no label or citation: not counted as accepted, the judge is
not deterministic), open, and the precision over accepted + rejected. One
finding per (MR, id): the same finding on five pushes counts once, the last
label wins, a quoted line ("> judge ...") is neither a label nor a citation,
and a label naming an id the MR's runs never produced is ignored.

    GITLAB_TOKEN=... python3 scripts/judge_pr_ledger.py --api https://<gitlab>/api/v4 --project <id> \\
        [--cookie <netscape cookie jar>] [--cache <dir>] [--json]

--cookie is for an instance behind a single sign-on proxy (curl -c writes the
jar); a redirect is reported as an expired session, never followed.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

LABEL = re.compile(r"^\s*judge\s+(j-[0-9a-f]{8})\s+(accept|reject)\b[ \t]*(.*)$", re.I | re.M)
CITE = re.compile(r"(?<![0-9A-Za-z-])(j-[0-9a-f]{8})(?![0-9A-Za-z])")
KINDS = [  # title prefix -> kind; the first match wins
    ("decorative test", "decorative"), ("test of unclear value", "unclear"), ("high-risk change", "high-risk"),
    ("unverified defect", "defect-unverified"), ("defect", "defect"), ("catalog advice", "advice"),
]
VERDICT = re.compile(r"(^|/)qa-runs/[^/]+/judge\.pr-adversarial\.verdict\.json$")


def utc(ts: str) -> str:
    """One comparable form for the API's timestamps (jobs say Z, commits their author's offset)."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


def kind_of(title: str) -> str:
    return next((k for p, k in KINDS if title.startswith(p)), "other")


def parse_labels(notes: list[str]) -> dict[str, tuple[str, str]]:
    """id -> (accept|reject, why); notes in order, the last label of an id wins."""
    labels: dict[str, tuple[str, str]] = {}
    for body in notes:
        for m in LABEL.finditer(body):
            labels[m.group(1).lower()] = (m.group(2).lower(), m.group(3).strip())
    return labels


# a code span opens and closes with backtick runs of the same length (CommonMark): ``j-...`` and
# `` a ` b `` are one span each, not two empty ones around a bare id (j-1b96d575)
INLINE_CODE = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def parse_citations(text: str) -> dict[str, str]:
    """id -> accept|reject for the ids a commit message or MR description names. A label line
    there is its label, any other mention an accept; quoted lines, code blocks and inline code
    (an id given as an example, as !95's own description does) are skipped."""
    out: dict[str, str] = {}
    fence = ""  # the open fence's run; a block closes on the same character, at least as long (j-09f57b81)
    for raw in text.splitlines():
        m = FENCE.match(raw)
        if m and not fence:
            fence = m.group(1)
            continue
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not raw.strip().lstrip(fence[0]):
                fence = ""
            continue
        line = INLINE_CODE.sub("", raw)
        if line.lstrip().startswith(">"):
            continue
        m = LABEL.match(line)
        if m:
            out[m.group(1).lower()] = m.group(2).lower()
            continue
        for fid in CITE.findall(line):
            out.setdefault(fid, "accept")
    return out


def resolve_citations(first_seen: dict[tuple[int, str], str], citations: list[tuple[str, int, str, str]]) -> dict[int, dict[str, str]]:
    """citations (when, citing MR, id, accept|reject), in any order -> MR -> id -> verdict.
    Each citation lands on one finding: the citing MR's own, else the latest one seen before it."""
    out: dict[int, dict[str, str]] = {}
    for when, cmr, fid, verdict in sorted(citations):
        if (cmr, fid) in first_seen:
            target = cmr
        else:
            earlier = [(t, mr) for (mr, i), t in first_seen.items() if i == fid and t <= when]
            if not earlier:
                continue
            target = max(earlier)[1]
        out.setdefault(target, {})[fid] = verdict
    return out


def tally(findings: dict[int, dict[str, dict]], labels: dict[int, dict[str, tuple[str, str]]],
          cited: dict[int, dict[str, str]] | None = None, last_ids: dict[int, set[str]] | None = None) -> dict[str, dict]:
    """findings: MR -> id -> finding; labels: MR -> id -> note label; cited: MR -> id -> verdict from
    commits and descriptions; last_ids: MR -> ids of its last run. Per kind counts and precision."""
    out: dict[str, dict] = {}
    for mr, by_id in findings.items():
        for fid, f in by_id.items():
            row = out.setdefault(kind_of(f["title"]), {"findings": 0, "accept": 0, "cited": 0, "reject": 0, "resolved": 0, "unlabelled": 0})
            row["findings"] += 1
            verdict = labels.get(mr, {}).get(fid, ("", ""))[0]
            if not verdict and (cited or {}).get(mr, {}).get(fid):
                verdict = cited[mr][fid]
                row["cited"] += verdict == "accept"
            if not verdict and last_ids is not None and fid not in last_ids.get(mr, set()):
                verdict = "resolved"
            row[verdict or "unlabelled"] += 1
    for row in out.values():
        done = row["accept"] + row["reject"]
        row["precision"] = round(row["accept"] / done, 2) if done else None
    return out


class Api:
    def __init__(self, base: str, project: str, token: str, cookie: str | None) -> None:
        self.base, self.project = base.rstrip("/"), urllib.parse.quote(project, safe="")
        jar = http.cookiejar.MozillaCookieJar()
        if cookie:
            jar.load(cookie, ignore_discard=True, ignore_expires=True)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):  # noqa: D401
                return None

        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())
        self.headers = {"PRIVATE-TOKEN": token}

    def get(self, path: str) -> bytes:
        req = urllib.request.Request(f"{self.base}/projects/{self.project}/{path}", headers=self.headers)
        try:
            with self.opener.open(req, timeout=120) as r:  # nosec B310 - the base URL is the operator's
                return r.read()
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                sys.exit(f"{path}: redirected ({exc.code}): the sign-on session expired, refresh the cookie jar")
            raise

    def pages(self, path: str):
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            items = json.loads(self.get(f"{path}{sep}per_page=100&page={page}"))
            if not items:
                return
            yield from items
            page += 1


def collect(api: Api, cache: Path | None) -> tuple[dict, dict, dict, dict]:
    findings: dict[int, dict[str, dict]] = {}
    first_seen: dict[tuple[int, str], str] = {}
    last_run: dict[int, tuple[str, set[str]]] = {}
    for job in api.pages("jobs?scope[]=success&scope[]=failed"):
        m = re.search(r"merge-requests/(\d+)/", job.get("ref", ""))
        if job.get("name") != "judge:pr" or not m:
            continue
        blob = None
        cached = cache / f"{job['id']}.zip" if cache else None
        if cached and cached.exists():
            blob = cached.read_bytes()
        elif any(a.get("file_type") == "archive" for a in job.get("artifacts", [])):
            blob = api.get(f"jobs/{job['id']}/artifacts")
            if cached:
                cached.write_bytes(blob)
        if not blob:
            continue
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = [n for n in z.namelist() if VERDICT.search(n)]
            verdict = json.loads(z.read(names[0])) if names else {}
        mr, when = int(m.group(1)), utc(job["created_at"])
        ids = {f["id"] for f in verdict.get("findings", []) if f.get("id")}  # findings from before the label ids have none
        for f in verdict.get("findings", []):
            if f.get("id"):
                findings.setdefault(mr, {})[f["id"]] = f
                first_seen[(mr, f["id"])] = min(when, first_seen.get((mr, f["id"]), when))
        # a judged run is the MR's last word even with no findings (j-d1e90ef1); an ERROR or
        # SKIPPED run judged nothing and must not make every earlier finding look resolved
        if verdict.get("status") in ("PASS", "FAIL") and (mr not in last_run or when > last_run[mr][0]):
            last_run[mr] = (when, ids)
    labels = {}
    for mr in findings:
        notes = [n["body"] for n in sorted(api.pages(f"merge_requests/{mr}/notes?sort=asc"), key=lambda n: n["created_at"])]
        labels[mr] = {k: v for k, v in parse_labels(notes).items() if k in findings[mr]}
    citations = []
    if first_seen:
        since = urllib.parse.quote(min(first_seen.values()), safe="")
        for mrd in api.pages(f"merge_requests?state=all&updated_after={since}"):
            # the description may be edited long after the MR opened: it stood by updated_at,
            # not by created_at (j-d0427b21)
            for fid, v in parse_citations(mrd.get("description") or "").items():
                citations.append((utc(mrd.get("updated_at") or mrd["created_at"]), mrd["iid"], fid, v))
            for c in api.pages(f"merge_requests/{mrd['iid']}/commits"):
                for fid, v in parse_citations(c.get("message") or "").items():
                    citations.append((utc(c.get("committed_date") or c["created_at"]), mrd["iid"], fid, v))
    return findings, labels, resolve_citations(first_seen, citations), {mr: ids for mr, (_, ids) in last_run.items() if mr in findings}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", required=True, help="GitLab API base, ending in /api/v4")
    ap.add_argument("--project", required=True, help="project id or path")
    ap.add_argument("--cookie", help="Netscape cookie jar for a sign-on proxy")
    ap.add_argument("--cache", help="directory to keep downloaded artifacts in")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    token = os.environ.get("GITLAB_TOKEN") or sys.exit("GITLAB_TOKEN is required")
    cache = Path(a.cache) if a.cache else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    findings, labels, cited, last_ids = collect(Api(a.api, a.project, token, a.cookie), cache)
    rows = tally(findings, labels, cited, last_ids)
    if a.json:
        print(json.dumps({"merge_requests": len(findings), "kinds": rows}, indent=2))
        return 0
    print(f"{len(findings)} merge request(s) with labelled-format findings")
    print(f"{'kind':<20}{'findings':>9}{'accept':>8}{'(cited)':>8}{'reject':>8}{'resolved':>9}{'open':>6}{'precision':>11}")
    for k, r in sorted(rows.items(), key=lambda kv: -kv[1]["findings"]):
        p = "-" if r["precision"] is None else f"{r['precision']:.2f}"
        print(f"{k:<20}{r['findings']:>9}{r['accept']:>8}{r['cited']:>8}{r['reject']:>8}{r['resolved']:>9}{r['unlabelled']:>6}{p:>11}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
