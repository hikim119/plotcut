"""bench.py — 프롬프트 변형을 **대본 품질**로 판정하는 실험 하네스.

목표 함수는 통계가 아니라 **승률**이다. 같은 영화로 두 대본을 뽑아 나란히
놓고 「어느 쪽 숏츠를 더 보고 싶은가」를 고르게 한다. `cli check` 같은 자동
지표는 목표가 아니라 **관문**이다 — 이미 대본 두 개가 다 경고 0건이라 안 갈린다.

  python bench.py gen  --tag base1 --films father,robber --runs 1
  python bench.py gen  --tag v1 --films father,robber --variant v1_reaction
  python bench.py pack --tag base1

**`--out` 을 절대 쓰지 않는다.** `pipeline.py:289` 가 `out` 이 있으면 `rdir=None`
을 만들고, 그러면 `script_gen` 이 공유 `.work/` 로 떨어진다. 성공 판정이
`생성중_초안.txt` **파일 존재**(`script_gen.py:681`)라서 동시에 두 개를 돌리면
**다른 실행의 대본을 자기 결과로 가져간다.** 항상 `run_script(out=None,
project_name=...)` 로만 부른다.

────────────────────────────────────────────────────────────────────────────
1차 실험 결과 (생성 22회 · 심사 82표) — **프롬프트는 안 바꿨다**

  눈금   도구 대본 vs 유저분 완성 숏츠 3편, 30표 → **도구 57%**
         ending 6/6 · gut 4/6 · hook 3/6 · fun 3/6 · **flow 1/6**
         → 대본은 이미 나쁘지 않고 **따라가지는가 하나만** 못한다

  변형   v2_dense 75%(6/8) · v4_context 67%(8/12) · v3_nostyle 50% ·
         v1_reaction 38%(졌다)
  검증   이긴 둘을 합쳐 넣고 다시 붙이니 **44%(7/16)** — gentleman 4표 전패

**교훈: 조건당 한 판으로는 못 가른다.** 같은 프롬프트로도 쓴 구간이
89.9분 vs 23.7분으로 갈린다. 승률 6/8·8/12·7/16 은 전부 동전 던지기로 흔히
나오는 크기다. 다시 하려면 **조건당 3판 이상** 뽑아 놓고 대결해야 한다.

**하네스 쪽 함정 둘** (selftest [22] 가 회귀로 들고 있다):
  · 심사자는 우열을 못 가르면 **전원 1번**을 찍는다 → 순서 반전 필수
  · 죽은 표 하나가 50/50 을 55.6/44.4 로 민다 → 짝 안 맞는 관점은 버린다
────────────────────────────────────────────────────────────────────────────
"""

import argparse
import json
import pathlib
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BENCH = ROOT / "results" / "_bench"

# 목표 초는 **유저분 완성본과 같은 길이**로 잡는다 — 눈금 대결에서 길이가
# 다르면 대본이 아니라 분량을 비교하게 된다.
FILMS = {
    "father":    {"srt": ROOT / "대본예시" / "원본자막" / "father 원본.vtt",
                  "seconds": 155.0, "title": "", "human": True},
    "gentleman": {"srt": ROOT / "대본예시" / "원본자막" / "gentlemen 원본.vtt",
                  "seconds": 111.0, "title": "", "human": True},
    "robber":    {"srt": ROOT / "대본예시" / "원본자막" / "robber 원본.vtt",
                  "seconds": 150.0, "title": "", "human": True},
    # 한국어 자막 — **다른 분기**를 탄다. 변형이 외국어 경로에만 맞춰지지
    # 않았는지 검증하는 표본이다. 유저분 완성본은 없다.
    "chungking": {"srt": ROOT / "test" / "중경삼림 리마스터링_자막.srt",
                  "seconds": 180.0, "title": "중경삼림", "human": False},
}

JOBS = 2      # 에이전트 CLI 가 무겁고 둘 다 같은 트리에 쓰기 권한을 갖는다


# ── 배치 생성 ───────────────────────────────────────────────────────────────

