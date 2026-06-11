from fastapi import FastAPI, UploadFile, File, HTTPException
from bs4 import BeautifulSoup

app = FastAPI()

courses = {}

@app.get("/")
def home():
    return {"message": "API running"}

@app.post("/api/v1/admin/catalog/import")
async def import_catalog(file: UploadFile = File(...)):
    content = await file.read()
    soup = BeautifulSoup(content, "html.parser")

    rows = soup.find_all("tr")

    for row in rows:
        cols = row.find_all("td")

        if len(cols) < 5:
            continue

        code = cols[0].text.strip()
        title = cols[1].text.strip()
        credits = cols[2].text.strip()
        prereq = cols[3].text.strip()
        cross = cols[4].text.strip()

        courses[code] = {
            "course_code": code,
            "title": title,
            "credits": credits,
            "prerequisites": prereq,
            "cross_listed": cross
        }

    return {"message": "Imported", "count": len(courses)}

@app.get("/api/v1/catalog/courses/{course_code}")
def get_course(course_code: str):
    if course_code not in courses:
        raise HTTPException(status_code=404, detail="Not found")
    return courses[course_code]