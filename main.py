import time
import bcrypt
import jwt
from typing import List, Dict, Optional
from collections import defaultdict
from fastapi import FastAPI, Depends, HTTPException, status, Security, File, UploadFile, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from bs4 import BeautifulSoup
import re

app = FastAPI(title="COSC-3506 Production API", version="5.0")

# =====================================================================
# SECTION 1: MEMORY STORAGE & SECURITY INITIALIZATION
# =====================================================================

JWT_SECRET = "super_secret_production_key_change_me"
JWT_ALGORITHM = "HS256"

USER_DB: Dict[str, bytes] = {
    "admin": bcrypt.hashpw(b"admin", bcrypt.gensalt())
}

GLOBAL_CATALOG: Dict[str, Dict] = {}               
STUDENT_COMPLETED_COURSES: Dict[str, List[Dict]] = defaultdict(list) 
STUDENT_PLANS: Dict[str, List[Dict]] = defaultdict(list)           
RATE_LIMIT_TRACKER: Dict[str, List[float]] = defaultdict(list)    
INITIALIZED_STUDENTS = set()

security_scheme = HTTPBearer(auto_error=False)

class UserAuth(BaseModel):
    username: str
    password: str

class CoursePlanItem(BaseModel):
    course_code: str
    term: str

class PlanPayload(BaseModel):
    planned_courses: List[CoursePlanItem]

class HistoryRecordItem(BaseModel):
    course_code: str
    term: str
    credits_earned: int
    status: str

class HistoryPayload(BaseModel):
    history: List[HistoryRecordItem]

# --- Security & Validation Layer ---

def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Security(security_scheme)) -> dict:
    if not credentials:
        return {"username": None, "role": "guest"}
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return {"username": payload.get("sub"), "role": payload.get("role")}
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token signature has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid authentication token")

def verify_student_existence(sid: str):
    s = str(sid).strip()
    if not s or s == "admin":
        return
    if s not in USER_DB and s not in INITIALIZED_STUDENTS and s not in STUDENT_COMPLETED_COURSES:
        raise HTTPException(status_code=404, detail="Student context not found")

def apply_rate_limit(request: Request, current_user: dict = Depends(get_current_user)):
    identifier = current_user.get("username") or request.client.host
    now = time.time()
    RATE_LIMIT_TRACKER[identifier] = [t for t in RATE_LIMIT_TRACKER[identifier] if now - t < 60]
    if len(RATE_LIMIT_TRACKER[identifier]) >= 10:
        raise HTTPException(status_code=429, detail="Too Many Requests: Rate limit exceeded")
    RATE_LIMIT_TRACKER[identifier].append(now)

# =====================================================================
# SECTION 2: IDENTITY & ACCESS MANAGEMENT
# =====================================================================

@app.post("/api/v1/auth/register", status_code=201)
async def register(user: UserAuth):
    if user.username in USER_DB:
        raise HTTPException(status_code=409, detail="Username already exists")
    USER_DB[user.username] = bcrypt.hashpw(user.password.encode('utf-8'), bcrypt.gensalt())
    INITIALIZED_STUDENTS.add(str(user.username).strip())
    return {"status": "registered"}

@app.post("/api/v1/auth/login")
async def login(user: UserAuth):
    hashed_pw = USER_DB.get(user.username)
    if not hashed_pw or not bcrypt.checkpw(user.password.encode('utf-8'), hashed_pw):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    role = "admin" if user.username == "admin" else "student"
    payload = {"sub": user.username, "role": role, "exp": time.time() + 3600}
    return {"access_token": jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM), "token_type": "bearer"}

# =====================================================================
# SECTION 3: TRANSCRIPT & HISTORY CONTROL
# =====================================================================

@app.post("/api/v1/catalog/import", status_code=201)
async def import_catalog(file: UploadFile = File(...)):
    content = await file.read()
    soup = BeautifulSoup(content, 'html.parser')
    for row in soup.find_all('tr')[1:]:
        cols = [td.get_text(strip=True) for td in row.find_all('td')]
        if len(cols) >= 2:
            course_code = cols[0]
            prereqs = [p.strip() for p in cols[2].split(',')] if len(cols) > 2 and cols[2] else []
            GLOBAL_CATALOG[course_code] = {"prereqs": [p for p in prereqs if p]}
    return {"status": "catalog imported", "courses_loaded": len(GLOBAL_CATALOG)}

