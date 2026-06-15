#!/usr/bin/env python3
"""shotsort 엔진 — OCR·분류·통합·캐시·휴지통 로직 (UI/CLI 공통).

출력(print)은 포함하지 않는다. 진행 상황은 콜백으로, 결과는 반환값으로 넘긴다.
이렇게 분리해 두면 CLI(cli.py)와 GUI(app.py)가 같은 로직을 그대로 재사용한다.

하이브리드 분석:
  1) 로컬 OCR (macOS Vision, 무료/오프라인) 로 이미지에서 텍스트 추출
  2) Claude 가 그 텍스트(+선택적 썸네일)를 읽고 project/kind/요약/삭제가능 태그 부여
  3) 2차 통합 패스로 free-form 프로젝트 추정치를 정규화된 그룹으로 묶음
ANTHROPIC_API_KEY 가 없으면 2)·3)을 로컬 휴리스틱으로 대체(무료/오프라인).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re as _re
import sqlite3
import subprocess
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HOME = Path.home()
STATE_DIR = HOME / ".shotsort"
DB_PATH = STATE_DIR / "cache.db"
DEFAULT_SCAN_DIR = HOME / "Desktop"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp", ".tiff", ".bmp"}

# 고용량 분류기 — 비용 민감하면 `--model claude-haiku-4-5` 권장.
DEFAULT_MODEL = "claude-opus-4-8"
CONSOLIDATE_MODEL = "claude-opus-4-8"

# 버전/배포 — 릴리스(.app) 자동 업데이트 비교용
VERSION = "0.1.1"
REPO_SLUG = "sj48695-labs/shotsort"


# ─────────────────────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────────────────────
def db() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS images (
            path        TEXT PRIMARY KEY,
            sha         TEXT,
            mtime       REAL,
            size        INTEGER,
            ocr_text    TEXT,
            project     TEXT,   -- 1차 free-form 추정
            grp         TEXT,   -- 2차 정규화된 그룹명
            kind        TEXT,   -- code/ui/error/doc/chat/receipt/meme/photo/other
            summary     TEXT,
            deletable   INTEGER DEFAULT 0,
            confidence  REAL DEFAULT 0,
            analyzed_at REAL
        )
        """
    )
    return conn


def file_sha(path: Path, limit: int = 2_000_000) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        h.update(f.read(limit))
    h.update(str(path.stat().st_size).encode())
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# OCR  (macOS Vision → tesseract → 건너뜀)
# ─────────────────────────────────────────────────────────────────────────────
def ocr_macos_vision(path: Path) -> str | None:
    try:
        import Quartz
        import Vision
        from Foundation import NSURL
    except Exception:
        return None
    try:
        url = NSURL.fileURLWithPath_(str(path))
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        if not src:
            return None
        img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if not img:
            return None
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(1)  # 0=fast, 1=accurate
        req.setUsesLanguageCorrection_(True)
        try:
            req.setRecognitionLanguages_(["ko-KR", "en-US"])
        except Exception:
            pass
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
        ok = handler.performRequests_error_([req], None)
        if not ok:
            return None
        lines = []
        for r in req.results() or []:
            cand = r.topCandidates_(1)
            if cand and len(cand):
                lines.append(cand[0].string())
        return "\n".join(lines)
    except Exception:
        return None


def ocr_tesseract(path: Path) -> str | None:
    if not _which("tesseract"):
        return None
    try:
        out = subprocess.run(
            ["tesseract", str(path), "-", "-l", "kor+eng"],
            capture_output=True, text=True, timeout=60,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def ocr(path: Path) -> str:
    return (ocr_macos_vision(path) or ocr_tesseract(path) or "").strip()


def _which(cmd: str) -> str | None:
    from shutil import which
    return which(cmd)


# ─────────────────────────────────────────────────────────────────────────────
# Claude 분류
# ─────────────────────────────────────────────────────────────────────────────
PER_IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {
            "type": "string",
            "description": "이 스크린샷이 속한 프로젝트/주제 추정 (예: 'act-server CI', '월세 영수증', 'React 디자인 참고'). 모르면 'unknown'.",
        },
        "kind": {
            "type": "string",
            "enum": ["code", "ui", "error", "doc", "chat", "receipt", "meme", "photo", "diagram", "other"],
        },
        "summary": {"type": "string", "description": "한 줄 요약 (한국어, 30자 내외)"},
        "deletable": {
            "type": "boolean",
            "description": "보관 가치가 낮아 지워도 될 가능성이 높으면 true (중복/일회성/흐릿함/맥락없는 캡처 등)",
        },
        "confidence": {"type": "number", "description": "0~1 분류 확신도"},
    },
    "required": ["project", "kind", "summary", "deletable", "confidence"],
    "additionalProperties": False,
}