def _run_one(tag, film, i, extra, variant, out, keep_log=False):
    import pipeline
    name = "bench_%s_%s_%d" % (tag, film, i)
    spec = FILMS[film]
    logs = []
    t0 = time.time()
    rec = {"tag": tag, "film": film, "run": i, "name": name,
           "extra": extra, "variant": variant}
    try:
        r = pipeline.run_script(str(spec["srt"]), out=None, project_name=name,
                                target_s=spec["seconds"], extra=extra,
                                movie_title=spec["title"], variant=variant,
                                keep_log=keep_log, log=logs.append)
        rec["script"] = str(r["script_path"])
        _lg = pathlib.Path(r["script_path"]).parent / "에이전트로그.txt"
        rec["agent_log"] = str(_lg) if _lg.exists() else None
        rec["ok"] = True
    except Exception as e:                                   # noqa: BLE001
        # 배치는 멈추지 않는다. 실패도 수확이다 — 외국어 분기는 실제 생성에서
        # 한 번도 안 돌았다.
        rec["ok"] = False
        rec["error"] = "%s: %s" % (type(e).__name__, e)
    rec["secs"] = round(time.time() - t0, 1)
    rec["log"] = logs
    out.append(rec)
    print("  %-30s %s (%.0f초)"
          % (name, "OK" if rec["ok"] else "실패 " + rec.get("error", "")[:40],
             rec["secs"]), flush=True)


def cmd_gen(a):
    films = [f.strip() for f in a.films.split(",") if f.strip()]
    for f in films:
        if f not in FILMS:
            sys.exit("모르는 영화: %s (%s)" % (f, ", ".join(FILMS)))
    jobs = [(f, i) for f in films for i in range(1, a.runs + 1)]
    # 이어서 하기 — 성공한 판은 다시 안 뽑는다. 실측(8/26): Codex 사용량 한도로
    # 9판 중 6판이 죽었는데, 그대로 다시 돌리면 살아 있는 3판까지 새로 뽑느라
    # 한도를 또 태우고, 결과 JSON 을 통째로 덮어써 **성공한 판을 잃는다.**
    keep = []
    if a.resume:
        pth = BENCH / ("%s.json" % a.tag)
        if pth.exists():
            for r in json.loads(pth.read_text(encoding="utf-8")):
                if r.get("ok") and Path(r["script"]).exists():
                    keep.append(r)
            done = {(r["film"], r["run"]) for r in keep}
            jobs = [j for j in jobs if j not in done]
            print("이어서 — 이미 된 %d판은 건너뜁니다" % len(keep))
    print("배치 %s — %d회 (영화 %d × %d회), 동시 %d개, 변형 %s"
          % (a.tag, len(jobs), len(films), a.runs, a.jobs,
             a.variant or "없음(기본)"))
    out, sem, lock = list(keep), threading.Semaphore(a.jobs), threading.Lock()

    def worker(film, i):
        with sem:
            local = []
            _run_one(a.tag, film, i, a.extra, a.variant, local, keep_log=a.keep_log)
            with lock:
                out.extend(local)

    ts = [threading.Thread(target=worker, args=j) for j in jobs]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    BENCH.mkdir(parents=True, exist_ok=True)
    p = BENCH / ("%s.json" % a.tag)
    out.sort(key=lambda r: (r["film"], r["run"]))
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for r in out if r["ok"])
    print("%d/%d 성공 · %s" % (ok, len(out), p))
    return 0 if ok else 1


# ── 대결용으로 굽기 ─────────────────────────────────────────────────────────
# 도구 대본은 txt(블록 헤더 · `@시각` 접두어)이고 유저분 완성본은 자막 srt 다.
# 그대로 보여 주면 **형식으로 어느 쪽인지 바로 티가 난다.** 그래서 둘 다
# 「대사/나레 + 한 줄」의 같은 중립 형식으로 굽는다. 시각·번호·헤더는 뺀다.
#
# 고유명사 마스킹은 **안 한다.** 모든 대결이 같은 영화끼리라 양쪽 이름이 같다
# (어제 판별 시험에서 마스킹한 건 서로 다른 영화를 섞었기 때문이다).