@app.post("/api/v1/students/{sid}/history/import", status_code=201)
async def import_history(sid: str, file: UploadFile = File(...), user=Depends(get_current_user)):
    student_key = str(sid).strip()
    INITIALIZED_STUDENTS.add(student_key)
    
    content = await file.read()
    soup = BeautifulSoup(content, "html.parser")
    
    local_parsed_rows = []
    VALID_STATUSES = {"Completed", "In-Progress", "Attempted"}
    
    for t in soup.find_all("table"):
        table_rows = t.find_all("tr")
        if not table_rows:
            continue
        
        first_row_cells = table_rows[0].find_all(["th", "td"])
        headers = [c.get_text(strip=True).lower() for c in first_row_cells]
        
        header_keywords = ["course", "code", "status", "grade", "mark", "term", "sem", "credit", "unit"]
        has_headers = any(any(kw in h for kw in header_keywords) for h in headers)
        
        idx_course = next((i for i, h in enumerate(headers) if "course" in h or "code" in h), 0)
        idx_status = next((i for i, h in enumerate(headers) if "status" in h), 1)
        idx_grade = next((i for i, h in enumerate(headers) if "grade" in h or "mark" in h), 2)
        idx_term = next((i for i, h in enumerate(headers) if "term" in h or "sem" in h), 3)
        idx_credits = next((i for i, h in enumerate(headers) if "credit" in h or "unit" in h), 4)
        
        start_row_index = 1 if has_headers else 0
        
        for r in table_rows[start_row_index:]:
            tds = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
            if len(tds) <= max(idx_course, idx_status, idx_grade, idx_term, idx_credits):
                continue
            
            course = tds[idx_course]
            status_val = tds[idx_status]
            grade = tds[idx_grade]
            term = tds[idx_term]
            credits_val = tds[idx_credits]
            
            if not course or status_val not in VALID_STATUSES or not term:
                continue
                
            try:
                ce = int(credits_val)
            except ValueError:
                ce = 0
            
            local_parsed_rows.append({
                "course_code": course, 
                "term": term, 
                "credits_earned": ce, 
                "status": status_val, 
                "_grade": grade
            })
            
    local_best_map = {}
    for r in local_parsed_rows:
        k = (r["course_code"], r["term"])
        
        g_raw = r["_grade"]
        if re.fullmatch(r"\d+(\.\d+)?", g_raw):
            grade_score = (2, float(g_raw))
        elif re.fullmatch(r"[A-EF][+-]?", g_raw, re.I):
            grade_score = (1, 0.0)
        else:
            grade_score = (0, 0.0)
            
        candidate_priority = (grade_score[0], grade_score[1], r["credits_earned"])
        
        if k not in local_best_map:
            local_best_map[k] = (candidate_priority, r)
        else:
            existing_priority, _ = local_best_map[k]
            if candidate_priority > existing_priority:
                local_best_map[k] = (candidate_priority, r)
            
    final_records = []
    for priority, r in local_best_map.values():
        final_records.append({
            "course_code": r["course_code"],
            "term": r["term"],
            "credits_earned": r["credits_earned"],
            "status": r["status"]
        })
        
    STUDENT_COMPLETED_COURSES[student_key] = final_records
    return {"status": "success", "past_courses_imported": len(final_records)}

@app.put("/api/v1/students/{sid}/history", status_code=200)
async def update_history_templated(sid: str, payload: HistoryPayload, user=Depends(get_current_user)):
    student_key = str(sid).strip()
    verify_student_existence(student_key)
    STUDENT_COMPLETED_COURSES[student_key] = [item.dict() for item in payload.history]
    return {"status": "success", "past_courses_imported": len(payload.history)}

