"""COSC-3506 Phase 2 service grader (URL submission).

Reads the student's deployed base URL from api_url.txt, wakes it, then exercises
the student's FastAPI student-profile API against hidden transcript HTML and
scores six rubric groups (100 pts). Ground truth is computed by parsing the
hidden HTML with the canonical extraction rule (same rule documented to students).

Canonical "past course" rule:
  - a row whose Status is in {Completed, In-Progress, Attempted} and whose Term is non-empty
  - deduplicated by (course_code, term), preferring a numeric grade > letter grade > P/blank,
    then higher credits
  - credits_earned = Credits cell as int (0 if blank/non-numeric); status = Status cell verbatim
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from grader._svc import load_base_url, wake, Grader, emit, emit_unreachable
except ImportError:  # local dev
    from lib.vpl_service_runtime import load_base_url, wake, Grader, emit, emit_unreachable

import httpx
from bs4 import BeautifulSoup

PAST_STATUSES = {"Completed", "In-Progress", "Attempted"}


def _grade_rank(g: str):
    g = (g or "").strip()
    if re.fullmatch(r"\d+(\.\d+)?", g):
        return (2, float(g))
    if re.fullmatch(r"[A-EF][+-]?", g, re.I):
        return (1, 0.0)
    return (0, 0.0)


def parse_transcript(html: str) -> list[dict]:
    """Canonical extraction. Returns list of {course_code, term, credits_earned, status}."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for t in soup.find_all("table"):
        ths = [th.get_text(strip=True) for th in t.find_all("th")]
        if not ("Status" in ths and "Course" in ths and "Credits" in ths):
            continue
        for r in t.find_all("tr"):
            tds = [c.get_text(" ", strip=True) for c in r.find_all("td")]
            if len(tds) < 6 or not tds[1]:
                continue
            status, course, grade, term, credits = tds[0], tds[1], tds[3], tds[4], tds[5]
            if status not in PAST_STATUSES or not term:
                continue
            try:
                ce = int(credits)
            except ValueError:
                ce = 0
            rows.append({"course_code": course, "term": term, "credits_earned": ce,
                         "status": status, "_grade": grade})
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r["course_code"], r["term"])
        cand = (_grade_rank(r["_grade"]), r["credits_earned"])
        if k not in best or cand > (_grade_rank(best[k]["_grade"]), best[k]["credits_earned"]):
            best[k] = r
    return [{k: r[k] for k in ("course_code", "term", "credits_earned", "status")}
            for r in best.values()]


def _hist_tuples(records) -> set:
    """Normalize a history list (student or expected) into a comparable tuple set."""
    out = set()
    for r in records or []:
        if not isinstance(r, dict):
            continue
        try:
            ce = int(r.get("credits_earned"))
        except (TypeError, ValueError):
            ce = r.get("credits_earned")
        out.add((str(r.get("course_code", "")).strip(),
                 str(r.get("term", "")).strip(),
                 ce,
                 str(r.get("status", "")).strip().lower()))
    return out


def _f1(student: set, expected: set) -> float:
    if not expected:
        return 1.0 if not student else 0.0
    inter = len(student & expected)
    if inter == 0:
        return 0.0
    prec = inter / len(student) if student else 0.0
    rec = inter / len(expected)
    return 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0


def _multipart(html_path: Path):
    return {"file": (html_path.name, html_path.read_bytes(), "text/html")}