NL = chr(10)


def render(seq):
    return NL.join("%s  %s" % ("나레" if k == "narration" else "대사", t)
                   for k, t in seq)


def pack_tool(script_path, film):
    """도구 대본 txt → [(kind, text)] 차례."""
    import script_io
    import subtitle
    cues = subtitle.parse_file(FILMS[film]["srt"])[0]
    doc = script_io.read(pathlib.Path(script_path).read_text(encoding="utf-8"),
                         cues, log=lambda *_: None)
    return [(it["kind"], it["text"].replace(NL, " ").strip())
            for b in doc["blocks"] for it in b["items"]]


def _norm(s):
    return "".join(s.split())


def pack_human(film):
    """유저분 완성 숏츠 자막 → 같은 형식.

    나레이션 판별은 `fixtures/narration_gold.json` 이 그 33문장을 들고 있어서
    가능하다 — 완성본 자막 자체에는 표시가 없다.

    다만 **문장 그대로는 안 맞는다.** 화면에서 나레이션은 조각으로 쪼개져
    여러 큐에 걸쳐 있다(`갑작스런 비보를` / `받게 된 할`). 문장으로 대조하면
    father 11개 중 6개, robber 10개 중 1개만 잡힌다 — 실측.

    `wrap.split_narration` 으로 같은 조각을 내서 맞춰도 안 된다. 유저분이 손으로
    나눈 자리와 어긋나는 문장이 남는다(father 1 · gentleman 2 · robber 3 누락).
    그래서 **분할 방식에 안 기댄다** — 연속 큐를 이어 붙여 정답 문장이 되면
    나레이션이다. 그러면 11/11 · 12/12 · 10/10 전부 잡힌다.
    """
    import subtitle
    gold = json.loads((ROOT / "fixtures" / "narration_gold.json")
                      .read_text(encoding="utf-8"))
    want = {_norm(t): t for t in gold[film]["narrations"]}
    cues = subtitle.parse_file(ROOT / "대본예시" / ("%s.srt" % film))[0]
    txt = [c["text"].replace(NL, " ").strip() for c in cues]
    out, i, used = [], 0, set()
    while i < len(txt):
        # 연속 큐를 이어 붙이며 정답 문장이 되는지 본다. 분할 방식에 안 기댄다 —
        # `wrap.split_narration` 으로 조각을 맞추면 유저분이 손으로 나눈 자리와
        # 어긋나는 문장이 남는다 (실측: father 1개 · gentleman 2 · robber 3 누락).
        acc, hit = "", None
        for k in range(i, min(i + 5, len(txt))):
            acc += _norm(txt[k])
            if acc in want and want[acc] not in used:
                hit = (want[acc], k - i + 1)
                break
        if hit:
            out.append(("narration", hit[0]))
            used.add(hit[0])
            i += hit[1]
        else:
            out.append(("dialogue", txt[i]))
            i += 1
    return out


def cmd_pack(a):
    recs = json.loads((BENCH / ("%s.json" % a.tag)).read_text(encoding="utf-8"))
    outdir = BENCH / "packed"
    outdir.mkdir(parents=True, exist_ok=True)
    made = []
    for r in recs:
        if not r.get("ok"):
            continue
        # 자막이 없거나 대본이 깨졌으면 그 항목만 건너뛴다 — `pack_human` 과 같은
        # 이유다. 한 개 때문에 배치 전체를 못 굽게 두지 않는다.
        try:
            seq = pack_tool(r["script"], r["film"])
        except Exception as e:                              # noqa: BLE001
            print("  %s 건너뜀 — %s: %s" % (r["name"], type(e).__name__, str(e)[:60]))
            continue
        p = outdir / ("%s.txt" % r["name"])
        p.write_text(render(seq), encoding="utf-8")
        made.append((r["name"], len(seq)))
    for film, spec in FILMS.items():
        if not spec["human"]:
            continue
        p = outdir / ("human_%s.txt" % film)
        if p.exists():
            continue
        # 완성본이 없는 PC 도 있다. 예전엔 여기서 `FileNotFoundError` 로 죽어
        # **도구 대본까지 못 굽었다** — 실측: 대본예시 없는 클론에서 크래시.
        # 눈금 대결만 못 할 뿐 변형 대결은 도구끼리라 굽는 게 맞다.
        try:
            seq = pack_human(film)
        except (OSError, KeyError) as e:
            print("  human_%s 건너뜀 — %s" % (film, type(e).__name__))
            continue
        p.write_text(render(seq), encoding="utf-8")
        made.append(("human_%s" % film, len(seq)))
    for name, n in made:
        print("  %-30s %d줄" % (name, n))
    print("%d개 · %s" % (len(made), outdir))
    return 0