@app.put("/history", status_code=200)
async def update_history_direct(payload: HistoryPayload, user=Depends(get_current_user)):
    target_sid = str(user.get("username")).strip() if user.get("username") else None
    if not target_sid:
        raise HTTPException(status_code=404, detail="Student context not found")
    verify_student_existence(target_sid)
    STUDENT_COMPLETED_COURSES[target_sid] = [item.dict() for item in payload.history]
    return {"status": "success", "past_courses_imported": len(payload.history)}

@app.delete("/api/v1/students/{sid}/history", status_code=204)
async def delete_history_templated(sid: str, user=Depends(get_current_user)):
    student_key = str(sid).strip()
    verify_student_existence(student_key)
    STUDENT_COMPLETED_COURSES[student_key] = []
    return None

@app.delete("/history", status_code=204)
async def delete_history_direct(user=Depends(get_current_user)):
    target_sid = str(user.get("username")).strip() if user.get("username") else None
    if not target_sid:
        raise HTTPException(status_code=404, detail="Student context not found")
    verify_student_existence(target_sid)
    STUDENT_COMPLETED_COURSES[target_sid] = []
    return None

@app.get("/api/v1/students/{sid}/profile")
async def get_profile_templated(sid: str, user=Depends(get_current_user)):
    student_key = str(sid).strip()
    verify_student_existence(student_key)
    return {
        "student_id": student_key,
        "history": STUDENT_COMPLETED_COURSES.get(student_key, []),
        "plan": STUDENT_PLANS.get(student_key, [])
    }

@app.get("/profile")
async def get_profile_direct(user=Depends(get_current_user)):
    target_sid = str(user.get("username")).strip() if user.get("username") else None
    if not target_sid:
        raise HTTPException(status_code=404, detail="Student context not found")
    verify_student_existence(target_sid)
    return {
        "student_id": target_sid,
        "history": STUDENT_COMPLETED_COURSES.get(target_sid, []),
        "plan": STUDENT_PLANS.get(target_sid, [])
    }

# =====================================================================
# SECTION 4: ACADEMIC PLAN MANAGEMENT
# =====================================================================

@app.get("/api/v1/students/{sid}/plan")
async def get_plan_templated(sid: str, user=Depends(get_current_user)):
    student_key = str(sid).strip()
    verify_student_existence(student_key)
    return {"student_id": student_key, "plan": STUDENT_PLANS.get(student_key, [])}

@app.get("/plan")
async def get_plan_direct(user=Depends(get_current_user)):
    target_sid = str(user.get("username")).strip() if user.get("username") else None
    if not target_sid:
        raise HTTPException(status_code=404, detail="Student context not found")
    verify_student_existence(target_sid)
    return {"student_id": target_sid, "plan": STUDENT_PLANS.get(target_sid, [])}

@app.post("/api/v1/students/{sid}/plan", status_code=200)
async def create_plan_templated(sid: str, plan_data: PlanPayload, user=Depends(get_current_user)):
    student_key = str(sid).strip()
    verify_student_existence(student_key)
    STUDENT_PLANS[student_key] = [item.dict() for item in plan_data.planned_courses]
    INITIALIZED_STUDENTS.add(student_key)
    return {"status": "plan created", "plan": STUDENT_PLANS[student_key]}

@app.post("/plan", status_code=200)
async def create_plan_direct(plan_data: PlanPayload, user=Depends(get_current_user)):
    target_sid = str(user.get("username")).strip() if user.get("username") else None
    if not target_sid:
        raise HTTPException(status_code=404, detail="Student context not found")
    verify_student_existence(target_sid)
    STUDENT_PLANS[target_sid] = [item.dict() for item in plan_data.planned_courses]
    INITIALIZED_STUDENTS.add(target_sid)
    return {"status": "plan created", "plan": STUDENT_PLANS[target_sid]}

@app.put("/api/v1/students/{sid}/plan", status_code=200)
async def update_plan_templated(sid: str, plan_data: PlanPayload, user=Depends(get_current_user)):
    student_key = str(sid).strip()
    verify_student_existence(student_key)
    STUDENT_PLANS[student_key] = [item.dict() for item in plan_data.planned_courses]
    return {"status": "plan updated", "plan": STUDENT_PLANS[student_key]}

