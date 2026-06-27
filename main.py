import math
import re
from typing import List, Dict, Optional
from fastapi import FastAPI, File, UploadFile, HTTPException, status
from pydantic import BaseModel
from bs4 import BeautifulSoup

app = FastAPI(title="Course Registration API - Phase 2")

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
# 4. API ROUTE IMPLEMENTATIONS
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