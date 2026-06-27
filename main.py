import math
from typing import List, Dict, Optional
from fastapi import FastAPI, File, UploadFile, HTTPException, status
from pydantic import BaseModel
from bs4 import BeautifulSoup

app = FastAPI(title="Course Registration API - Phase 2")

# ==========================================
# 1. IN-MEMORY DATABASE WITH STUDENT ISOLATION
# ==========================================
# Structure: { student_id: { "history": [...], "plan": [...] } }
students_db: Dict[str, Dict[str, list]] = {}

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
def score_grade(grade_str: str) -> int:
    """Assigns a score to grades to determine the 'most informative'."""
    g = grade_str.strip()
    if not g:
        return 0  # Blank/Empty
    if g.upper() == 'P':
        return 1  # Pass letter grade
    # Check if it's a numeric grade (e.g., "85" or "72.5")
    try:
        float(g)
        return 3  # Numeric grade wins
    except ValueError:
        return 2  # Standard descriptive letter grade (e.g., A, B, C, F)

def parse_transcript_html(html_content: bytes) -> List[dict]:
    soup = BeautifulSoup(html_content, "html.parser")
    tables = soup.find_all("table")
    
    # Global tracking dict to handle cross-table deduplication
    # Key: (course_code, term) -> Value: processed record dict
    dedup_map = {}

    valid_statuses = {"Completed", "In-Progress", "Attempted"}

    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue
            
        # Extract headers to accurately identify columns dynamically
        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        
        # Verify if this table matches the expected target format
        expected_headers = ["Status", "Course", "Grade", "Term", "Credits"]
        if not all(h in headers for h in expected_headers):
            continue  # Not our target requirement table, skip it

        # Map header names to their zero-based column indices
        col_indices = {h: headers.index(h) for h in expected_headers}

        # Process data rows
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < len(headers):
                continue

            raw_status = cells[col_indices["Status"]].get_text(strip=True)
            raw_course = cells[col_indices["Course"]].get_text(strip=True)
            raw_grade = cells[col_indices["Grade"]].get_text(strip=True)
            raw_term = cells[col_indices["Term"]].get_text(strip=True)
            raw_credits = cells[col_indices["Credits"]].get_text(strip=True)

            # --- Canonical Filter Rule ---
            if raw_status not in valid_statuses:
                continue
            if not raw_term:  # Drops Fulfilled / Planned placeholder rows
                continue

            # Clean and safely cast fields
            course_code = raw_course  # Kept verbatim (hyphenated)
            term = raw_term          # Kept verbatim
            
            try:
                credits_earned = int(float(raw_credits))
            except ValueError:
                credits_earned = 0

            current_record = {
                "course_code": course_code,
                "term": term,
                "credits_earned": credits_earned,
                "status": raw_status,
                "_raw_grade": raw_grade  # Temporary field kept purely for tie-breaking
            }

            combo_key = (course_code, term)

            if combo_key not in dedup_map:
                dedup_map[combo_key] = current_record
            else:
                # --- Tie-Breaking Logic Execution ---
                existing = dedup_map[combo_key]
                existing_score = score_grade(existing["_raw_grade"])
                current_score = score_grade(raw_grade)

                if current_score > existing_score:
                    dedup_map[combo_key] = current_record
                elif current_score == existing_score:
                    if credits_earned > existing["credits_earned"]:
                        dedup_map[combo_key] = current_record

    # Strip out the temporary grading score fields before returning clean JSON lists
    cleaned_history = []
    for record in dedup_map.values():
        record.pop("_raw_grade", None)
        cleaned_history.append(record)

    return cleaned_history

# ==========================================
# 4. API ROUTE IMPLEMENTATIONS
# ==========================================

# --- HISTORY ENDPOINTS (HTML IN) ---
@app.post("/api/v1/students/{student_id}/history/import", status_code=status.HTTP_201_CREATED)
async def import_student_history(student_id: str, file: UploadFile = File(...)):
    contents = await file.read()
    try:
        parsed_history = parse_transcript_html(contents)
    except Exception as e:
        throw_msg = f"Failed to process the uploaded DOM structure: {str(e)}"
        raise HTTPException(status_code=400, detail=throw_msg)

    # Initialize or overwrite student entry completely to preserve isolation state
    if student_id not in students_db:
        students_db[student_id] = {"history": [], "plan": []}
        
    students_db[student_id]["history"] = parsed_history
    
    return {
        "status": "success",
        "past_courses_imported": len(parsed_history)
    }

@app.put("/api/v1/students/{student_id}/history")
def update_student_history(student_id: str, payload: UpdateHistoryPayload):
    if student_id not in students_db:
        raise HTTPException(status_code=404, detail="Student record does not exist.")
    
    # Overwrite historical array tracking
    students_db[student_id]["history"] = [item.dict() for item in payload.history]
    return {"status": "success", "message": "Academic history updated successfully"}

@app.delete("/api/v1/students/{student_id}/history", status_code=status.HTTP_200_OK)
def delete_student_history(student_id: str):
    if student_id not in students_db:
        raise HTTPException(status_code=404, detail="Student record does not exist.")
    
    students_db[student_id]["history"] = []
    return {"status": "success", "message": "History cleared successfully"}

# --- PLAN ENDPOINTS (JSON IN) ---
@app.post("/api/v1/students/{student_id}/plan")
def create_or_append_plan(student_id: str, payload: PlanPayload):
    if student_id not in students_db:
        raise HTTPException(status_code=404, detail="Student record does not exist.")
    
    incoming_plans = [item.dict() for item in payload.planned_courses]
    students_db[student_id]["plan"].extend(incoming_plans)
    
    return {
        "status": "success",
        "planned_courses_saved": len(incoming_plans)
    }

@app.put("/api/v1/students/{student_id}/plan")
def overwrite_student_plan(student_id: str, payload: PlanPayload):
    if student_id not in students_db:
        raise HTTPException(status_code=404, detail="Student record does not exist.")
    
    students_db[student_id]["plan"] = [item.dict() for item in payload.planned_courses]
    return {"status": "success", "message": "Plan updated successfully"}

@app.delete("/api/v1/students/{student_id}/plan")
def delete_student_plan(student_id: str):
    if student_id not in students_db:
        raise HTTPException(status_code=404, detail="Student record does not exist.")
    
    students_db[student_id]["plan"] = []
    return {"status": "success", "message": "Plan cleared successfully"}

# --- PROFILE UNIFIED GATEWAY ---
@app.get("/api/v1/students/{student_id}/profile", response_model=StudentProfileResponse)
def get_student_profile(student_id: str):
    if student_id not in students_db:
        raise HTTPException(status_code=404, detail="Student record does not exist.")
        
    student_data = students_db[student_id]
    return {
        "student_id": student_id,
        "history": student_data["history"],
        "plan": student_data["plan"]
    }