PER_IMAGE_SYSTEM = (
    "너는 스크린샷 정리 도우미다. 주어진 OCR 텍스트(와 있으면 썸네일)를 보고 "
    "이 스크린샷이 어떤 프로젝트/주제에 속하는지, 어떤 종류인지, 보관 가치가 있는지 판단한다. "
    "project 는 나중에 비슷한 것끼리 묶을 수 있도록 구체적이되 일관된 이름으로 적어라."
)


def classify_image(client, model: str, ocr_text: str, image_path: Path, with_image: bool) -> dict:
    content = []
    if with_image:
        img_b64, media = _downscaled_b64(image_path)
        if img_b64:
            content.append(
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": img_b64}}
            )
    text = ocr_text.strip() or "(OCR 텍스트 없음 - 이미지/사진일 가능성)"
    content.append({"type": "text", "text": f"파일명: {image_path.name}\n\nOCR 텍스트:\n{text[:6000]}"})

    resp = client.messages.create(
        model=model,
        max_tokens=600,
        system=PER_IMAGE_SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": PER_IMAGE_SCHEMA}},
    )
    out = next(b.text for b in resp.content if b.type == "text")
    return json.loads(out)


def _downscaled_b64(path: Path, max_edge: int = 1024):
    try:
        from PIL import Image
    except Exception:
        return None, None
    try:
        img = Image.open(path).convert("RGB")
        img.thumbnail((max_edge, max_edge))
        from io import BytesIO
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return base64.standard_b64encode(buf.getvalue()).decode(), "image/jpeg"
    except Exception:
        return None, None


_THUMB_CACHE: dict[str, tuple[float, str | None]] = {}


def thumbnail_uri(path: str | Path, max_edge: int = 320) -> str | None:
    """이미지 → data-URI 문자열(`data:image/jpeg;base64,...`). 파일 mtime 기준 캐시.

    GUI 가 매 렌더마다 호출하므로 같은(변경 안 된) 파일은 재인코딩하지 않는다.
    """
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    key = f"{p}|{max_edge}"
    hit = _THUMB_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    b64, media = _downscaled_b64(p, max_edge=max_edge)
    uri = f"data:{media};base64,{b64}" if b64 else None
    _THUMB_CACHE[key] = (mtime, uri)
    return uri


CONSOLIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "group": {"type": "string", "description": "정규화된 그룹명"},
                },
                "required": ["id", "group"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


def consolidate_groups(client, items: list[dict]) -> dict[int, str]:
    """free-form project 추정치들을 깔끔한 그룹명으로 정규화."""
    listing = "\n".join(
        f'{it["id"]}: project="{it["project"]}", kind={it["kind"]}, summary="{it["summary"]}"'
        for it in items
    )
    system = (
        "여러 스크린샷의 1차 분류 결과를 보고, 비슷한 것끼리 묶이도록 일관된 그룹명을 부여하라. "
        "거의 같은 주제는 같은 그룹명으로 통일하고, 지워도 될 잡다한 것들은 '정리(삭제후보)' 그룹으로 모아라. "
        "그룹 수는 너무 많지 않게(대략 4~12개) 의미 단위로 묶어라."
    )
    resp = client.messages.create(
        model=CONSOLIDATE_MODEL,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": f"스크린샷 목록:\n{listing}"}],
        output_config={"format": {"type": "json_schema", "schema": CONSOLIDATE_SCHEMA}},
    )
    out = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(out)
    return {a["id"]: a["group"] for a in data["assignments"]}


# ─────────────────────────────────────────────────────────────────────────────
# 로컬 폴백 (ANTHROPIC_API_KEY 없을 때) — OCR + 휴리스틱, Claude 미사용
#   무료·오프라인. 정확도는 LLM 모드보다 낮지만, OCR 텍스트 기반으로
#   종류 추정 + 토큰 겹침 클러스터링으로 "비슷한 것끼리" 묶어준다.
# ─────────────────────────────────────────────────────────────────────────────
_STOP = {
    "the", "and", "for", "you", "with", "this", "that", "from", "are", "was",
    "http", "https", "com", "www", "오전", "오후", "있습니다", "합니다", "그리고",
    "이미지", "screenshot", "스크린샷", "png", "jpg",
}

_KIND_RULES = [
    ("error",   _re.compile(r"\b(error|exception|traceback|stack ?trace|errno|failed|fatal)\b|에러|오류|실패|예외", _re.I)),
    ("receipt", _re.compile(r"영수증|결제|합계|부가세|invoice|receipt|total\s*[:：]|₩|\b\d[\d,]{2,}\s*원", _re.I)),
    ("code",    _re.compile(r"(def |function |import |const |class |=>|</[a-z]|};|public |private |#include|console\.)", _re.I)),
    ("chat",    _re.compile(r"kakaotalk|카카오톡|slack|discord|메시지|messages|보낸사람|받는사람|님\b", _re.I)),
    ("diagram", _re.compile(r"diagram|sequence|flowchart|아키텍처|architecture|mermaid", _re.I)),
]


def _tokens(text: str) -> set[str]:
    """클러스터링용 신호 토큰. 깨진 OCR 노이즈(짧은 라틴 조각·숫자)는 버린다."""
    ws = _re.findall(r"[a-z0-9가-힣]{2,}", (text or "").lower())
    out: set[str] = set()
    for w in ws:
        if w in _STOP or w.isdigit():
            continue
        if _re.search(r"[가-힣]", w):  # 한글 토큰은 2자 이상이면 신호로 인정
            out.add(w)
        elif len(w) >= 4 and sum(c.isalpha() for c in w) >= len(w) * 0.6:
            out.add(w)  # 라틴 토큰은 4자+ & 알파 비율 높을 때만(ef4·v7 같은 잡음 컷)
    return out


def _name_score(s: str) -> int:
    """그룹/프로젝트 이름 후보의 '깨끗함' 점수. 깨진 OCR 첫 줄을 피하려고 쓴다."""
    s = (s or "").strip()
    if not s:
        return -1
    letters = sum(1 for c in s if c.isalpha() or "가" <= c <= "힣")
    junk = sum(1 for c in s if not (c.isalnum() or c in " .-_/+:()"))
    return letters - junk * 2


def _looks_named(s: str) -> bool:
    """진짜 이름처럼 보이는가. 깨진 OCR(대문자 조각·자모깨짐)을 그룹명에서 거른다.

    실제 앱/창 제목엔 소문자 단어(devloop, hosting)나 한글 단어가 거의 있다.
    깨진 OCR 은 대문자 조각(AFAOFLI, EFAI)·단발 문자라 이 패턴이 없다.
    """
    return bool(_re.search(r"[a-z]{3,}", s or "") or _re.search(r"[가-힣]{2,}", s or ""))


# 종류 → 싱글톤/미분류 흡수용 한국어 라벨
_KIND_LABEL = {
    "error": "에러", "receipt": "영수증", "code": "코드", "chat": "메시지",
    "diagram": "다이어그램", "doc": "문서", "ui": "화면", "photo": "사진", "other": "기타",
}
CLEANUP_GROUP = "정리(삭제후보)"


def classify_local(text: str, path: Path) -> dict:
    t = (text or "").strip()
    kind = "other"
    for name, rx in _KIND_RULES:
        if rx.search(t):
            kind = name
            break
    if kind == "other" and len(t) > 250:
        kind = "doc"
    elif kind == "other" and len(t) >= 5:
        kind = "ui"
    elif kind == "other":
        kind = "photo"

    # 프로젝트 추정: OCR 줄 중 가장 '깨끗한' 줄(앱/창 제목) → 없으면 대표 토큰
    project = "unknown"
    candidates = [ln.strip() for ln in t.splitlines() if len(ln.strip()) >= 3]
    best = max(candidates, key=_name_score, default="")
    if best and _name_score(best) > 0:
        project = best[:40]
    else:
        toks = list(_tokens(t))
        if toks:
            project = max(toks, key=len)

    deletable = len(t) < 5  # 거의 빈 캡처/오발사진은 삭제후보(보수적)
    summary = (t.replace("\n", " ")[:40] or path.stem)
    return {"project": project, "kind": kind, "summary": summary,
            "deletable": deletable, "confidence": 0.4}


def consolidate_local(items: list[dict]) -> dict[int, str]:
    """로컬 그룹핑.

    1) 삭제후보는 '정리(삭제후보)' 한 그룹으로.
    2) 나머지는 토큰 겹침(Jaccard) union-find 로 클러스터링.
    3) 2장+ 클러스터 → 대표 깨끗한 이름. 안 묶인 1장(싱글톤)은 '기타·{종류}' 버킷으로
       흡수 → 1장당 1그룹(미분류) 폭증을 막는다.
    """
    docs = []
    for it in items:
        blob = f"{it.get('project','')} {it.get('summary','')} {(it.get('ocr_text') or '')[:1500]}"
        docs.append((it["id"], _tokens(blob), it))

    parent = {d[0]: d[0] for d in docs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    deletables = {d[0] for d in docs if d[2].get("deletable")}

    n = len(docs)
    for i in range(n):
        if docs[i][0] in deletables or not docs[i][1]:
            continue
        ti = docs[i][1]
        for j in range(i + 1, n):
            if docs[j][0] in deletables or not docs[j][1]:
                continue
            tj = docs[j][1]
            inter = len(ti & tj)
            if inter and inter / len(ti | tj) >= 0.3:
                parent[find(docs[i][0])] = find(docs[j][0])

    clusters: dict[int, list] = {}
    for did, toks, it in docs:
        clusters.setdefault(find(did), []).append((did, toks, it))

    def cluster_name(members: list) -> str:
        projects = [m[2].get("project", "") for m in members
                    if m[2].get("project") and m[2]["project"] != "unknown"]
        if projects:
            return (max(projects, key=_name_score)[:30]) or "기타"
        tok_counter: Counter = Counter()
        for _, toks, _it in members:
            tok_counter.update(toks)
        return tok_counter.most_common(1)[0][0] if tok_counter else "기타"

    # 신뢰 클러스터만 프로젝트 그룹으로 남긴다(3장+ & 이름이 깨끗할 때).
    # 그 외(작은/깨진 클러스터·싱글톤)는 모두 종류 버킷(문서/화면/에러…)으로 흡수해
    # 그룹 수가 폭증하지 않게 한다. 로컬 휴리스틱의 한계를 종류 분류로 보완.
    CONFIDENT_MIN = 3
    mapping: dict[int, str] = {}
    for members in clusters.values():
        ids = [m[0] for m in members]
        if all(i in deletables for i in ids):
            for i in ids:
                mapping[i] = CLEANUP_GROUP
            continue
        name = cluster_name(members)
        if len(members) >= CONFIDENT_MIN and _looks_named(name):
            for i in ids:
                mapping[i] = name[:30]
        else:
            for did, _toks, it in members:
                mapping[did] = _KIND_LABEL.get(it.get("kind"), "기타")
    return mapping


# ─────────────────────────────────────────────────────────────────────────────
# 키/클라이언트
# ─────────────────────────────────────────────────────────────────────────────
def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def resolve_mode(local: bool = False) -> bool:
    """분석 모드 결정의 단일 출처. True=Claude(LLM), False=로컬 휴리스틱.

    `local=True`(강제)거나 API 키가 없으면 로컬 모드. CLI·GUI 가 동일하게 이걸 쓴다.
    """
    return (not local) and has_api_key()


def human_mb(nbytes: int) -> str:
    return f"{nbytes / 1_048_576:.1f} MB"


def get_client():
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic SDK 가 필요합니다:  pip install -r requirements.txt")
    return anthropic.Anthropic()


def find_images(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in IMAGE_EXTS else []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


# ─────────────────────────────────────────────────────────────────────────────
# 고수준 동작 (UI/CLI 공통) — print 없음, 콜백/반환값으로 소통
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ScanResult:
    total: int = 0
    new: int = 0
    skipped: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)  # (filename, message)
    consolidate_error: str | None = None
    used_llm: bool = False


# on_item(i, total, path: Path, tag: dict | None, error: Exception | None)
ItemCallback = Callable[[int, int, Path, "dict | None", "Exception | None"], None]


def scan_images(
    root: Path,
    *,
    use_llm: bool,
    model: str = DEFAULT_MODEL,
    with_image: bool = False,
    force: bool = False,
    consolidate: bool = True,
    on_item: ItemCallback | None = None,
) -> ScanResult:
    """이미지를 분석해 DB 에 저장하고(캐시되지 않은 것만), 2차 그룹 정규화까지 수행."""
    res = ScanResult(used_llm=use_llm)
    imgs = find_images(root)
    res.total = len(imgs)
    if not imgs:
        return res

    conn = db()
    client = get_client() if use_llm else None

    for i, path in enumerate(imgs, 1):
        sp = str(path)
        try:
            sha = file_sha(path)
        except OSError:
            continue
        row = conn.execute("SELECT sha FROM images WHERE path=?", (sp,)).fetchone()
        if row and row["sha"] == sha and not force:
            res.skipped += 1
            continue

        text = ocr(path)
        try:
            tag = (classify_image(client, model, text, path, with_image)
                   if use_llm else classify_local(text, path))
        except Exception as e:
            res.errors.append((path.name, str(e)))
            if on_item:
                on_item(i, res.total, path, None, e)
            continue

        st = path.stat()
        conn.execute(
            """INSERT INTO images(path,sha,mtime,size,ocr_text,project,kind,summary,deletable,confidence,analyzed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,strftime('%s','now'))
               ON CONFLICT(path) DO UPDATE SET
                 sha=excluded.sha, mtime=excluded.mtime, size=excluded.size, ocr_text=excluded.ocr_text,
                 project=excluded.project, kind=excluded.kind, summary=excluded.summary,
                 deletable=excluded.deletable, confidence=excluded.confidence, analyzed_at=excluded.analyzed_at""",
            (sp, sha, st.st_mtime, st.st_size, text, tag["project"], tag["kind"],
             tag["summary"], int(tag["deletable"]), float(tag["confidence"])),
        )
        conn.commit()
        res.new += 1
        if on_item:
            on_item(i, res.total, path, tag, None)

    if consolidate:
        try:
            consolidate_all(conn=conn, use_llm=use_llm)
        except Exception as e:
            res.consolidate_error = str(e)
    return res


def consolidate_all(conn: sqlite3.Connection | None = None, *, use_llm: bool) -> int:
    """DB 전체를 대상으로 2차 그룹 정규화. 반환: 갱신된 행 수."""
    conn = conn or db()
    rows = conn.execute(
        "SELECT rowid AS id, project, kind, summary, ocr_text, deletable FROM images"
    ).fetchall()
    if not rows:
        return 0
    items = [dict(r) for r in rows]
    mapping = (consolidate_groups(get_client(), items) if use_llm
               else consolidate_local(items))
    for rid, grp in mapping.items():
        conn.execute("UPDATE images SET grp=? WHERE rowid=?", (grp, rid))
    conn.commit()
    return len(mapping)


def list_groups(deletable: bool = False) -> "OrderedDict[str, list[dict]]":
    """그룹명 → 이미지 행 리스트(dict). 행에는 path/summary/kind/deletable/project/grp 포함."""
    conn = db()
    where = "WHERE deletable=1" if deletable else ""
    rows = conn.execute(
        f"SELECT COALESCE(grp, project) AS g, path, summary, kind, deletable, project, grp, size "
        f"FROM images {where} ORDER BY g, deletable DESC, path"
    ).fetchall()
    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in rows:
        groups.setdefault(r["g"], []).append(dict(r))

    # 정렬: '정리(삭제후보)' 최상단 → 큰 그룹 → 이름. 큰/유용한 그룹이 먼저 보이게.
    def key(item):
        name, its = item
        return (0 if name == CLEANUP_GROUP else 1, -len(its), name)

    return OrderedDict(sorted(groups.items(), key=key))


def collect_paths(group: str | None, deletable: bool) -> list[str]:
    """그룹명 또는 삭제후보에 해당하는, 실제 존재하는 파일 경로들."""
    conn = db()
    if deletable:
        rows = conn.execute("SELECT path FROM images WHERE deletable=1").fetchall()
    elif group:
        rows = conn.execute(
            "SELECT path FROM images WHERE COALESCE(grp, project)=?", (group,)
        ).fetchall()
    else:
        return []
    return [r["path"] for r in rows if Path(r["path"]).exists()]


def move_to_trash(paths: list[str]) -> bool:
    """macOS 휴지통으로 이동(복구 가능). Finder 의 put-back 메타 유지."""
    if not paths:
        return True
    posix = ", ".join('POSIX file "%s"' % p.replace('"', '\\"') for p in paths)
    script = f'tell application "Finder" to delete {{{posix}}}'
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"휴지통 이동 실패: {e.stderr}")


def forget_paths(paths: list[str]) -> None:
    """DB 에서 해당 경로 행 제거(파일을 휴지통으로 보낸 뒤 호출)."""
    if not paths:
        return
    conn = db()
    conn.executemany("DELETE FROM images WHERE path=?", [(p,) for p in paths])
    conn.commit()


def trash(paths: list[str]) -> int:
    """주어진 경로들을 휴지통으로 보내고 DB 에서 제거. 반환: 처리한 개수."""
    if not paths:
        return 0
    move_to_trash(paths)
    forget_paths(paths)
    return len(paths)


@dataclass
class Stats:
    total: int = 0
    groups: int = 0
    deletable: int = 0
    deletable_bytes: int = 0


def stats() -> Stats:
    conn = db()
    total = conn.execute("SELECT COUNT(*) c FROM images").fetchone()["c"]
    if not total:
        return Stats()
    dele = conn.execute("SELECT COUNT(*) c FROM images WHERE deletable=1").fetchone()["c"]
    size = conn.execute("SELECT COALESCE(SUM(size),0) s FROM images WHERE deletable=1").fetchone()["s"]
    ngroups = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(grp,project)) c FROM images"
    ).fetchone()["c"]
    return Stats(total=total, groups=ngroups, deletable=dele, deletable_bytes=size)