# ── 대결 ────────────────────────────────────────────────────────────────────
# 심사자는 두 대본을 전문으로 읽고 「어느 쪽 숏츠를 더 보고 싶은가」를 고른다.
# **순서를 반드시 뒤집어 두 번 돌린다** — 위치 편향은 LLM 심사의 고질이고,
# 안 잡으면 승률이 통째로 왜곡된다. 같은 대본을 자기 자신과 붙였을 때 50%가
# 나오는지가 그 검사다 — selftest [22] 가 그 결과를 회귀로 들고 있다.

LENSES = {
    "hook":   "특히 **첫 30초**를 봐라 — 계속 보게 만드는가, 아니면 설명부터 시작하는가.",
    "flow":   "특히 **따라가지는가**를 봐라 — 누가 뭘 하는지 헷갈리는 대목이 있는가.",
    "fun":    "특히 **재미**를 봐라 — 웃기거나 쫄깃한가, 밋밋한가.",
    "ending": "특히 **끝**을 봐라 — 여운이 남는가, 그냥 끊기는가.",
    "gut":    "분석하지 말고 **끝까지 보고 싶은 쪽**을 첫인상으로 골라라.",
}

DUEL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pick", "why"],
    "properties": {
        "pick": {"type": "string", "enum": ["1", "2"]},
        "why": {"type": "string", "description": "고른 이유 두 문장 이내"},
        "margin": {"type": "string", "enum": ["큼", "보통", "근소"]},
    },
}


def duel_prompt(first, second, lens_hint):
    return NL.join([
        "아래는 같은 영화로 만든 **유튜브 숏츠 영화 리뷰 대본** 두 개다.",
        "각 줄은 화면에 뜨는 자막 한 장이고, `대사` 는 영화 대사, `나레` 는 내레이션이다.",
        "",
        "**어느 쪽 숏츠를 더 보고 싶은가?** 하나를 골라라.",
        "",
        "- 둘 다 같은 영화라 줄거리가 겹치는 건 당연하다. **어느 쪽이 더 잘 만들었는지**만 봐라.",
        "- 누가 썼는지 추측하지 마라. 오타나 띄어쓰기로 판단하지 마라.",
        "- 길이가 조금 다를 수 있다. 분량이 아니라 대본을 봐라.",
        "- 반드시 둘 중 하나를 골라라. 비긴다는 선택지는 없다.",
        lens_hint,
        "",
        "=" * 30 + " 1번 " + "=" * 30,
        first,
        "",
        "=" * 30 + " 2번 " + "=" * 30,
        second,
    ])