def run(base: str, data_dir=None) -> dict:
    # default lets the engine's in-process self-test call run(base) without --data
    if data_dir is None:
        data_dir = Path(__file__).resolve().parent.parent / "data" / "hidden"
    data_dir = Path(data_dir)
    hidden_html = data_dir / "studentB_hidden.html"
    visible_html = data_dir / "student-example.html"
    expected_hidden = _hist_tuples(parse_transcript(hidden_html.read_text(encoding="utf-8", errors="replace")))
    expected_visible = _hist_tuples(parse_transcript(visible_html.read_text(encoding="utf-8", errors="replace")))

    c = httpx.Client(base_url=base, timeout=30, follow_redirects=True)
    g = Grader()

    def get_history(sid):
        r = c.get(f"/api/v1/students/{sid}/profile")
        return r.json().get("history", []) if r.status_code == 200 else []

    # ---- A. History ingest (HTML parse) — 30 ----
    def group_a():
        r = c.post("/api/v1/students/11111/history/import", files=_multipart(hidden_html))
        ok_status = r.status_code == 201
        try:
            ok_count = int(r.json().get("past_courses_imported")) == len(expected_hidden)
        except Exception:
            ok_count = False
        recs_f1 = _f1(_hist_tuples(get_history("11111")), expected_hidden)
        score = 0.2 * ok_status + 0.2 * ok_count + 0.6 * recs_f1
        failed = []
        if not ok_status:
            failed.append(f"import: POST /api/v1/students/{{sid}}/history/import -> {r.status_code}")
        if not ok_count:
            failed.append("import: past_courses_imported count incorrect")
        if recs_f1 < 0.95:
            failed.append("import: stored records don't fully match transcript")
        return score, failed

    # ---- B. History REST lifecycle — 15 ----
    def group_b():
        c.post("/api/v1/students/22222/history/import", files=_multipart(hidden_html))
        visible_records = parse_transcript(visible_html.read_text(encoding="utf-8", errors="replace"))
        r_put = c.put("/api/v1/students/22222/history", json={"history": visible_records})
        r_del = c.delete("/api/v1/students/22222/history")
        r_ne_put = c.put("/api/v1/students/90001/history", json={"history": []})
        r_ne_del = c.delete("/api/v1/students/90002/history")
        failed = []
        if r_put.status_code != 200:
            failed.append(f"PUT /api/v1/students/{{sid}}/history -> {r_put.status_code}")
        if r_del.status_code not in (200, 204):
            failed.append(f"DELETE /api/v1/students/{{sid}}/history -> {r_del.status_code}")
        if len(get_history("22222")) != 0:
            failed.append("DELETE /history: records not cleared after delete")
        if r_ne_put.status_code != 404:
            failed.append(f"PUT /history non-existent student -> {r_ne_put.status_code} (expected 404)")
        if r_ne_del.status_code != 404:
            failed.append(f"DELETE /history non-existent student -> {r_ne_del.status_code} (expected 404)")
        return (5 - len(failed)) / 5, failed

    # ---- C. Plan (JSON) endpoints — 20 ----
    def group_c():
        c.post("/api/v1/students/33333/history/import", files=_multipart(hidden_html))
        plan1 = {"planned_courses": [{"course_code": "COSC-3506", "term": "26F"},
                                     {"course_code": "ITEC-3506", "term": "26F"}]}
        r_post = c.post("/api/v1/students/33333/plan", json=plan1)
        try:
            prof1 = c.get("/api/v1/students/33333/profile").json()
        except Exception:
            prof1 = {}
        plan_set1 = {(p.get("course_code"), p.get("term")) for p in prof1.get("plan", [])}
        plan2 = {"planned_courses": [{"course_code": "MATH-4106", "term": "27W"}]}
        r_put = c.put("/api/v1/students/33333/plan", json=plan2)
        try:
            prof2 = c.get("/api/v1/students/33333/profile").json()
        except Exception:
            prof2 = {}
        plan_set2 = {(p.get("course_code"), p.get("term")) for p in prof2.get("plan", [])}
        r_del = c.delete("/api/v1/students/33333/plan")
        try:
            cleared = len(c.get("/api/v1/students/33333/profile").json().get("plan", [])) == 0
        except Exception:
            cleared = False
        r_ne = c.post("/api/v1/students/90003/plan", json=plan1)
        failed = []
        if r_post.status_code != 200:
            failed.append(f"POST /api/v1/students/{{sid}}/plan -> {r_post.status_code}")
        if ("COSC-3506", "26F") not in plan_set1 or ("ITEC-3506", "26F") not in plan_set1:
            failed.append("POST /plan: data not persisted in profile")
        if r_put.status_code != 200:
            failed.append(f"PUT /api/v1/students/{{sid}}/plan -> {r_put.status_code}")
        if plan_set2 != {("MATH-4106", "27W")}:
            failed.append("PUT /plan: did not replace existing plan")
        if r_del.status_code not in (200, 204):
            failed.append(f"DELETE /api/v1/students/{{sid}}/plan -> {r_del.status_code}")
        if not cleared:
            failed.append("DELETE /plan: records not cleared after delete")
        if r_ne.status_code != 404:
            failed.append(f"POST /plan non-existent student -> {r_ne.status_code} (expected 404)")
        return (7 - len(failed)) / 7, failed

    # ---- D. Unified profile / merge — 15 ----
    def group_d():
        c.post("/api/v1/students/44444/history/import", files=_multipart(hidden_html))
        c.post("/api/v1/students/44444/plan",
               json={"planned_courses": [{"course_code": "COSC-4106", "term": "27F"}]})
        r_prof = c.get("/api/v1/students/44444/profile")
        if r_prof.status_code != 200:
            return 0.0, [f"GET /api/v1/students/{{sid}}/profile -> {r_prof.status_code}"]
        try:
            prof = r_prof.json()
        except Exception:
            return 0.0, ["GET /profile: response is not valid JSON"]
        return {
            "profile.student_id == requested id":                    str(prof.get("student_id")) == "44444",
            "profile.history matches imported transcript (F1>=0.9)": isinstance(prof.get("history"), list) and _f1(_hist_tuples(prof["history"]), expected_hidden) >= 0.9,
            "profile.plan reflects the saved plan":                  isinstance(prof.get("plan"), list) and {(p.get("course_code"), p.get("term")) for p in prof["plan"]} == {("COSC-4106", "27F")},
            "profile has exactly {student_id, history, plan}":       set(prof.keys()) == {"student_id", "history", "plan"},
        }

    # ---- E. Data isolation — 15 ----
    def group_e():
        c.post("/api/v1/students/111/history/import", files=_multipart(hidden_html))
        c.post("/api/v1/students/222/history/import", files=_multipart(visible_html))
        a = _f1(_hist_tuples(get_history("111")), expected_hidden)
        b = _f1(_hist_tuples(get_history("222")), expected_visible)
        failed = []
        if a < 0.95:
            failed.append("isolation: student 111 records incorrect or contaminated")
        if b < 0.95:
            failed.append("isolation: student 222 records incorrect or contaminated")
        return 0.5 * a + 0.5 * b, failed

    # ---- F. REST strictness — 5 ----
    def group_f():
        r_get = c.get("/api/v1/students/11111/history/import")
        r_post = c.post("/api/v1/students/11111/profile")
        failed = []
        if r_get.status_code not in (404, 405):
            failed.append(f"GET /api/v1/students/{{sid}}/history/import -> {r_get.status_code} (expected 404/405)")
        if r_post.status_code not in (404, 405):
            failed.append(f"POST /api/v1/students/{{sid}}/profile -> {r_post.status_code} (expected 404/405)")
        return (2 - len(failed)) / 2, failed

    g.check("A_history_ingest", 30, group_a)
    g.check("B_history_lifecycle", 15, group_b)
    g.check("C_plan_endpoints", 20, group_c)
    g.check("D_profile_merge", 15, group_d)
    g.check("E_data_isolation", 15, group_e)
    g.check("F_rest_strictness", 5, group_f)
    return g.result()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("submission")
    ap.add_argument("--data", required=True)
    a = ap.parse_args()
    base = load_base_url(a.submission)
    if not wake(base, "/", timeout=90):
        emit_unreachable()
        return
    emit(run(base, Path(a.data)))


if __name__ == "__main__":
    main()
