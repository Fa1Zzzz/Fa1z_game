import os, json, random, asyncio
from typing import Dict, List
import socketio
from fastapi import FastAPI
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.staticfiles import StaticFiles
import uvicorn

# تحديد مسار ملف الأسئلة
QUESTIONS_FILE = os.path.join(os.path.dirname(__file__), "static", "questions.json")

# تحميل الأسئلة من JSON
with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
    QUESTIONS = json.load(f)

# ========= إعداد Socket.IO + FastAPI =========
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)

@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")

# ========= إعدادات عامة =========
QUESTION_TIME = 12     # مدة السؤال بالثواني
PAUSE_BETWEEN = 3      # استراحة بين الأسئلة
QUESTIONS_PATH = os.path.join("static", "questions.json")

# ========= تحميل الأسئلة من ملف JSON =========
def load_questions() -> Dict[str, List[dict]]:
    """
    بنية الملف:
    {
      "general": [
        {"q": "...", "choices": ["A) ...","B) ...","C) ...","D) ..."], "answer":"B", "explain":"..."},
        ...
      ],
      "sports": [ ... ]
    }
    """
    try:
        with open(QUESTIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # تنظيف الإجابات
        for arr in data.values():
            for it in arr:
                it["answer"] = (it.get("answer") or "").strip().upper()
        return data
    except Exception as e:
        print("Failed to load questions.json:", e)
        # fallback: فئة وحيدة افتراضية
        return {
            "general": [
                {
                    "q": "ما هي عاصمة اليابان؟",
                    "choices": ["A) أوساكا", "B) طوكيو", "C) كيوتو", "D) كوبي"],
                    "answer": "B",
                    "explain": "طوكيو العاصمة منذ 1869."
                }
            ]
        }

QUEST_BANK = load_questions()

@app.get("/categories")
async def categories():
    """إرجاع أسماء الفئات وعدد الأسئلة بكل فئة (لواجهة الاختيار)."""
    cats = [{"key": k, "count": len(v)} for k, v in QUEST_BANK.items()]
    return JSONResponse(cats)

# ========= حالة اللعبة =========
scores: Dict[str, int] = {}       # name -> score
names: Dict[str, str] = {}        # sid -> name
running: bool = False
join_open: bool = True            # يُقفل بعد بدء الجولة
current_answer: str | None = None
correct_order: List[str] = []     # ترتيب الـ sid الذين جاوبوا صح في السؤال الجاري (للنقاط 3/2/1)

# ========= مساعدات =========
async def broadcast_leaderboard():
    lb = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    await sio.emit("leaderboard", {"scores": lb})

def points_for_rank(rank: int) -> int:
    """الترتيب 1→3 نقاط، 2→2، 3→1، غير ذلك 0."""
    return {1: 3, 2: 2, 3: 1}.get(rank, 0)

# ========= Socket.IO Events =========
@sio.event
async def connect(sid, environ):
    await sio.emit("system", {"msg": "✅ تم الاتصال بالسيرفر."}, to=sid)

@sio.event
async def join(sid, data):
    """انضمام لاعب بالاسم (يُمنع إذا الانضمام مقفول)."""
    global join_open
    if not join_open:
        await sio.emit("system", {"msg": "⛔️ الانضمام مقفول. انتظر الجولة التالية."}, to=sid)
        return
    name = (data.get("name") or "Player").strip()[:16]
    base = name
    i = 1
    while name in names.values():
        i += 1
        name = f"{base}{i}"
    names[sid] = name
    scores.setdefault(name, 0)
    await sio.emit("system", {"msg": f"{name} انضم للعبة. لاعبين: {len(names)}"})
    await sio.emit("joined", {"name": name}, to=sid)
    await broadcast_leaderboard()

@sio.event
async def start(sid, data=None):
    """
    بدء الجولة مع فئة مختارة (data = {"category": "general"})
    يقفل الانضمام.
    """
    cat = None
    if isinstance(data, dict):
        cat = (data.get("category") or "").strip()
    await quiz_loop(category=cat)

@sio.event
async def answer(sid, data):
    """استلام إجابة لاعب، مع نقاط متدرّجة لأول ثلاثة صحيح."""
    global current_answer, correct_order
    letter = (data.get("letter") or "").upper().strip()
    if not current_answer or letter not in {"A","B","C","D"}:
        return
    # صحيح؟
    if letter == current_answer:
        # هل هذا اللاعب احتسب من قبل في السؤال الحالي؟
        if sid not in correct_order:
            correct_order.append(sid)
            rank = len(correct_order)  # 1,2,3,...
            gain = points_for_rank(rank)
            if gain > 0:
                player = names.get(sid, "Player")
                scores[player] = scores.get(player, 0) + gain
                await sio.emit("correct_first", {"player": player, "gain": gain, "rank": rank})
                await broadcast_leaderboard()

@sio.event
async def disconnect(sid):
    name = names.pop(sid, None)
    if name:
        await sio.emit("system", {"msg": f"{name} غادر. لاعبين: {len(names)}"})
        await broadcast_leaderboard()

# ========= حلقة الأسئلة =========
async def quiz_loop(category: str | None = None):
    global running, join_open, current_answer, correct_order
    if running:
        return
    if not names:
        await sio.emit("system", {"msg": "لا يوجد لاعبين بعد. اطلب من اللاعبين الانضمام ثم ابدأ."})
        return

    # اختر الفئة
    cat = category if category in QUEST_BANK else (list(QUEST_BANK.keys())[0])
    questions = list(QUEST_BANK[cat])
    random.shuffle(questions)
    total = len(questions)

    # اقفل الانضمام أثناء الجولة
    join_open = False
    running = True
    await sio.emit("system", {"msg": f"🚀 بدأت الجولة! الفئة: {cat} ({total} سؤال) "})

    for idx, item in enumerate(questions, start=1):
        current_answer = item["answer"].upper()
        correct_order = []  # إعادة ضبط ترتيب المصحّحين لكل سؤال

        # بث السؤال
        await sio.emit("question", {
            "index": idx, "total": total,
            "q": item["q"], "choices": item["choices"], "time": QUESTION_TIME
        })

        # مؤقت السؤال
        await asyncio.sleep(QUESTION_TIME)

        # إعلان الإجابة + شرح
        payload = {"answer": current_answer}
        if item.get("explain"):
            payload["explain"] = item["explain"]
        await sio.emit("reveal", payload)
        await broadcast_leaderboard()

        current_answer = None
        await asyncio.sleep(PAUSE_BETWEEN)

    # نهاية الجولة
    lb = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    top = lb[0][1] if lb else 0
    winners = [n for n, sc in lb if sc == top] if lb else []
    await sio.emit("game_over", {"winners": winners, "top_score": top, "scores": lb})

    # افتح الانضمام من جديد لجولة أخرى
    running = False
    join_open = True

# ========= تشغيل محلي =========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(asgi_app, host="0.0.0.0", port=port)