def tally(votes):
    """[{a, b, order, lens, pick}] → 이름별 승수.

    `order` 는 1번 자리에 놓인 쪽이다. `pick` 은 "1"/"2" 라 자리 기준이므로
    이름으로 되돌려야 한다.

    **짝이 안 맞는 표는 버린다.** 자기 검사에서 드러났다 — 심사자는 우열을
    못 가르면 **전원 1번을 찍는다**(같은 대본을 양쪽에 놓자 9표 전원 1번).
    순서를 뒤집어 두 번 돌리면 그 쏠림이 상쇄되지만, 한쪽이 API 오류로 죽으면
    그 관점은 1번 자리에 있던 쪽에 표를 통째로 얹는다. 실측: 죽은 표 하나가
    50/50 을 55.6/44.4 로 밀었다.
    """
    seen = {}
    for v in votes:
        seen.setdefault((v["a"], v["b"], v["lens"]), set()).add(v["order"])
    votes = [v for v in votes
             if len(seen[(v["a"], v["b"], v["lens"])]) == 2]
    win, tot, pos = {}, {}, {"1": 0, "2": 0}
    for v in votes:
        first = v["order"]
        second = v["b"] if first == v["a"] else v["a"]
        winner = first if v["pick"] == "1" else second
        pos[v["pick"]] += 1
        for nm in (v["a"], v["b"]):
            tot[nm] = tot.get(nm, 0) + 1
        win[winner] = win.get(winner, 0) + 1
    return {"win": win, "total": tot, "position": pos,
            "rate": {k: round(win.get(k, 0) / v, 3) for k, v in tot.items()}}


# ── Codex 관문 — 제품 심사자가 독립 심사자와 맞는가 ──────────────────────────
# **이 명령은 지금 쓸 데가 없다.** 순위 심사 자체가 관문에 떨어졌기 때문이다
# (8/26: 같은 대본을 두 번 재워 τ 0.333 · 합의 1위 3편 중 1편). 재현되지 않는
# 자를 두 개 놓고 서로 맞는지 재 봐야 의미가 없다. 조건 비교는 쌍대 대결로 한다.
# 남겨 두는 건 나중에 **더 나은 순위 심사**를 만들었을 때 같은 자리에서 재기 위해서다.
# 실험 심사는 sonnet(Workflow)이고 제품 심사는 Codex(`script_gen.rank_candidates`)다.
# 실험이 순위 심사를 검증해도 그건 sonnet 의 순위다. 후보 기능의 가치는 「Codex 가
# 고른 후보가 독립 심사자가 고른 후보와 얼마나 일치하는가」인데, 그걸 여기서 잰다.
# 판 k 로 묶어 {tag1_k, tag2_k, tag3_k} 를 한 블록으로 순위 매긴다 — 제품과 같은
# 함수, 같은 순환 3순서.

def cmd_rank(a):
    import script_gen
    import subtitle
    tags = [t.strip() for t in a.tags.split(",") if t.strip()]
    films = [f.strip() for f in a.films.split(",") if f.strip()]
    recs = {}
    for t in tags:
        for r in json.loads((BENCH / ("%s.json" % t)).read_text(encoding="utf-8")):
            if r.get("ok"):
                recs[(t, r["film"], r["run"])] = r["script"]
    out = []
    for film in films:
        cues = subtitle.parse_file(FILMS[film]["srt"])[0]
        for k in range(1, a.runs + 1):
            paths = [recs.get((t, film, k)) for t in tags]
            if any(p is None for p in paths):
                print("  %s 판%d — 빠진 조건이 있어 건너뜀" % (film, k))
                continue
            work = BENCH / "rank" / ("%s_%d" % (film, k))
            res = script_gen.rank_candidates(paths, cues, work, prefer=a.agent,
                                             log=lambda m: print("   " + m.strip()))
            if res is None:
                print("  %s 판%d — 심사 실패" % (film, k))
                out.append({"film": film, "run": k, "tags": tags, "ok": False})
                continue
            best, avg, why = res
            print("  %s 판%d → 1위 %s · 평균 %s" % (film, k, tags[best],
                  " ".join("%s=%.1f" % (t, v) for t, v in zip(tags, avg))))
            out.append({"film": film, "run": k, "tags": tags, "ok": True,
                        "best": tags[best], "avg": avg, "why": why})
    BENCH.mkdir(parents=True, exist_ok=True)
    pth = BENCH / ("rank_%s.json" % "_".join(tags))
    pth.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("저장:", pth)
    return 0