@app.put("/plan", status_code=200)
async def update_plan_direct(plan_data: PlanPayload, user=Depends(get_current_user)):
    target_sid = str(user.get("username")).strip() if user.get("username") else None
    if not target_sid:
        raise HTTPException(status_code=404, detail="Student context not found")
    verify_student_existence(target_sid)
    STUDENT_PLANS[target_sid] = [item.dict() for item in plan_data.planned_courses]
    return {"status": "plan updated", "plan": STUDENT_PLANS[target_sid]}

@app.delete("/api/v1/students/{sid}/plan", status_code=204)
async def delete_plan_templated(sid: str, user=Depends(get_current_user)):
    student_key = str(sid).strip()
    verify_student_existence(student_key)
    STUDENT_PLANS[student_key] = []
    return None

@app.delete("/plan", status_code=204)
async def delete_plan_direct(user=Depends(get_current_user)):
    target_sid = str(user.get("username")).strip() if user.get("username") else None
    if not target_sid:
        raise HTTPException(status_code=404, detail="Student context not found")
    verify_student_existence(target_sid)
    STUDENT_PLANS[target_sid] = []
    return None

@app.get("/api/v1/students/{sid}/audit-report")
async def get_audit_report_templated(sid: str, user=Depends(get_current_user), _=Depends(apply_rate_limit)):
    student_key = str(sid).strip()
    verify_student_existence(student_key)
    return {
        "student_id": student_key,
        "audit_executed_at": time.time(),
        "status": "Evaluated",
        "completed_courses": STUDENT_COMPLETED_COURSES.get(student_key, [])
    }

@app.get("/audit-report")
async def get_audit_report_direct(user=Depends(get_current_user), _=Depends(apply_rate_limit)):
    target_sid = str(user.get("username")).strip() if user.get("username") else None
    if not target_sid:
        raise HTTPException(status_code=404, detail="Student context not found")
    verify_student_existence(target_sid)
    return {
        "student_id": target_sid,
        "audit_executed_at": time.time(),
        "status": "Evaluated",
        "completed_courses": STUDENT_COMPLETED_COURSES.get(target_sid, [])
    }

# =====================================================================
# SECTION 5: RECOMMENDATIONS ENGINE
# =====================================================================

def term_label_generator(start_year: int = 26, start_season: str = "F"):
    year = start_year
    season = start_season
    while True:
        yield f"{year}{season}"
        if season == "F":
            year += 1
            season = "W"
        else:
            season = "F"

@app.get("/api/v1/students/{sid}/recommendations")
async def get_recommendations_templated(sid: str, user=Depends(get_current_user)):
    student_key = str(sid).strip()
    verify_student_existence(student_key)
    return execute_recommendations(student_key)

@app.get("/recommendations")
async def get_recommendations_direct(user=Depends(get_current_user)):
    target_sid = str(user.get("username")).strip() if user.get("username") else None
    if not target_sid:
        raise HTTPException(status_code=404, detail="Student context not found")
    verify_student_existence(target_sid)
    return execute_recommendations(target_sid)

def execute_recommendations(target_sid: str):
    student_key = str(target_sid).strip()
    completed = {r["course_code"] for r in STUDENT_COMPLETED_COURSES.get(student_key, [])}
    remaining_courses = [c for c in GLOBAL_CATALOG.keys() if c not in completed]
    
    adj_list = defaultdict(list)
    in_degree = {course: 0 for course in remaining_courses}
    
    for course in remaining_courses:
        prereqs = GLOBAL_CATALOG[course].get("prereqs", [])
        for prereq in prereqs:
            if prereq in remaining_courses:
                adj_list[prereq].append(course)
                in_degree[course] += 1

    queue = [c for c in remaining_courses if in_degree[c] == 0]
    recommended_pathway = []
    term_stream = term_label_generator(26, "F")
    
    while queue:
        current_term_label = next(term_stream)
        term_courses = []
        next_queue = []
        
        for course in queue:
            term_courses.append(course)
            for neighbor in adj_list[course]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_queue.append(neighbor)
                    
        recommended_pathway.append({
            "term": current_term_label,
            "courses": sorted(term_courses)
        })
        queue = next_queue

    return {
        "student_id": student_key,
        "recommended_pathway": recommended_pathway
    }