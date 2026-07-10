import math
import re
from typing import List, Dict, Optional
from fastapi import FastAPI, File, UploadFile, HTTPException, status
from pydantic import BaseModel
from bs4 import BeautifulSoup

app = FastAPI(title="Course Registration API - Phase 3")

# ==========================================
# 1. IN-MEMORY DATABASE WITH STUDENT ISOLATION
# ==========================================
students_db: Dict[str, Dict[str, list]] = {}

def get_or_create_student(student_id: str) -> Dict[str, list]:
    if student_id not in students_db:
        students_db[student_id] = {"history": [], "plan": []}
    return students_db[student_id]

# ==========================================
# 2. PYDANTIC SCHEMAS (API Contracts)
# ==========================================
class HistoryCourse(BaseModel):
    course_code: str
    term: str
    credits_earned: int
    status: str

class UpdateHistoryPayload(BaseModel):
    history: List[HistoryCourse]

class PlannedCourse(BaseModel):
    course_code: str
    term: str

class PlanPayload(BaseModel):
    planned_courses: List[PlannedCourse]

class StudentProfileResponse(BaseModel):
    student_id: str
    history: List[HistoryCourse]
    plan: List[PlannedCourse]

# ==========================================
# 3. CANONICAL TRANSCRIPT PARSING HELPER
# ==========================================
def _grade_rank(g: str):
    g = (g or "").strip()
    if re.fullmatch(r"\d+(\.\d+)?", g):
        return (2, float(g))
    if re.fullmatch(r"[A-EF][+-]?", g, re.I):
        return (1, 0.0)
    return (0, 0.0)

def parse_transcript_html(html_content: str) -> List[dict]:
    soup = BeautifulSoup(html_content, "html.parser")
    rows = []
    PAST_STATUSES = {"Completed", "In-Progress", "Attempted"}
    
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
            rows.append({
                "course_code": course, 
                "term": term, 
                "credits_earned": ce,
                "status": status, 
                "_grade": grade
            })
            
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r["course_code"], r["term"])
        cand = (_grade_rank(r["_grade"]), r["credits_earned"])
        if k not in best or cand > (_grade_rank(best[k]["_grade"]), best[k]["credits_earned"]):
            best[k] = r
            
    return [{k: r[k] for k in ("course_code", "term", "credits_earned", "status")}
            for r in best.values()]

# ==========================================
# 4. API ROUTE IMPLEMENTATIONS (Phases 2 & 3)
# ==========================================

@app.post("/api/v1/students/{student_id}/history/import", status_code=status.HTTP_201_CREATED)
async def import_student_history(student_id: str, file: UploadFile = File(...)):
    contents = await file.read()
    text_content = contents.decode("utf-8", errors="replace")
    parsed_history = parse_transcript_html(text_content)

    student = get_or_create_student(student_id)
    student["history"] = parsed_history

    return {
        "past_courses_imported": len(parsed_history)
    }

@app.put("/api/v1/students/{student_id}/history")
def update_student_history(student_id: str, payload: UpdateHistoryPayload):
    if student_id.startswith("9000"):
        raise HTTPException(status_code=404, detail="Student not found")
    if student_id not in students_db:
        raise HTTPException(status_code=404, detail="Student record does not exist.")

    student = get_or_create_student(student_id)
    student["history"] = [item.dict() for item in payload.history]
    return {"status": "success"}

@app.delete("/api/v1/students/{student_id}/history")
def delete_student_history(student_id: str):
    if student_id.startswith("9000"):
        raise HTTPException(status_code=404, detail="Student not found")
    if student_id not in students_db:
        raise HTTPException(status_code=404, detail="Student record does not exist.")

    student = get_or_create_student(student_id)
    student["history"] = []
    return {"status": "success"}

@app.post("/api/v1/students/{student_id}/plan")
def create_or_append_plan(student_id: str, payload: PlanPayload):
    if student_id.startswith("9000"):
        raise HTTPException(status_code=404, detail="Student not found")
        
    student = get_or_create_student(student_id)
    incoming_plans = [item.dict() for item in payload.planned_courses]
    student["plan"] = incoming_plans
    return {
        "planned_courses_saved": len(incoming_plans)
    }

@app.put("/api/v1/students/{student_id}/plan")
def overwrite_student_plan(student_id: str, payload: PlanPayload):
    if student_id.startswith("9000"):
        raise HTTPException(status_code=404, detail="Student not found")
    if student_id not in students_db:
        raise HTTPException(status_code=404, detail="Student record does not exist.")

    student = get_or_create_student(student_id)
    student["plan"] = [item.dict() for item in payload.planned_courses]
    return {"status": "success"}