# ─────────────────────────────────────────────────────────────────────────────
# 자동 업데이트 (git 기반) — 앱이 원격과 비교해 뒤처지면 알림, 원클릭 pull
# ─────────────────────────────────────────────────────────────────────────────
def repo_dir() -> Path:
    return Path(__file__).resolve().parent


def _git(*args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_dir()), *args], capture_output=True, text=True
        )
    except (FileNotFoundError, OSError) as e:
        return 1, str(e)  # git 미설치(.app 번들 등) → 호출부가 error 로 처리
    return r.returncode, (r.stdout + r.stderr).strip()


@dataclass
class UpdateStatus:
    available: bool = False
    mode: str = "none"        # "git" | "release" | "none"
    behind: int = 0           # git 모드: 원격이 로컬보다 앞선 커밋 수
    latest: str | None = None  # release 모드: 최신 버전(tag)
    url: str | None = None     # release 모드: 다운로드 페이지
    error: str | None = None


def _ver_tuple(s: str) -> tuple:
    out = []
    for p in (s or "").split("."):
        n = "".join(c for c in p if c.isdigit())
        out.append(int(n) if n else 0)
    return tuple(out) or (0,)


def check_update(fetch: bool = True) -> UpdateStatus:
    """업데이트 확인. git 저장소면 원격 커밋 비교, 아니면(.app 번들) GitHub 릴리스 비교."""
    if _git("rev-parse", "--is-inside-work-tree")[0] == 0:
        return _check_update_git(fetch)
    return _check_update_release()