# ── 조건 대결 준비 ──────────────────────────────────────────────────────────
# 순위 심사가 재현율 관문에 떨어져서(τ 0.333) 조건 비교는 **쌍대**로 돌아왔다.
# 판 k 끼리 짝지어 A/B 파일과 정답 키를 만든다. 심사자는 두 파일만 읽으면 되고,
# **순서를 뒤집은 두 번**을 반드시 같이 돌린다(위치 편향). A/B 배치는 판 번호로
# 번갈아 — 한 조건이 늘 1번 자리에 서면 그 편향이 조건에 그대로 얹힌다.

def cmd_duel(a):
    t1, t2 = [t.strip() for t in a.tags.split(",")]
    films = [f.strip() for f in a.films.split(",") if f.strip()]
    recs = {}
    for t in (t1, t2):
        for r in json.loads((BENCH / ("%s.json" % t)).read_text(encoding="utf-8")):
            if r.get("ok"):
                recs[(t, r["film"], r["run"])] = r
    out = BENCH / a.name
    out.mkdir(parents=True, exist_ok=True)
    key, made = {}, 0
    for film in films:
        for k in range(1, a.runs + 1):
            r1, r2 = recs.get((t1, film, k)), recs.get((t2, film, k))
            if not r1 or not r2:
                print("  %s 판%d — 빠진 조건이 있어 건너뜀" % (film, k))
                continue
            pair = "%s_%d" % (film, k)
            # 판이 바뀔 때마다 A/B 를 뒤집는다
            first, second = (r1, r2) if k % 2 else (r2, r1)
            try:
                for side, rec in (("A", first), ("B", second)):
                    (out / ("%s_%s.txt" % (pair, side))).write_text(
                        render(pack_tool(rec["script"], film)), encoding="utf-8")
            except Exception as e:                          # noqa: BLE001
                print("  %s 건너뜀 — %s: %s" % (pair, type(e).__name__, str(e)[:50]))
                continue
            key[pair] = {"A": first["tag"], "B": second["tag"]}
            made += 1
    (out / "_key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    print("%d쌍 · %s" % (made, out))
    for pair, sides in sorted(key.items()):
        print("  %-16s A=%-8s B=%s" % (pair, sides["A"], sides["B"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bench")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("gen", help="대본을 배치로 뽑는다")
    p.add_argument("--tag", required=True)
    p.add_argument("--films", default="father,robber,gentleman")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--extra", default="")
    p.add_argument("--resume", action="store_true",
                   help="이미 성공한 판은 건너뛰고 실패·안 한 판만 채운다")
    p.add_argument("--keep-log", dest="keep_log", action="store_true",
                   help="성공해도 에이전트 로그를 남긴다 (자기 점검 수행 여부 확인용)")
    p.add_argument("--variant", default=None,
                   help="script_gen.VARIANTS 의 키. 생략하면 기본 프롬프트")
    p.add_argument("--jobs", type=int, default=JOBS)
    p = sub.add_parser("pack", help="대결용 중립 형식으로 굽는다")
    p.add_argument("--tag", required=True)
    p = sub.add_parser("duel", help="두 조건을 판끼리 짝지어 A/B 파일과 키를 만든다")
    p.add_argument("--tags", required=True, help="예: base3,nameN")
    p.add_argument("--name", required=True, help="만들 폴더 이름 (예: duel5)")
    p.add_argument("--films", default="father,robber,gentleman")
    p.add_argument("--runs", type=int, default=3)
    p = sub.add_parser("rank", help="[보류] 순위 심사는 재현율 관문에 떨어졌다 — 쌍대를 써라")
    p.add_argument("--tags", required=True, help="예: base3,nameN,selfS")
    p.add_argument("--films", default="gentleman")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--agent", default=None, choices=["codex", "claude"])
    a = ap.parse_args(argv)
    if a.cmd == "gen":
        return cmd_gen(a)
    if a.cmd == "pack":
        return cmd_pack(a)
    if a.cmd == "duel":
        return cmd_duel(a)
    if a.cmd == "rank":
        return cmd_rank(a)
    return 1


if __name__ == "__main__":
    sys.exit(main())
