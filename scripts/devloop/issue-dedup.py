#!/usr/bin/env python3
"""자식 이슈 중복 필터 — DevLoop 의 모든 이슈 생성 경로 공용 백스톱.

stdin  (JSON): {"candidates":[{"title","description"|"body",...}],
                "existing":[{"iid","title","body"|"description"}],
                "threshold": 0.7}
stdout (JSON): {"keep":[<원본 candidate 그대로>...],
                "skip":[{"title","reason","dupOf"}]}

판정(같은 repo 한정 — 'Parent: #N' 마커. cross-project 'Parent: proj #N' 은 비교 불가라 무시):
  1) 정규화 제목 완전일치          → dup-title       (기존 열린 이슈와 같은 제목 재생성)
  2) 토큰겹침 ≥ threshold 이고
     상대가 내 부모이거나 같은 부모 → echoes-parent / dup-sibling
     (#247 이 부모 #245 를 거의 그대로 재서술한 케이스를 잡는다)
  3) 토큰겹침 ≥ 0.85 (거의 동일)   → near-duplicate   (부모 무관 명백한 클론)
배치 내부(candidate 끼리)도 동일 규칙으로 중복 제거.
의존성 없으면(파이썬 없음 등) 호출측이 dedup 을 건너뛰고 기존 동작 유지.
"""
import json
import re
import sys

BRACKET = re.compile(r"^\s*\[[^\]]*\]\s*")
PARENT = re.compile(r"Parent:\s*#(\d+)", re.IGNORECASE)  # 같은 repo 자식만 (proj 토큰 없는 형태)
NONWORD = re.compile(r"[^0-9a-z가-힣]+")  # 영숫자 + 한글 음절만 토큰


def norm_title(t):
    t = (t or "").lower()
    t = BRACKET.sub("", t)  # [Subscription] / [Feature] 같은 머리 태그 제거
    return NONWORD.sub(" ", t).strip()


def tokens(t):
    return set(w for w in norm_title(t).split() if w)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def parent_of(body):
    m = PARENT.search(body or "")
    return int(m.group(1)) if m else None


def main():
    data = json.load(sys.stdin)
    cands = data.get("candidates", [])
    existing = data.get("existing", [])
    thr = float(data.get("threshold", 0.7))

    # 기존 이슈 인덱스
    ex = []
    by_iid = {}
    for e in existing:
        iid = e.get("iid")
        title = e.get("title", "")
        body = e.get("body") or e.get("description") or ""
        rec = {"iid": iid, "ntitle": norm_title(title), "toks": tokens(title), "parent": parent_of(body)}
        ex.append(rec)
        if iid is not None:
            by_iid[int(iid)] = rec

    keep, skip = [], []
    for c in cands:
        title = c.get("title", "")
        body = c.get("description") or c.get("body") or ""
        cn = norm_title(title)
        ct = tokens(title)
        cp = parent_of(body)
        hit = None
        for e in ex:
            if cn and e["ntitle"] == cn:
                hit = ("dup-title", e["iid"]); break
            j = jaccard(ct, e["toks"])
            if j >= thr:
                if cp is not None and e["iid"] is not None and int(e["iid"]) == cp:
                    hit = ("echoes-parent", e["iid"]); break
                if cp is not None and e["parent"] == cp:
                    hit = ("dup-sibling", e["iid"]); break
            if j >= 0.85:
                hit = ("near-duplicate", e["iid"]); break
        if hit:
            skip.append({"title": title, "reason": hit[0], "dupOf": hit[1]})
        else:
            keep.append(c)
            # 배치 내부 중복도 막기 위해 채택분을 기존 집합에 추가
            ex.append({"iid": None, "ntitle": cn, "toks": ct, "parent": cp})

    # ── 단일자식 규칙(결정론) ──────────────────────────────────────────────
    # 같은 '열린 부모'의 자식이 (기존 + 이번 배치) 합쳐 1개뿐이면 분해가 아니라 부모 작업을
    # 둘로 쪼갠 것 → 생성 차단(부모에서 진행). 진짜 분해는 distinct 자식 2개+ 일 때만.
    # (#293→#298, #189→#244 처럼 단일자식 Phase-split 차단.) loneChild=false 로 끌 수 있음.
    if data.get("loneChild", True) and keep:
        open_iids = set(int(e["iid"]) for e in existing if e.get("iid") is not None)
        existing_children = {}
        for e in existing:
            p = parent_of(e.get("body") or e.get("description") or "")
            if p is not None:
                existing_children[p] = existing_children.get(p, 0) + 1
        batch_children = {}
        for c in keep:
            p = parent_of(c.get("description") or c.get("body") or "")
            if p is not None:
                batch_children[p] = batch_children.get(p, 0) + 1
        keep2 = []
        for c in keep:
            p = parent_of(c.get("description") or c.get("body") or "")
            # 부모가 같은 repo 열린 이슈이고, 그 부모의 자식 총합이 1이면 lone-child → fold
            if p is not None and p in open_iids and (existing_children.get(p, 0) + batch_children.get(p, 0)) == 1:
                skip.append({"title": c.get("title", ""), "reason": "lone-child", "dupOf": p})
            else:
                keep2.append(c)
        keep = keep2

    json.dump({"keep": keep, "skip": skip}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