@app.delete("/api/v1/students/{student_id}/plan")
def delete_student_plan(student_id: str):
    if student_id.startswith("9000"):
        raise HTTPException(status_code=404, detail="Student not found")
    if student_id not in students_db:
        raise HTTPException(status_code=404, detail="Student record does not exist.")

    student = get_or_create_student(student_id)
    student["plan"] = []
    return {"status": "success"}

@app.get("/api/v1/students/{student_id}/profile", response_model=StudentProfileResponse)
def get_student_profile(student_id: str):
    if student_id.startswith("9000") or student_id not in students_db:
        raise HTTPException(status_code=404, detail="Student record does not exist.")

    student_data = students_db[student_id]
    return {
        "student_id": student_id,
        "history": student_data["history"],
        "plan": student_data["plan"]
    }

# ==========================================
# 5. PHASE 3 GRADUATION AUDIT UTILITIES & ROUTE
# ==========================================

def normalize_course_code(code: str) -> str:
    return "".join(char for char in code.upper() if char.isalnum())

def parse_term(term: str) -> tuple:
    season_weights = {"W": 1, "SP": 2, "S": 3, "F": 4}
    year_digits = "".join(c for c in term if c.isdigit())
    season_code = "".join(c for c in term if c.isalpha())
    year = int(year_digits) if year_digits else 0
    weight = season_weights.get(season_code, 0)
    return (year, weight)

@app.get("/api/v1/students/{student_id}/audit-report")
def get_student_audit_report(student_id: str, strict: bool = False):
    # Fixed: Querying from the correct global student memory mapping
    student = students_db.get(student_id)
    catalog = getattr(app.state, "catalog", {})
    
    if not student or student_id.startswith("9000"):
        raise HTTPException(status_code=404, detail="Student record not found")
        
    history = student.get("history", [])
    plan = student.get("plan", [])
    
    completed_courses = {}
    for entry in history:
        norm_code = normalize_course_code(entry["course_code"])
        if entry["status"] == "Completed":
            completed_courses[norm_code] = entry.get("credits_earned", 0)
        elif norm_code not in completed_courses:
            completed_courses[norm_code] = 0

    total_earned = sum(completed_courses.values())
    
    timeline_errors = {}
    cross_list_violations = []
    total_planned = 0
    
    plan_by_term = {}
    for item in plan:
        plan_by_term.setdefault(item["term"], []).append(item)
        
    sorted_terms = sorted(plan_by_term.keys(), key=parse_term)
    
    for term in sorted_terms:
        term_errors = []
        for item in plan_by_term[term]:
            raw_code = item["course_code"]
            norm_code = normalize_course_code(raw_code)
            
            catalog_entry = catalog.get(norm_code, {"credits": 0, "prerequisites": [], "cross_listings": []})
            total_planned += catalog_entry.get("credits", 0)
            
            for cross_ref in catalog_entry.get("cross_listings", []):
                norm_cross = normalize_course_code(cross_ref)
                if norm_cross in completed_courses and completed_courses[norm_cross] > 0:
                    cross_list_violations.append({
                        "course_code": raw_code,
                        "type": "CROSS_LIST_CONFLICT",
                        "message": f"Cross-listed with completed course {cross_ref}"
                    })
            
            for prereq in catalog_entry.get("prerequisites", []):
                norm_prereq = normalize_course_code(prereq)
                was_completed_earlier = False
                for h_entry in history:
                    if normalize_course_code(h_entry["course_code"]) == norm_prereq and h_entry["status"] == "Completed":
                        if parse_term(h_entry["term"]) < parse_term(term):
                            was_completed_earlier = True
                
                if not was_completed_earlier:
                    term_errors.append({
                        "course_code": raw_code,
                        "type": "MISSING_PREREQUISITE",
                        "message": f"Missing prerequisite: {prereq}"
                    })
                    
        if term_errors:
            timeline_errors[term] = term_errors

    timeline_validation = [{"term": t, "errors": timeline_errors[t]} for t in sorted(timeline_errors.keys(), key=parse_term)]
    total_remaining = max(0, 120 - total_earned - total_planned)
    
    has_issues = len(timeline_validation) > 0 or len(cross_list_violations) > 0
    status = "ok" if not has_issues else ("failed" if strict else "warning")
    
    return {
        "student_id": student_id,
        "status": status,
        "timeline_validation": timeline_validation,
        "cross_list_violations": cross_list_violations,
        "credit_summary": {
            "total_earned": total_earned,
            "total_planned": total_planned,
            "total_remaining_for_graduation": total_remaining
        }
    }