def _check_update_git(fetch: bool) -> UpdateStatus:
    if fetch:
        code, out = _git("fetch", "--quiet")
        if code != 0:
            return UpdateStatus(mode="git", error=f"fetch 실패: {out[:200]}")
    code, up = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if code != 0:
        return UpdateStatus(mode="git", error="upstream(원격 추적 브랜치) 없음")
    code, out = _git("rev-list", "--count", f"HEAD..{up}")
    if code != 0:
        return UpdateStatus(mode="git", error=out[:200])
    behind = int(out or "0")
    return UpdateStatus(available=behind > 0, mode="git", behind=behind)


def _check_update_release() -> UpdateStatus:
    """GitHub 최신 릴리스 tag 와 VERSION 비교(.app 번들용). 공개 API라 인증 불필요."""
    import json
    import ssl
    import urllib.request

    try:  # 시스템 CA 미설정 대비 certifi 번들 사용
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()

    url = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json", "User-Agent": "shotsort"}
        )
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            data = json.load(r)
    except Exception as e:
        return UpdateStatus(mode="release", error=str(e)[:200])
    tag = (data.get("tag_name") or "").lstrip("v")
    if not tag:
        return UpdateStatus(mode="release", error="릴리스를 찾을 수 없음")
    newer = _ver_tuple(tag) > _ver_tuple(VERSION)
    return UpdateStatus(
        available=newer, mode="release", latest=tag,
        url=data.get("html_url") or f"https://github.com/{REPO_SLUG}/releases/latest",
    )


def apply_update() -> tuple[bool, str]:
    """git 모드: fast-forward pull 로 최신 코드를 받는다. 적용 후 재시작 필요.

    release(.app) 모드는 자체 교체 대신 다운로드 페이지(UpdateStatus.url)를 연다 — app.py 참고.
    """
    code, out = _git("pull", "--ff-only")
    return code == 0, out[:300]
